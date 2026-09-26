import json
import unittest
from types import SimpleNamespace
from unittest import mock

import myweather


class CollectReadingsTests(unittest.TestCase):
    def test_collects_available_weather_hat_metrics(self) -> None:
        source = SimpleNamespace(
            temperature=lambda: 12.3,
            humidity=45.6,
            pressure=lambda: 1007.8,
            wind_direction_cardinal="NW",
        )

        self.assertEqual(
            myweather.collect_readings(source),
            {
                "temperature": 12.3,
                "humidity": 45.6,
                "pressure": 1007.8,
                "wind_direction": "NW",
            },
        )


class AllskyFormattingTests(unittest.TestCase):
    def test_converts_readings_to_as_variables(self) -> None:
        variables = myweather.to_allsky_variables(
            {"temperature": 12.3, "wind direction": "NNE"}
        )

        self.assertEqual(
            variables,
            {"AS_TEMPERATURE": "12.3", "AS_WIND_DIRECTION": "NNE"},
        )

    def test_formats_shell_exports(self) -> None:
        exports = myweather.format_allsky_exports(
            {"wind_direction": "NNE", "temperature": 12.3}
        )

        self.assertEqual(
            exports,
            "export AS_TEMPERATURE=12.3\nexport AS_WIND_DIRECTION=NNE",
        )


class MqttPublishingTests(unittest.TestCase):
    def test_publishes_json_payload(self) -> None:
        client = mock.Mock()
        publish_result = mock.Mock()
        client.publish.return_value = publish_result

        myweather.publish_mqtt(
            {"temperature": 12.3},
            "mqtt.example.net",
            "allsky/weather",
            retain=True,
            client_factory=lambda: client,
        )

        client.connect.assert_called_once_with("mqtt.example.net", 1883)
        client.publish.assert_called_once_with(
            "allsky/weather",
            json.dumps({"temperature": 12.3}, sort_keys=True),
            retain=True,
        )
        publish_result.wait_for_publish.assert_called_once_with()
        client.loop_start.assert_called_once_with()
        client.loop_stop.assert_called_once_with()
        client.disconnect.assert_called_once_with()


class MainTests(unittest.TestCase):
    def test_main_prints_exports(self) -> None:
        with mock.patch.object(
            myweather,
            "read_pimoroni_weather_hat",
            return_value={"temperature": 9.8, "humidity": 82},
        ):
            with mock.patch("sys.stdout.write") as stdout:
                exit_code = myweather.main([])

        self.assertEqual(exit_code, 0)
        written = "".join(call.args[0] for call in stdout.mock_calls if call.args)
        self.assertIn("export AS_TEMPERATURE=9.8", written)
        self.assertIn("export AS_HUMIDITY=82", written)

    def test_main_requires_full_mqtt_configuration(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            myweather.main(["--mqtt-host", "mqtt.example.net"])

        self.assertEqual(exc.exception.code, 2)

    def test_main_rejects_mqtt_options_without_mqtt_mode(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            myweather.main(["--mqtt-retain"])

        self.assertEqual(exc.exception.code, 2)

    def test_main_rejects_mqtt_password_without_username(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            myweather.main(
                [
                    "--mqtt-host",
                    "mqtt.example.net",
                    "--mqtt-topic",
                    "allsky/weather",
                    "--mqtt-password",
                    "weather-pass",
                ]
            )

        self.assertEqual(exc.exception.code, 2)

    def test_main_suppresses_blank_output_when_no_readings_exist(self) -> None:
        with mock.patch.object(
            myweather,
            "read_pimoroni_weather_hat",
            return_value={},
        ), mock.patch("sys.stdout.write") as stdout:
            exit_code = myweather.main([])

        self.assertEqual(exit_code, 0)
        self.assertFalse(stdout.called)

    def test_main_passes_mqtt_retain_flag(self) -> None:
        with mock.patch.object(
            myweather,
            "read_pimoroni_weather_hat",
            return_value={"temperature": 9.8},
        ), mock.patch.object(myweather, "publish_mqtt") as publish_mqtt:
            with mock.patch("sys.stdout.write"):
                exit_code = myweather.main(
                    [
                        "--mqtt-host",
                        "mqtt.example.net",
                        "--mqtt-port",
                        "1884",
                        "--mqtt-topic",
                        "allsky/weather",
                        "--mqtt-username",
                        "weather-user",
                        "--mqtt-password",
                        "weather-pass",
                        "--mqtt-retain",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            publish_mqtt.call_args.args[:3],
            ({"temperature": 9.8}, "mqtt.example.net", "allsky/weather"),
        )
        self.assertEqual(publish_mqtt.call_args.kwargs["port"], 1884)
        self.assertEqual(publish_mqtt.call_args.kwargs["username"], "weather-user")
        self.assertEqual(publish_mqtt.call_args.kwargs["password"], "weather-pass")
        self.assertTrue(publish_mqtt.call_args.kwargs["retain"])


if __name__ == "__main__":
    unittest.main()
