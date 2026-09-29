#!/usr/bin/env python3

#############################################################
# Weather data acquisition and processing for the WeatherHAT
#############################################################

# Author: Carlos Gil (ea1ii)
# Date: 2026-09-27
# Version: 0.4.1
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

import csv
import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
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
ASCII_TENDENCY_SYMBOL_TRANSLATION = str.maketrans({"↗": "^", "↘": "v", "→": "="})


class Weather:
    def __init__(self, hat=None):
        self.process_started_at = datetime.now(timezone.utc)
        self.process_started_monotonic = time.monotonic()
        self.allsky_extra_path = Path("/home/pi/allsky/config/overlay/extra/weather.json")
        self.config_path = Path(__file__).resolve().parent.parent / "config" / "settings.json"
        self.config = self.read_config()
        helpers.validate_calibration_settings(self.config["temperature"], "Temperature")
        helpers.validate_calibration_settings(self.config["humidity"], "Humidity")
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
        self.hat_mixing_ratio_g_kg = None
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
        self._rain_event_started_monotonic = None
        self._rain_event_last_rain_monotonic = None
        self._rain_event_dry_period_seconds(self.config)
        self._pressure_history_restore_max_age_seconds(self.config)
        self._datalog_interval_seconds(self.config)
        self.pressure_tendency_buffer = deque()
        self.pressure_tendency = None
        data_dir = Path(__file__).resolve().parent.parent / "data"
        self.datalog_dir = data_dir / "logs"
        self._datalog_path = None
        self._datalog_last_sample_monotonic = None
        self.debug_data_path = data_dir / "data.json"
        self.pressure_history_path = data_dir / "cache" / "pressure_history.json"
        self.cpu_temperature_buffer = deque()
        self._temperature_lock = threading.Lock()
        self.resize_cpu_temperature_buffer(
            self.config["sampling"]["cpu_temperature_samples_to_average"]
        )
        self.resize_pressure_tendency_buffer(
            self._pressure_tendency_buffer_capacity(self.config)
        )
        self._load_pressure_tendency_history()
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
            "humidity_corrected_percent_float": self.hat_humidity,
            "mixing_ratio_g_kg": self.hat_mixing_ratio_g_kg,
            "pressure_raw_hpa": self.hat_pressure_raw,
            "pressure_corrected_hpa": self.hat_pressure_corrected,
            "pressure_tendency_code": tendency.get("code"),
            "light_lux": self.hat_light_lux,
            "wind_speed_m_s": self.hat_wind_speed_m_s,
            "wind_direction_degrees": self.hat_wind_direction_degrees,
            "rain_rate_mm_s": self.hat_rain_rate_mm_s,
            "rain_interval_total_mm": self.hat_rain_total_mm,
            "rain_interval_minutes": self.hat_rain_total_period_minutes,
            "rain_event_total_mm": self.hat_rain_event_total_mm,
            "rain_event_duration_minutes": self._rain_event_duration_minutes(),
        }
        point = Point(self._influxdb_measurement).tag("station", self._influxdb_station)
        has_fields = False
        for name, value in fields.items():
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if name == "humidity_corrected_percent_float":
                value = float(value)
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

            helpers.validate_calibration_settings(new_config["temperature"], "Temperature")
            helpers.validate_calibration_settings(new_config["humidity"], "Humidity")
            sample_count = new_config["sampling"]["cpu_temperature_samples_to_average"]
            if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
                return False
            if not isinstance(new_config.get("datalogging", False), bool):
                return False
            self._datalog_interval_seconds(new_config)
            self._rain_event_dry_period_seconds(new_config)
            self._pressure_history_restore_max_age_seconds(new_config)
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

    def _pressure_history_restore_max_age_seconds(self, config):
        max_age_minutes = config["pressure_tendency"]["restore_max_age_minutes"]
        if (isinstance(max_age_minutes, bool) or not isinstance(max_age_minutes, (int, float)) or
                not math.isfinite(max_age_minutes) or max_age_minutes <= 0):
            raise ValueError("Pressure history restore age must be a positive number of minutes")
        return max_age_minutes * 60

    def _datalog_interval_seconds(self, config):
        interval_minutes = config["sampling"]["datalogging_interval_minutes"]
        if (isinstance(interval_minutes, bool) or not isinstance(interval_minutes, (int, float)) or
                not math.isfinite(interval_minutes) or interval_minutes <= 0):
            raise ValueError("Datalogging interval must be a positive number of minutes")
        return interval_minutes * 60

    def _update_rain_event(self, interval_total_mm, observed_at, observed_at_monotonic):
        dry_period_seconds = self._rain_event_dry_period_seconds(self.config)
        if (self._rain_event_last_rain_monotonic is not None and
                observed_at_monotonic - self._rain_event_last_rain_monotonic >= dry_period_seconds):
            self.hat_rain_event_total_mm = 0.0
            self.hat_rain_event_started_at = None
            self.hat_rain_event_last_rain_at = None
            self._rain_event_started_monotonic = None
            self._rain_event_last_rain_monotonic = None

        if interval_total_mm > 0:
            timestamp = observed_at.isoformat(timespec="seconds").replace("+00:00", "Z")
            if self._rain_event_last_rain_monotonic is None:
                self.hat_rain_event_started_at = timestamp
                self._rain_event_started_monotonic = observed_at_monotonic
            self.hat_rain_event_total_mm += interval_total_mm
            self.hat_rain_event_last_rain_at = timestamp
            self._rain_event_last_rain_monotonic = observed_at_monotonic

    def _rain_event_duration_minutes(self):
        if self._rain_event_started_monotonic is None:
            return None
        return (time.monotonic() - self._rain_event_started_monotonic) / 60

    def _write_datalog(self):
        if not self.config.get("datalogging", False):
            self._datalog_path = None
            self._datalog_last_sample_monotonic = None
            return
        if self.hat_temperature_raw is None or self.hat_humidity_raw is None:
            return

        sample_monotonic = time.monotonic()
        interval_seconds = self._datalog_interval_seconds(self.config)
        if (self._datalog_last_sample_monotonic is not None and
                sample_monotonic - self._datalog_last_sample_monotonic < interval_seconds):
            return

        timestamp = datetime.now(timezone.utc)
        if self._datalog_path is None:
            filename_timestamp = timestamp.strftime("%Y%m%d_%H%MZ")
            self._datalog_path = self.datalog_dir / f"datalog_{filename_timestamp}.csv"

        self.datalog_dir.mkdir(parents=True, exist_ok=True)
        needs_header = not self._datalog_path.exists()
        try:
            with self._datalog_path.open("a", newline="", encoding="utf-8") as datalog_file:
                writer = csv.writer(datalog_file)
                if needs_header:
                    writer.writerow(("timestamp_utc", "raw_temperature_celsius", "raw_humidity_percent"))
                writer.writerow((
                    timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    self.hat_temperature_raw,
                    self.hat_humidity_raw,
                ))
            self._datalog_last_sample_monotonic = sample_monotonic
        except OSError as error:
            print(f"Unable to write datalog: {error}", file=sys.stderr, flush=True)

    def _rain_event_reset_due_at(self):
        if self._rain_event_last_rain_monotonic is None:
            return None

        remaining_seconds = (
            self._rain_event_dry_period_seconds(self.config) -
            (time.monotonic() - self._rain_event_last_rain_monotonic)
        )
        if remaining_seconds <= 0:
            return None

        reset_due_at = datetime.now(timezone.utc) + timedelta(seconds=remaining_seconds)
        return reset_due_at.isoformat(timespec="seconds").replace("+00:00", "Z")

    def resize_pressure_tendency_buffer(self, sample_count):
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 2:
            raise ValueError("Pressure tendency buffer must hold at least two samples")

        recent_samples = list(self.pressure_tendency_buffer)[-sample_count:]
        self.pressure_tendency_buffer = deque(recent_samples, maxlen=sample_count)
        self._update_pressure_tendency()

    def _load_pressure_tendency_history(self):
        try:
            history = json.loads(self.pressure_history_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            print(f"Unable to read pressure history; starting with an empty buffer: {error}", file=sys.stderr, flush=True)
            return

        try:
            if not isinstance(history, dict):
                raise ValueError("pressure history root is not an object")
            if history.get("version") != 1 or not isinstance(history.get("samples"), list):
                raise ValueError("unsupported pressure history format")
            max_age_seconds = self._pressure_history_restore_max_age_seconds(self.config)
            now_utc = datetime.now(timezone.utc)
            saved_at_text = history.get("saved_at")
            if not isinstance(saved_at_text, str):
                raise ValueError("pressure history has no valid save time")
            saved_at = datetime.fromisoformat(saved_at_text.replace("Z", "+00:00"))
            if saved_at.tzinfo is None:
                raise ValueError("pressure history save time has no timezone")
            saved_age_seconds = (now_utc - saved_at.astimezone(timezone.utc)).total_seconds()
            if saved_age_seconds < 0 or saved_age_seconds > max_age_seconds:
                return

            now_monotonic = time.monotonic()
            restored_samples = []
            previous_timestamp = None
            for sample in history["samples"]:
                if not isinstance(sample, dict) or not isinstance(sample.get("timestamp"), str):
                    raise ValueError("pressure history contains an invalid sample")
                timestamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
                pressure = sample.get("pressure_hpa")
                if timestamp.tzinfo is None:
                    raise ValueError("pressure sample time has no timezone")
                if isinstance(pressure, bool) or not isinstance(pressure, (int, float)) or not math.isfinite(pressure):
                    raise ValueError("pressure history contains an invalid pressure value")

                timestamp = timestamp.astimezone(timezone.utc)
                if previous_timestamp is not None and timestamp <= previous_timestamp:
                    raise ValueError("pressure history sample times are not increasing")
                age_seconds = (now_utc - timestamp).total_seconds()
                if age_seconds < 0:
                    raise ValueError("pressure history contains a future sample")
                restored_samples.append((now_monotonic - age_seconds, pressure, timestamp))
                previous_timestamp = timestamp

            if not restored_samples:
                return
            newest_age_seconds = (now_utc - restored_samples[-1][2]).total_seconds()
            if newest_age_seconds > max_age_seconds:
                return

            max_samples = self.pressure_tendency_buffer.maxlen
            self.pressure_tendency_buffer = deque(restored_samples[-max_samples:], maxlen=max_samples)
            self._update_pressure_tendency()
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            print(f"Ignoring invalid pressure history; starting with an empty buffer: {error}", file=sys.stderr, flush=True)

    def _save_pressure_tendency_history(self):
        if not self.pressure_tendency_buffer:
            return

        saved_at = datetime.now(timezone.utc)
        history = {
            "version": 1,
            "saved_at": saved_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "samples": [
                {
                    "timestamp": sample_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "pressure_hpa": pressure,
                }
                for _, pressure, sample_time in self.pressure_tendency_buffer
            ],
        }

        self.pressure_history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.pressure_history_path.parent,
                prefix=".pressure-history-",
                suffix=".json.tmp",
                delete=False,
            ) as history_file:
                json.dump(history, history_file, indent=2, allow_nan=False)
                history_file.write("\n")
                temporary_path = Path(history_file.name)
            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, self.pressure_history_path)
        except (OSError, ValueError) as error:
            print(f"Unable to save pressure history: {error}", file=sys.stderr, flush=True)
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

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
            sample_time, sample_pressure, _ = samples[index]
            if sample_time >= window_start:
                previous_time, previous_pressure, _ = samples[index - 1]
                fraction = (window_start - previous_time) / (sample_time - previous_time)
                start_pressure = previous_pressure + fraction * (sample_pressure - previous_pressure)
                pressures = [start_pressure] + [pressure for _, pressure, _ in samples[index:]]
                if sample_time == window_start:
                    pressures = [sample_pressure] + [pressure for _, pressure, _ in samples[index + 1:]]
                threshold = self.config["pressure_tendency"]["steady_threshold_hpa"]
                self.pressure_tendency = self._classify_pressure_tendency(pressures, threshold)
                return

    @staticmethod
    def _classify_pressure_tendency(pressures, threshold):
        """Classify a pressure curve using net change and significant turns.

        ``threshold`` is an hPa deadband. Net changes within +/- this value
        count as steady, and a peak or trough must clear it relative to both
        endpoints to count as a reversal.
        """
        if len(pressures) < 3:
            return None

        # Use the first, index-middle, and last samples to compare the whole
        # window and its two halves. The midpoint is by sample count, not time.
        start = pressures[0]
        middle = pressures[len(pressures) // 2]
        end = pressures[-1]
        net_change = end - start
        first_change = middle - start
        second_change = end - middle
        peak_index = max(range(1, len(pressures) - 1), key=pressures.__getitem__)
        trough_index = min(range(1, len(pressures) - 1), key=pressures.__getitem__)
        # Require an interior extreme to exceed both endpoints by the
        # deadband; this filters small fluctuations out of reversal patterns.
        rises_then_falls = (
            pressures[peak_index] - start > threshold and
            pressures[peak_index] - end > threshold
        )
        falls_then_rises = (
            start - pressures[trough_index] > threshold and
            end - pressures[trough_index] > threshold
        )

        # A net rise is further classified by reversals and by whether the
        # rise starts weakly or loses strength in the second half.
        if net_change > threshold:
            if rises_then_falls:
                code = 0
            # A weak/negative first half or a stronger second half represents
            # pressure that falls/is steady before rising.
            elif first_change <= threshold or second_change - first_change > threshold:
                code = 3
            # A weak or clearly weaker second half represents a rise that
            # becomes steady or slows.
            elif second_change <= threshold or first_change - second_change > threshold:
                code = 1
            else:
                code = 2
        # Apply the corresponding shape tests when pressure falls overall.
        elif net_change < -threshold:
            if falls_then_rises:
                code = 5
            # A weak/positive first half represents steady/rising pressure
            # before the overall fall.
            elif first_change >= -threshold:
                code = 8
            # A weak second-half fall or a stronger first-half decline
            # represents falling pressure that becomes steady or slows.
            elif second_change >= -threshold or second_change - first_change > threshold:
                code = 6
            else:
                code = 7
        # Inside the net-change deadband, keep a clear reversal classification;
        # otherwise treat the complete window as steady.
        elif rises_then_falls and first_change >= 0:
            code = 0
        elif falls_then_rises:
            code = 5
        else:
            code = 4

        keyword, symbol, description = WMO_PRESSURE_TENDENCIES[code]
        return {
            "code": code,
            "keyword": keyword,
            "symbol": symbol,
            "symbol_ascii": symbol.translate(ASCII_TENDENCY_SYMBOL_TRANSLATION),
            "description": description,
        }

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
            self.hat_mixing_ratio_g_kg = helpers.mixing_ratio_g_kg(
                self.hat_temperature,
                self.hat_humidity,
                self.hat_pressure_raw,
            )
            self._write_datalog()
            sample_time_utc = datetime.now(timezone.utc)
            sample_time_monotonic = time.monotonic()
            self.pressure_tendency_buffer.append(
                (sample_time_monotonic, self.hat_pressure_corrected, sample_time_utc)
            )
            self._update_pressure_tendency()
            self._save_pressure_tendency_history()
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
                "mixing_ratio_g_kg": self.hat_mixing_ratio_g_kg,
                "pressure_raw_hpa": self.hat_pressure_raw,
                "pressure_corrected_hpa": self.hat_pressure_corrected,
                "pressure_tendency": tendency.get("keyword"),
                "pressure_tendency_symbol": tendency.get("symbol"),
                "pressure_tendency_symbol_ascii": tendency.get("symbol_ascii"),
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
                "rain_event_duration_minutes": self._rain_event_duration_minutes(),
                "rain_event_started_at": self.hat_rain_event_started_at,
                "rain_event_last_rain_at": self.hat_rain_event_last_rain_at,
            }

    def _write_debug_data(self):
        if not self.config.get("debug", False):
            return

        tendency = self.pressure_tendency or {}
        elapsed_minutes = int((time.monotonic() - self.process_started_monotonic) // 60)
        uptime_days, remaining_minutes = divmod(elapsed_minutes, 24 * 60)
        uptime_hours, uptime_minutes = divmod(remaining_minutes, 60)
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "process_started_at": self.process_started_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "uptime": {
                "days": uptime_days,
                "hours": uptime_hours,
                "minutes": uptime_minutes,
            },
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
                "mixing_ratio_g_kg": self.hat_mixing_ratio_g_kg,
                "humidity": {
                    "raw_percent": self.hat_humidity_raw,
                    "corrected_percent": self.hat_humidity,
                },
                "pressure": {
                    "raw_hpa": self.hat_pressure_raw,
                    "corrected_hpa": self.hat_pressure_corrected,
                    "tendency": tendency.get("keyword"),
                    "tendency_symbol": tendency.get("symbol"),
                    "tendency_symbol_ascii": tendency.get("symbol_ascii"),
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
                    "event_duration_minutes": self._rain_event_duration_minutes(),
                    "event_started_at": self.hat_rain_event_started_at,
                    "event_last_rain_at": self.hat_rain_event_last_rain_at,
                    "event_reset_due_at": self._rain_event_reset_due_at(),
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
            "EA1II_MIXING_RATIO_G_KG": self.hat_mixing_ratio_g_kg,
            "EA1II_PRESSURE": self.hat_pressure_corrected,
            "EA1II_TENDENCY": tendency.get("keyword"),
            "EA1II_TENDENCY_SYMBOL": tendency.get("symbol"),
            "EA1II_TENDENCY_SYMBOL_ALT": tendency.get("symbol_ascii"),
            "EA1II_TENDENCY_CODE": tendency.get("code"),
            "EA1II_TENDENCY_DESCRIPTION": tendency.get("description"),
            "EA1II_LIGHT": self.hat_light_lux,
            "EA1II_WIND_SPEED": self.hat_wind_speed_m_s,
            "EA1II_WIND_DIRECTION": self.hat_wind_direction_cardinal,
            "EA1II_WIND_DIRECTION_DEGREES": self.hat_wind_direction_degrees,
            "EA1II_RAIN_RATE": self.hat_rain_rate_mm_s,
            "EA1II_RAIN_TOTAL": self.hat_rain_total_mm,
            "EA1II_RAIN_TOTAL_PERIOD_MINUTES": self.hat_rain_total_period_minutes,
            "EA1II_RAIN_EVENT_TOTAL": self.hat_rain_event_total_mm,
            "EA1II_RAIN_EVENT_DURATION_MINUTES": self._rain_event_duration_minutes(),
            "EA1II_RAIN_EVENT_STARTED_AT": self.hat_rain_event_started_at,
            "EA1II_RAIN_EVENT_LAST_RAIN_AT": self.hat_rain_event_last_rain_at,
        }
        string_variables = {
            "EA1II_TENDENCY",
            "EA1II_TENDENCY_SYMBOL",
            "EA1II_TENDENCY_SYMBOL_ALT",
            "EA1II_TENDENCY_DESCRIPTION",
            "EA1II_WIND_DIRECTION",
            "EA1II_RAIN_EVENT_STARTED_AT",
            "EA1II_RAIN_EVENT_LAST_RAIN_AT",
        }
        expiry_seconds = max(
            180,
            int(self.config["sampling"]["hat_measurements_interval_minutes"] * 60 * 3),
        )
        extra_data = {
            name: {
                "value": value if value is not None else ("-" if name in string_variables else 0),
                "expires": expiry_seconds,
            }
            for name, value in variables.items()
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

