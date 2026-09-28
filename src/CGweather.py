#!/usr/bin/env python3

#############################################################
# Weather data acquisition and processing for the WeatherHAT
#############################################################

# Author: Carlos Gil (ea1ii)
# Date: 2026-09-27
# Version: 0.2
# License: MIT (see ../LICENSE)
# GitHub: https://github.com/ea1ii/myweather
#
# Description: This script reads weather data from the WeatherHAT,
#               applies corrections based on configuration settings,
#               and provides processed weather information.
#
# Notes:
# - Ensure the WeatherHAT is properly connected before running this script.
# - The script continuously reads data in a separate thread and updates internal state.
#
# Usage:
# - Enable automatic startup at boot: sudo systemctl enable weatherhat
# - Disable automatic startup at boot: sudo systemctl disable weatherhat
# - Start: sudo systemctl start weatherhat
# - Stop:  sudo systemctl stop weatherhat

import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import helpers

WIND_DIRECTION_TOKENS = {
    "North": "N",
    "North East": "NE",
    "East": "E",
    "South East": "SE",
    "South": "S",
    "South West": "SW",
    "West": "W",
    "North West": "NW",
}

WMO_PRESSURE_TENDENCIES = {
    0: ("rising_then_falling", "↗↘", "Increasing, then decreasing; pressure is the same or higher than at the start of the window."),
    1: ("rising_then_steady_or_slower", "↗", "Increasing, then steady or increasing more slowly; pressure is higher than at the start of the window."),
    2: ("rising", "↗→", "Increasing steadily or unsteadily; pressure is higher than at the start of the window."),
    3: ("falling_or_steady_then_rising", "↘↗", "Decreasing or steady, then increasing, or increasing more rapidly; pressure is higher than at the start of the window."),
    4: ("steady", "→", "Steady; pressure is effectively unchanged over the window."),
    5: ("falling_then_rising", "↘→", "Decreasing, then increasing; pressure is the same or lower than at the start of the window."),
    6: ("falling_then_steady_or_slower", "↘→", "Decreasing, then steady or decreasing more slowly; pressure is lower than at the start of the window."),
    7: ("falling", "↘", "Decreasing steadily or unsteadily; pressure is lower than at the start of the window."),
    8: ("steady_or_rising_then_falling", "→↘", "Steady or increasing, then decreasing; pressure is lower than at the start of the window."),
}


class Weather:
    def __init__(self, hat=None):
        self.allsky_extra_path = Path("/home/pi/allsky/config/overlay/extra/weather.json")
        self.config_path = Path(__file__).resolve().parent.parent / "config" / "settings.json"
        self.config = self.read_config()
        self._config_mtime_ns = self.config_path.stat().st_mtime_ns
        self._influxdb_client = None
        self._influxdb_write_api = None
        self._influxdb_bucket = None
        self._influxdb_org = None
        self._influxdb_measurement = "weatherhat"
        self._influxdb_station = socket.gethostname()
        self._configure_influxdb()
        if hat is None:
            from weatherhat import WeatherHAT
            hat = WeatherHAT()
        self.hat = hat
        self.cpu_temperature = None
        self.cpu_temperature_sample = None
        self.hat_temperature_raw = None
        self.hat_temperature = None
        self.hat_dewpoint_celsius = None
        self.hat_humidity_raw = None
        self.hat_humidity = None
        self.hat_pressure_raw = None
        self.hat_pressure_corrected = None
        self.hat_light_lux = None
        self.hat_wind_speed_m_s = None
        self.hat_wind_direction_degrees = None
        self.hat_wind_direction_cardinal = None
        self.hat_rain_rate_mm_s = None
        self.hat_rain_total_mm = None
        self.hat_rain_total_period_minutes = None
        self.hat_rain_event_total_mm = 0.0
        self.hat_rain_event_started_at = None
        self.hat_rain_event_last_rain_at = None
        self._rain_event_last_rain_monotonic = None
        self._rain_event_dry_period_seconds(self.config)
        self.pressure_tendency_buffer = deque()
        self.pressure_tendency = None
        self.debug_data_path = Path(__file__).resolve().parent.parent / "data" / "data.json"
        self.cpu_temperature_buffer = deque()
        self._temperature_lock = threading.Lock()
        self.resize_cpu_temperature_buffer(
            self.config["sampling"]["cpu_temperature_samples_to_average"]
        )
        self.resize_pressure_tendency_buffer(
            self._pressure_tendency_buffer_capacity(self.config)
        )
        self._stop_event = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._run,
            name="weather-temperature-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def read_config(self):
        with self.config_path.open(encoding="utf-8") as config_file:
            return json.load(config_file)

    def _configure_influxdb(self):
        if self._influxdb_client is not None:
            self._influxdb_client.close()
        self._influxdb_client = None
        self._influxdb_write_api = None
        self._influxdb_bucket = None
        self._influxdb_org = None

        if not self.config.get("publish_to_influxdb", False):
            return

        required_environment = ("INFLUXDB_URL", "INFLUXDB_TOKEN", "INFLUXDB_ORG", "INFLUXDB_BUCKET")
        missing = [name for name in required_environment if not os.environ.get(name)]
        if missing:
            print(
                "InfluxDB publishing enabled but environment variables are missing: " + ", ".join(missing),
                file=sys.stderr,
                flush=True,
            )
            return

        try:
            from influxdb_client import InfluxDBClient
            from influxdb_client.client.write_api import SYNCHRONOUS

            self._influxdb_client = InfluxDBClient(
                url=os.environ["INFLUXDB_URL"],
                token=os.environ["INFLUXDB_TOKEN"],
                org=os.environ["INFLUXDB_ORG"],
                timeout=5000,
            )
            self._influxdb_write_api = self._influxdb_client.write_api(write_options=SYNCHRONOUS)
            self._influxdb_bucket = os.environ["INFLUXDB_BUCKET"]
            self._influxdb_org = os.environ["INFLUXDB_ORG"]
            influxdb_settings = self.config.get("influxdb", {})
            self._influxdb_measurement = influxdb_settings.get("measurement", "weatherhat")
            self._influxdb_station = influxdb_settings.get("station", socket.gethostname())
        except Exception:
            print(
                "Unable to initialize InfluxDB publishing; check the client installation and settings.",
                file=sys.stderr,
                flush=True,
            )
            if self._influxdb_client is not None:
                self._influxdb_client.close()
            self._influxdb_client = None
            self._influxdb_write_api = None

    def _write_influxdb(self):
        if self._influxdb_write_api is None:
            return

        from influxdb_client import Point, WritePrecision

        tendency = self.pressure_tendency or {}
        fields = {
            "cpu_temperature_sample_celsius": self.cpu_temperature_sample,
            "cpu_temperature_average_celsius": self.cpu_temperature,
            "temperature_raw_celsius": self.hat_temperature_raw,
            "temperature_corrected_celsius": self.hat_temperature,
            "dewpoint_celsius": self.hat_dewpoint_celsius,
            "humidity_raw_percent": self.hat_humidity_raw,
            "humidity_corrected_percent": self.hat_humidity,
            "pressure_raw_hpa": self.hat_pressure_raw,
            "pressure_corrected_hpa": self.hat_pressure_corrected,
            "pressure_tendency_code": tendency.get("code"),
            "pressure_tendency": tendency.get("keyword"),
            "pressure_tendency_symbol": tendency.get("symbol"),
            "light_lux": self.hat_light_lux,
            "wind_speed_m_s": self.hat_wind_speed_m_s,
            "wind_direction_degrees": self.hat_wind_direction_degrees,
            "wind_direction_cardinal": self.hat_wind_direction_cardinal,
            "rain_rate_mm_s": self.hat_rain_rate_mm_s,
            "rain_interval_total_mm": self.hat_rain_total_mm,
            "rain_interval_minutes": self.hat_rain_total_period_minutes,
            "rain_event_total_mm": self.hat_rain_event_total_mm,
            "rain_event_started_at": self.hat_rain_event_started_at,
            "rain_event_last_rain_at": self.hat_rain_event_last_rain_at,
        }
        point = Point(self._influxdb_measurement).tag("station", self._influxdb_station)
        has_fields = False
        for name, value in fields.items():
            if value is None:
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            point.field(name, value)
            has_fields = True
        if not has_fields:
            return

        point.time(datetime.now(timezone.utc), WritePrecision.S)
        try:
            self._influxdb_write_api.write(
                bucket=self._influxdb_bucket,
                org=self._influxdb_org,
                record=point,
            )
        except Exception as error:
            print(f"InfluxDB write failed; will retry next HAT update: {error}", file=sys.stderr, flush=True)

    def reload_config_if_changed(self):
        try:
            config_mtime_ns = self.config_path.stat().st_mtime_ns
            if config_mtime_ns == self._config_mtime_ns:
                return False

            new_config = self.read_config()
            if self.config_path.stat().st_mtime_ns != config_mtime_ns:
                return False

            sample_count = new_config["sampling"]["cpu_temperature_samples_to_average"]
            if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
                return False
            self._rain_event_dry_period_seconds(new_config)
            pressure_buffer_capacity = self._pressure_tendency_buffer_capacity(new_config)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False

        influxdb_settings_changed = (
            new_config.get("publish_to_influxdb", False) != self.config.get("publish_to_influxdb", False) or
            new_config.get("influxdb", {}) != self.config.get("influxdb", {})
        )
        self.config = new_config
        helpers.CORRECTION_FACTORS = new_config
        self._config_mtime_ns = config_mtime_ns
        if sample_count != self.cpu_temperature_buffer.maxlen:
            self.resize_cpu_temperature_buffer(sample_count)
        self.resize_pressure_tendency_buffer(pressure_buffer_capacity)
        if influxdb_settings_changed:
            self._configure_influxdb()
        return True

    def resize_cpu_temperature_buffer(self, sample_count):
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
            raise ValueError("CPU temperature sample count must be a positive integer")

        with self._temperature_lock:
            recent_samples = list(self.cpu_temperature_buffer)[-sample_count:]
            self.cpu_temperature_buffer = deque(recent_samples, maxlen=sample_count)
            self.config["sampling"]["cpu_temperature_samples_to_average"] = sample_count
            self._update_cpu_temperature_average()

    def _update_cpu_temperature_average(self):
        if len(self.cpu_temperature_buffer) < self.cpu_temperature_buffer.maxlen:
            self.cpu_temperature = None
        else:
            self.cpu_temperature = sum(self.cpu_temperature_buffer) / self.cpu_temperature_buffer.maxlen

    def _pressure_tendency_buffer_capacity(self, config):
        tendency_config = config["pressure_tendency"]
        window_hours = tendency_config["window_hours"]
        available_windows = tendency_config["available_window_hours"]
        interval_minutes = config["sampling"]["hat_measurements_interval_minutes"]
        threshold = tendency_config["steady_threshold_hpa"]
        if type(window_hours) is not int or window_hours not in available_windows:
            raise ValueError("Pressure tendency window must be one of the configured hour options")
        if (isinstance(interval_minutes, bool) or not isinstance(interval_minutes, (int, float)) or
                not math.isfinite(interval_minutes) or interval_minutes <= 0):
            raise ValueError("HAT measurement interval must be a positive number of minutes")
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or
                not math.isfinite(threshold) or threshold < 0):
            raise ValueError("Pressure tendency threshold must be a non-negative number")
        return math.ceil(window_hours * 60 / interval_minutes) + 1

    def _rain_event_dry_period_seconds(self, config):
        dry_period_minutes = config["rain_event"]["dry_period_minutes"]
        if (isinstance(dry_period_minutes, bool) or not isinstance(dry_period_minutes, (int, float)) or
                not math.isfinite(dry_period_minutes) or dry_period_minutes <= 0):
            raise ValueError("Rain event dry period must be a positive number of minutes")
        return dry_period_minutes * 60

    def _update_rain_event(self, interval_total_mm, observed_at, observed_at_monotonic):
        dry_period_seconds = self._rain_event_dry_period_seconds(self.config)
        if (self._rain_event_last_rain_monotonic is not None and
                observed_at_monotonic - self._rain_event_last_rain_monotonic >= dry_period_seconds):
            self.hat_rain_event_total_mm = 0.0
            self.hat_rain_event_started_at = None
            self.hat_rain_event_last_rain_at = None
            self._rain_event_last_rain_monotonic = None

        if interval_total_mm > 0:
            timestamp = observed_at.isoformat(timespec="seconds").replace("+00:00", "Z")
            if self._rain_event_last_rain_monotonic is None:
                self.hat_rain_event_started_at = timestamp
            self.hat_rain_event_total_mm += interval_total_mm
            self.hat_rain_event_last_rain_at = timestamp
            self._rain_event_last_rain_monotonic = observed_at_monotonic

    def resize_pressure_tendency_buffer(self, sample_count):
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 2:
            raise ValueError("Pressure tendency buffer must hold at least two samples")

        recent_samples = list(self.pressure_tendency_buffer)[-sample_count:]
        self.pressure_tendency_buffer = deque(recent_samples, maxlen=sample_count)
        self._update_pressure_tendency()

    def _update_pressure_tendency(self):
        self.pressure_tendency = None
        samples = list(self.pressure_tendency_buffer)
        if len(samples) < self.pressure_tendency_buffer.maxlen:
            return

        window_seconds = self.config["pressure_tendency"]["window_hours"] * 3600
        window_start = samples[-1][0] - window_seconds
        if samples[0][0] > window_start:
            return

        for index in range(1, len(samples)):
            sample_time, sample_pressure = samples[index]
            if sample_time >= window_start:
                previous_time, previous_pressure = samples[index - 1]
                fraction = (window_start - previous_time) / (sample_time - previous_time)
                start_pressure = previous_pressure + fraction * (sample_pressure - previous_pressure)
                pressures = [start_pressure] + [pressure for _, pressure in samples[index:]]
                if sample_time == window_start:
                    pressures = [sample_pressure] + [pressure for _, pressure in samples[index + 1:]]
                threshold = self.config["pressure_tendency"]["steady_threshold_hpa"]
                self.pressure_tendency = self._classify_pressure_tendency(pressures, threshold)
                return

    @staticmethod
    def _classify_pressure_tendency(pressures, threshold):
        if len(pressures) < 3:
            return None

        start = pressures[0]
        middle = pressures[len(pressures) // 2]
        end = pressures[-1]
        net_change = end - start
        first_change = middle - start
        second_change = end - middle
        peak_index = max(range(1, len(pressures) - 1), key=pressures.__getitem__)
        trough_index = min(range(1, len(pressures) - 1), key=pressures.__getitem__)
        rises_then_falls = (
            pressures[peak_index] - start > threshold and
            pressures[peak_index] - end > threshold
        )
        falls_then_rises = (
            start - pressures[trough_index] > threshold and
            end - pressures[trough_index] > threshold
        )

        if net_change > threshold:
            if rises_then_falls:
                code = 0
            elif first_change <= threshold or second_change - first_change > threshold:
                code = 3
            elif second_change <= threshold or first_change - second_change > threshold:
                code = 1
            else:
                code = 2
        elif net_change < -threshold:
            if falls_then_rises:
                code = 5
            elif first_change >= -threshold:
                code = 8
            elif second_change >= -threshold or second_change - first_change > threshold:
                code = 6
            else:
                code = 7
        elif rises_then_falls and first_change >= 0:
            code = 0
        elif falls_then_rises:
            code = 5
        else:
            code = 4

        keyword, symbol, description = WMO_PRESSURE_TENDENCIES[code]
        return {"code": code, "keyword": keyword, "symbol": symbol, "description": description}

    def read_cpu_temperature(self):
        with self._temperature_lock:
            temperature_millidegrees = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
            self.cpu_temperature_sample = float(temperature_millidegrees.strip()) / 1000
            self.cpu_temperature_buffer.append(self.cpu_temperature_sample)
            self._update_cpu_temperature_average()
            self._write_debug_data()
            return self.cpu_temperature

    def read_hat_measurements(self):
        with self._temperature_lock:
            self.hat.update()
            self.hat_pressure_raw = self.hat.pressure
            self.hat_light_lux = self.hat.lux
            self.hat_dewpoint_celsius = self.hat.dewpoint
            self.hat_humidity_raw, self.hat_humidity = helpers.adjusted_humidity(self.hat.humidity)
            if self.hat.updated_wind_rain:
                self.hat_wind_speed_m_s = self.hat.wind_speed
                if self.hat_wind_speed_m_s > 0:
                    self.hat_wind_direction_degrees = self.hat.wind_direction
                    direction_name = self.hat.degrees_to_cardinal(self.hat_wind_direction_degrees)
                    self.hat_wind_direction_cardinal = WIND_DIRECTION_TOKENS[direction_name]
                else:
                    self.hat_wind_direction_degrees = None
                    self.hat_wind_direction_cardinal = None
                self.hat_rain_rate_mm_s = self.hat.rain
                self.hat_rain_total_mm = self.hat.rain_total
                self.hat_rain_total_period_minutes = self.config["sampling"]["hat_measurements_interval_minutes"]
                self._update_rain_event(
                    self.hat_rain_total_mm,
                    datetime.now(timezone.utc),
                    time.monotonic(),
                )
            else:
                self.hat_wind_speed_m_s = None
                self.hat_wind_direction_degrees = None
                self.hat_wind_direction_cardinal = None
                self.hat_rain_rate_mm_s = None
                self.hat_rain_total_mm = None
                self.hat_rain_total_period_minutes = None
            self.hat_temperature_raw, self.hat_temperature = helpers.adjusted_temperature(self.hat.temperature)
            altitude = self.config["barometer"]["altitude_meters_asl"]
            pressure_factor = helpers.barometer_altitude_comp_factor(altitude, self.hat_temperature)
            self.hat_pressure_corrected = self.hat_pressure_raw * pressure_factor
            self.pressure_tendency_buffer.append((time.monotonic(), self.hat_pressure_corrected))
            self._update_pressure_tendency()
            self._write_debug_data()
            self._write_allsky_extra_data()
            self._write_influxdb()
            tendency = self.pressure_tendency or {}
            return {
                "temperature_raw_celsius": self.hat_temperature_raw,
                "temperature_corrected_celsius": self.hat_temperature,
                "dewpoint_celsius": self.hat_dewpoint_celsius,
                "humidity_raw_percent": self.hat_humidity_raw,
                "humidity_corrected_percent": self.hat_humidity,
                "pressure_raw_hpa": self.hat_pressure_raw,
                "pressure_corrected_hpa": self.hat_pressure_corrected,
                "pressure_tendency": tendency.get("keyword"),
                "pressure_tendency_symbol": tendency.get("symbol"),
                "pressure_tendency_code": tendency.get("code"),
                "pressure_tendency_description": tendency.get("description"),
                "light_lux": self.hat_light_lux,
                "wind_speed_m_s": self.hat_wind_speed_m_s,
                "wind_direction_degrees": self.hat_wind_direction_degrees,
                "wind_direction_cardinal": self.hat_wind_direction_cardinal,
                "rain_rate_mm_s": self.hat_rain_rate_mm_s,
                "rain_interval_total_mm": self.hat_rain_total_mm,
                "rain_interval_minutes": self.hat_rain_total_period_minutes,
                "rain_event_total_mm": self.hat_rain_event_total_mm,
                "rain_event_started_at": self.hat_rain_event_started_at,
                "rain_event_last_rain_at": self.hat_rain_event_last_rain_at,
            }

    def _write_debug_data(self):
        if not self.config.get("debug", False):
            return

        tendency = self.pressure_tendency or {}
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "cpu": {
                "temperature": {
                    "sample_celsius": self.cpu_temperature_sample,
                    "average_celsius": self.cpu_temperature,
                }
            },
            "hat": {
                "temperature": {
                    "raw_celsius": self.hat_temperature_raw,
                    "corrected_celsius": self.hat_temperature,
                },
                "dewpoint_celsius": self.hat_dewpoint_celsius,
                "humidity": {
                    "raw_percent": self.hat_humidity_raw,
                    "corrected_percent": self.hat_humidity,
                },
                "pressure": {
                    "raw_hpa": self.hat_pressure_raw,
                    "corrected_hpa": self.hat_pressure_corrected,
                    "tendency": tendency.get("keyword"),
                    "tendency_symbol": tendency.get("symbol"),
                    "tendency_code": tendency.get("code"),
                    "tendency_description": tendency.get("description"),
                },
                "light": {
                    "lux": self.hat_light_lux,
                },
                "wind": {
                    "speed_m_s": self.hat_wind_speed_m_s,
                    "direction_degrees": self.hat_wind_direction_degrees,
                    "direction_cardinal": self.hat_wind_direction_cardinal,
                },
                "rain": {
                    "rate_mm_s": self.hat_rain_rate_mm_s,
                    "interval_total_mm": self.hat_rain_total_mm,
                    "interval_minutes": self.hat_rain_total_period_minutes,
                    "event_total_mm": self.hat_rain_event_total_mm,
                    "event_started_at": self.hat_rain_event_started_at,
                    "event_last_rain_at": self.hat_rain_event_last_rain_at,
                },
            },
        }
        self.debug_data_path.parent.mkdir(parents=True, exist_ok=True)
        self.debug_data_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _write_allsky_extra_data(self):
        if not self.config.get("publish_as_vars", False):
            return

        tendency = self.pressure_tendency or {}
        variables = {
            "EA1II_TEMPERATURE": self.hat_temperature,
            "EA1II_DEWPOINT": self.hat_dewpoint_celsius,
            "EA1II_HUMIDITY": self.hat_humidity,
            "EA1II_PRESSURE": self.hat_pressure_corrected,
            "EA1II_TENDENCY": tendency.get("keyword"),
            "EA1II_TENDENCY_SYMBOL": tendency.get("symbol"),
            "EA1II_LIGHT": self.hat_light_lux,
            "EA1II_WIND_SPEED": self.hat_wind_speed_m_s,
            "EA1II_WIND_DIRECTION": self.hat_wind_direction_cardinal,
            "EA1II_WIND_DIRECTION_DEGREES": self.hat_wind_direction_degrees,
            "EA1II_RAIN_RATE": self.hat_rain_rate_mm_s,
            "EA1II_RAIN_TOTAL": self.hat_rain_total_mm,
            "EA1II_RAIN_TOTAL_PERIOD_MINUTES": self.hat_rain_total_period_minutes,
            "EA1II_RAIN_EVENT_TOTAL": self.hat_rain_event_total_mm,
            "EA1II_RAIN_EVENT_STARTED_AT": self.hat_rain_event_started_at,
            "EA1II_RAIN_EVENT_LAST_RAIN_AT": self.hat_rain_event_last_rain_at,
        }
        expiry_seconds = max(
            180,
            int(self.config["sampling"]["hat_measurements_interval_minutes"] * 60 * 3),
        )
        extra_data = {
            name: {"value": value, "expires": expiry_seconds}
            for name, value in variables.items()
            if value is not None
        }

        self.allsky_extra_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.allsky_extra_path.parent,
                prefix=".weather-",
                suffix=".json.tmp",
                delete=False,
            ) as extra_file:
                json.dump(extra_data, extra_file, indent=4)
                extra_file.write("\n")
                temporary_path = Path(extra_file.name)
            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, self.allsky_extra_path)
        except OSError as error:
            print(f"Unable to write Allsky Extra Data: {error}", flush=True)
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _run(self):
        next_cpu_temp_read = time.monotonic()
        next_hat_temp_read = next_cpu_temp_read
        while not self._stop_event.is_set():
            if self.reload_config_if_changed():
                next_cpu_temp_read = time.monotonic()
                next_hat_temp_read = next_cpu_temp_read

            sampling = self.config["sampling"]
            cpu_temp_interval = sampling["cpu_temperature_interval_seconds"]
            hat_measurements_interval = sampling["hat_measurements_interval_minutes"] * 60
            current_time = time.monotonic()
            if current_time >= next_cpu_temp_read:
                self.read_cpu_temperature()
                next_cpu_temp_read = time.monotonic() + cpu_temp_interval

            current_time = time.monotonic()
            if current_time >= next_hat_temp_read:
                if self.cpu_temperature is not None:
                    self.read_hat_measurements()
                next_hat_temp_read = time.monotonic() + hat_measurements_interval

            next_read_time = min(next_cpu_temp_read, next_hat_temp_read)
            wait_time = max(0.0, next_read_time - time.monotonic())
            self._stop_event.wait(wait_time)

    def stop(self):
        self._stop_event.set()
        self._reader_thread.join()
        if self._influxdb_client is not None:
            self._influxdb_client.close()


def main():
    weather = Weather()
    try:
        weather._reader_thread.join()
    except KeyboardInterrupt:
        weather.stop()


if __name__ == "__main__":
    main()

