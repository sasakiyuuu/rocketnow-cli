"""Category CLI output is small and does not expose captured private fields."""

import sys
import types
import unittest
from unittest.mock import patch

from rocketnow_cli.cli import _parser, run


class CategoryCLITests(unittest.TestCase):
    def test_app_session_works_without_loading_cli_session(self):
        catalog = {
            "source": "app_proxy_live",
            "capturedAt": "2026-09-28T12:00:00Z",
            "categories": [{"id": 5, "name": "洋食", "secret": "omit"}],
            "categoryStores": {
                "5": {
                    "name": "洋食",
                    "capturedAt": "2026-09-28T12:01:00Z",
                    "stores": [{"id": 9, "name": "定食屋", "reviewRating": 4.5, "latitude": 35.6, "address": "omit"}],
                }
            },
            "menuByStore": {
                "9": {"id": 9, "name": "定食屋", "address": "omit", "dishes": [
                    {"id": 91, "name": "カレー", "price": 880, "available": True, "latitude": 35.6}
                ]}
            },
        }
        module = types.ModuleType("rocketnow_cli.proxy_catalog")
        module.load_app_catalog = lambda: catalog
        with patch.dict(sys.modules, {"rocketnow_cli.proxy_catalog": module}), patch(
            "rocketnow_cli.cli.load_session", side_effect=AssertionError("session read")
        ):
            categories = run(_parser().parse_args(["categories", "--app-session"]))
            stores = run(_parser().parse_args(["category", "5", "--app-session"]))
            menu = run(_parser().parse_args(["store", "9", "--app-session"]))
        self.assertEqual(categories["source"], "app_proxy_live")
        self.assertEqual(categories["categories"], [{"id": 5, "name": "洋食"}])
        self.assertEqual(stores["stores"], [{"id": 9, "name": "定食屋", "reviewRating": 4.5}])
        self.assertNotIn("address", str(stores))
        self.assertEqual(menu["dishes"], [{"id": 91, "name": "カレー", "price": 880, "available": True}])
        self.assertNotIn("latitude", str(menu))
        self.assertEqual(menu["source"], "app_proxy_live")

    def test_store_app_session_requires_captured_menu(self):
        module = types.ModuleType("rocketnow_cli.proxy_catalog")
        module.load_app_catalog = lambda: {"menuByStore": {}}
        with patch.dict(sys.modules, {"rocketnow_cli.proxy_catalog": module}), patch(
            "rocketnow_cli.cli.load_session", side_effect=AssertionError("session read")
        ):
            with self.assertRaisesRegex(ValueError, "open this store"):
                run(_parser().parse_args(["store", "9", "--app-session"]))

    def test_live_store_requires_coordinates(self):
        with patch("rocketnow_cli.cli.load_session", side_effect=AssertionError("session read")):
            with self.assertRaisesRegex(ValueError, "--lat and --lon"):
                run(_parser().parse_args(["store", "9"]))

    def test_missing_app_category_is_clear(self):
        module = types.ModuleType("rocketnow_cli.proxy_catalog")
        module.load_app_catalog = lambda: {"categories": [], "categoryStores": {}}
        with patch.dict(sys.modules, {"rocketnow_cli.proxy_catalog": module}):
            with self.assertRaisesRegex(ValueError, "not been captured"):
                run(_parser().parse_args(["category", "5", "--app-session"]))

    def test_api_mode_maps_and_filters_store_cards(self):
        api = types.SimpleNamespace(
            category_list=lambda: {"title": "カテゴリ", "list": [{"id": 5, "name": "洋食", "address": "omit"}]},
            category_stores=lambda value: {
                "entityList": [
                    {"viewType": "storeCardWithMenu", "entity": {"data": {"id": 9, "name": "定食屋", "openStatus": "OPEN", "latitude": 35.6}}},
                    {"viewType": "banner", "entity": {"data": {"id": 10, "name": "除外"}}},
                ],
                "nextToken": "opaque",
            },
        )
        with patch("rocketnow_cli.cli.load_session", return_value=object()), patch(
            "rocketnow_cli.cli.HTTPTransport", return_value=object()
        ), patch("rocketnow_cli.cli.RocketNowAPI", return_value=api):
            categories = run(_parser().parse_args(["categories"]))
            stores = run(_parser().parse_args(["category", "5"]))
        self.assertEqual(categories["categories"], [{"id": 5, "name": "洋食"}])
        self.assertEqual(stores["stores"], [{"id": 9, "name": "定食屋", "openStatus": "OPEN"}])
        self.assertTrue(stores["hasMore"])
        self.assertNotIn("opaque", str(stores))


if __name__ == "__main__":
    unittest.main()
