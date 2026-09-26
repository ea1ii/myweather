# myweather

Collect weather data from a Pimoroni Weather HAT and publish it for Allsky via
`AS_` environment variables. Optionally, publish the same readings as JSON to an
MQTT topic.

## Usage

Print shell exports for Allsky:

```bash
python myweather.py
```

To avoid executing unexpected output directly, write the exports to a file, check
it, and then source it:

```bash
python myweather.py > myweather.env
cat myweather.env
. ./myweather.env
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