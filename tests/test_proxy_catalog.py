"""The app-proxy catalog keeps public food fields and rejects private data."""

import unittest

from rocketnow_cli.proxy_catalog import catalog_from_flows


def flow(identifier, endpoint, timestamp, *, host="csg.rocketnow.co.jp"):
    return {
        "id": identifier,
        "request": {
            "host": host, "method": "GET", "path": endpoint,
            "timestamp_start": timestamp,
            "headers": [["Authorization", "private-token"]],
        },
        "response": {"status_code": 200},
    }


class ProxyCatalogTests(unittest.TestCase):
    def test_recent_category_store_and_menu_are_whitelisted(self):
        now = 1_800_000_000
        captured = [
            flow("category-list", "/endpoint/store.get_clp_categories?categoryId=0", now - 60),
            flow("old-category", "/endpoint/store.get_clp?categoryId=8", now - 50),
            flow("new-category", "/endpoint/store.get_clp?categoryId=8", now - 10),
            flow("store-menu", "/endpoint/store.get_store_with_menu?storeId=7479", now - 8),
            flow("untrusted-host", "/endpoint/store.get_clp?categoryId=8", now, host="example.test"),
        ]
        responses = {
            "category-list": {"data": {"list": [
                {"id": 8, "name": "お弁当", "privateAddress": "omit"},
                {"id": 2, "name": "洋食"},
            ]}},
            "old-category": {"data": {"entityList": []}},
            "new-category": {"data": {"entityList": [
                {"viewType": "storeCardWithMenu", "entity": {"data": {
                    "id": 7479, "name": "おばんざいや早乙女",
                    "estimatedDeliveryTime": "23分", "openStatus": "OPEN",
                    "reviewRating": 4.2, "address": "private", "latitude": 35.0,
                    "authorization": "private-token",
                }}},
                {"viewType": "banner", "entity": {"data": {"id": 2, "name": "not-a-store"}}},
            ]}},
            "store-menu": {"data": {
                "id": 7479, "name": "おばんざいや早乙女", "address": "private",
                "menus": [{"dishes": [
                    {"id": 10, "name": "のり弁", "salePrice": 800.0,
                     "displayStatus": "ON_SALE", "userPromotionId": "private"},
                    {"id": 11, "name": "売り切れ", "salePrice": 900,
                     "displayStatus": "SOLD_OUT"},
                ]}],
            }},
        }
        result = catalog_from_flows(captured, responses.__getitem__, now=now)
        self.assertEqual(result["source"], "app_proxy_live")
        self.assertEqual(result["categories"], [{"id": 8, "name": "お弁当"}, {"id": 2, "name": "洋食"}])
        self.assertEqual(result["categoryStores"]["8"]["stores"], [
            {"id": 7479, "name": "おばんざいや早乙女", "openStatus": "OPEN",
             "estimatedDeliveryTime": "23分", "reviewRating": 4.2}
        ])
        self.assertEqual(result["menuByStore"]["7479"]["dishes"][0],
                         {"id": 10, "name": "のり弁", "price": 800, "available": True})
        self.assertNotIn("private", str(result))
        self.assertNotIn("untrusted-host", str(result))

    def test_old_capture_is_not_misrepresented_as_current(self):
        now = 1_800_000_000
        with self.assertRaisesRegex(RuntimeError, "No recent app category"):
            catalog_from_flows(
                [flow("old", "/endpoint/store.get_clp?categoryId=8", now - 90000)],
                lambda _: {"data": {}}, now=now,
            )


if __name__ == "__main__":
    unittest.main()
