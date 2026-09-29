# myweather

Weather HAT data acquisition and AllSky integration for Raspberry Pi.

## Project

- Author: Carlos Gil (ea1ii)
- Version: 0.4.1
- License: MIT (see [LICENSE](LICENSE))
- GitHub: https://github.com/ea1ii/myweather
- Development conversation: [verbatim indexed transcript](DEVELOPMENT_CONVERSATION.md)

## Changelog

### 0.4.1 - 2026-09-29

- Add standalone calibration tool version and document its compatibility with CGweather 0.4.1.

### 0.4 - 2026-09-29

- Add opt-in timestamped CSV logging for raw temperature and humidity.
- Replace linear/polynomial selectors with per-channel calibration switches and degree-0-to-4 polynomial corrections.
- Add a calibration CLI for fitting temperature and humidity corrections from logged/reference samples.
- Add optional SVG calibration plots for remote viewing.
- Add optional local MAD-based rejection of magnitude outliers in calibration data.
- Add rain-event duration to debug, InfluxDB, and Allsky outputs.
- Add derived mixing ratio from corrected temperature/humidity and raw station pressure.

### 0.3 - 2026-09-29

- Persist pressure tendency history across restarts when the saved samples are recent enough.
- Add process start time and uptime to debug output.
- Refine InfluxDB and Allsky fields, unset-value defaults, and ASCII pressure-tendency symbols.

### 0.2

- Add InfluxDB publishing and Allsky Extra Data JSON integration.
- Add cumulative rain-event totals with a configurable dry-period reset and timestamped debug snapshots.
- Leave wind direction unset when wind speed is zero, and restart the service automatically after failures.

### 0.1

- Establish Weather HAT and Raspberry Pi CPU measurements, including configurable temperature and humidity corrections.
- Add rolling CPU-temperature averages, grouped JSON settings and CLI, debug output, wind/rain readings, and pressure-tendency classification.
- Add the initial systemd service and project setup documentation.

## Measurements

The service reads CPU temperature every 5 seconds and reports a rolling average after the configured five-sample buffer fills. Weather HAT measurements are updated every minute by default.

When `datalogging` is enabled, raw HAT temperature and humidity are appended at each HAT update to a timestamped CSV file in `data/logs/`.

HAT output includes corrected temperature and pressure, dew point, humidity, derived mixing ratio, light, wind speed and direction, and rain rate and interval total. Mixing ratio is calculated in g/kg from corrected temperature and humidity plus raw station pressure. Rain also includes a cumulative event total, elapsed event duration in minutes, and start and last-rain timestamps; debug output shows the pending reset due time, or `null` when no reset is pending. Duration runs from the first positive interval until the event resets after the configured dry period. The duration is also exported to Allsky as `EA1II_RAIN_EVENT_DURATION_MINUTES`. Wind direction is available in degrees and as an 8-point cardinal token. Wind and rain values become available when the Weather HAT driver completes its pulse-count interval.

### Humidity Metrics

- **Relative humidity (RH)** is the actual water-vapor partial pressure divided by the saturation vapor pressure at the same air temperature, expressed as a percentage. It describes how close the air is to saturation, not the absolute amount of water vapor.
- **Dew point** is the temperature to which air must cool, at approximately constant pressure and water-vapor content, to become saturated. The current dew-point output is supplied by the Weather HAT driver; it is not recalculated from the calibrated temperature and RH values.
- **Mixing ratio** is the mass of water vapor per mass of dry air, reported here in g/kg. It is calculated from calibrated temperature and RH and raw station pressure. The calculation uses the Magnus approximation for saturation vapor pressure:

$$
e_s(T) = 6.112\,\exp\!\left(\frac{17.67T}{T+243.5}\right)\;\text{hPa}, \qquad
e = \frac{RH}{100}e_s(T), \qquad
w = 621.98\frac{e}{p-e}\;\text{g/kg}
$$

Here, $T$ is in degrees Celsius, $RH$ is in percent, $e$ is actual vapor pressure, and $p$ is raw station pressure in hPa.

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

## Calibration CLI

Use the executable [src/calibrate.py](src/calibrate.py) to pair raw datalog samples with the reference readings in the `sample.txt` format. Datalog UTC timestamps are converted to local time; reference timestamps are interpreted in the system's local timezone. Rows are paired with the nearest reference reading within 30 seconds by default.

```bash
./src/calibrate.py \
	--input data/logs/datalog_sample.csv \
	--calibration data/logs/sample.txt \
	--channel both \
	--all-degrees \
	--show-fit \
	--plot \
	--reject-outliers \
	--report-json data/logs/calibration-report.json
```

Channels are `temperature`, `humidity`, or `both` (default). Pressure calibration is not supported because the supplied calibration format has no pressure reference. Timestamp pairs beyond `--max-time-difference-seconds` are skipped and counted. `--reject-outliers` additionally filters local raw/reference magnitude spikes with a rolling median/MAD test; it defaults to off. The default cutoff is 3.5 robust sigma with three neighboring samples on each side and minimum deviations of `0.5 C` for temperature and `2 %RH` for humidity. Rejected rows and reasons are recorded per channel in the JSON report. Without `--all-degrees`, the degree configured for each channel is used. `--all-degrees` compares the configured degrees and selects by cross-validated RMSE. Add `--update-config` to write the selected coefficients and degree and switch the selected channels to polynomial correction. The configuration is never changed unless this option is supplied. JSON report filenames receive a UTC timestamp before the extension, so repeated runs do not overwrite earlier reports. `--plot` writes one composite SVG per channel: raw HAT, reference, and corrected values over time above the calibration scatter and all fitted-degree curves. Each curve's legend shows its CV RMSE and is sorted best-to-worst; the suggested degree is highlighted. `--plot-data` separately writes time-series SVGs for raw, calibration, or both (default when the option is specified). Reference readings are limited to the datalog's time range. SVGs are saved beside the report (or beside the input file if no report was requested) and can be opened in VS Code or a browser on the PC when using Remote SSH.

Timestamp pairs outside `--max-time-difference-seconds` are always skipped. Add `--reject-outliers` to also filter local magnitude spikes from the raw and reference series using a rolling median/MAD test; `--outlier-sigma` and `--outlier-window` adjust its sensitivity and neighborhood. When enabled, the fit and plots use only retained matched pairs, and the JSON report lists rejected values and reasons. Filtering is off by default.

### Parameter Reference

Top-level switches:

- `debug` (boolean): write the current readings to `data/data.json` after measurements.
- `datalogging` (boolean): append raw temperature and humidity samples with UTC timestamps to `data/logs/datalog_<timestamp>.csv`. Defaults to `false`; enabling it while running takes effect on the next HAT update.
- `publish_as_vars` (boolean): write non-raw weather measurements to Allsky Extra Data at `/home/pi/allsky/config/overlay/extra/weather.json`. CPU temperatures and the debug timestamp are not exported. Disabled by default. Keys use the unique `EA1II_` prefix; Allsky exposes them to overlays with an `AS_` prefix.
- `publish_to_influxdb` (boolean): send all numeric measurements, including raw readings, to InfluxDB. Disabled by default; credentials come from the systemd environment file.
- `influxdb.measurement`: InfluxDB measurement name; defaults to `weatherhat`.
- `influxdb.station`: station tag attached to each point; defaults to `CGallsky`.

`barometer`:

- `altitude_meters_asl`: station altitude above sea level in meters; used for pressure altitude compensation.

`pressure_tendency`:

- `available_window_hours`: supported evaluation windows. This list is used to validate `window_hours`.
- `window_hours`: pressure-history window, currently one of 3, 6, or 24 hours. The WMO tendency standard uses 3 hours.
- `steady_threshold_hpa`: pressure change in hPa at or below which a change is treated as steady.
- `restore_max_age_minutes`: maximum age of the most recent saved pressure sample for restoring the history buffer after startup. History is stored in `data/cache/pressure_history.json`. If it is older or invalid, the service starts with an empty buffer. Defaults to 5 minutes.

`rain_event`:

- `dry_period_minutes`: duration without measured rain before the cumulative event total resets to zero. Defaults to 30 minutes.

`temperature` and `humidity`:

- `calibration_enabled`: apply the channel's polynomial correction when `true`; when `false`, use the raw reading unchanged.
- `polynomial.available_degrees`: informational list of supported degrees, 0 through 4.
- `polynomial.degree`: selected degree from 0 to 4. Degree 0 applies a constant offset; degree 1 is linear. Both channels currently use degree 1 with coefficients fitted from the calibration samples.
- `polynomial.coef_0` through `coef_4`: coefficients for the constant through fourth-power terms; only coefficients up to `degree` are used. Corrected humidity remains capped at 100%.

`sampling`:

- `cpu_temperature_interval_seconds`: time between CPU temperature samples.
- `cpu_temperature_samples_to_average`: rolling CPU sample count; no average is available until the buffer fills. The buffer resizes when this setting changes.
- `hat_measurements_interval_minutes`: time between Weather HAT updates. It also defines the interval represented by each rain-total reading and determines pressure-history buffer capacity.
- `datalogging_interval_minutes`: minimum time between raw temperature/humidity CSV samples when `datalogging` is enabled. Defaults to 1 minute; logging still occurs only on HAT measurement updates.

Debug output to [data/data.json](data/data.json) is controlled by `debug`. Allsky publishing includes non-raw weather measurements with expiry times; unset strings are exported as `-` and unset numeric readings as `0`. It excludes the debug timestamp and CPU temperatures. `EA1II_TENDENCY_SYMBOL_ALT` provides an ASCII alternative to the Unicode tendency symbol (`^` rising, `v` falling, `=` steady). In an Allsky overlay, reference values by their `EA1II_` key, for example `${EA1II_TEMPERATURE}`; Allsky adds the `AS_` environment-variable prefix internally. InfluxDB receives only numeric debug measurements, including raw HAT readings; string fields are excluded.

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