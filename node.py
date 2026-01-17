#!/usr/bin/env python3
"""
Necromancy on NATS Node
A Raspberry Pi service that connects to NATS and controls GPIO pins.
"""

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import nats
from nats.aio.client import Client as NATS
from nats.aio.subscription import Subscription

# Use gpiozero for GPIO control - works on all Raspberry Pi models
GPIO_AVAILABLE = False
try:
    from gpiozero import OutputDevice, InputDevice, DigitalInputDevice, DigitalOutputDevice
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# Keyboard event listening for media keys
KEYBOARD_AVAILABLE = False
try:
    from pynput import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False


class NodeService:
    """Service that connects to NATS and handles GPIO control operations."""
    
    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.nats_client: Optional[NATS] = None
        self.subscriptions: list[Subscription] = []
        self.running = False
        self.gpio_enabled = False
        self.gpio_devices = {}  # Store gpiozero device objects by pin name
        self.keyboard_listener = None  # For keyboard event listening
        self.gpio_toggle_state = {}  # Track GPIO state for toggling
        self.event_loop = None  # Store event loop for keyboard listener thread
        self._setup_logging()
        self._setup_gpio()
        
    def _setup_logging(self):
        """Configure logging based on config."""
        log_level = self.config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Reduce verbosity of NATS library logs (only show WARNING and above)
        nats_logger = logging.getLogger("nats")
        nats_logger.setLevel(logging.WARNING)
        
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        return config
    
    def _setup_gpio(self):
        """Initialize GPIO pins based on configuration using gpiozero."""
        if not GPIO_AVAILABLE:
            self.logger.warning("gpiozero not available - running in simulation mode")
            self.gpio_enabled = False
            return
        
        gpio_config = self.config.get("gpio", {})
        if not gpio_config.get("enabled", True):
            self.logger.info("GPIO control is disabled in configuration")
            self.gpio_enabled = False
            return
        
        pins = gpio_config.get("pins", {})
        
        try:
            for pin_name, pin_config in pins.items():
                pin_number = pin_config["number"]
                pin_mode = pin_config.get("mode", "OUT")
                
                if pin_mode == "OUT":
                    initial_state = pin_config.get("initial", False)
                    device = DigitalOutputDevice(pin_number, initial_value=initial_state)
                    self.gpio_devices[pin_name] = device
                    self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as OUTPUT, initial={initial_state}")
                elif pin_mode == "IN":
                    pull = pin_config.get("pull", "UP")
                    pull_up = (pull == "UP")
                    device = DigitalInputDevice(pin_number, pull_up=pull_up)
                    self.gpio_devices[pin_name] = device
                    self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as INPUT, pull={pull}")
            
            self.logger.info("GPIO setup complete (using gpiozero)")
            self.gpio_enabled = True
            
        except Exception as e:
            self.logger.warning(f"GPIO setup failed: {e}")
            self.logger.warning("Continuing in simulation mode - GPIO operations will be logged but not executed")
            self.gpio_enabled = False
    
    def _setup_keyboard_listener(self):
        """Set up keyboard event listener for play/pause to toggle GPIO."""
        keyboard_config = self.config.get("keyboard", {})
        
        if not keyboard_config.get("enabled", False):
            return
        
        if not KEYBOARD_AVAILABLE:
            self.logger.warning("pynput not available - keyboard events disabled")
            return
        
        pin_name = keyboard_config.get("pin", "relay1")
        toggle_key = keyboard_config.get("key", "play/pause")  # Can be "play/pause", "media_play_pause", etc.
        
        # Initialize toggle state from GPIO device if available
        if pin_name in self.gpio_devices:
            device = self.gpio_devices[pin_name]
            self.gpio_toggle_state[pin_name] = bool(device.value) if hasattr(device, 'value') else False
        else:
            self.gpio_toggle_state[pin_name] = False
        
        def on_key_press(key):
            """Handle key press events."""
            try:
                # Handle play/pause media key
                if hasattr(key, 'name'):
                    key_name = key.name
                else:
                    key_name = str(key)
                
                # Check for play/pause media key (keyboard.Key.media_play_pause)
                is_play_pause = False
                try:
                    if hasattr(keyboard.Key, 'media_play_pause'):
                        is_play_pause = (key == keyboard.Key.media_play_pause)
                    # Also check by string comparison
                    if hasattr(key, 'name'):
                        is_play_pause = is_play_pause or (key.name == 'media_play_pause')
                    if hasattr(key, '__str__'):
                        key_str = str(key)
                        is_play_pause = is_play_pause or ('media_play_pause' in key_str or 'play_pause' in key_str)
                except:
                    pass
                
                # Also check if toggle_key matches (case-insensitive)
                if is_play_pause or (toggle_key.lower() in str(key).lower()):
                    # Schedule coroutine from keyboard listener thread
                    if self.event_loop:
                        asyncio.run_coroutine_threadsafe(
                            self._handle_keyboard_toggle(pin_name),
                            self.event_loop
                        )
                    else:
                        self.logger.warning("Event loop not available - keyboard toggle ignored")
                    
            except Exception as e:
                self.logger.debug(f"Error handling key press: {e}")
        
        # Start keyboard listener in a separate thread
        self.keyboard_listener = keyboard.Listener(on_press=on_key_press)
        self.keyboard_listener.start()
        self.logger.info(f"Keyboard listener started - {toggle_key} key will toggle GPIO pin '{pin_name}'")
    
    async def _handle_keyboard_toggle(self, pin_name: str):
        """Handle keyboard-triggered GPIO toggle via NATS."""
        # Toggle the state
        current_state = self.gpio_toggle_state.get(pin_name, False)
        new_state = not current_state
        self.gpio_toggle_state[pin_name] = new_state
        
        # Publish NATS message to toggle GPIO
        if self.nats_client and self.nats_client.is_connected:
            subject = self.config.get("keyboard", {}).get("subject", "necromancy.node.gpio.control")
            message = {
                "pin": pin_name,
                "action": "set",
                "value": new_state
            }
            
            try:
                await self.nats_client.publish(subject, json.dumps(message).encode())
                self.logger.info(f"Keyboard toggle: Published GPIO {pin_name} = {new_state} to NATS")
            except Exception as e:
                self.logger.error(f"Failed to publish keyboard toggle message: {e}")
        else:
            self.logger.warning("NATS not connected - keyboard toggle message not sent")
    
    async def _handle_message(self, msg, operation: str):
        """Handle incoming NATS messages."""
        try:
            data = json.loads(msg.data.decode()) if msg.data else {}
            self.logger.info(f"Received message on operation '{operation}': {data}")
            
            # Route to appropriate handler
            if operation == "gpio_control":
                await self._handle_gpio_control(data)
            elif operation == "service_trigger":
                await self._handle_service_trigger(data)
            else:
                self.logger.warning(f"Unknown operation: {operation}")
                await msg.ack()
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse message as JSON: {e}")
            await msg.nak()
        except Exception as e:
            self.logger.error(f"Error handling message: {e}", exc_info=True)
            await msg.nak()
    
    async def _handle_gpio_control(self, data: Dict[str, Any]):
        """Handle GPIO control operations."""
        pin_name = data.get("pin")
        action = data.get("action")  # "set", "get", "toggle", "pulse"
        value = data.get("value")
        duration = data.get("duration", 0.5)  # For pulse operations
        
        if not pin_name:
            self.logger.error("GPIO control message missing 'pin' field")
            return
        
        gpio_config = self.config.get("gpio", {})
        pins = gpio_config.get("pins", {})
        
        if pin_name not in pins:
            self.logger.error(f"Pin '{pin_name}' not configured")
            return
        
        pin_number = pins[pin_name]["number"]
        pin_mode = pins[pin_name].get("mode", "OUT")
        
        # Check if GPIO is actually enabled (not just available)
        if not GPIO_AVAILABLE or not getattr(self, 'gpio_enabled', False):
            self.logger.info(f"[SIMULATE] GPIO {pin_name} ({pin_number}): {action} = {value}")
            return
        
        try:
            device = self.gpio_devices.get(pin_name)
            
            if action == "set":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                if device:
                    device.value = value
                # Update toggle state for keyboard listener
                self.gpio_toggle_state[pin_name] = bool(value)
                self.logger.info(f"Set GPIO {pin_name} ({pin_number}) to {value}")
                
            elif action == "get":
                if pin_mode != "IN":
                    self.logger.error(f"Pin {pin_name} is not configured as INPUT")
                    return
                if device:
                    state = device.value
                    self.logger.info(f"GPIO {pin_name} ({pin_number}) state: {state}")
                # Could publish response back to NATS here
                
            elif action == "toggle":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                if device:
                    device.toggle()
                    new_state = device.value
                    self.logger.info(f"Toggled GPIO {pin_name} ({pin_number}) to {new_state}")
                
            elif action == "pulse":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                # Pulse high
                if device:
                    device.on()
                    await asyncio.sleep(duration)
                    device.off()
                    self.logger.info(f"Pulsed GPIO {pin_name} ({pin_number}) for {duration}s")
                
            else:
                self.logger.error(f"Unknown GPIO action: {action}")
                
        except Exception as e:
            self.logger.error(f"Error controlling GPIO pin {pin_name}: {e}", exc_info=True)
    
    async def _handle_service_trigger(self, data: Dict[str, Any]):
        """Handle service trigger operations."""
        service_name = data.get("service")
        action = data.get("action", "start")  # "start", "stop", "restart"
        
        if not service_name:
            self.logger.error("Service trigger message missing 'service' field")
            return
        
        # This is a placeholder - implement actual service control here
        self.logger.info(f"Service trigger: {action} {service_name}")
        
        # Example: You could use subprocess to run systemd commands or other scripts
        # import subprocess
        # subprocess.run(["systemctl", action, service_name])
    
    async def connect_nats(self):
        """Connect to NATS server."""
        nats_config = self.config.get("nats", {})
        servers = nats_config.get("servers", ["nats://localhost:4222"])
        
        if isinstance(servers, str):
            servers = [servers]
        
        self.logger.info(f"Connecting to NATS servers: {servers}")
        self.logger.info("Note: If connection fails, ensure NATS server is running and accessible")
        
        try:
            self.nats_client = await nats.connect(
                servers=servers,
                name=nats_config.get("client_name", "necromancy-node"),
                reconnect_time_wait=nats_config.get("reconnect_time_wait", 2),
                max_reconnect_attempts=nats_config.get("max_reconnect_attempts", -1),
                ping_interval=nats_config.get("ping_interval", 20),
                connect_timeout=nats_config.get("connect_timeout", 4),
            )
            self.logger.info("Connected to NATS successfully")
        except Exception as e:
            error_msg = str(e)
            if "Timeout" in error_msg or "Connection" in error_msg:
                self.logger.error(f"Failed to connect to NATS server at {servers}")
                self.logger.error("Please check:")
                self.logger.error("  - NATS server is running and accessible")
                self.logger.error("  - Network connectivity to the server")
                self.logger.error("  - Firewall settings allow connections on port 4222")
                self.logger.error("  - Server address and port are correct in config.json")
                self.logger.info("Will continue retrying in the background...")
            else:
                self.logger.error(f"Failed to connect to NATS: {e}")
            raise
    
    async def setup_subscriptions(self):
        """Set up NATS subscriptions based on configuration."""
        operations = self.config.get("operations", [])
        
        if not operations:
            self.logger.warning("No operations configured")
            return
        
        for op_config in operations:
            subject = op_config.get("subject")
            queue = op_config.get("queue")
            operation = op_config.get("operation")
            
            if not subject:
                self.logger.error("Operation missing 'subject' field")
                continue
            
            if not operation:
                self.logger.error(f"Operation for subject '{subject}' missing 'operation' field")
                continue
            
            self.logger.info(f"Subscribing to subject '{subject}' (queue={queue}, operation={operation})")
            
            # Create an async callback wrapper for this operation
            # Capture operation as default parameter to avoid closure issues
            async def message_callback(msg, op=operation):
                await self._handle_message(msg, op)
            
            sub = await self.nats_client.subscribe(
                subject,
                queue=queue,
                cb=message_callback
            )
            
            self.subscriptions.append(sub)
            self.logger.info(f"Subscribed to {subject}")
    
    async def run(self):
        """Run the service main loop."""
        self.running = True
        self.event_loop = asyncio.get_event_loop()
        
        try:
            await self.connect_nats()
            await self.setup_subscriptions()
            
            # Setup keyboard listener after NATS is connected
            self._setup_keyboard_listener()
            
            self.logger.info("Node service started. Waiting for messages...")
            
            # Keep running until stopped
            while self.running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        except Exception as e:
            self.logger.error(f"Error in service loop: {e}", exc_info=True)
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown of the service."""
        self.logger.info("Shutting down...")
        self.running = False
        
        # Close NATS connection
        if self.nats_client:
            await self.nats_client.close()
            self.logger.info("NATS connection closed")
        
        # Cleanup keyboard listener
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                self.logger.info("Keyboard listener stopped")
            except Exception as e:
                self.logger.debug(f"Error stopping keyboard listener: {e}")
        
        # Cleanup GPIO
        if GPIO_AVAILABLE and self.gpio_enabled:
            try:
                # Close all gpiozero devices
                for pin_name, device in self.gpio_devices.items():
                    try:
                        device.close()
                    except Exception as e:
                        self.logger.debug(f"Error closing device {pin_name}: {e}")
                self.gpio_devices.clear()
                self.logger.info("GPIO cleaned up (gpiozero)")
            except Exception as e:
                self.logger.warning(f"Error during GPIO cleanup: {e}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Necromancy on NATS Node Service")
    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="Path to configuration file (default: config.json)"
    )
    
    args = parser.parse_args()
    
    # Setup signal handlers
    service = None
    
    def signal_handler(sig, frame):
        if service:
            service.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and run service
    try:
        service = NodeService(config_path=args.config)
        asyncio.run(service.run())
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
