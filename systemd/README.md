# myweather

Weather HAT integration for AllSky running on Raspberry Pi.

## Features
- Reads Weather HAT sensor data
- Generates AllSky AS_ variables
- Runs as a systemd daemon
- Fully modular and version-controlled

## Folder Structure
src/       → Python daemon and helpers
config/    → Settings (future)
systemd/   → Service file
data/      → Logs and cache

## Deployment
Copy systemd/weatherhat.service to /etc/systemd/system/
Enable and start the service:
sudo systemctl enable weatherhat
sudo systemctl start weatherhat
