# myweather

Weather HAT data acquisition and AllSky integration for Raspberry Pi.

## Project

- Author: Carlos Gil (ea1ii)
- Version: 0.1
- License: MIT (see [LICENSE](LICENSE))
- GitHub: https://github.com/ea1ii/myweather

## Measurements

The service reads CPU temperature every 5 seconds and reports a rolling average after the configured five-sample buffer fills. Weather HAT measurements are updated every minute by default.

HAT output includes corrected temperature and pressure, dew point, humidity, light, wind speed and direction, and rain rate and interval total. Wind direction is available in degrees and as an 8-point cardinal token. Wind and rain values become available when the Weather HAT driver completes its pulse-count interval.

Pressure tendency is classified over a configurable 3-, 6-, or 24-hour window and includes a keyword, WMO-style code, symbol, and description. The 3-hour window is the WMO standard interval; longer windows use the same classification over a longer period.

## Settings

Runtime settings are in [config/settings.json](config/settings.json). The service reloads valid changes while running. Use [src/settings.py](src/settings.py) to list, read, or update a setting:

```bash
python src/settings.py --list
python src/settings.py --get sampling.hat_measurements_interval_minutes
python src/settings.py --set sampling.hat_measurements_interval_minutes 1
```

> **Setup reminder:** Before relying on corrected pressure, set `barometer.altitude_meters_asl` to the Weather HAT's actual altitude above sea level. AllSky variable publishing is off by default; set `publish_as_vars` to `true` if you want the service to write `AS_` files.

```bash
python src/settings.py --set barometer.altitude_meters_asl YOUR_ALTITUDE_METERS
python src/settings.py --set publish_as_vars true
```

### Parameter Reference

Top-level switches:

- `debug` (boolean): write the current readings to `data/data.json` after measurements.
- `publish_as_vars` (boolean): write readings as uppercase `AS_*.txt` files for AllSky. Disabled by default.

`barometer`:

- `altitude_meters_asl`: station altitude above sea level in meters; used for pressure altitude compensation.

`pressure_tendency`:

- `available_window_hours`: supported evaluation windows. This list is used to validate `window_hours`.
- `window_hours`: pressure-history window, currently one of 3, 6, or 24 hours. The WMO tendency standard uses 3 hours.
- `steady_threshold_hpa`: pressure change in hPa at or below which a change is treated as steady.

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

Debug output to [data/data.json](data/data.json) is controlled by `debug`. AllSky `AS_` variable publishing is controlled by `publish_as_vars` and is disabled by default. When enabled, the service writes temperature, dew point, humidity, pressure, light, wind, and rain values to `/home/pi/allsky/variables/`.

## Install And Run

The systemd unit expects the project at `/home/pi/myweather` and runs `src/CGweather.py`. Install and start it with:

```bash
sudo install -m 644 systemd/weatherhat.service /etc/systemd/system/weatherhat.service
sudo systemctl daemon-reload
sudo systemctl start weatherhat
sudo systemctl status weatherhat
```

For boot startup and service lifecycle commands, see [systemd/README.md](systemd/README.md).