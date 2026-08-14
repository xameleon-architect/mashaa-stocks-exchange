import json
import argparse
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync


class SyncTests(unittest.TestCase):
    def write_small_catalog(self, base: Path) -> None:
        data_dir = base / "data"
        data_dir.mkdir()
        (data_dir / "catalog.csv").write_text(
            "Tilda UID;External ID;Title;Quantity;Parent UID;Editions\n"
            "100;PRODUCT-1;Dress;;;\n"
            "101;EXT-1;Dress M;10;100;Размер:M\n",
            encoding="utf-8-sig",
        )

    def small_config(self):
        return {
            "moysklad_api_base": "https://example.invalid",
            "store_name": "Склад офиса",
            "tilda_catalog_csv": "data/catalog.csv",
            "state_file": "state.json",
            "log_dir": "logs",
            "output_dir": "output",
            "status_dir": "status",
            "request_timeout_seconds": 1,
            "retry_attempts": 1,
            "retry_backoff_seconds": 0,
            "page_size": 1000,
            "clamp_negative_to_zero": True,
            "allow_fractional_quantity": False,
            "minimum_match_ratio": 1.0,
            "trust_local_state": False,
            "commerce_catalog_id": "catalog",
            "commerce_catalog_name": "Catalog",
        }

    def test_run_once_dry_run_produces_human_and_machine_status(self):
        class FakeMoySklad:
            def __init__(self, *args, **kwargs):
                pass

            def resolve_store(self, name):
                return {"name": name}

            def stock_by_store(self, store):
                return {
                    "EXT-1": sync.SourceStock(
                        "EXT-1", "Dress M", "variant",
                        Decimal("7"), Decimal("0"), Decimal("0"), Decimal("7"),
                    )
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            self.write_small_catalog(base)
            args = argparse.Namespace(apply=False, confirm="", full=False)
            with mock.patch.object(sync, "require_env", return_value="test-token"), mock.patch.object(sync, "MoySkladClient", FakeMoySklad):
                result = sync.run_once(base, self.small_config(), args)

            latest = json.loads((base / "status" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual("DRY_RUN_READY", result["status"])
            self.assertEqual("DRY_RUN_READY", latest["status"])
            self.assertEqual(1, latest["changes"])
            self.assertFalse(latest["submitted"])
            self.assertTrue(Path(latest["audit_file"]).exists())

    def test_tilda_snapshot_counts_and_ids(self):
        base = Path(__file__).resolve().parents[1]
        items = sync.read_tilda_catalog(base / "data" / "tilda_catalog.csv")
        self.assertEqual(208, len(items))
        self.assertEqual(208, len({item.tilda_uid for item in items}))
        self.assertEqual(208, len({item.external_id for item in items}))
        self.assertEqual(165, sum(item.current_quantity is not None for item in items))

    def test_change_formula_and_negative_clamp(self):
        items = [
            sync.TildaItem("1", "A", "Variant A", Decimal("10"), "P", "Размер:M"),
            sync.TildaItem("2", "B", "Variant B", Decimal("10"), "P", "Размер:L"),
        ]
        source = {
            "A": sync.SourceStock("A", "A", "variant", Decimal("10"), Decimal("3"), Decimal("2"), Decimal("9")),
            "B": sync.SourceStock("B", "B", "variant", Decimal("0"), Decimal("2"), Decimal("0"), Decimal("-2")),
        }
        changes, summary = sync.calculate_changes(
            items,
            source,
            {"A": Decimal("10"), "B": Decimal("10")},
            clamp_negative=True,
            allow_fractional=False,
            full=False,
            minimum_match_ratio=1.0,
        )
        self.assertEqual(2, summary["changes"])
        self.assertEqual([Decimal("9"), Decimal("0")], [change.new_quantity for change in changes])

    def test_low_match_ratio_blocks_write_plan(self):
        items = [
            sync.TildaItem("1", "A", "A", Decimal("10"), "", ""),
            sync.TildaItem("2", "B", "B", Decimal("10"), "", ""),
        ]
        source = {
            "A": sync.SourceStock("A", "A", "product", Decimal("10"), Decimal("0"), Decimal("0"), Decimal("10"))
        }
        with self.assertRaises(sync.SyncError):
            sync.calculate_changes(
                items,
                source,
                {},
                clamp_negative=True,
                allow_fractional=False,
                full=False,
                minimum_match_ratio=0.98,
            )

    def test_offers_xml_links_variant_to_parent_and_contains_quantity(self):
        items = [
            sync.TildaItem("100", "PRODUCT-1", "Dress", None, "", ""),
            sync.TildaItem("101", "EXT-1", "Dress M", Decimal("10"), "100", "Размер:M"),
        ]
        changes = [
            sync.Change("EXT-1", "101", "Dress M", "variant", Decimal("10"), Decimal("3"), Decimal("0"), Decimal("0"), Decimal("3"))
        ]
        xml = sync.build_offers_xml(changes, items, "catalog", "Catalog").decode("utf-8")
        self.assertIn("<Ид>PRODUCT-1#EXT-1</Ид>", xml)
        self.assertIn("<ХарактеристикиТовара>", xml)
        self.assertIn("<Наименование>Размер</Наименование>", xml)
        self.assertIn("<Значение>M</Значение>", xml)
        self.assertIn("<Количество>3</Количество>", xml)
        self.assertNotIn("101", xml)

    def test_import_xml_contains_changed_parent_product(self):
        items = [
            sync.TildaItem("100", "PRODUCT-1", "Dress", None, "", ""),
            sync.TildaItem("101", "EXT-1", "Dress M", Decimal("10"), "100", "Размер:M"),
        ]
        changes = [
            sync.Change("EXT-1", "101", "Dress M", "variant", Decimal("10"), Decimal("3"), Decimal("0"), Decimal("0"), Decimal("3"))
        ]
        xml = sync.build_import_xml(items, changes, "catalog", "Catalog").decode("utf-8")
        self.assertIn('<Каталог СодержитТолькоИзменения="true">', xml)
        self.assertIn("<Ид>catalog</Ид>", xml)
        self.assertIn("<Товар>", xml)
        self.assertIn("<Ид>PRODUCT-1</Ид>", xml)
        self.assertIn("<Наименование>Dress</Наименование>", xml)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            sync.write_state_atomic(path, {"A": Decimal("3"), "B": Decimal("10")})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("3", payload["quantities"]["A"])
            self.assertEqual("10", payload["quantities"]["B"])

    def test_moysklad_store_stock_mapping(self):
        store_href = "https://api.moysklad.ru/api/remap/1.2/entity/store/store-1"
        product_href = "https://api.moysklad.ru/api/remap/1.2/entity/variant/variant-1"

        class FakeHttp:
            def get(self, url):
                if "/entity/assortment?" in url:
                    return {
                        "meta": {"size": 1},
                        "rows": [{
                            "meta": {"href": product_href, "type": "variant"},
                            "externalCode": "EXT-1",
                            "name": "Dress M",
                        }],
                    }
                if "/report/stock/bystore?" in url:
                    return {
                        "meta": {"size": 1},
                        "rows": [{
                            "meta": {"href": product_href, "type": "variant"},
                            "stockByStore": [{
                                "meta": {"href": store_href, "type": "store"},
                                "stock": 10,
                                "reserve": 3,
                                "inTransit": 2,
                            }],
                        }],
                    }
                raise AssertionError(url)

        client = sync.MoySkladClient("https://api.moysklad.ru/api/remap/1.2", FakeHttp(), 1000)
        result = client.stock_by_store({"name": "Склад офиса", "meta": {"href": store_href}})
        self.assertEqual(Decimal("9"), result["EXT-1"].available)
        self.assertEqual("variant", result["EXT-1"].entity_type)

    def test_moysklad_mapping_ignores_url_query_and_host(self):
        assortment_href = "https://api.moysklad.ru/api/remap/1.2/entity/variant/variant-1"
        report_href = "https://proxy.example/api/remap/1.2/entity/variant/variant-1?expand=product"
        store_href = "https://api.moysklad.ru/api/remap/1.2/entity/store/store-1"

        class FakeHttp:
            def get(self, url):
                if "/entity/assortment?" in url:
                    return {
                        "meta": {"size": 1},
                        "rows": [{
                            "meta": {"href": assortment_href, "type": "variant"},
                            "externalCode": "EXT-QUERY",
                            "name": "Dress L",
                        }],
                    }
                if "/report/stock/bystore?" in url:
                    return {
                        "meta": {"size": 1},
                        "rows": [{
                            "meta": {"href": report_href, "type": "variant"},
                            "stockByStore": [{
                                "meta": {"href": store_href, "type": "store"},
                                "stock": 10,
                                "reserve": 0,
                                "inTransit": 0,
                            }],
                        }],
                    }
                raise AssertionError(url)

        client = sync.MoySkladClient("https://api.moysklad.ru/api/remap/1.2", FakeHttp(), 1000)
        result = client.stock_by_store({"name": "Склад офиса", "meta": {"href": store_href}})
        self.assertEqual(Decimal("10"), result["EXT-QUERY"].available)

    def test_entity_key_falls_back_to_href_type(self):
        meta = {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/abc?expand=supplier"}
        self.assertEqual("product:abc", sync.entity_key(meta))

    def test_entity_key_prefers_entity_path_over_report_meta_type(self):
        meta = {
            "href": "https://api.moysklad.ru/api/remap/1.2/entity/variant/abc?expand=product",
            "type": "stockbystore",
        }
        self.assertEqual("variant:abc", sync.entity_key(meta))

    def test_commerceml_progress_is_polled_until_success(self):
        class FakeCommerceML(sync.CommerceMLClient):
            def __init__(self):
                self.headers = {}
                self.responses = iter([
                    "success\nsession\nabc",
                    "zip=no\nfile_limit=1000000",
                    "success",
                    "success",
                    "progress\n50",
                    "success",
                    "success",
                ])
                self.calls = []

            def _request(self, mode, *, method="GET", data=None, filename=None):
                self.calls.append((mode, method, filename, data))
                return next(self.responses)

        client = FakeCommerceML()
        transcript = client.upload_offers(b"<import/>", b"<xml/>")
        self.assertEqual(
            [
                "checkauth",
                "init",
                "file-import",
                "file-offers",
                "import-import0_1.xml-1",
                "import-import0_1.xml-2",
                "import-offers0_1.xml-1",
            ],
            [row[0] for row in transcript],
        )
        self.assertEqual(3, sum(call[0] == "import" for call in client.calls))
        file_calls = [call for call in client.calls if call[0] == "file"]
        self.assertEqual(["import0_1.xml", "offers0_1.xml"], [call[2] for call in file_calls])
        self.assertEqual(b"<import/>", file_calls[0][3])
        self.assertEqual(b"<xml/>", file_calls[1][3])
        self.assertEqual("session=abc", client.headers["Cookie"])

    def test_checkauth_cookie_is_required(self):
        class MinimalCommerceML(sync.CommerceMLClient):
            def __init__(self):
                self.headers = {}

        client = MinimalCommerceML()
        with self.assertRaises(sync.SyncError):
            client._apply_checkauth_cookie("success")

    def test_checkauth_cookie_is_not_logged_in_status(self):
        class MinimalCommerceML(sync.CommerceMLClient):
            def __init__(self):
                self.headers = {}

        client = MinimalCommerceML()
        client._apply_checkauth_cookie("success\nPHPSESSID\nsecret-session-value")
        self.assertEqual("PHPSESSID=secret-session-value", client.headers["Cookie"])

    def test_checkauth_cookie_is_redacted_from_transcript(self):
        response = "success\nPHPSESSID\nsecret-session-value"
        self.assertEqual("success", sync.safe_transcript_response("checkauth", response))

    def test_existing_summary_cookie_is_sanitized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary-old.json"
            path.write_text(json.dumps({
                "mode": "apply",
                "applied": True,
                "commerce_transcript": [{
                    "operation": "checkauth",
                    "response": "success\nPHPSESSID\nsecret-session-value",
                }]
            }), encoding="utf-8")
            self.assertEqual(1, sync.sanitize_summary_files(path.parent))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("success", payload["commerce_transcript"][0]["response"])
            self.assertFalse(payload["applied"])
            self.assertTrue(payload["submitted"])
            self.assertFalse(payload["verified"])


if __name__ == "__main__":
    unittest.main()
