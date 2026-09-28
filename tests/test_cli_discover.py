"""The bulk menu command uses the CLI account and never exposes the saved address."""

import sys
import types
import unittest
from unittest.mock import patch

from rocketnow_cli.cli import _parser, run


class DiscoverCLITests(unittest.TestCase):
    def test_defaults_and_authenticated_dispatch(self):
        calls = []
        api = types.SimpleNamespace(default_address=lambda: {
            "latitude": 35.66, "longitude": 139.70, "addressName": "private address",
        })
        module = types.ModuleType("rocketnow_cli.discover")

        def scan_catalog(*args, **kwargs):
            calls.append((args, kwargs))
            return {"source": "api", "dishes": []}

        module.scan_catalog = scan_catalog
        with patch.dict(sys.modules, {"rocketnow_cli.discover": module}), patch(
            "rocketnow_cli.cli.load_session", return_value=object()
        ), patch("rocketnow_cli.cli.HTTPTransport", return_value=object()), patch(
            "rocketnow_cli.cli.RocketNowAPI", return_value=api
        ):
            result = run(_parser().parse_args(["discover-dishes"]))
        self.assertEqual(result, {"source": "api", "dishes": []})
        self.assertEqual(calls[0][0], (api, 35.66, 139.70, [1, 2, 3, 6, 8, 9, 11, 17, 21, 22]))
        self.assertEqual(calls[0][1], {"max_price": 1000, "limit_stores": 100, "delay_seconds": 0.3})
        self.assertNotIn("private address", str(result))

    def test_overrides_and_deduplicates_categories(self):
        calls = []
        api = types.SimpleNamespace(default_address=lambda: {"latitude": 35.66, "longitude": 139.70})
        module = types.ModuleType("rocketnow_cli.discover")
        module.scan_catalog = lambda *args, **kwargs: calls.append((args, kwargs)) or {}
        with patch.dict(sys.modules, {"rocketnow_cli.discover": module}), patch(
            "rocketnow_cli.cli.load_session", return_value=object()
        ), patch("rocketnow_cli.cli.HTTPTransport", return_value=object()), patch(
            "rocketnow_cli.cli.RocketNowAPI", return_value=api
        ):
            run(_parser().parse_args([
                "discover-dishes", "--max-price", "850", "--limit-stores", "12",
                "--categories", "2, 3,2", "--delay", "0.5",
            ]))
        self.assertEqual(calls[0][0][3], [2, 3])
        self.assertEqual(calls[0][1], {"max_price": 850, "limit_stores": 12, "delay_seconds": 0.5})

    def test_invalid_arguments_do_not_load_session(self):
        cases = [
            (["--max-price", "0"], "max-price"),
            (["--limit-stores", "101"], "limit-stores"),
            (["--limit-stores", "0"], "limit-stores"),
            (["--categories", "1,x"], "categories"),
            (["--categories", "1,0"], "categories"),
            (["--delay", "0.1"], "delay"),
            (["--delay", "nan"], "delay"),
        ]
        with patch("rocketnow_cli.cli.load_session", side_effect=AssertionError("session read")):
            for options, expected in cases:
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, expected):
                    run(_parser().parse_args(["discover-dishes", *options]))

    def test_invalid_saved_coordinates_are_rejected(self):
        api = types.SimpleNamespace(default_address=lambda: {"latitude": 999, "longitude": 139.70})
        with patch("rocketnow_cli.cli.load_session", return_value=object()), patch(
            "rocketnow_cli.cli.HTTPTransport", return_value=object()
        ), patch("rocketnow_cli.cli.RocketNowAPI", return_value=api):
            with self.assertRaisesRegex(ValueError, "valid coordinates"):
                run(_parser().parse_args(["discover-dishes"]))


if __name__ == "__main__":
    unittest.main()
