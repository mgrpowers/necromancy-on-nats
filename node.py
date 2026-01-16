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

# Try to import GPIO libraries - prefer RPi.GPIO for older Pis, gpiod for Pi 5
GPIO_TYPE = None
GPIO_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_TYPE = "RPi"
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    try:
        import gpiod
        GPIO_TYPE = "gpiod"
        GPIO_AVAILABLE = True
    except ImportError:
        GPIO_AVAILABLE = False


class NodeService:
    """Service that connects to NATS and handles GPIO control operations."""
    
    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.nats_client: Optional[NATS] = None
        self.subscriptions: list[Subscription] = []
        self.running = False
        self.gpio_enabled = False
        self.gpio_type = GPIO_TYPE
        self.gpio_chip = None  # For gpiod
        self.gpio_lines = {}  # For gpiod - stores line objects by pin name
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
        """Initialize GPIO pins based on configuration."""
        if not GPIO_AVAILABLE:
            self.logger.warning("GPIO library not available - running in simulation mode")
            self.gpio_enabled = False
            return
        
        gpio_config = self.config.get("gpio", {})
        if not gpio_config.get("enabled", True):
            self.logger.info("GPIO control is disabled in configuration")
            self.gpio_enabled = False
            return
        
        pins = gpio_config.get("pins", {})
        
        # Try RPi.GPIO first (for older Raspberry Pis)
        if GPIO_TYPE == "RPi":
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(gpio_config.get("warnings", False))
                
                for pin_name, pin_config in pins.items():
                    pin_number = pin_config["number"]
                    pin_mode = pin_config.get("mode", "OUT")
                    
                    if pin_mode == "OUT":
                        GPIO.setup(pin_number, GPIO.OUT)
                        initial_state = pin_config.get("initial", False)
                        GPIO.output(pin_number, GPIO.LOW if not initial_state else GPIO.HIGH)
                        self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as OUTPUT, initial={initial_state}")
                    elif pin_mode == "IN":
                        pull = pin_config.get("pull", "UP")
                        if pull == "UP":
                            GPIO.setup(pin_number, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                        elif pull == "DOWN":
                            GPIO.setup(pin_number, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
                        else:
                            GPIO.setup(pin_number, GPIO.IN)
                        self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as INPUT, pull={pull}")
                
                self.logger.info("GPIO setup complete (using RPi.GPIO)")
                self.gpio_enabled = True
                return
                
            except RuntimeError as e:
                error_msg = str(e)
                if "SOC peripheral base address" in error_msg:
                    self.logger.warning(f"RPi.GPIO failed (likely Raspberry Pi 5): {error_msg}")
                    self.logger.info("Trying gpiod library instead...")
                else:
                    raise
        
        # Try gpiod for Raspberry Pi 5
        if GPIO_TYPE == "gpiod":
            try:
                import gpiod
                
                # Open GPIO chip (usually gpiochip4 on Pi 5)
                chip_name = gpio_config.get("chip", "gpiochip4")
                self.gpio_chip = gpiod.Chip(chip_name)
                
                for pin_name, pin_config in pins.items():
                    pin_number = pin_config["number"]
                    pin_mode = pin_config.get("mode", "OUT")
                    
                    line = self.gpio_chip.get_line(pin_number)
                    
                    if pin_mode == "OUT":
                        line.request(consumer=f"necromancy-{pin_name}", type=gpiod.LINE_REQ_DIR_OUT)
                        initial_state = pin_config.get("initial", False)
                        line.set_value(1 if initial_state else 0)
                        self.gpio_lines[pin_name] = line
                        self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as OUTPUT, initial={initial_state}")
                    elif pin_mode == "IN":
                        pull = pin_config.get("pull", "UP")
                        pull_type = gpiod.LINE_REQ_PULL_UP if pull == "UP" else (gpiod.LINE_REQ_PULL_DOWN if pull == "DOWN" else gpiod.LINE_REQ_PULL_NONE)
                        line.request(consumer=f"necromancy-{pin_name}", type=gpiod.LINE_REQ_DIR_IN, flags=pull_type)
                        self.gpio_lines[pin_name] = line
                        self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as INPUT, pull={pull}")
                
                self.logger.info(f"GPIO setup complete (using gpiod on {chip_name})")
                self.gpio_enabled = True
                return
                
            except Exception as e:
                self.logger.warning(f"gpiod setup failed: {e}")
                self.gpio_enabled = False
        
        # Fallback to simulation mode
        self.logger.warning("Continuing in simulation mode - GPIO operations will be logged but not executed")
        self.gpio_enabled = False
    
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
            if action == "set":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                if self.gpio_type == "RPi":
                    GPIO.output(pin_number, GPIO.HIGH if value else GPIO.LOW)
                elif self.gpio_type == "gpiod":
                    line = self.gpio_lines[pin_name]
                    line.set_value(1 if value else 0)
                self.logger.info(f"Set GPIO {pin_name} ({pin_number}) to {value}")
                
            elif action == "get":
                if pin_mode != "IN":
                    self.logger.error(f"Pin {pin_name} is not configured as INPUT")
                    return
                if self.gpio_type == "RPi":
                    state = GPIO.input(pin_number)
                elif self.gpio_type == "gpiod":
                    line = self.gpio_lines[pin_name]
                    state = line.get_value()
                self.logger.info(f"GPIO {pin_name} ({pin_number}) state: {state}")
                # Could publish response back to NATS here
                
            elif action == "toggle":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                if self.gpio_type == "RPi":
                    current_state = GPIO.input(pin_number)
                    new_state = not current_state
                    GPIO.output(pin_number, GPIO.HIGH if new_state else GPIO.LOW)
                elif self.gpio_type == "gpiod":
                    line = self.gpio_lines[pin_name]
                    current_state = line.get_value()
                    new_state = 1 - current_state  # Toggle 0<->1
                    line.set_value(new_state)
                self.logger.info(f"Toggled GPIO {pin_name} ({pin_number}) to {new_state}")
                
            elif action == "pulse":
                if pin_mode != "OUT":
                    self.logger.error(f"Pin {pin_name} is not configured as OUTPUT")
                    return
                # Pulse high
                if self.gpio_type == "RPi":
                    GPIO.output(pin_number, GPIO.HIGH)
                    await asyncio.sleep(duration)
                    GPIO.output(pin_number, GPIO.LOW)
                elif self.gpio_type == "gpiod":
                    line = self.gpio_lines[pin_name]
                    line.set_value(1)
                    await asyncio.sleep(duration)
                    line.set_value(0)
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
        
        try:
            await self.connect_nats()
            await self.setup_subscriptions()
            
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
        
        # Cleanup GPIO
        if GPIO_AVAILABLE and self.gpio_enabled:
            try:
                if self.gpio_type == "RPi":
                    GPIO.cleanup()
                    self.logger.info("GPIO cleaned up (RPi.GPIO)")
                elif self.gpio_type == "gpiod":
                    # Release all gpiod lines
                    for pin_name, line in self.gpio_lines.items():
                        line.release()
                    if self.gpio_chip:
                        self.gpio_chip.close()
                    self.logger.info("GPIO cleaned up (gpiod)")
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

