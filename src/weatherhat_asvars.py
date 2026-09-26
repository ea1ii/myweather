from weatherhat import WeatherHAT
import time
import os

hat = WeatherHAT()
base = "/home/pi/allsky/variables"

def write(name, value):
    path = f"{base}/AS_{name}.txt"
    with open(path, "w") as f:
        f.write(str(value))

while True:
    write("TEMPERATURE", round(hat.temperature, 2))
    write("HUMIDITY", round(hat.humidity, 1))
    write("PRESSURE", round(hat.pressure, 1))
    write("LIGHT", round(hat.light, 1))
    write("WIND", round(hat.wind_speed, 2))
    write("DIR", round(hat.wind_direction, 1))
    write("RAIN", round(hat.rain, 3))
    time.sleep(10)
