#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from typing import Any, Callable

METRIC_SOURCES: dict[str, tuple[str, ...]] = {
    "temperature": ("temperature", "get_temperature"),
    "humidity": ("humidity", "get_humidity"),
    "pressure": ("pressure", "get_pressure"),
    "lux": ("lux", "light", "get_lux"),
    "wind_speed": ("wind_speed", "get_wind_speed"),
    "wind_direction": (
        "wind_direction",
        "wind_direction_cardinal",
        "get_wind_direction",
    ),
    "rainfall": ("rainfall", "rain", "get_rainfall"),
}


def _resolve_value(source: Any, name: str) -> Any:
    if not hasattr(source, name):
        return None
    value = getattr(source, name)
    return value() if callable(value) else value


def collect_readings(source: Any) -> dict[str, Any]:
    readings: dict[str, Any] = {}
    for metric, candidates in METRIC_SOURCES.items():
        for candidate in candidates:
            value = _resolve_value(source, candidate)
            if value is not None:
                readings[metric] = value
                break
    return readings


def read_pimoroni_weather_hat(weatherhat_module: Any | None = None) -> dict[str, Any]:
    if weatherhat_module is None:
        try:
            import weatherhat as weatherhat_module
        except ImportError as exc:
            raise RuntimeError(
                "The weatherhat module is required to read Pimoroni HAT data."
            ) from exc
    return collect_readings(weatherhat_module)


def to_allsky_variables(readings: dict[str, Any]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for key, value in readings.items():
        if value is None:
            continue
        normalized = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
        variables[f"AS_{normalized}"] = str(value)
    return variables


def format_allsky_exports(readings: dict[str, Any]) -> str:
    variables = to_allsky_variables(readings)
    return "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in sorted(variables.items())
    )


def publish_mqtt(
    readings: dict[str, Any],
    host: str,
    topic: str,
    *,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    retain: bool = False,
    client_factory: Callable[[], Any] | None = None,
) -> None:
    if client_factory is None:
        try:
            from paho.mqtt.client import Client
        except ImportError as exc:
            raise RuntimeError(
                "MQTT publishing requires paho-mqtt to be installed."
            ) from exc
        client_factory = Client

    client = client_factory()
    if username is not None:
        client.username_pw_set(username, password)
    client.connect(host, port)
    client.publish(topic, json.dumps(readings, sort_keys=True), retain=retain)
    client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read Pimoroni Weather HAT data and expose Allsky AS_ variables."
    )
    parser.add_argument("--mqtt-host", help="Optional MQTT broker hostname.")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port.")
    parser.add_argument("--mqtt-topic", help="MQTT topic for JSON weather payloads.")
    parser.add_argument("--mqtt-username", help="MQTT username.")
    parser.add_argument("--mqtt-password", help="MQTT password.")
    parser.add_argument(
        "--mqtt-retain",
        action="store_true",
        help="Publish retained MQTT messages.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if bool(args.mqtt_host) != bool(args.mqtt_topic):
        parser.error("--mqtt-host and --mqtt-topic must be provided together.")

    try:
        readings = read_pimoroni_weather_hat()
        print(format_allsky_exports(readings))
        if args.mqtt_host and args.mqtt_topic:
            publish_mqtt(
                readings,
                args.mqtt_host,
                args.mqtt_topic,
                port=args.mqtt_port,
                username=args.mqtt_username,
                password=args.mqtt_password,
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
