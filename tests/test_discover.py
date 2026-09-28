"""Read-only catalog scanning using fake API responses."""

import unittest
from unittest.mock import patch

from rocketnow_cli.api import RocketNowAPIError
from rocketnow_cli.discover import scan_catalog


def card(store_id, name=None, **fields):
    return {"viewType": "storeCardWithMenu", "entity": {"data": {
        "id": store_id, "name": name or f"Store {store_id}", **fields,
    }}}


class FakeAPI:
    def __init__(self, categories, menus):
        self.categories = categories
        self.menus = menus
        self.calls = []

    def category_stores(self, category_id):
        self.calls.append(("category", category_id))
        value = self.categories[category_id]
        if isinstance(value, Exception):
            raise value
        return {"entityList": value}

    def store_with_menu(self, store_id, latitude, longitude, source_type="SEARCH"):
        self.calls.append(("store", store_id, latitude, longitude, source_type))
        value = self.menus[int(store_id)]
        if isinstance(value, Exception):
            raise value
        return {"menus": [{"dishes": value}]}


class CatalogScanTests(unittest.TestCase):
    def test_duplicate_dish_in_multiple_menu_groups_is_returned_once(self):
        dish = {"id": 5, "name": "Dinner", "salePrice": 800,
                "displayStatus": "ON_SALE"}
        api = FakeAPI({1: [card(10)]}, {10: [dish, dict(dish)]})
        with patch("rocketnow_cli.discover.time.sleep"):
            result = scan_catalog(api, 35.6, 139.7, [1])
        self.assertEqual([item["id"] for item in result["candidateProducts"]], [5])

    def test_round_robin_dedupe_all_available_dishes_and_no_private_fields(self):
        api = FakeAPI(
            {1: [card(10, "A", estimatedDeliveryTime="20分", privateToken="secret"),
                 card(11, "B")],
             2: [card(10, "A"), card(12, "C")],
             3: [card(13, "D")]},
            {10: [{"id": 1, "name": "Rice", "salePrice": 800.0,
                   "displayStatus": "ON_SALE", "privateToken": "secret", "hasOptions": True},
                  {"id": 2, "name": "Side", "salePrice": 300,
                   "displayStatus": "ON_SALE"},
                  {"id": 3, "name": "Sold", "salePrice": 100,
                   "displayStatus": "SOLD_OUT"},
                  {"id": 4, "name": "Over", "salePrice": 1001,
                   "displayStatus": "ON_SALE"}],
             11: [], 12: [], 13: []},
        )
        with patch("rocketnow_cli.discover.time.sleep"):
            result = scan_catalog(api, 35.6, 139.7, [1, 2, 3], limit_stores=3)
        self.assertEqual([call[1] for call in api.calls if call[0] == "store"], ["10", "13", "11"])
        self.assertEqual(result["storesScanned"], 3)
        self.assertEqual(result["storesDiscovered"], 3)
        self.assertEqual([row["name"] for row in result["candidateProducts"]], ["Side", "Rice"])
        self.assertEqual(result["candidateProducts"][0]["categoryIds"], [1, 2])
        self.assertTrue(result["candidateProducts"][1]["hasOptions"])
        self.assertNotIn("secret", repr(result))
        self.assertTrue(all(call[-1] == "SEARCH" for call in api.calls if call[0] == "store"))

    def test_store_limit_hard_caps_at_one_hundred(self):
        api = FakeAPI({1: [card(i) for i in range(150)]}, {i: [] for i in range(150)})
        with patch("rocketnow_cli.discover.time.sleep"):
            result = scan_catalog(api, 35.6, 139.7, [1], limit_stores=500)
        self.assertEqual(result["storesScanned"], 100)
        self.assertEqual(len([call for call in api.calls if call[0] == "store"]), 100)

    def test_non_auth_failure_is_summarized_without_message(self):
        api = FakeAPI({1: [card(10)], 2: ValueError("private URL")},
                      {10: RuntimeError("private token")})
        with patch("rocketnow_cli.discover.time.sleep"):
            result = scan_catalog(api, 35.6, 139.7, [1, 2])
        self.assertEqual(result["storesScanned"], 0)
        self.assertEqual(result["failures"], [
            {"stage": "category", "categoryId": 2, "errorType": "ValueError"},
            {"stage": "store", "storeId": 10, "errorType": "RuntimeError"},
        ])
        self.assertNotIn("private", repr(result))

    def test_auth_failure_stops_scan(self):
        api = FakeAPI({1: [card(10), card(11)]},
                      {10: RocketNowAPIError({"code": "UNAUTHORIZED", "message": "token expired"}), 11: []})
        with self.assertRaises(RocketNowAPIError), patch("rocketnow_cli.discover.time.sleep"):
            scan_catalog(api, 35.6, 139.7, [1])
        self.assertEqual([call[1] for call in api.calls if call[0] == "store"], ["10"])

    def test_invalid_inputs_make_no_requests(self):
        api = FakeAPI({}, {})
        with self.assertRaises(ValueError):
            scan_catalog(api, 91, 139.7, [1])
        with self.assertRaises(ValueError):
            scan_catalog(api, 35.6, 139.7, [1], max_price=-1)
        with self.assertRaises(ValueError):
            scan_catalog(api, 35.6, 139.7, [1], delay_seconds=-1)
        self.assertEqual(api.calls, [])

    def test_every_api_request_is_paced(self):
        api = FakeAPI({1: [card(10)], 2: [card(11)]}, {10: [], 11: []})
        clock = [0.0]
        starts = []
        original_category = api.category_stores
        original_store = api.store_with_menu

        def category(*args):
            starts.append(clock[0])
            return original_category(*args)

        def store(*args, **kwargs):
            starts.append(clock[0])
            return original_store(*args, **kwargs)

        def sleep(seconds):
            clock[0] += seconds

        api.category_stores = category
        api.store_with_menu = store
        with patch("rocketnow_cli.discover.time.monotonic", side_effect=lambda: clock[0]), patch(
            "rocketnow_cli.discover.time.sleep", side_effect=sleep
        ):
            scan_catalog(api, 35.6, 139.7, [1, 2], delay_seconds=0.3)
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(round(b - a, 6) >= 0.3 for a, b in zip(starts, starts[1:])))


if __name__ == "__main__":
    unittest.main()
