# systemd service

The `weatherhat` service runs `/home/pi/myweather/src/CGweather.py`. Install or refresh the unit from the project root:

```bash
sudo install -m 644 systemd/weatherhat.service /etc/systemd/system/weatherhat.service
sudo systemctl daemon-reload
```

## Service Commands

Start now:

```bash
sudo systemctl start weatherhat
```

Stop now:

```bash
sudo systemctl stop weatherhat
```

Enable automatic startup at boot:

```bash
sudo systemctl enable weatherhat
```

Disable automatic startup at boot. This does not stop a currently running service:

```bash
sudo systemctl disable weatherhat
```

Stop now and disable boot startup together:

```bash
sudo systemctl disable --now weatherhat
```

Check status and recent logs:

```bash
sudo systemctl status weatherhat
sudo journalctl -u weatherhat -f
```

The unit uses `Restart=always`, so systemd restarts the process if it exits unexpectedly. An explicit `systemctl stop` remains stopped.
