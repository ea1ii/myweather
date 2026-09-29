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

# Calculate adjusted temperature based on correction factors
def validate_temperature_degree(temperature):
    degree = temperature["polynomial"]["degree"]
    if type(degree) is not int or not 2 <= degree <= 6:
        raise ValueError("Temperature polynomial degree must be an integer from 2 to 6")
    return degree


def adjusted_temperature(raw_temp):
    #raw_temp = bme280.get_temperature()
    temperature = CORRECTION_FACTORS["temperature"]
    method = temperature["adjustment_method"]
    if method == "polynomial":
        coefficients = temperature["polynomial"]
        degree = validate_temperature_degree(temperature)
        comp_temp = coefficients[f"coef_{degree}"]
        for power in range(degree - 1, -1, -1):
            comp_temp = comp_temp * raw_temp + coefficients[f"coef_{power}"]
    elif method == "linear":
        coefficients = temperature["linear"]
        comp_temp = coefficients["slope"] * raw_temp + coefficients["intercept"]
    else:
        raise ValueError(f"Unsupported temperature adjustment method: {method}")
    return raw_temp, comp_temp

# Calculate adjusted humidity based on correction factors
def adjusted_humidity(raw_hum):
    #raw_hum = bme280.get_humidity()
    humidity = CORRECTION_FACTORS["humidity"]
    method = humidity["adjustment_method"]
    if method == "polynomial":
        coefficients = humidity["polynomial"]
        degree = validate_humidity_degree(humidity)
        comp_hum = coefficients[f"coef_{degree}"]
        for power in range(degree - 1, -1, -1):
            comp_hum = comp_hum * raw_hum + coefficients[f"coef_{power}"]
    elif method == "linear":
        coefficients = humidity["linear"]
        comp_hum = coefficients["slope"] * raw_hum + coefficients["intercept"]
    else:
        raise ValueError(f"Unsupported humidity adjustment method: {method}")
    return raw_hum, min(100, comp_hum)


def validate_humidity_degree(humidity):
    degree = humidity["polynomial"]["degree"]
    if type(degree) is not int or not 2 <= degree <= 6:
        raise ValueError("Humidity polynomial degree must be an integer from 2 to 6")
    return degree

