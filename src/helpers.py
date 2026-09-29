import math
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"
with CONFIG_PATH.open(encoding="utf-8") as config_file:
    CORRECTION_FACTORS = json.load(config_file)

# Helper functions for weather data adjustments based on correction factors

# Calculate barometer compensation factor based on altitude and temperature
def barometer_altitude_comp_factor(alt, temp):
    altitude = CORRECTION_FACTORS["barometer"]["altitude_meters_asl"]
    comp_factor = math.pow(1 - (0.0065 * altitude/(temp + 0.0065 * alt + 273.15)), -5.257)
    return comp_factor


def mixing_ratio_g_kg(temperature_celsius, humidity_percent, pressure_hpa):
    values = (temperature_celsius, humidity_percent, pressure_hpa)
    if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in values):
        return None
    if temperature_celsius <= -243.5 or not 0 <= humidity_percent <= 100 or pressure_hpa <= 0:
        return None

    saturation_vapor_pressure_hpa = 6.112 * math.exp(
        17.67 * temperature_celsius / (temperature_celsius + 243.5)
    )
    vapor_pressure_hpa = humidity_percent / 100 * saturation_vapor_pressure_hpa
    pressure_difference_hpa = pressure_hpa - vapor_pressure_hpa
    if pressure_difference_hpa <= 0:
        return None
    return 621.98 * vapor_pressure_hpa / pressure_difference_hpa

# Validate a channel's calibration switch, degree, and coefficients.
def validate_calibration_settings(calibration, label):
    if type(calibration["calibration_enabled"]) is not bool:
        raise ValueError(f"{label} calibration_enabled must be a boolean")

    polynomial = calibration["polynomial"]
    degree = polynomial["degree"]
    if type(degree) is not int or not 0 <= degree <= 4:
        raise ValueError(f"{label} polynomial degree must be an integer from 0 to 4")

    for power in range(5):
        coefficient = polynomial[f"coef_{power}"]
        if (isinstance(coefficient, bool) or not isinstance(coefficient, (int, float)) or
                not math.isfinite(coefficient)):
            raise ValueError(f"{label} coef_{power} must be a finite number")
    return degree


def adjusted_temperature(raw_temp):
    #raw_temp = bme280.get_temperature()
    temperature = CORRECTION_FACTORS["temperature"]
    comp_temp = raw_temp
    if temperature["calibration_enabled"]:
        coefficients = temperature["polynomial"]
        degree = validate_calibration_settings(temperature, "Temperature")
        comp_temp = coefficients[f"coef_{degree}"]
        for power in range(degree - 1, -1, -1):
            comp_temp = comp_temp * raw_temp + coefficients[f"coef_{power}"]
    return raw_temp, comp_temp

# Calculate adjusted humidity based on correction factors
def adjusted_humidity(raw_hum):
    #raw_hum = bme280.get_humidity()
    humidity = CORRECTION_FACTORS["humidity"]
    comp_hum = raw_hum
    if humidity["calibration_enabled"]:
        coefficients = humidity["polynomial"]
        degree = validate_calibration_settings(humidity, "Humidity")
        comp_hum = coefficients[f"coef_{degree}"]
        for power in range(degree - 1, -1, -1):
            comp_hum = comp_hum * raw_hum + coefficients[f"coef_{power}"]
    return raw_hum, float(min(100.0, comp_hum))

