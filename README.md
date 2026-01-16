# necromancy-on-nats
bring your at home ewaste to life!

A distributed control system using NATS messaging to coordinate hubs and nodes for monitoring and controlling applications and services. Perfect for repurposing e-waste and creating IoT networks.

## Overview

Necromancy on NATS consists of:
- **Hubs**: Central control and monitoring services
- **Nodes**: Individual service instances (e.g., Raspberry Pi devices) that connect to NATS and perform operations

## Node Service

The node service runs on Raspberry Pi devices (or any Linux system) and connects to a NATS server to receive commands for controlling GPIO pins or triggering other services.

### Features

- **NATS Integration**: Connect to NATS servers for messaging
- **GPIO Control**: Control Raspberry Pi GPIO pins via NATS messages
- **Service Triggers**: Execute commands or trigger services remotely
- **Configurable Operations**: Define custom operations and message subjects
- **Simulation Mode**: Test without hardware (GPIO library not required)

### Prerequisites

- Python 3.7+
- NATS server (already running)
- Raspberry Pi (for GPIO functionality) - optional for testing

### Installation

1. **Clone or navigate to this repository**

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

   Note: `RPi.GPIO` requires a Raspberry Pi. If running on a different system for testing, the service will run in simulation mode.

3. **Create configuration file**:
   ```bash
   cp config.json.example config.json
   ```

4. **Edit `config.json`** to match your setup:
   - Set your NATS server address
   - Configure GPIO pins for your hardware
   - Define operations and message subjects

### Configuration

The `config.json` file contains:

- **nats**: NATS connection settings (servers, client name, reconnect behavior)
- **operations**: List of NATS subjects to subscribe to and their operation types
- **gpio**: GPIO pin configuration (pins, modes, initial states)
- **logging**: Logging level configuration

Example GPIO pin configuration:
```json
"gpio": {
  "enabled": true,
  "pins": {
    "relay1": {
      "number": 18,
      "mode": "OUT",
      "initial": false
    }
  }
}
```

### Usage

**Run the node service**:
```bash
python node.py
```

Or with a custom config file:
```bash
python node.py --config /path/to/config.json
```

**Make it executable** (optional):
```bash
chmod +x node.py
./node.py
```

### Sending Commands

#### GPIO Control

Send a message to the configured GPIO control subject (default: `necromancy.node.gpio.control`):

```json
{
  "pin": "relay1",
  "action": "set",
  "value": true
}
```

Available GPIO actions:
- `set`: Set pin to high (true) or low (false)
- `toggle`: Toggle current state
- `pulse`: Pulse high for a duration (default 0.5s)
- `get`: Read current pin state (for INPUT pins)

Example pulse:
```json
{
  "pin": "relay1",
  "action": "pulse",
  "duration": 1.0
}
```

#### Service Trigger

Send a message to the service trigger subject (default: `necromancy.node.service.trigger`):

```json
{
  "service": "my-service",
  "action": "start"
}
```

### GPIO Pin Modes

- **OUT**: Output pin for controlling relays, LEDs, etc.
- **IN**: Input pin for reading sensors, buttons, etc. (with optional pull-up/down)

### Testing Without Hardware

The service will automatically run in simulation mode if `RPi.GPIO` is not available. GPIO operations will be logged but not actually executed. This allows development and testing on non-Raspberry Pi systems.

### Systemd Service (Optional)

To run as a system service on your Raspberry Pi, create `/etc/systemd/system/necromancy-node.service`:

```ini
[Unit]
Description=Necromancy on NATS Node Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/path/to/necromancy-on-nats
ExecStart=/usr/bin/python3 /path/to/necromancy-on-nats/node.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable necromancy-node
sudo systemctl start necromancy-node
```

### Development

The node service is designed to be extensible. You can add custom operations by:
1. Adding new operation handlers in the `_handle_message` method
2. Defining new operation types in your configuration
3. Subscribing to additional NATS subjects

## License

MIT
