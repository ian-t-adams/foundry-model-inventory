import csv
import io
from pathlib import Path
import tempfile
import unittest

from dashboard.store import Store
from test_dashboard_store import COLUMNS, report_row


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.store = Store(self.root / "inventory.sqlite3")

    def tearDown(self):
        self.store.close()
        self.workspace.cleanup()

    def ingest(self, rows):
        scan = self.store.start_scan("fixture")
        path = self.root / f"{scan}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        self.store.ingest_csvs(scan, [path])
        return scan

    def test_empty_and_shared_pool_contract(self):
        self.assertEqual(self.store.quota({})["rows"], [])
        self.ingest([report_row(Version="v1"), report_row(Version="v2"), report_row(Model="another")])
        result = self.store.quota({})
        self.assertEqual(result["total"], 1)
        pool = result["rows"][0]
        self.assertEqual((pool["quota_limit"], pool["allocated"], pool["remaining"]), (10, 3, 7))
        self.assertEqual(pool["sharing_choices"], 3)
        self.assertEqual(len(pool["choices"]), 3)
        self.assertEqual(pool["availability"], "available")
        self.assertEqual(result["summary"]["with_headroom"], 1)
        self.assertEqual(result["summary"]["versions"], 3)

    def test_pool_identity_is_subscription_region_and_name(self):
        self.ingest([
            report_row(), report_row(Region="westus"),
            report_row(SubscriptionId="sub-b"), report_row(QuotaName="other"),
        ])
        pools = self.store.quota({})["rows"]
        self.assertEqual(len(pools), 4)
        self.assertEqual(len({pool["key"] for pool in pools}), 4)
        self.assertTrue(all(pool["remaining"] == 7 for pool in pools))

    def test_model_filter_does_not_hide_shared_pool_or_conflicts(self):
        self.ingest([report_row(Model="a"), report_row(Model="b", Limit="20", Allocated="13")])
        result = self.store.quota({"model": "a"})
        pool = result["rows"][0]
        self.assertEqual(pool["sharing_choices"], 2)
        self.assertEqual([choice["model"] for choice in pool["choices"]], ["a"])
        self.assertEqual(pool["availability"], "unknown")
        self.assertIsNone(pool["remaining"])
        self.assertEqual(self.store.quota({"model": "a", "availability": "available"})["total"], 0)
        self.assertEqual(self.store.quota({"model": "a", "minimum": "1"})["total"], 0)

    def test_units_cannot_split_or_conceal_a_conflicting_pool(self):
        self.ingest([report_row(Model="a"), report_row(Model="b", Unit="PTU")])
        pool = self.store.quota({"unit": "1K TPM"})["rows"][0]
        self.assertEqual(pool["unit"], "Mixed")
        self.assertEqual(pool["availability"], "unknown")
        self.assertIsNone(pool["quota_limit"])

    def test_unmapped_entries_are_unknown_and_not_combined(self):
        self.ingest([report_row(QuotaName="", Version="v1"), report_row(QuotaName="", Version="v2")])
        result = self.store.quota({})
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["summary"]["quota_pools"], 0)
        self.assertEqual(result["summary"]["unknown_quota"], 2)
        self.assertTrue(all(row["remaining"] is None for row in result["rows"]))

    def test_known_zero_unknown_and_minimum_remain_distinct(self):
        self.ingest([
            report_row(QuotaName="free"), report_row(QuotaName="zero", Limit="0", Allocated="0", Remaining="0"),
            report_row(QuotaName="unknown", QuotaStatus="Unknown", Limit="", Allocated="", Remaining=""),
        ])
        summary = self.store.quota({})["summary"]
        self.assertEqual((summary["with_headroom"], summary["zero_quota"], summary["unknown_quota"]), (1, 1, 1))
        self.assertEqual(self.store.quota({"minimum": "7", "unit": "1K TPM"})["total"], 1)
        self.assertEqual(self.store.quota({"minimum": "8", "unit": "1K TPM"})["total"], 0)
        self.assertEqual(self.store.quota({"availability": ["unknown", "exhausted"]})["total"], 2)

    def test_lifecycle_review_and_units_are_not_a_capacity_ranking(self):
        self.ingest([
            report_row(QuotaName="retired", Lifecycle="Deprecated", Limit="100", Remaining="97"),
            report_row(QuotaName="tokens"),
            report_row(QuotaName="ptu", Unit="PTU", Limit="1000", Remaining="997"),
        ])
        result = self.store.quota({})
        self.assertEqual([row["quota_name"] for row in result["rows"]], ["tokens", "ptu", "retired"])
        self.assertEqual(result["rows"][-1]["current_choices"], 0)
        self.assertEqual(result["rows"][-1]["availability"], "available")

    def test_pool_filters_and_exact_versions_reuse_existing_predicates(self):
        self.ingest([
            report_row(Model="a", Version="v1"), report_row(Model="a", Version="v2"),
            report_row(Model="b", QuotaName="other", SKU="DataZoneStandard"),
        ])
        result = self.store.quota({"model_version": [["OpenAI", "a", "v2"]]})
        self.assertEqual(result["rows"][0]["sharing_choices"], 2)
        self.assertEqual(result["rows"][0]["choices"][0]["version"], "v2")
        self.assertEqual(self.store.inventory({"quota_name": "shared-gpt-pool"})["total"], 2)
        self.assertEqual(self.store.inventory({"sku": "DataZoneStandard"})["total"], 1)
        self.assertEqual(self.store.quota({"quota_name": "' OR 1=1 --"})["total"], 0)

    def test_export_is_unpaginated_deduplicated_and_formula_safe(self):
        self.ingest([
            report_row(Subscription="=unsafe", Version="v1"),
            report_row(Subscription="=unsafe", Version="v2"),
            report_row(QuotaName="other"),
        ])
        self.assertEqual(len(self.store.quota({"page_size": "1"})["rows"]), 1)
        rows = list(csv.DictReader(io.StringIO(self.store.export_quota_csv({"page_size": "1"}))))
        self.assertEqual(len(rows), 2)
        unsafe = next(row for row in rows if row["quota_name"] == "shared-gpt-pool")
        self.assertEqual(unsafe["remaining"], "7")
        self.assertEqual(unsafe["subscription"], "'=unsafe")
        self.assertIn("gpt-example v1", unsafe["matching_model_versions"])


if __name__ == "__main__":
    unittest.main()
