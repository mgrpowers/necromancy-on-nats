#!/usr/bin/env python3
"""
HID Node Service
Listens for HID keyboard input (e.g., itsybitsy 6 key keyboard) and publishes NATS commands.
"""

import asyncio
import json
import logging
import platform
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import nats
from nats.aio.client import Client as NATS

# Try to import evdev for Linux HID input
EVDEV_AVAILABLE = False
try:
    from evdev import InputDevice, list_devices, categorize, ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False

# Try to import pynput for macOS/Windows keyboard input
PYNPUT_AVAILABLE = False
try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

# Detect platform
IS_LINUX = platform.system() == 'Linux'
IS_MACOS = platform.system() == 'Darwin'
IS_WINDOWS = platform.system() == 'Windows'


class HIDNodeService:
    """Service that listens for HID keyboard input and publishes NATS commands."""
    
    def __init__(self):
        self.nats_client: Optional[NATS] = None
        self.running = False
        self.input_device = None
        self.keyboard_listener = None  # For pynput on macOS/Windows
        self.input_thread = None
        self.toggle_states = {}  # Track toggle state for each key/pin mapping
        self.event_loop = None
        
        # Hardcoded configuration for now
        self.nats_server = "nats://192.168.50.118:4222"
        self.subject = "necromancy.node.gpio.control"
        
        # Key mappings: key_code -> (pin_name, toggle_state_key)
        # Linux (evdev): uses numeric key codes (KEY_1 = 2, KEY_2 = 3, etc.)
        # macOS (pynput): uses key names or characters ('1', '2', etc.)
        self.use_pynput = (IS_MACOS or IS_WINDOWS) and PYNPUT_AVAILABLE and not EVDEV_AVAILABLE
        self.use_evdev = IS_LINUX and EVDEV_AVAILABLE
        
        if self.use_evdev:
            # Linux: use evdev key codes
            default_key_code = ecodes.KEY_1
            self.key_mappings = {
                default_key_code: {
                    "pin": "relay1",
                    "toggle_key": "relay1"
                }
            }
            self.logger.info(f"Configured key mappings (evdev codes): {self.key_mappings}")
            self.logger.info(f"Looking for KEY_1 (code: {ecodes.KEY_1})")
        elif self.use_pynput:
            # macOS/Windows: use pynput key names
            # Map character '1' to relay1 (itsybitsy first key)
            self.key_mappings = {
                '1': {
                    "pin": "relay1",
                    "toggle_key": "relay1"
                }
            }
        else:
            # Fallback: numeric codes
            self.key_mappings = {
                2: {
                    "pin": "relay1",
                    "toggle_key": "relay1"
                }
            }
        
        self._setup_logging()
    
    def _setup_logging(self):
        """Configure logging."""
        logging.basicConfig(
            level=logging.DEBUG,  # Use DEBUG to see all events
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
    
    def _find_input_device(self) -> Optional[str]:
        """Find the itsybitsy keyboard input device."""
        if not EVDEV_AVAILABLE:
            self.logger.error("evdev library not available - install with: pip install evdev")
            return None
        
        devices = [InputDevice(path) for path in list_devices()]
        
        self.logger.info("Available input devices:")
        for device in devices:
            self.logger.info(f"  {device.path}: {device.name} ({device.phys})")
        
        # Try to find itsybitsy keyboard by name
        for device in devices:
            name_lower = device.name.lower()
            if 'itsybitsy' in name_lower or 'keyboard' in name_lower:
                self.logger.info(f"Found potential keyboard: {device.path} ({device.name})")
                return device.path
        
        # If not found by name, try to find any keyboard device
        # Check if device has KEY capability
        for device in devices:
            capabilities = device.capabilities()
            if ecodes.EV_KEY in capabilities:
                keys = capabilities[ecodes.EV_KEY]
                # Check if it has standard keyboard keys
                keyboard_keys = [ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3]
                if any(k in keys for k in keyboard_keys):
                    self.logger.info(f"Found keyboard-like device: {device.path} ({device.name})")
                    return device.path
        
        self.logger.warning("Could not find itsybitsy keyboard - will need to specify device path")
        if devices:
            self.logger.info(f"Using first available device: {devices[0].path}")
            return devices[0].path
        
        return None
    
    def _setup_input_device(self, device_path: Optional[str] = None):
        """Set up the input device for reading keyboard events."""
        if self.use_evdev:
            # Linux: use evdev
            if device_path is None:
                device_path = self._find_input_device()
            
            if device_path is None:
                self.logger.error("No input device found")
                return False
            
            try:
                self.input_device = InputDevice(device_path)
                self.logger.info(f"Using input device: {self.input_device.path} ({self.input_device.name})")
                return True
            except Exception as e:
                self.logger.error(f"Failed to open input device {device_path}: {e}")
                return False
        elif self.use_pynput:
            # macOS/Windows: use pynput (listens to all keyboards)
            self.logger.info("Using pynput for keyboard input (macOS/Windows)")
            self.logger.info("Note: pynput captures all keyboard input, not device-specific")
            return True
        else:
            self.logger.error("No keyboard input method available")
            self.logger.error("Install evdev for Linux or pynput for macOS/Windows")
            return False
    
    def _on_key_event(self, event=None, key=None):
        """Handle key press/release events.
        
        Args:
            event: evdev event object (Linux)
            key: pynput key object (macOS/Windows)
        """
        key_code = None
        key_name = None
        
        if self.use_evdev and event is not None:
            # Linux: evdev event
            # Log all key events (press, release, repeat) for debugging
            if event.type == ecodes.EV_KEY:
                key_event = categorize(event)
                key_code = event.code
                key_name = str(key_event.keycode)
                
                # Log key press (value == 1), release (value == 0), or repeat (value == 2)
                if event.value == 1:
                    self.logger.info(f"Key PRESSED: {key_name} (code: {key_code})")
                elif event.value == 0:
                    self.logger.debug(f"Key released: {key_name} (code: {key_code})")
                elif event.value == 2:
                    self.logger.debug(f"Key repeat: {key_name} (code: {key_code})")
                
                # Only process key presses (value == 1)
                if event.value != 1:
                    return
        
        elif self.use_pynput and key is not None:
            # macOS/Windows: pynput key
            try:
                # Get key representation
                if hasattr(key, 'char') and key.char:
                    # Regular character key
                    key_name = key.char
                    key_code = key.char
                elif hasattr(key, 'name'):
                    # Special key (media keys, etc.)
                    key_name = key.name
                    key_code = key.name
                else:
                    key_name = str(key)
                    key_code = str(key)
                
                self.logger.info(f"Key pressed: {key_name}")
            except Exception as e:
                self.logger.debug(f"Error processing key: {e}")
                return
        
        if key_code is None:
            return
        
        # Log all received key codes for debugging
        self.logger.debug(f"Received key_code: {key_code} (looking for: {list(self.key_mappings.keys())})")
        
        # Check if this key is mapped
        if key_code in self.key_mappings:
            mapping = self.key_mappings[key_code]
            pin = mapping["pin"]
            toggle_key = mapping["toggle_key"]
            
            # Toggle state
            current_state = self.toggle_states.get(toggle_key, False)
            new_state = not current_state
            self.toggle_states[toggle_key] = new_state
            
            self.logger.info(f"Toggling {pin} to {new_state}")
            
            # Publish NATS message
            self._publish_gpio_control(pin, new_state)
        else:
            self.logger.debug(f"Key code {key_code} ({key_name}) not in key_mappings")
    
    def _on_pynput_key_press(self, key):
        """Callback for pynput key press events."""
        try:
            self._on_key_event(key=key)
        except Exception as e:
            self.logger.error(f"Error in pynput key handler: {e}")
    
    def _input_loop(self):
        """Run input event loop in a separate thread."""
        if self.use_evdev and self.input_device is not None:
            # Linux: evdev loop
            self.logger.info("Starting evdev input event loop...")
            try:
                for event in self.input_device.read_loop():
                    if not self.running:
                        break
                    # Log all events for debugging (filter out sync events)
                    if event.type != ecodes.EV_SYN:
                        self.logger.debug(f"Event: type={event.type}, code={event.code}, value={event.value}")
                    self._on_key_event(event=event)
            except Exception as e:
                self.logger.error(f"Error in evdev input loop: {e}", exc_info=True)
            finally:
                self.logger.info("Input event loop stopped")
        elif self.use_pynput:
            # macOS/Windows: pynput listener
            self.logger.info("Starting pynput keyboard listener...")
            try:
                self.keyboard_listener = keyboard.Listener(on_press=self._on_pynput_key_press)
                self.keyboard_listener.start()
                self.logger.info("Keyboard listener started - waiting for key presses...")
                # Keep the thread alive
                while self.running:
                    threading.Event().wait(1)
            except Exception as e:
                self.logger.error(f"Error in pynput keyboard listener: {e}")
            finally:
                if self.keyboard_listener:
                    self.keyboard_listener.stop()
                self.logger.info("Keyboard listener stopped")
    
    def _publish_gpio_control(self, pin: str, value: bool):
        """Publish GPIO control message to NATS."""
        if self.event_loop is None:
            self.logger.error("Event loop not set - cannot publish NATS message")
            return
        
        message = {
            "pin": pin,
            "action": "set",
            "value": value
        }
        
        # Schedule coroutine from thread
        future = asyncio.run_coroutine_threadsafe(
            self._publish_nats(message),
            self.event_loop
        )
        
        try:
            future.result(timeout=5.0)
        except Exception as e:
            self.logger.error(f"Failed to publish NATS message: {e}")
    
    async def _publish_nats(self, message: Dict[str, Any]):
        """Publish message to NATS."""
        if self.nats_client is None:
            self.logger.error("NATS client not connected")
            return
        
        try:
            msg_json = json.dumps(message)
            await self.nats_client.publish(self.subject, msg_json.encode())
            self.logger.info(f"Published: {msg_json}")
        except Exception as e:
            self.logger.error(f"Error publishing to NATS: {e}")
    
    async def connect_nats(self):
        """Connect to NATS server."""
        self.logger.info(f"Connecting to NATS server: {self.nats_server}")
        
        try:
            self.nats_client = await nats.connect(
                servers=[self.nats_server],
                name="hid-node",
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
                ping_interval=20,
                connect_timeout=4,
            )
            self.logger.info("Connected to NATS successfully")
        except Exception as e:
            self.logger.error(f"Failed to connect to NATS: {e}")
            raise
    
    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, shutting down...")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def run(self):
        """Run the HID node service."""
        self.running = True
        self.event_loop = asyncio.get_event_loop()
        
        # Set up signal handlers
        self._setup_signal_handlers()
        
        # Connect to NATS
        await self.connect_nats()
        
        # Set up input device
        if not self._setup_input_device():
            self.logger.error("Failed to set up input device")
            self.running = False
            return
        
        # Start input thread
        self.input_thread = threading.Thread(target=self._input_loop, daemon=True)
        self.input_thread.start()
        
        self.logger.info("HID node service started")
        self.logger.info("Listening for key presses...")
        
        # Keep running until stopped
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Shutdown the service gracefully."""
        self.logger.info("Shutting down...")
        self.running = False
        
        # Stop keyboard listener (pynput)
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
            except:
                pass
        
        if self.nats_client:
            await self.nats_client.close()
            self.logger.info("NATS connection closed")
        
        if self.input_device:
            # Input device will close when thread exits
            pass


async def main():
    """Main entry point."""
    service = HIDNodeService()
    try:
        await service.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

