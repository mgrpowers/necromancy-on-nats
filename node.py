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
GPIO_MODULE = None
GPIOD_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_MODULE = GPIO
    GPIO_TYPE = "RPi"
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_MODULE = None

# Also try to import gpiod (needed for Raspberry Pi 5)
try:
    import gpiod
    GPIOD_AVAILABLE = True
    if not GPIO_AVAILABLE:
        GPIO_TYPE = "gpiod"
        GPIO_AVAILABLE = True
except ImportError:
    pass


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
        
        # Try gpiod for Raspberry Pi 5 (if RPi.GPIO failed or gpiod is available)
        if GPIOD_AVAILABLE and (GPIO_TYPE == "gpiod" or (GPIO_TYPE == "RPi" and not self.gpio_enabled)):
            try:
                import gpiod
                import os
                # Update gpio_type since we're using gpiod
                self.gpio_type = "gpiod"
                
                # Find all available GPIO chips
                available_chips = []
                for i in range(10):  # Check gpiochip0-9
                    chip_path = f"/dev/gpiochip{i}"
                    if os.path.exists(chip_path):
                        available_chips.append(f"gpiochip{i}")
                
                if not available_chips:
                    raise FileNotFoundError("No GPIO chip devices found in /dev/. Make sure gpiod is installed and GPIO is enabled.")
                
                # Try to find the GPIO chip - check config first, then auto-detect
                chip_name = gpio_config.get("chip")
                chips_to_try = []
                
                if chip_name:
                    # Use configured chip if available
                    if chip_name in available_chips:
                        chips_to_try = [chip_name]
                    else:
                        self.logger.warning(f"Configured chip '{chip_name}' not found. Available chips: {', '.join(available_chips)}")
                        chips_to_try = available_chips
                else:
                    # Auto-detect: prefer common chips in order
                    preferred_order = ["gpiochip4", "gpiochip0", "gpiochip1"]
                    for preferred in preferred_order:
                        if preferred in available_chips:
                            chips_to_try.append(preferred)
                    # Add any remaining chips
                    for chip in available_chips:
                        if chip not in chips_to_try:
                            chips_to_try.append(chip)
                
                # Try each chip until one works
                chip_opened = False
                last_error = None
                
                for chip_to_try in chips_to_try:
                    try:
                        self.logger.info(f"Trying to open GPIO chip: {chip_to_try}")
                        # Try with just the name first
                        self.gpio_chip = gpiod.Chip(chip_to_try)
                        chip_name = chip_to_try
                        chip_opened = True
                        self.logger.info(f"Successfully opened GPIO chip: {chip_name}")
                        break
                    except Exception as e:
                        last_error = e
                        # Try with full path if name doesn't work
                        try:
                            chip_path = f"/dev/{chip_to_try}"
                            self.logger.info(f"Trying with full path: {chip_path}")
                            self.gpio_chip = gpiod.Chip(chip_path)
                            chip_name = chip_to_try
                            chip_opened = True
                            self.logger.info(f"Successfully opened GPIO chip: {chip_name}")
                            break
                        except Exception as e2:
                            self.logger.debug(f"Failed to open {chip_to_try}: {e}, {e2}")
                            continue
                
                if not chip_opened:
                    raise last_error or FileNotFoundError(f"Could not open any GPIO chip. Tried: {', '.join(chips_to_try)}")
                
                # Debug: log available methods
                chip_methods = [m for m in dir(self.gpio_chip) if not m.startswith('_')]
                self.logger.debug(f"Available Chip methods: {', '.join(chip_methods)}")
                
                # Check which gpiod API is available
                has_get_line = hasattr(self.gpio_chip, 'get_line')
                has_get_lines = hasattr(self.gpio_chip, 'get_lines')
                
                if not has_get_line and not has_get_lines:
                    # Try to use request_lines (v2 API)
                    if hasattr(gpiod, 'request_lines'):
                        self.logger.info("Using gpiod v2.x API (request_lines)")
                        
                        # Check what attributes are available in gpiod module
                        gpiod_attrs = [attr for attr in dir(gpiod) if not attr.startswith('_')]
                        self.logger.debug(f"gpiod module attributes: {', '.join(gpiod_attrs)}")
                        
                        # Try to find the correct enum/class names
                        LineSettings = None
                        LineConfig = None
                        LineDirection = None
                        LineValue = None
                        LineBias = None
                        
                        # Check common attribute patterns
                        for attr in gpiod_attrs:
                            if 'LineSettings' in attr or 'Settings' == attr:
                                LineSettings = getattr(gpiod, attr)
                            elif 'LineConfig' in attr or 'Config' == attr:
                                LineConfig = getattr(gpiod, attr)
                            elif 'Direction' in attr:
                                LineDirection = getattr(gpiod, attr)
                            elif 'Value' in attr and 'Line' in attr:
                                LineValue = getattr(gpiod, attr)
                            elif 'Bias' in attr:
                                LineBias = getattr(gpiod, attr)
                        
                        # Try alternative paths: gpiod.line.Direction, etc.
                        if hasattr(gpiod, 'line'):
                            line_module = gpiod.line
                            if not LineDirection and hasattr(line_module, 'Direction'):
                                LineDirection = line_module.Direction
                            if not LineValue and hasattr(line_module, 'Value'):
                                LineValue = line_module.Value
                            if not LineBias and hasattr(line_module, 'Bias'):
                                LineBias = line_module.Bias
                        
                        if not LineSettings or not LineConfig:
                            raise RuntimeError(f"gpiod v2 API not fully available. Found request_lines but missing LineSettings/LineConfig. Available: {gpiod_attrs}")
                        
                        for pin_name, pin_config in pins.items():
                            pin_number = pin_config["number"]
                            pin_mode = pin_config.get("mode", "OUT")
                            
                            settings = LineSettings()
                            if pin_mode == "OUT":
                                if LineDirection:
                                    settings.direction = LineDirection.OUTPUT
                                if LineValue and pin_config.get("initial", False):
                                    settings.output_value = LineValue.ACTIVE
                                elif LineValue:
                                    settings.output_value = LineValue.INACTIVE
                            else:
                                if LineDirection:
                                    settings.direction = LineDirection.INPUT
                                pull = pin_config.get("pull", "UP")
                                if LineBias:
                                    if pull == "UP":
                                        settings.bias = LineBias.PULL_UP
                                    elif pull == "DOWN":
                                        settings.bias = LineBias.PULL_DOWN
                            
                            config = LineConfig()
                            config.add_line_settings([pin_number], settings)
                            
                            line_request = gpiod.request_lines(
                                chip=self.gpio_chip,
                                consumer=f"necromancy-{pin_name}",
                                config=config
                            )
                            self.gpio_lines[pin_name] = {"request": line_request, "pin": pin_number, "v2": True}
                            initial_state = pin_config.get("initial", False) if pin_mode == "OUT" else None
                            self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as {pin_mode}, initial={initial_state}")
                    else:
                        raise RuntimeError("Unsupported gpiod API. Available methods: " + str([m for m in dir(self.gpio_chip) if not m.startswith('_')]))
                elif has_get_line:
                    # gpiod v1.x API - use get_line()
                    self.logger.info("Using gpiod v1.x API (get_line)")
                    for pin_name, pin_config in pins.items():
                        pin_number = pin_config["number"]
                        pin_mode = pin_config.get("mode", "OUT")
                        
                        line = self.gpio_chip.get_line(pin_number)
                        
                        if pin_mode == "OUT":
                            line.request(consumer=f"necromancy-{pin_name}", type=gpiod.LINE_REQ_DIR_OUT)
                            initial_state = pin_config.get("initial", False)
                            line.set_value(1 if initial_state else 0)
                            self.gpio_lines[pin_name] = {"line": line, "pin": pin_number, "v2": False}
                            self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as OUTPUT, initial={initial_state}")
                        elif pin_mode == "IN":
                            pull = pin_config.get("pull", "UP")
                            pull_type = gpiod.LINE_REQ_PULL_UP if pull == "UP" else (gpiod.LINE_REQ_PULL_DOWN if pull == "DOWN" else gpiod.LINE_REQ_PULL_NONE)
                            line.request(consumer=f"necromancy-{pin_name}", type=gpiod.LINE_REQ_DIR_IN, flags=pull_type)
                            self.gpio_lines[pin_name] = {"line": line, "pin": pin_number, "v2": False}
                            self.logger.info(f"Configured GPIO pin {pin_number} ({pin_name}) as INPUT, pull={pull}")
                
                self.logger.info(f"GPIO setup complete (using gpiod on {chip_name})")
                self.gpio_enabled = True
                return
                
            except Exception as e:
                error_msg = str(e)
                self.logger.warning(f"gpiod setup failed: {error_msg}")
                
                # Log available methods for debugging
                if hasattr(self, 'gpio_chip') and self.gpio_chip:
                    chip_methods = [m for m in dir(self.gpio_chip) if not m.startswith('_')]
                    self.logger.debug(f"Available Chip methods: {', '.join(chip_methods)}")
                gpiod_attrs = [m for m in dir(gpiod) if not m.startswith('_')]
                self.logger.debug(f"Available gpiod module attributes: {', '.join(gpiod_attrs[:10])}")
                
                # Try to list available chips for debugging
                if "No such file" in error_msg or "not found" in error_msg.lower():
                    import os
                    available_chips = []
                    for i in range(10):
                        chip_path = f"/dev/gpiochip{i}"
                        if os.path.exists(chip_path):
                            available_chips.append(f"gpiochip{i}")
                    
                    if available_chips:
                        self.logger.info(f"Available GPIO chips: {', '.join(available_chips)}")
                        self.logger.info("You can specify the chip in config.json: \"gpio\": {\"chip\": \"gpiochip0\", ...}")
                    else:
                        self.logger.warning("No GPIO chip devices found. Make sure:")
                        self.logger.warning("  1. gpiod is installed: sudo apt install python3-libgpiod")
                        self.logger.warning("  2. GPIO is enabled in the system")
                
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

