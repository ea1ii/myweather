# myweather

Weather HAT data acquisition and AllSky integration for Raspberry Pi.

## Project

- Author: Carlos Gil (ea1ii)
- Version: 0.1
- License: MIT (see [LICENSE](LICENSE))
- GitHub: https://github.com/ea1ii/myweather
- Development conversation: [verbatim indexed transcript](DEVELOPMENT_CONVERSATION.md)

## Measurements

The service reads CPU temperature every 5 seconds and reports a rolling average after the configured five-sample buffer fills. Weather HAT measurements are updated every minute by default.

HAT output includes corrected temperature and pressure, dew point, humidity, light, wind speed and direction, and rain rate and interval total. Rain also includes a cumulative event total with start and last-rain timestamps. The event total resets after the configured dry period. Wind direction is available in degrees and as an 8-point cardinal token. Wind and rain values become available when the Weather HAT driver completes its pulse-count interval.

Pressure tendency is classified over a configurable 3-, 6-, or 24-hour window and includes a keyword, WMO-style code, symbol, and description. The 3-hour window is the WMO standard interval; longer windows use the same classification over a longer period.

## Settings

Runtime settings are in [config/settings.json](config/settings.json). The service reloads valid changes while running. Use [src/settings.py](src/settings.py) to list, read, or update a setting:

```bash
python src/settings.py --list
python src/settings.py --get sampling.hat_measurements_interval_minutes
python src/settings.py --set sampling.hat_measurements_interval_minutes 1
```

> **Setup reminder:** Before relying on corrected pressure, set `barometer.altitude_meters_asl` to the Weather HAT's actual altitude above sea level. Allsky Extra Data publishing is off by default; set `publish_as_vars` to `true` to publish readings.

```bash
python src/settings.py --set barometer.altitude_meters_asl YOUR_ALTITUDE_METERS
python src/settings.py --set publish_as_vars true
```

### Parameter Reference

Top-level switches:

- `debug` (boolean): write the current readings to `data/data.json` after measurements.
- `publish_as_vars` (boolean): write Allsky Extra Data to `/home/pi/allsky/config/overlay/extra/weather.json`. Disabled by default. Keys use the unique `EA1II_` prefix; Allsky exposes them to overlays with an `AS_` prefix.
- `publish_to_influxdb` (boolean): send measurements to InfluxDB. Disabled by default; credentials come from the systemd environment file.
- `influxdb.measurement`: InfluxDB measurement name; defaults to `weatherhat`.
- `influxdb.station`: station tag attached to each point; defaults to `CGallsky`.

`barometer`:

- `altitude_meters_asl`: station altitude above sea level in meters; used for pressure altitude compensation.

`pressure_tendency`:

- `available_window_hours`: supported evaluation windows. This list is used to validate `window_hours`.
- `window_hours`: pressure-history window, currently one of 3, 6, or 24 hours. The WMO tendency standard uses 3 hours.
- `steady_threshold_hpa`: pressure change in hPa at or below which a change is treated as steady.

`rain_event`:

- `dry_period_minutes`: duration without measured rain before the cumulative event total resets to zero. Defaults to 60 minutes.

`temperature` and `humidity`:

- `adjustment_method`: choose `linear` or `polynomial`; each group's `available_adjustment_methods` lists the accepted values and is used by the settings CLI for validation.
- `linear.slope` and `linear.intercept`: apply `slope * raw_value + intercept`.
- Temperature `polynomial.cubic_a` through `cubic_d`: coefficients for `a*x^3 + b*x^2 + c*x + d`.
- Humidity `polynomial.quadratic_a` through `quadratic_c`: coefficients for `a*x^2 + b*x + c`; corrected humidity is capped at 100%.
- `temperature.temp_factor` is present for reference but is not currently used by the runtime.

`sampling`:

- `cpu_temperature_interval_seconds`: time between CPU temperature samples.
- `cpu_temperature_samples_to_average`: rolling CPU sample count; no average is available until the buffer fills. The buffer resizes when this setting changes.
- `hat_measurements_interval_minutes`: time between Weather HAT updates. It also defines the interval represented by each rain-total reading and determines pressure-history buffer capacity.
- `weather_interval_seconds` is present for reference but is not currently used by the runtime.

Debug output to [data/data.json](data/data.json) is controlled by `debug`. When publishing is enabled, the Extra Data JSON contains temperature, dew point, humidity, pressure, light, wind, and rain values with expiry times. In an Allsky overlay, reference them by their `EA1II_` key, for example `${EA1II_TEMPERATURE}`; Allsky adds the `AS_` environment-variable prefix internally.

### InfluxDB Cloud Setup

InfluxDB publishing is optional and controlled by `publish_to_influxdb` (disabled by default). Each HAT update is written to the configured `weatherhat` measurement with a station tag. The official `influxdb-client` package is used; credentials are read from a systemd environment file and are never stored in Git.

1. In InfluxDB Cloud, create or select an organization and bucket for the weather data. Create an API token with write access to that bucket. Copy the organization name, bucket name, cluster URL, and token from the InfluxDB console. Treat the token as a password; do not paste it into chat, the README, or `settings.json`.
2. On the Pi, install the client and create a private environment file from the safe template:

	```bash
	sudo apt install python3-influxdb-client
	sudo install -d -m 700 /etc/myweather
	sudo install -m 600 config/influxdb.env.example /etc/myweather/influxdb.env
	sudoedit /etc/myweather/influxdb.env
	```

3. In the editor, replace the placeholders with your InfluxDB values. Use the cluster URL exactly as shown in the console (including `https://`), but do not append `/api/v2/write`. Keep the file as simple `KEY=value` lines without `export`:

	```text
	INFLUXDB_URL=https://your-cluster-url
	INFLUXDB_TOKEN=your-write-token
	INFLUXDB_ORG=your-organization
	INFLUXDB_BUCKET=weather
	```

4. Install the current unit file, enable publishing, and restart the service. Reinstalling the unit is important when its `EnvironmentFile` setting has changed; `daemon-reload` alone does not copy the repository file into systemd:

	```bash
	sudo install -m 644 systemd/weatherhat.service /etc/systemd/system/weatherhat.service
	python src/settings.py --set publish_to_influxdb true
	sudo systemctl daemon-reload
	sudo systemctl restart weatherhat
	sudo systemctl status weatherhat
	```

5. Check the service log for connection or authorization errors, then open the bucket in InfluxDB Data Explorer. Look for measurement `weatherhat`, station tag from `influxdb.station` (currently `CGallsky`), and fields such as `temperature_corrected_celsius`, `pressure_corrected_hpa`, `wind_speed_m_s`, and `rain_interval_total_mm`.

The optional systemd environment file is `/etc/myweather/influxdb.env`; its mode should remain `600`. Never copy the real file into the repository. The tracked `config/influxdb.env.example` contains placeholders only. To stop sending data, set `publish_to_influxdb` back to `false` and restart the service. Allsky Extra Data publishing remains independent.

## Install And Run

The systemd unit expects the project at `/home/pi/myweather` and runs `src/CGweather.py`. Install and start it with:

```bash
sudo install -m 644 systemd/weatherhat.service /etc/systemd/system/weatherhat.service
sudo systemctl daemon-reload
sudo systemctl start weatherhat
sudo systemctl status weatherhat
```

For boot startup and service lifecycle commands, see [systemd/README.md](systemd/README.md).