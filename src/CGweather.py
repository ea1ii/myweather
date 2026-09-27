import json
import threading
import time
from collections import deque
from pathlib import Path

import helpers


class Weather:
    def __init__(self, hat=None):
        self.variables_path = Path("/home/pi/allsky/variables")
        self.config_path = Path(__file__).resolve().parent.parent / "config" / "correction_factors.json"
        self.config = self.read_config()
        self._config_mtime_ns = self.config_path.stat().st_mtime_ns
        if hat is None:
            from weatherhat import WeatherHAT

            hat = WeatherHAT()
        self.hat = hat
        self.cpu_temperature = None
        self.cpu_temperature_sample = None
        self.hat_temperature_raw = None
        self.hat_temperature = None
        self.debug_data_path = Path(__file__).resolve().parent.parent / "data" / "data.json"
        self.cpu_temperature_buffer = deque()
        self._temperature_lock = threading.Lock()
        self.resize_cpu_temperature_buffer(
            self.config["sampling"]["cpu_temperature_samples_to_average"]
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
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return False

        self.config = new_config
        helpers.CORRECTION_FACTORS = new_config
        self._config_mtime_ns = config_mtime_ns
        if sample_count != self.cpu_temperature_buffer.maxlen:
            self.resize_cpu_temperature_buffer(sample_count)
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

    def read_cpu_temperature(self):
        with self._temperature_lock:
            temperature_millidegrees = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
            self.cpu_temperature_sample = float(temperature_millidegrees.strip()) / 1000
            self.cpu_temperature_buffer.append(self.cpu_temperature_sample)
            self._update_cpu_temperature_average()
            self._write_debug_data()
            return self.cpu_temperature

    def read_hat_temperature(self):
        with self._temperature_lock:
            self.hat_temperature_raw, self.hat_temperature = helpers.adjusted_temperature(self.hat.temperature)
            self._write_debug_data()
            return self.hat_temperature

    def _write_debug_data(self):
        if not self.config.get("debug", False):
            return

        data = {
            "cpu_temperature_sample_celsius": self.cpu_temperature_sample,
            "cpu_temperature_average_celsius": self.cpu_temperature,
            "hat_temperature_raw_celsius": self.hat_temperature_raw,
            "hat_temperature_corrected_celsius": self.hat_temperature,
        }
        self.debug_data_path.parent.mkdir(parents=True, exist_ok=True)
        self.debug_data_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _run(self):
        next_cpu_temp_read = time.monotonic()
        next_hat_temp_read = next_cpu_temp_read
        while not self._stop_event.is_set():
            if self.reload_config_if_changed():
                next_cpu_temp_read = time.monotonic()
                next_hat_temp_read = next_cpu_temp_read

            sampling = self.config["sampling"]
            cpu_temp_interval = sampling["cpu_temperature_interval_seconds"]
            hat_temp_interval = sampling["hat_temperature_interval_minutes"] * 60
            current_time = time.monotonic()
            if current_time >= next_cpu_temp_read:
                self.read_cpu_temperature()
                next_cpu_temp_read = time.monotonic() + cpu_temp_interval

            current_time = time.monotonic()
            if current_time >= next_hat_temp_read:
                if self.cpu_temperature is not None:
                    self.read_hat_temperature()
                next_hat_temp_read = time.monotonic() + hat_temp_interval

            next_read_time = min(next_cpu_temp_read, next_hat_temp_read)
            wait_time = max(0.0, next_read_time - time.monotonic())
            self._stop_event.wait(wait_time)

    def stop(self):
        self._stop_event.set()
        self._reader_thread.join()

