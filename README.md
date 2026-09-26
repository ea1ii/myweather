# myweather

Collect weather data from a Pimoroni Weather HAT and publish it for Allsky via
`AS_` environment variables. Optionally, publish the same readings as JSON to an
MQTT topic.

## Usage

Print shell exports for Allsky:

```bash
python myweather.py
```

Use the output with `eval` if you want the variables in your current shell:

```bash
eval "$(python myweather.py)"
```

Publish the same readings to MQTT:

```bash
python myweather.py \
  --mqtt-host mqtt.example.net \
  --mqtt-topic allsky/weather
```

## Notes

- The script expects the Pimoroni `weatherhat` Python module to be installed on
  the target device.
- MQTT publishing uses `paho-mqtt` when MQTT options are provided.