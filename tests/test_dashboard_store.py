"""Offline snapshot-store tests; scratch data stays inside this checkout."""

import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dashboard.store import MAX_FILTER_FIELD, MAX_FILTER_ITEMS, ROW_FIELDS, Store


COLUMNS = [
    "Subscription", "SubscriptionId", "TenantId", "ScanStartedUtc", "Model",
    "Version", "Region", "Type", "SKU", "Catalog", "Lifecycle", "Limit",
    "Allocated", "Remaining", "Unit", "QuotaStatus", "QuotaName",
    "QuotaDescription", "Kind", "Format", "InferenceDeprecation", "SkuDeprecation",
    "Notes", "RetailPrices",
]


def report_row(**updates):
    result = dict.fromkeys(COLUMNS, "")
    result.update(
        Subscription="Development", SubscriptionId="sub-a", TenantId="tenant-a",
        Model="gpt-example", Version="2026-01-01", Region="eastus",
        Type="Global", SKU="GlobalStandard", Catalog="Listed",
        Lifecycle="GenerallyAvailable", Limit="10", Allocated="3", Remaining="7",
        Unit="1K TPM", QuotaStatus="Reported", QuotaName="shared-gpt-pool",
        Kind="OpenAI", Format="OpenAI",
    )
    result.update(updates)
    return result


def status_row(catalog, **updates):
    return report_row(
        **dict(Model="", Version="", SKU="", Type="", Catalog=catalog,
               Limit="", Allocated="", Remaining="", Unit="", QuotaStatus="",
               QuotaName="", Kind="", Format="", **updates)
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(
            prefix=".dashboard-store-test-", dir=Path(__file__).resolve().parent
        )
        self.root = Path(self.workspace.name)
        self.store = Store(self.root / "data" / "inventory.sqlite3")
        self.file_number = 0

    def tearDown(self):
        self.store.close()
        self.workspace.cleanup()

    def csv(self, rows, columns=COLUMNS, *, bom=True):
        self.file_number += 1
        path = self.root / f"report-{self.file_number}.csv"
        with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def ingest(self, rows, *, started_at=None, failures=None, expected=None):
        scan_id = self.store.start_scan("test", started_at=started_at)
        result = self.store.ingest_csvs(
            scan_id, [self.csv(rows)], failures=failures, expected_subscriptions=expected
        )
        return result["id"]

    def rows(self, **filters):
        return self.store.inventory(filters)["rows"]

    def legacy_mistral(self, scan_id):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            db.execute(
                "UPDATE inventory SET family='Mistral AI' WHERE scan_id=? AND format='Mistral AI'",
                (scan_id,),
            )
            db.commit()
            return db.execute("SELECT * FROM inventory WHERE scan_id=? ORDER BY id", (scan_id,)).fetchall()

    def test_empty_store_has_contract_shapes(self):
        result = self.store.inventory({})
        self.assertEqual(set(result), {"rows", "total", "page", "page_size", "snapshot", "summary"})
        self.assertIsNone(result["snapshot"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["page_size"], 50)
        self.assertEqual(set(result["summary"]), {
            "models", "versions", "regions", "subscriptions", "rows", "quota_pools",
            "with_headroom", "zero_quota", "unknown_quota",
        })
        self.assertTrue(all(value == 0 for value in result["summary"].values()))
        self.assertEqual(self.store.coverage(), {"rows": [], "snapshot": None})
        self.assertEqual(self.store.history({}), {"points": []})
        self.assertTrue(all(value == [] for value in self.store.facets().values()))

    def test_deleting_old_snapshots_keeps_the_latest_complete_and_running_scans(self):
        old = self.ingest([report_row()], started_at="2026-01-01T12:00:00+00:00")
        failed = self.store.start_scan("test", started_at="2026-02-01T12:00:00+00:00")
        self.store.fail_scan(failed, "Sign-in failed.")
        running = self.store.start_scan("test", started_at="2026-01-15T12:00:00+00:00")
        recent = self.ingest([report_row(Model="gpt-recent")], started_at="2026-06-01T12:00:00+00:00")
        cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.assertEqual(self.store.delete_snapshots_before(cutoff), 2)
        self.assertEqual([scan["id"] for scan in self.store.scans()], [recent, running])
        with closing(sqlite3.connect(self.store.db_path)) as db:
            for table in ("inventory", "coverage"):
                with self.subTest(table=table):
                    scans = {row[0] for row in db.execute(f"SELECT DISTINCT scan_id FROM {table}")}
                    self.assertEqual(scans, {recent})
        self.assertNotIn(old, [scan["id"] for scan in self.store.scans()])
        self.assertEqual(self.store.delete_snapshots_before(cutoff), 0)
        self.assertEqual(self.store.delete_snapshots_before(datetime(2027, 1, 1, tzinfo=timezone.utc)), 0,
                         "the latest complete snapshot is kept even when it is older than the cutoff")
        self.assertEqual(self.store.inventory({})["snapshot"]["id"], recent)
        with self.assertRaises(ValueError):
            self.store.delete_snapshots_before(datetime(2026, 5, 1))

    def test_initialization_is_idempotent_wal_and_persistent(self):
        scan_id = self.ingest([report_row()])
        second = Store(self.store.db_path)
        try:
            self.assertEqual(second.inventory({})["snapshot"]["id"], scan_id)
            with closing(sqlite3.connect(self.store.db_path)) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertGreaterEqual(len(db.execute("PRAGMA index_list(inventory)").fetchall()), 5)
        finally:
            second.close()
        self.store.close()
        self.store.close()
        self.assertEqual(self.rows()[0]["model"], "gpt-example")

    def test_utf8_bom_and_unicode_round_trip(self):
        self.ingest([report_row(Subscription="Équipe 東京", Notes="first\nsecond", Model="modèle")])
        row = self.rows()[0]
        self.assertEqual(row["subscription"], "Équipe 東京")
        self.assertEqual(row["notes"], "first\nsecond")
        self.assertEqual(set(row), set(ROW_FIELDS))
        self.assertIsInstance(row["id"], int)
        self.assertEqual(self.store.scans()[0]["status"], "complete")

    def test_plain_utf8_is_also_supported(self):
        scan_id = self.store.start_scan("import")
        self.store.ingest_csvs(scan_id, [self.csv([report_row()], bom=False)])
        self.assertEqual(len(self.rows()), 1)

    def test_identical_account_kinds_are_deduplicated(self):
        self.ingest([
            report_row(Kind="OpenAI"), report_row(Kind="AIServices"),
            report_row(Kind="OpenAI"),
        ])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_kinds"], ["AIServices", "OpenAI"])
        self.assertEqual(self.store.scans()[0]["record_count"], 1)

    def test_differing_metadata_and_optional_prices_are_not_collapsed(self):
        self.ingest([
            report_row(Kind="OpenAI"),
            report_row(Kind="AIServices", Lifecycle="Preview"),
            report_row(Kind="AIServices", RetailPrices="meter: 1 USD"),
        ])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["key"] for row in rows}), 3)
        self.assertEqual(self.store.inventory({})["summary"]["quota_pools"], 1)

    def test_shared_quota_versions_count_one_pool_without_capacity_sums(self):
        self.ingest([
            report_row(Version="v1"), report_row(Version="v2"),
            report_row(Model="another-model", Version="v3"),
        ])
        summary = self.store.inventory({"page_size": "1"})["summary"]
        self.assertEqual(summary["rows"], 3)
        self.assertEqual(summary["models"], 2)
        self.assertEqual(summary["versions"], 3)
        self.assertEqual(summary["quota_pools"], 1)
        self.assertEqual(summary["with_headroom"], 1)
        self.assertNotIn("remaining", summary)

    def test_pool_keys_include_region_and_subscription(self):
        self.ingest([
            report_row(), report_row(Region="westus"),
            report_row(SubscriptionId="sub-b", Subscription="Production"),
        ])
        summary = self.store.inventory({})["summary"]
        self.assertEqual(summary["quota_pools"], 3)
        self.assertEqual(summary["subscriptions"], 2)
        self.assertEqual(summary["regions"], 2)

    def test_conflicting_shared_pool_values_or_units_are_unknown(self):
        self.ingest([
            report_row(Version="v1"), report_row(Version="v2", Unit="RPM"),
            report_row(Version="v3", Limit="20", Remaining="17"),
        ])
        summary = self.store.inventory({})["summary"]
        self.assertEqual(summary["quota_pools"], 1)
        self.assertEqual(summary["with_headroom"], 0)
        self.assertEqual(summary["unknown_quota"], 1)

    def test_zero_null_and_fractional_values_are_distinct(self):
        self.ingest([
            report_row(Model="zero", QuotaName="zero", Limit="0", Allocated="0", Remaining="0"),
            report_row(Model="unknown", QuotaName="unknown", Limit="", Allocated="",
                       Remaining="", QuotaStatus="Unknown"),
            report_row(Model="fraction", QuotaName="fraction", Limit="1.5", Allocated=".25",
                       Remaining="1.25"),
            report_row(Model="unmapped", QuotaName="", Limit="", Allocated="",
                       Remaining="", Unit="", QuotaStatus="Unknown"),
        ])
        rows = {row["model"]: row for row in self.rows()}
        self.assertEqual(rows["zero"]["remaining"], 0)
        self.assertIsNone(rows["unknown"]["remaining"])
        self.assertEqual(rows["fraction"]["allocated"], 0.25)
        summary = self.store.inventory({})["summary"]
        self.assertEqual((summary["quota_pools"], summary["with_headroom"],
                          summary["zero_quota"], summary["unknown_quota"]), (3, 1, 1, 2))
        self.assertEqual({row["model"] for row in self.rows(availability="unknown")},
                         {"unknown", "unmapped"})
        self.assertEqual([row["model"] for row in self.rows(availability="exhausted")], ["zero"])
        self.assertEqual({row["model"] for row in self.rows(minimum="0")}, {"zero", "fraction"})

    def test_allocated_above_limit_can_report_clamped_zero(self):
        self.ingest([report_row(Limit="2", Allocated="3", Remaining="0")])
        self.assertEqual(self.rows()[0]["remaining"], 0)

    def test_partial_numeric_unknowns_are_not_fabricated(self):
        self.ingest([report_row(Limit="10", Allocated="", Remaining="", QuotaStatus="Unknown")])
        row = self.rows()[0]
        self.assertEqual(row["quota_limit"], 10)
        self.assertIsNone(row["remaining"])
        self.assertEqual(self.store.inventory({})["summary"]["unknown_quota"], 1)

    def test_description_normalizes_legacy_and_exact_units(self):
        samples = [
            ("One Thousand Tokens Per Minute", "1K TPM"),
            ("Tokens Per Minute (thousands)", "1K TPM"),
            ("Tokens Per Minute (millions)", "1M TPM"),
            ("One Million Tokens Per Minute", "1M TPM"),
            ("Requests Per Minute (thousands)", "1K RPM"),
            ("One Thousand Requests Per Minute", "1K RPM"),
            ("Requests Per Minute", "RPM"),
            ("Requests Per Minute - claude-example - GlobalStandard", "RPM"),
            ("Provisioned Managed Throughput Units", "PTU"),
            ("Tokens Per Minute", "TPM"),
            ("Tokens Per Second", "TPS"),
            ("Requests Per Day", "RPD"),
            ("Tokens Per Day", "TPD"),
            ("Million Enqueued Tokens", "1M tokens"),
            ("General count of quota units", "Count"),
            ("Default Quota for OpenAI.GlobalStandard.gpt-example", "Count"),
        ]
        self.ingest([
            report_row(Model=f"unit-{index}", Unit="Count", QuotaDescription=text,
                       QuotaName=f"quota-{index}")
            for index, (text, unit) in enumerate(samples)
        ])
        rows = {row["model"]: row for row in self.rows()}
        for index, (description, unit) in enumerate(samples):
            with self.subTest(description=description):
                self.assertEqual(rows[f"unit-{index}"]["unit"], unit)
                self.assertEqual(rows[f"unit-{index}"]["quota_description"], description)

    def test_family_provider_and_sku_types(self):
        self.ingest([
            report_row(Model="claude-example", Format="OpenAI", SKU="GlobalStandard"),
            report_row(Model="gpt-example", Format="OpenAI", SKU="DataZoneProvisionedManaged", Type="DataZone"),
            report_row(Model="deepseek-v3", Format="DeepSeek", SKU="Batch", Type="Regional"),
            report_row(Model="phi-4", Format="Microsoft", SKU="custom", Type="Unknown"),
        ])
        rows = {row["model"]: row for row in self.rows()}
        self.assertEqual(rows["claude-example"]["family"], "Claude")
        self.assertEqual(rows["claude-example"]["format"], "OpenAI")
        self.assertEqual(rows["gpt-example"]["capacity_type"], "PTU")
        self.assertEqual(rows["deepseek-v3"]["capacity_type"], "Batch")
        self.assertEqual(rows["phi-4"]["capacity_type"], "Other")
        self.assertEqual(rows["phi-4"]["family"], "Microsoft")

    def test_new_mistral_ingestion_canonicalizes_family_but_preserves_format(self):
        scan_id = self.ingest([
            report_row(Model="provider-model-a", Format="Mistral AI"),
            report_row(Model="provider-model-b", Format="Mistral"),
            report_row(Model="provider-model-c", Format="MISTRAL AI"),
            report_row(Model="provider-model-d", Format="Mistral A"),
        ])
        raw = self.store._db().execute(
            "SELECT model,format,family FROM inventory WHERE scan_id=? ORDER BY model", (scan_id,)
        ).fetchall()
        self.assertEqual([(row["format"], row["family"]) for row in raw], [
            ("Mistral AI", "Mistral"), ("Mistral", "Mistral"),
            ("MISTRAL AI", "Mistral"), ("Mistral A", "Mistral A"),
        ])

    def test_legacy_mistral_reads_merge_facets_filters_and_drills_without_rewriting(self):
        scan_id = self.ingest([
            report_row(Model="alpha", Format="Mistral AI", Version="1"),
            report_row(Model="beta", Format="Mistral", Version="2"),
            report_row(Model="gamma", Format="Mistral A", QuotaName="unrelated"),
        ])
        original = self.legacy_mistral(scan_id)
        facets = self.store.facets(scan_id)
        self.assertEqual(facets["family"], ["Mistral", "Mistral A"])
        for option in facets["model_version"]:
            expected = "Mistral A" if option["model"] == "gamma" else "Mistral"
            self.assertEqual(option["family"], expected)
            self.assertEqual(json.loads(option["value"])[0], option["format"])
        for selection in ("Mistral", "Mistral AI", "mistral ai", "Mistral AI,Mistral",
                          ["Mistral AI", "Mistral"]):
            with self.subTest(selection=selection):
                inventory = self.store.inventory({"snapshot": scan_id, "family": selection})
                self.assertEqual(inventory["total"], 2)
                self.assertEqual({row["family"] for row in inventory["rows"]}, {"Mistral"})
                self.assertEqual({row["format"] for row in inventory["rows"]}, {"Mistral AI", "Mistral"})
        self.assertEqual([row["model"] for row in self.rows(sort="family")], ["alpha", "beta", "gamma"])
        families = self.store.groups({"group_by": "family"})
        self.assertEqual(families["total"], 2)
        merged = next(row for row in families["rows"] if row["family"] == "Mistral")
        self.assertEqual((merged["entries"], merged["models"], merged["versions"],
                          merged["quota_pools"], merged["with_headroom"]), (2, 2, 2, 1, 1))
        self.assertEqual(merged["value"], "Mistral")
        self.assertEqual(merged["filters"], {"family": ["Mistral"]})
        self.assertEqual(len(self.rows(**merged["filters"])), 2)
        models = self.store.groups({"group_by": "model", "family": "Mistral AI"})
        self.assertEqual(models["total"], 2)
        variants = self.store.groups({"group_by": "model_version", "family": "Mistral AI"})
        self.assertEqual(variants["total"], 2)
        for row in variants["rows"]:
            self.assertEqual(row["family"], "Mistral")
            details = self.rows(**row["filters"])
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["format"], row["format"])
        export = self.store.export_csv({"family": "Mistral AI"})
        exported = list(csv.DictReader(io.StringIO(export.lstrip("\ufeff"))))
        self.assertEqual({row["family"] for row in exported}, {"Mistral"})
        self.assertEqual({row["format"] for row in exported}, {"Mistral AI", "Mistral"})
        point = self.store.history({"family": ["Mistral AI"]})["points"][0]
        self.assertEqual((point["record_count"], point["model_count"], point["quota_pools"]), (2, 2, 1))
        with closing(sqlite3.connect(self.store.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM inventory WHERE scan_id=? ORDER BY id", (scan_id,)).fetchall(),
                original,
            )

    def test_merged_mistral_groups_recompute_zero_unknown_and_shared_pools(self):
        cases = [
            ({"Limit": "0", "Allocated": "0", "Remaining": "0"},
             {"Limit": "0", "Allocated": "0", "Remaining": "0"}, (1, 0, 1, 0)),
            ({}, {"Limit": "20", "Remaining": "17"}, (1, 0, 0, 1)),
            ({"Unit": "RPM"}, {"Unit": "PTU"}, (1, 0, 0, 1)),
            ({"Limit": "", "Allocated": "", "Remaining": "", "QuotaStatus": "Unknown"},
             {}, (1, 0, 0, 1)),
        ]
        for older, canonical, counts in cases:
            with self.subTest(older=older, canonical=canonical):
                scan_id = self.ingest([
                    report_row(Model="alpha", Format="Mistral AI", **older),
                    report_row(Model="beta", Format="Mistral", **canonical),
                ])
                self.legacy_mistral(scan_id)
                result = self.store.groups({"snapshot": scan_id, "group_by": "family"})
                self.assertEqual(result["total"], 1)
                group = result["rows"][0]
                self.assertEqual(group["label"], "Mistral")
                self.assertEqual(tuple(group[field] for field in
                                       ("quota_pools", "with_headroom", "zero_quota", "unknown_quota")),
                                 counts)
                self.assertEqual(group["entries"], 2)

    def test_mistral_read_aliases_do_not_create_comparison_changes(self):
        rows = [
            report_row(Model="alpha", Format="Mistral AI"),
            report_row(Model="beta", Format="Mistral"),
        ]
        before = self.ingest(rows)
        original = self.legacy_mistral(before)
        same = self.ingest(rows)
        for family in ("Mistral", "Mistral AI", ["Mistral AI", "Mistral"]):
            comparison = self.store.compare(before, same, {"family": family})
            self.assertEqual(comparison["rows"], [])
            self.assertEqual(comparison["summary"]["added"], 0)
            self.assertEqual(comparison["summary"]["removed"], 0)
        changed = self.ingest([dict(rows[0], Limit="20", Remaining="17"), rows[1]])
        comparison = self.store.compare(before, changed, {"family": "Mistral AI"})
        self.assertEqual(comparison["summary"]["changed"], 1)
        self.assertEqual(comparison["summary"]["quota_changed"], 1)
        change = comparison["rows"][0]
        self.assertEqual(set(change["fields"]), {"quota_limit", "remaining"})
        for row in (change["before"], change["after"]):
            self.assertEqual(row["family"], "Mistral")
            self.assertEqual(row["format"], "Mistral AI")
        history = self.store.history({"family": "Mistral AI"})["points"]
        self.assertEqual([point["record_count"] for point in history], [2, 2, 2])
        with closing(sqlite3.connect(self.store.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM inventory WHERE scan_id=? ORDER BY id", (before,)).fetchall(),
                original,
            )

    def test_no_sku_and_deprecated_models_stay_catalog_metadata(self):
        self.ingest([report_row(
            SKU="", Type="Unknown", Catalog="No SKUs", Lifecycle="Deprecated",
            InferenceDeprecation="2026-10-01", SkuDeprecation="2026-09-30",
            Limit="", Allocated="", Remaining="", Unit="", QuotaStatus="Unknown", QuotaName="",
        )])
        row = self.rows()[0]
        self.assertEqual((row["catalog"], row["lifecycle"]), ("No SKUs", "Deprecated"))
        self.assertEqual(row["inference_deprecation"], "2026-10-01")
        self.assertEqual(self.rows(availability="available"), [])

    def test_empty_catalog_is_successful_coverage_not_a_fake_model(self):
        scan_id = self.ingest([status_row("Empty")], expected=["sub-a"])
        self.assertEqual(self.store.scans()[0]["status"], "complete")
        self.assertEqual(self.rows(), [])
        coverage = self.store.coverage(scan_id)["rows"]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["catalog_status"], "Empty")
        self.assertTrue(coverage[0]["successful"])
        self.assertEqual(coverage[0]["record_count"], 0)
        self.assertEqual(self.store.facets()["region"], ["eastus"])

    def test_catalog_and_subscription_failures_are_distinct_from_empty(self):
        scan_id = self.ingest([
            status_row("Empty"), status_row("ERROR", Region="westus", Notes="catalog denied"),
        ], failures=[{"subscription_id": "sub-b", "message": "login expired"}],
            expected=["sub-a", "sub-b"])
        scan = self.store.scans()[0]
        self.assertEqual(scan["status"], "partial")
        self.assertEqual(scan["error_count"], 2)
        by_scope = {(row["subscription_id"], row["region"]): row
                    for row in self.store.coverage(scan_id)["rows"]}
        self.assertEqual(by_scope[("sub-a", "eastus")]["catalog_status"], "Empty")
        self.assertEqual(by_scope[("sub-a", "westus")]["catalog_status"], "ERROR")
        self.assertEqual(by_scope[("sub-b", "")]["status"], "failed")
        self.assertIsNone(self.store.inventory({})["snapshot"])

    def test_quota_errors_deduplicate_health_without_losing_catalog_rows(self):
        scan_id = self.ingest([
            report_row(Model="first", Limit="", Allocated="", Remaining="",
                       QuotaStatus="ERROR", Notes="quota denied"),
            report_row(Model="second", Limit="", Allocated="", Remaining="",
                       QuotaStatus="ERROR", Notes="quota denied"),
        ])
        scan = self.store.scans()[0]
        self.assertEqual(scan["status"], "partial")
        self.assertEqual(scan["error_count"], 1)
        self.assertEqual(len(self.rows(snapshot=scan_id)), 2)
        self.assertTrue(self.store.coverage(scan_id)["rows"][0]["successful"])

    def test_all_failed_scope_snapshot_is_failed(self):
        self.ingest([status_row("ERROR", Notes="not authorized")], expected=["sub-a"])
        self.assertEqual(self.store.scans()[0]["status"], "failed")

    def test_missing_expected_subscription_is_not_treated_as_empty(self):
        scan_id = self.ingest([report_row()], expected=["sub-a", "sub-b"])
        self.assertEqual(self.store.scans()[0]["status"], "partial")
        failed = [row for row in self.store.coverage(scan_id)["rows"] if row["status"] == "failed"]
        self.assertEqual(failed[0]["subscription_id"], "sub-b")
        self.assertIn("No report", failed[0]["notes"])

    def test_header_only_and_no_reports_are_failures(self):
        self.ingest([], expected=["sub-a"])
        self.assertEqual(self.store.scans()[0]["status"], "failed")
        scan_id = self.store.start_scan("test")
        result = self.store.ingest_csvs(scan_id, [])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_count"], 1)

    def test_atomic_multifile_failure_does_not_publish_any_rows(self):
        complete = self.ingest([report_row(Model="previous")])
        scan_id = self.store.start_scan("invalid")
        valid = self.csv([report_row(Model="new")])
        invalid = self.csv([report_row(Limit="NaN")])
        with self.assertRaises(ValueError):
            self.store.ingest_csvs(scan_id, [valid, invalid])
        self.assertEqual(self.rows(snapshot=scan_id), [])
        self.assertEqual(self.store.scans()[0]["status"], "running")
        self.assertEqual(self.store.inventory({})["snapshot"]["id"], complete)
        self.store.fail_scan(scan_id, "malformed input")
        self.assertEqual(self.store.scans()[0]["status"], "failed")

    def test_database_insert_failure_rolls_back_rows_and_snapshot_metadata(self):
        complete = self.ingest([report_row(Model="previous")])
        with closing(sqlite3.connect(self.store.db_path)) as db:
            db.execute(
                "CREATE TRIGGER fixture_failure BEFORE INSERT ON inventory "
                "WHEN NEW.model='stop' BEGIN SELECT RAISE(ABORT,'fixture error'); END"
            )
        scan_id = self.store.start_scan("atomic-write-test")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest_csvs(
                scan_id, [self.csv([report_row(Model="a-first"), report_row(Model="stop")])]
            )
        self.assertEqual(self.rows(snapshot=scan_id), [])
        self.assertEqual(self.store.coverage(scan_id)["rows"], [])
        self.assertEqual(self.store.scans()[0]["status"], "running")
        self.assertEqual(self.store.inventory({})["snapshot"]["id"], complete)

    def test_missing_second_file_does_not_publish_first_file(self):
        scan_id = self.store.start_scan("missing-file")
        with self.assertRaises(FileNotFoundError):
            self.store.ingest_csvs(scan_id, [self.csv([report_row()]), self.root / "absent.csv"])
        self.assertEqual(self.rows(snapshot=scan_id), [])
        self.assertEqual(self.store.scans()[0]["status"], "running")

    def test_invalid_critical_numbers_are_rejected(self):
        for value in ("NaN", "Infinity", "-1", "1,000", "=1+1", "1e999", "1e-9999",
                      "9007199254740992"):
            with self.subTest(value=value):
                scan_id = self.store.start_scan("invalid")
                with self.assertRaises(ValueError):
                    self.store.ingest_csvs(scan_id, [self.csv([report_row(Limit=value)])])
                self.assertEqual(self.rows(snapshot=scan_id), [])

    def test_reported_missing_and_inconsistent_remaining_are_rejected(self):
        for updates in ({"Remaining": ""}, {"Remaining": "9"}, {"QuotaStatus": ""},
                        {"Allocated": ""}, {"Catalog": "made-up"}, {"SubscriptionId": ""},
                        {"Region": ""}, {"Model": ""}, {"SKU": ""},
                        {"Catalog": "No SKUs"}, {"Catalog": "Empty"}):
            with self.subTest(updates=updates):
                scan_id = self.store.start_scan("invalid")
                with self.assertRaises(ValueError):
                    self.store.ingest_csvs(scan_id, [self.csv([report_row(**updates)])])

    def test_invalid_csv_shapes_and_encoding_are_rejected_atomically(self):
        cases = [
            b"", b"SubscriptionId,Region\nsub-a,eastus\n",
            (",".join(COLUMNS + ["Model"]) + "\n").encode(),
            (",".join(COLUMNS) + "\nsub-a,eastus\n").encode(),
            (",".join(COLUMNS) + "\n" + ",".join(["x"] * (len(COLUMNS) + 1))).encode(),
            (",".join(COLUMNS) + '\n"unterminated').encode(),
            b"\xff\xfeinvalid utf8",
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                path = self.root / f"invalid-{index}.csv"
                path.write_bytes(data)
                scan_id = self.store.start_scan("invalid")
                with self.assertRaises(ValueError):
                    self.store.ingest_csvs(scan_id, [path])
                self.assertEqual(self.rows(snapshot=scan_id), [])

    def test_conflicting_tenants_empty_scopes_and_unexpected_subscriptions_are_rejected(self):
        cases = [
            ([report_row(), report_row(TenantId="different")], None),
            ([report_row(), status_row("Empty")], None),
            ([report_row(SubscriptionId="wrong")], ["sub-a"]),
        ]
        for rows, expected in cases:
            scan_id = self.store.start_scan("invalid")
            with self.assertRaises(ValueError):
                self.store.ingest_csvs(scan_id, [self.csv(rows)], expected_subscriptions=expected)

    def test_finalized_snapshots_are_immutable(self):
        scan_id = self.ingest([report_row()])
        with self.assertRaises(RuntimeError):
            self.store.ingest_csvs(scan_id, [self.csv([report_row(Model="replacement")])])
        with self.assertRaises(RuntimeError):
            self.store.fail_scan(scan_id, "replacement")
        self.assertEqual(self.rows()[0]["model"], "gpt-example")

    def test_latest_selection_uses_last_complete_start_time_not_attempt_id(self):
        latest = self.ingest([report_row(Model="latest")], started_at="2026-09-19T12:00:00Z")
        yesterday = self.ingest([report_row(Model="older")], started_at="2026-09-18T12:00:00Z")
        partial = self.ingest([report_row(Model="partial")], started_at="2026-09-20T12:00:00Z",
                              expected=["sub-a", "sub-b"])
        failed = self.store.start_scan("failed", "2026-09-21T12:00:00Z")
        self.store.fail_scan(failed, "offline")
        running = self.store.start_scan("running", "2026-09-22T12:00:00Z")
        self.assertEqual(self.store.inventory({})["snapshot"]["id"], latest)
        self.assertEqual(self.rows(snapshot=yesterday)[0]["model"], "older")
        self.assertEqual(self.rows(snapshot=partial)[0]["model"], "partial")
        self.assertEqual([scan["id"] for scan in self.store.scans()],
                         [running, failed, partial, latest, yesterday])

    def test_timestamp_and_snapshot_validation(self):
        for date in ("yesterday", "2026-01-01", "2026-01-01T01:01:01", 123):
            with self.assertRaises(ValueError):
                self.store.start_scan("test", date)
        for selection in ("0", "-1", "99999", "1 OR 1=1", True):
            with self.assertRaises(ValueError):
                self.store.inventory({"snapshot": selection})

    def test_filters_intersect_and_support_multiple_bound_values(self):
        self.ingest([
            report_row(Model="claude-sample", Version="v1", SKU="DataZoneStandard",
                       Type="DataZone", Region="eastus", Unit="RPM"),
            report_row(Model="gpt-sample", Version="v2", SKU="GlobalProvisionedManaged",
                       Type="Global", Region="westus", Unit="PTU"),
            report_row(Model="gpt-batch", Version="v3", SKU="Batch", Type="Regional",
                       Region="eastus", Lifecycle="Preview", Unit="1M tokens"),
        ])
        filtered = self.rows(subscription="sub-a", family="Claude", model="claude-sample",
                             version="v1", region="EASTUS", deployment_type="DataZone",
                             capacity_type="PAYG", lifecycle="GenerallyAvailable",
                             availability="available", unit="RPM", minimum="7", q="sample")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(self.rows(region="eastus,westus", capacity_type="PTU,Batch")), 2)
        self.assertEqual(len(self.rows(subscription="Development")), 3)
        self.assertEqual(self.rows(minimum="8"), [])

    def test_tenant_filter_and_facets_keep_scopes_separate(self):
        rows = [
            report_row(Model="gpt-example", SubscriptionId="sub-a", TenantId="tenant-a"),
            report_row(Model="claude-example", Subscription="Other subscription",
                       SubscriptionId="sub-b", TenantId="tenant-b", Kind="AIServices",
                       Format="Anthropic", QuotaName="other-pool"),
        ]
        self.ingest(rows, expected=["sub-a", "sub-b"])
        self.assertEqual(self.store.facets()["tenant"], ["tenant-a", "tenant-b"])
        self.assertEqual([row["model"] for row in self.rows(tenant="tenant-b")], ["claude-example"])
        self.assertEqual(self.store.inventory({"tenant": "tenant-b"})["summary"]["subscriptions"], 1)
        self.assertEqual(self.store.quota({"tenant": "tenant-b"})["total"], 1)
        self.assertEqual(self.rows(tenant=["tenant-a", "tenant-b"], model="claude-example")[0]["tenant_id"], "tenant-b")
        export = self.store.export_csv({"tenant": "tenant-b"})
        self.assertIn("claude-example", export)
        self.assertNotIn("gpt-example", export)
        self.assertEqual(self.rows(tenant="tenant-c"), [])

    def test_search_wildcards_are_literals_and_sql_injection_is_data(self):
        malicious = "x' OR 1=1 --"
        self.ingest([
            report_row(Model=malicious), report_row(Model="100%_special\\model"),
            report_row(Model="ordinary"),
        ])
        self.assertEqual([row["model"] for row in self.rows(model=malicious)], [malicious])
        self.assertEqual([row["model"] for row in self.rows(q="%_")], ["100%_special\\model"])
        self.assertEqual(len(self.rows(q="\\")), 1)
        self.assertEqual(self.rows(subscription=malicious), [])
        self.assertEqual(len(self.rows()), 3)

    def test_invalid_filters_and_pagination_are_rejected(self):
        cases = [
            {"sort": "remaining;DROP TABLE scans"}, {"direction": "sideways"},
            {"capacity_type": "Unlimited"}, {"deployment_type": "Anywhere"},
            {"availability": "yes"}, {"page": "0"}, {"page": "-1"}, {"page": "1.5"},
            {"page": "1000001"}, {"page_size": "501"}, {"page_size": "0"},
            {"minimum": "-1"}, {"minimum": "NaN"}, {"q": "a" * 257},
            {"region": "eastus,"}, {"region": ["eastus", None]}, {"untrusted": "value"},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    self.store.inventory(filters)

    def test_pagination_sorting_and_null_last(self):
        self.ingest([
            report_row(Model="a", QuotaName="a", Limit="0", Allocated="0", Remaining="0"),
            report_row(Model="B", QuotaName="b", Limit="", Allocated="", Remaining="",
                       QuotaStatus="Unknown"),
            report_row(Model="c", QuotaName="c"),
            report_row(Model="d", QuotaName="d", Limit="20", Remaining="17"),
        ])
        result = self.store.inventory({"page": "2", "page_size": "2"})
        self.assertEqual(result["total"], 4)
        self.assertEqual([row["model"] for row in result["rows"]], ["c", "d"])
        self.assertEqual([row["model"] for row in self.rows(sort="remaining", direction="desc")],
                         ["d", "c", "a", "B"])
        self.assertEqual([row["model"] for row in self.rows(sort="remaining", direction="asc")],
                         ["a", "c", "d", "B"])
        self.assertEqual(self.rows(page="10"), [])

    def test_facets_are_snapshot_specific_and_include_observed_empty_regions(self):
        first = self.ingest([report_row(), status_row("Empty", Region="westus")])
        self.ingest([report_row(Model="claude-sample", Region="north", Unit="RPM")])
        facets = self.store.facets(first)
        self.assertEqual(facets["subscription"], [{"value": "sub-a", "label": "Development"}])
        self.assertEqual(facets["region"], ["eastus", "westus"])
        self.assertEqual(facets["family"], ["OpenAI"])
        self.assertEqual(self.store.facets()["family"], ["Claude"])

    def test_model_sort_keeps_version_order_and_provider_ties_predictable(self):
        self.ingest([
            report_row(Model="B", Version="2"), report_row(Model="a", Version="2"),
            report_row(Model="a", Version="1", Format="OtherProvider"),
            report_row(Model="a", Version="1", Region="westus"),
            report_row(Model="B", Version="1"), report_row(Model="a", Version=""),
            report_row(Model="a", Version="1"),
        ])
        ascending = [
            ("a", "", "OpenAI", "eastus"),
            ("a", "1", "OpenAI", "eastus"),
            ("a", "1", "OpenAI", "westus"),
            ("a", "1", "OtherProvider", "eastus"),
            ("a", "2", "OpenAI", "eastus"),
            ("B", "1", "OpenAI", "eastus"),
            ("B", "2", "OpenAI", "eastus"),
        ]
        descending = [ascending[index] for index in (6, 5, 4, 1, 2, 3, 0)]
        fields = ("model", "version", "format", "region")
        for direction, expected in (("asc", ascending), ("desc", descending)):
            with self.subTest(direction=direction):
                filters = {"sort": "model", "direction": direction}
                self.assertEqual([tuple(row[field] for field in fields) for row in self.rows(**filters)],
                                 expected)
                page = self.store.inventory(dict(filters, page="2", page_size="2"))
                self.assertEqual([tuple(row[field] for field in fields) for row in page["rows"]],
                                 expected[2:4])
                exported = list(csv.DictReader(io.StringIO(
                    self.store.export_csv(dict(filters, page="2", page_size="2")).lstrip("\ufeff")
                )))
                self.assertEqual([tuple(row[field] for field in fields) for row in exported], expected)

    def test_compare_only_common_successful_scopes(self):
        before = self.ingest([
            report_row(Model="changed"), report_row(Model="removed"),
            report_row(Model="not-lost", SubscriptionId="sub-b", Region="westus"),
        ])
        after = self.ingest([
            report_row(Model="changed", Limit="20", Remaining="17"),
            report_row(Model="added"),
            report_row(Model="new-scope", SubscriptionId="sub-c", Region="north"),
        ], failures=[{"subscription_id": "sub-b", "message": "not authorized"}],
            expected=["sub-a", "sub-b", "sub-c"])
        comparison = self.store.compare(before, after, {})
        self.assertEqual(comparison["summary"],
                         dict(added=1, removed=1, changed=1, quota_changed=1, uncomparable=2))
        self.assertTrue(comparison["warnings"])
        removed = next(row for row in comparison["rows"] if row["change"] == "removed")
        self.assertEqual(removed["before"]["model"], "removed")
        self.assertNotIn("not-lost", [row["before"]["model"] for row in comparison["rows"]
                                     if row["before"]])

    def test_catalog_error_does_not_create_false_removals(self):
        before = self.ingest([report_row(Region="westus")])
        after = self.ingest([status_row("ERROR", Region="westus", Notes="denied")])
        comparison = self.store.compare(before, after, {})
        self.assertEqual(comparison["summary"]["removed"], 0)
        self.assertEqual(comparison["summary"]["uncomparable"], 1)
        self.assertEqual(comparison["rows"], [])
        self.assertTrue(any("No common" in warning for warning in comparison["warnings"]))

    def test_successfully_empty_catalog_can_show_real_removals(self):
        before = self.ingest([report_row()])
        after = self.ingest([status_row("Empty")])
        comparison = self.store.compare(before, after, {})
        self.assertEqual(comparison["summary"]["removed"], 1)
        self.assertEqual(comparison["summary"]["uncomparable"], 0)

    def test_unit_and_quota_status_changes_keep_stable_keys(self):
        before = self.ingest([report_row(Unit="Count")])
        unit_change = self.ingest([report_row(Unit="RPM")])
        comparison = self.store.compare(before, unit_change, {})
        change = comparison["rows"][0]
        self.assertEqual(change["fields"], ["unit"])
        self.assertEqual(change["before"]["key"], change["after"]["key"])
        after = self.ingest([report_row(Unit="", Limit="", Allocated="", Remaining="",
                                        QuotaStatus="ERROR", Notes="quota denied")])
        comparison = self.store.compare(unit_change, after, {})
        self.assertEqual(comparison["summary"]["quota_changed"], 1)
        self.assertIn("quota_status", comparison["rows"][0]["fields"])
        self.assertIsNone(comparison["rows"][0]["after"]["remaining"])
        self.assertTrue(any("not zero" in warning for warning in comparison["warnings"]))

    def test_filter_transition_is_changed_not_false_removal(self):
        before = self.ingest([report_row()])
        after = self.ingest([report_row(Limit="0", Allocated="0", Remaining="0")])
        comparison = self.store.compare(before, after, {"availability": "available"})
        self.assertEqual(comparison["summary"]["changed"], 1)
        self.assertEqual(comparison["summary"]["removed"], 0)
        self.assertIsNotNone(comparison["rows"][0]["after"])

    def test_duplicate_variant_keys_are_stable_when_metadata_changes(self):
        before = self.ingest([
            report_row(Kind="AIServices", Lifecycle="Preview"),
            report_row(Kind="OpenAI", Lifecycle="GenerallyAvailable"),
        ])
        after = self.ingest([
            report_row(Kind="OpenAI", Lifecycle="Deprecated"),
            report_row(Kind="AIServices", Lifecycle="Preview", Unit="RPM"),
        ])
        comparison = self.store.compare(before, after, {})
        self.assertEqual(comparison["summary"]["changed"], 2)
        self.assertEqual(comparison["summary"]["added"], 0)
        self.assertEqual(comparison["summary"]["removed"], 0)
        for change in comparison["rows"]:
            self.assertEqual(change["before"]["account_kinds"], change["after"]["account_kinds"])

    def test_keys_survive_a_missing_scope_between_variant_observations(self):
        original = self.ingest([report_row(Kind="OpenAI")])
        variants = self.ingest([
            report_row(Kind="AIServices", Lifecycle="Preview"), report_row(Kind="OpenAI"),
        ])
        self.ingest([report_row(SubscriptionId="sub-b", Region="westus")],
                    failures=[{"subscription_id": "sub-a", "message": "not observed"}])
        recovered = self.ingest([
            report_row(Kind="AIServices", Lifecycle="Preview", Unit="RPM"),
            report_row(Kind="OpenAI"),
        ])
        comparison = self.store.compare(variants, recovered, {})
        self.assertEqual(comparison["summary"]["changed"], 1)
        self.assertEqual(comparison["summary"]["added"], 0)
        self.assertEqual(comparison["summary"]["removed"], 0)
        changed = comparison["rows"][0]
        self.assertEqual(changed["before"]["account_kinds"], ["AIServices"])
        self.assertEqual(changed["after"]["account_kinds"], ["AIServices"])
        openai = next(row for row in self.rows(snapshot=recovered)
                      if row["account_kinds"] == ["OpenAI"])
        self.assertEqual(self.rows(snapshot=original)[0]["key"], openai["key"])

    def test_comparison_sorts_unknown_numeric_values_last_in_both_directions(self):
        before = self.ingest([report_row(Model="a"), report_row(Model="b"), report_row(Model="c")])
        after = self.ingest([
            report_row(Model="a", Limit="", Allocated="", Remaining="", QuotaStatus="Unknown"),
            report_row(Model="b", Limit="20", Remaining="17"),
            report_row(Model="c", Limit="0", Allocated="0", Remaining="0"),
        ])
        for direction, models in (("asc", ["c", "b", "a"]), ("desc", ["b", "c", "a"])):
            comparison = self.store.compare(before, after, {"sort": "remaining", "direction": direction})
            self.assertEqual([row["after"]["model"] for row in comparison["rows"]], models)

    def test_compare_dedup_reordering_is_not_a_change_and_paginates(self):
        rows = [report_row(Model=f"model-{index}") for index in range(4)]
        before = self.ingest(rows)
        same = self.ingest(list(reversed(rows)))
        self.assertEqual(self.store.compare(before, same, {})["rows"], [])
        after = self.ingest([dict(row, Lifecycle="Preview") for row in rows])
        comparison = self.store.compare(same, after, {"page_size": "2", "page": "2"})
        self.assertEqual(comparison["total"], 4)
        self.assertEqual(comparison["summary"]["changed"], 4)
        self.assertEqual(len(comparison["rows"]), 2)
        self.assertEqual(comparison["rows"][0]["after"]["model"], "model-2")

    def test_history_uses_complete_snapshots_and_distinct_pools(self):
        yesterday = self.ingest([report_row(Version="v1"), report_row(Version="v2")],
                                started_at="2026-09-18T12:00:00Z")
        today = self.ingest([report_row(Model="claude-example")],
                            started_at="2026-09-19T12:00:00Z")
        self.ingest([status_row("ERROR")], started_at="2026-09-20T12:00:00Z")
        points = self.store.history({})["points"]
        self.assertEqual([point["id"] for point in points], [yesterday, today])
        self.assertEqual(points[0]["quota_pools"], 1)
        self.assertEqual(points[0]["record_count"], 2)
        self.assertEqual([point["model_count"] for point in self.store.history({"family": "Claude"})["points"]],
                         [0, 1])

    def test_export_includes_all_filtered_pages_and_safe_text_only(self):
        self.ingest([
            report_row(Model="=1+1", Notes="\t=HYPERLINK(\"invalid\")", Version="@bad"),
            report_row(Model="+cmd", Subscription="-name"),
            report_row(Model="ordinary", SubscriptionId="other"),
        ])
        text = self.store.export_csv({"subscription": "sub-a", "page": "2", "page_size": "1"})
        self.assertTrue(text.startswith("\ufeff"))
        exported = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(len(exported), 2)
        self.assertEqual({row["model"] for row in exported}, {"'=1+1", "'+cmd"})
        self.assertTrue(next(row for row in exported if row["model"] == "'=1+1")["notes"].startswith("'="))
        self.assertEqual(next(row for row in exported if row["model"] == "'+cmd")["subscription"], "'-name")
        self.assertTrue(all(row["remaining"] == "7" for row in exported))
        self.assertTrue(all(not row["quota_limit"].startswith("'") for row in exported))

    def test_export_keeps_unknown_blank_and_zero_numeric(self):
        self.ingest([
            report_row(Model="unknown", Limit="", Allocated="", Remaining="", QuotaStatus="Unknown"),
            report_row(Model="zero", Limit="0", Allocated="0", Remaining="0"),
        ])
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv({}).lstrip("\ufeff"))))
        self.assertEqual([row["remaining"] for row in rows], ["", "0"])

    def test_thread_connections_are_isolated_and_parallel_reads_are_consistent(self):
        scan_id = self.ingest([report_row()])
        barrier = threading.Barrier(3)

        def read():
            try:
                connection = self.store._db()
                barrier.wait(timeout=5)
                return id(connection), self.store.inventory({})["snapshot"]["id"]
            finally:
                self.store.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: read(), range(3)))
        self.assertEqual(len({item[0] for item in results}), 3)
        self.assertTrue(all(item[1] == scan_id for item in results))

    def test_historical_report_scale_ingestion_filtering_and_complete_export(self):
        subscriptions = ["sub-a", "sub-b", "sub-c"]
        rows = [
            report_row(
                SubscriptionId=subscriptions[index % 3], Model=f"scale-model-{index:05d}",
                QuotaName=f"shared-pool-{index % 64}", Unit="Count",
                QuotaDescription="Requests Per Minute - model - GlobalStandard",
            )
            for index in range(40_122)
        ]
        rows.extend(
            report_row(
                SubscriptionId=subscriptions[index % 3], Model=f"no-sku-{index:03d}",
                SKU="", Type="Unknown", Catalog="No SKUs", QuotaName="",
                Limit="", Allocated="", Remaining="", Unit="", QuotaStatus="Unknown",
            )
            for index in range(150)
        )
        rows.extend(status_row("Empty", SubscriptionId=subscription, Region="qatarcentral")
                    for subscription in subscriptions)
        self.assertEqual(len(rows), 40_275)
        scan_id = self.ingest(rows, expected=subscriptions)
        scan = self.store.scans()[0]
        self.assertEqual(scan["status"], "complete")
        self.assertEqual(scan["record_count"], 40_272)
        self.assertEqual(scan["subscription_count"], 3)
        self.assertEqual(scan["region_count"], 2)
        inventory = self.store.inventory({"unit": "RPM", "page": "2", "page_size": "2"})
        self.assertEqual(inventory["snapshot"]["id"], scan_id)
        self.assertEqual(inventory["total"], 40_122)
        self.assertEqual(len(inventory["rows"]), 2)
        self.assertEqual(inventory["summary"]["quota_pools"], 192)
        self.assertEqual(inventory["summary"]["with_headroom"], 192)
        self.assertEqual(self.store.inventory({"capacity_type": "Other"})["total"], 150)
        self.assertEqual(self.store.facets()["region"], ["eastus", "qatarcentral"])
        coverage = self.store.coverage()["rows"]
        self.assertEqual(sum(row["catalog_status"] == "Empty" for row in coverage), 3)
        exported = self.store.export_csv({"unit": "RPM", "page": "800", "page_size": "50"})
        self.assertEqual(
            sum(1 for _ in csv.DictReader(io.StringIO(exported.lstrip("\ufeff")))), 40_122
        )

    def test_list_filters_or_within_categories_and_intersect_across_categories(self):
        self.ingest([
            report_row(Model="A", Version="v1", Unit="RPM"),
            report_row(Model="B", Version="v2", Unit="RPM", Lifecycle="Preview",
                       Limit="", Allocated="", Remaining="", QuotaStatus="Unknown"),
            report_row(Model="C", Version="v1", Region="westus", Unit="PTU",
                       SKU="ProvisionedManaged", Type="Regional"),
            report_row(Model="D", Version="v1", Limit="0", Allocated="0", Remaining="0"),
            report_row(Model="claude-example", Version="v1", Unit="RPM"),
        ])
        filters = dict(
            subscription=["sub-a", "unused"], region=["eastus", "westus"],
            family=["OpenAI"], model=["A", "B", "C"], version=["v1", "v2"],
            deployment_type=["Global", "Regional"], capacity_type=["PAYG", "PTU"],
            lifecycle=["GenerallyAvailable", "Preview"], unit=["RPM", "PTU"],
            availability=["available", "unknown"],
        )
        self.assertEqual({row["model"] for row in self.rows(**filters)}, {"A", "B", "C"})
        self.assertEqual({row["model"] for row in self.rows(availability=["available", "unknown"])},
                         {"A", "B", "C", "claude-example"})
        self.assertEqual({row["model"] for row in self.rows(model="A,C", unit="RPM,PTU")}, {"A", "C"})
        self.assertEqual(self.rows(model=["A", "A"]), self.rows(model="A"))
        self.assertEqual(self.rows(region=[]), self.rows())

    def test_list_choices_preserve_literal_commas(self):
        self.ingest([
            report_row(Model="named,with-comma", Version="v,1"),
            report_row(Model="named"), report_row(Model="with-comma"),
        ])
        self.assertEqual([row["model"] for row in self.rows(model=["named,with-comma"], version=["v,1"])],
                         ["named,with-comma"])
        self.assertEqual({row["model"] for row in self.rows(model="named,with-comma")},
                         {"named", "with-comma"})

    def test_list_filter_bounds_types_and_minimum_unit_guard(self):
        self.ingest([report_row(Unit="RPM")])
        self.assertEqual(len(self.rows(region=["eastus"] * MAX_FILTER_ITEMS)), 1)
        self.assertEqual(len(self.rows(unit=["RPM", "rpm"], minimum="7")), 1)
        self.assertEqual(len(self.rows(minimum="7")), 1)
        for filters in (
            {"region": ["eastus"] * (MAX_FILTER_ITEMS + 1)},
            {"region": "eastus," * MAX_FILTER_ITEMS + "eastus"},
            {"model": ["x" * (MAX_FILTER_FIELD + 1)]}, {"model": [1]},
            {"model": [["nested"]]}, {"model": [True]}, {"model": [""]},
            {"page": ["1"]}, {"snapshot": ["latest"]}, {"minimum": ["1"]},
            {"unit": ["RPM", "PTU"], "minimum": "0"},
            {"unit": "RPM,PTU", "minimum": "0"},
        ):
            with self.subTest(filters=str(filters)[:100]):
                with self.assertRaises(ValueError):
                    self.store.inventory(filters)

    def test_model_version_pairs_do_not_form_cross_products_or_cross_providers(self):
        self.ingest([
            report_row(Model=model, Version=version)
            for model in ("A", "B") for version in ("1", "2")
        ] + [report_row(Model="A", Version="1", Format="OtherProvider")])
        pairs = [["OpenAI", "A", "1"], ["OpenAI", "B", "2"]]
        expected = {tuple(triple) for triple in pairs}
        for selection in (pairs, json.dumps(pairs)):
            with self.subTest(selection=selection):
                rows = self.rows(model_version=selection)
                self.assertEqual({(row["format"], row["model"], row["version"]) for row in rows}, expected)
        self.assertEqual([row["model"] for row in self.rows(model_version=pairs, version=["1"])], ["A"])
        self.assertEqual(len(self.rows(model=["A"], version=["1"])), 2)
        self.assertEqual(len(self.rows(model_version=[pairs[0], pairs[0]])), 1)

    def test_model_version_pairs_accept_empty_versions_and_literal_punctuation(self):
        model = "model,with'punctuation"
        self.ingest([
            report_row(Model=model, Version="", Format="Provider,Name"),
            report_row(Model="ordinary", Version="", Format=""),
        ])
        pairs = [["Provider,Name", model, ""]]
        self.assertEqual(self.rows(model_version=json.dumps(pairs))[0]["model"], model)
        self.assertEqual(self.rows(model_version=[["", "ordinary", ""]])[0]["version"], "")
        self.assertEqual(len(self.rows(model_version=[])), 2)
        self.assertEqual(len(self.rows(model_version="[]")), 2)
        self.assertEqual(self.rows(model_version=[["provider,name", model, ""]]), [])

    def test_model_version_json_structure_types_and_bounds_are_validated(self):
        invalid = [
            "", "null", "{}", "not-json", 1, True, None,
            ["OpenAI", "A", "1"], [["OpenAI", "A"]], [["OpenAI", "A", "1", "extra"]],
            [["OpenAI", "", "1"]], [["OpenAI", "A", None]], [["OpenAI", 1, "1"]],
            [["OpenAI", "A", True]], [["OpenAI", "A", "\x00"]],
            [["OpenAI", "A", "\ud800"]], [["OpenAI", "A", "v" * (MAX_FILTER_FIELD + 1)]],
            [["OpenAI", "A", "1"]] * (MAX_FILTER_ITEMS + 1),
            '[["OpenAI","A",NaN]]', "[" * 1200 + "]" * 1200,
        ]
        for selection in invalid:
            with self.subTest(selection=repr(selection)[:100]):
                with self.assertRaises(ValueError):
                    self.store.inventory({"model_version": selection})
        long_name = "m" * MAX_FILTER_FIELD
        self.ingest([report_row(Model=long_name, Version="v1")])
        self.assertEqual(len(self.rows(model_version=[["OpenAI", long_name, "v1"]])), 1)
        pairs = [["OpenAI", long_name, "v1"]] * MAX_FILTER_ITEMS
        self.assertEqual(len(self.rows(model_version=pairs)), 1)

    def test_model_version_predicates_are_bound_and_literal(self):
        name = "model' OR 1=1 --"
        version = "v'); DROP TABLE scans; --"
        provider = "format' UNION SELECT 1 --"
        self.ingest([report_row(Model=name, Version=version, Format=provider), report_row()])
        rows = self.rows(model_version=[[provider, name, version]])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], version)
        self.assertEqual(self.rows(model_version=[["OpenAI", "x' OR 1=1 --", "1"]]), [])
        self.assertEqual(len(self.store.scans()), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_variant_facets_preserve_legacy_keys_and_provider_identity(self):
        self.ingest([
            report_row(Model="Shared", Version="v1"),
            report_row(Model="Shared", Version="v1", Kind="AIServices"),
            report_row(Model="Shared", Version="v1", Format="OtherProvider"),
            report_row(Model="Earlier", Version=""),
        ])
        facets = self.store.facets()
        self.assertTrue({"model", "version", "family", "unit", "subscription", "region"} <= set(facets))
        options = facets["model_version"]
        self.assertEqual(len(options), 3)
        self.assertEqual([option["label"] for option in options],
                         ["Earlier (version not reported)", "Shared v1", "Shared v1"])
        for option in options:
            triple = [option[field] for field in ("format", "model", "version")]
            self.assertEqual(option["value"], json.dumps(triple, separators=(",", ":")))
            self.assertEqual(set(option), {"value", "label", "model", "version", "format", "family"})
        self.assertEqual(options[0]["version"], "")
        self.assertNotEqual(options[1]["value"], options[2]["value"])

    def test_all_data_apis_use_identical_paired_and_multi_filter_predicates(self):
        rows = [report_row(Model=model, Version=version, Unit="RPM")
                for model in ("A", "B") for version in ("1", "2")]
        before = self.ingest(rows)
        after = self.ingest([dict(row, Lifecycle="Preview", Limit="20", Remaining="17") for row in rows])
        filters = {
            "model_version": [["OpenAI", "A", "1"], ["OpenAI", "B", "2"]],
            "region": ["eastus", "westus"], "availability": ["available", "unknown"],
            "lifecycle": ["GenerallyAvailable", "Preview"], "unit": ["RPM"],
        }
        inventory = self.store.inventory(filters)
        self.assertEqual(inventory["total"], 2)
        exported = list(csv.DictReader(io.StringIO(self.store.export_csv(
            dict(filters, page="2", page_size="1")
        ).lstrip("\ufeff"))))
        self.assertEqual({(row["model"], row["version"]) for row in exported}, {("A", "1"), ("B", "2")})
        self.assertEqual([point["record_count"] for point in self.store.history(filters)["points"]], [2, 2])
        groups = self.store.groups(dict(filters, group_by="model_version", page_size="1"))
        self.assertEqual(groups["total"], 2)
        self.assertEqual(groups["summary"], inventory["summary"])
        comparison = self.store.compare(before, after, filters)
        self.assertEqual(comparison["summary"]["changed"], 2)
        self.assertEqual(comparison["summary"]["added"], 0)
        self.assertEqual(comparison["summary"]["removed"], 0)
        self.assertEqual({(row["after"]["model"], row["after"]["version"]) for row in comparison["rows"]},
                         {("A", "1"), ("B", "2")})

    def test_paired_multi_filters_classify_transitions_before_filtering(self):
        before = self.ingest([
            report_row(Model="A", Version="1", Unit="RPM"),
            report_row(Model="B", Version="2", Unit="RPM", Lifecycle="Deprecated",
                       Limit="", Allocated="", Remaining="", QuotaStatus="Unknown"),
        ])
        after = self.ingest([
            report_row(Model="A", Version="1", Unit="RPM", Lifecycle="Deprecated",
                       Limit="0", Allocated="0", Remaining="0"),
            report_row(Model="B", Version="2", Unit="RPM"),
        ])
        filters = {
            "model_version": [["OpenAI", "A", "1"], ["OpenAI", "B", "2"]],
            "availability": ["available"], "lifecycle": ["GenerallyAvailable"],
            "unit": ["RPM"], "minimum": "7",
        }
        for first, second in ((before, after), (after, before)):
            comparison = self.store.compare(first, second, filters)
            self.assertEqual(comparison["summary"]["changed"], 2)
            self.assertEqual(comparison["summary"]["added"], 0)
            self.assertEqual(comparison["summary"]["removed"], 0)
            self.assertTrue(all(row["before"] is not None and row["after"] is not None
                                for row in comparison["rows"]))

    def test_group_rollups_cover_all_filtered_rows_and_deduplicate_quota_pools(self):
        self.ingest([
            report_row(Model="A", Version="v1", Unit="RPM"),
            report_row(Model="A", Version="v1", Unit="RPM", Kind="AIServices"),
            report_row(Model="A", Version="v2", Unit="RPM"),
            report_row(Model="A", Version="v1", Unit="RPM", Region="westus"),
            report_row(Model="A", Version="v1", Unit="RPM", SubscriptionId="sub-b"),
            report_row(Model="B", Unit="PTU", QuotaName="zero", Limit="0", Allocated="0", Remaining="0"),
            report_row(Model="C", QuotaName="unknown", Limit="", Allocated="", Remaining="",
                       QuotaStatus="Unknown"),
            report_row(Model="D", QuotaName="", Limit="", Allocated="", Remaining="",
                       QuotaStatus="Unknown"),
            report_row(Model="claude-example", Unit="RPM"),
        ])
        families = self.store.groups({"group_by": "family", "page_size": "1"})
        self.assertEqual(set(families), {"rows", "total", "page", "page_size", "snapshot", "group_by", "summary"})
        self.assertEqual(families["total"], 2)
        self.assertEqual(families["summary"], self.store.inventory({})["summary"])
        self.assertEqual(families["summary"]["rows"], 8)
        self.assertEqual((families["summary"]["quota_pools"], families["summary"]["with_headroom"],
                          families["summary"]["zero_quota"], families["summary"]["unknown_quota"]),
                         (5, 3, 1, 2))
        all_families = self.store.groups({})["rows"]
        self.assertEqual(sum(row["quota_pools"] for row in all_families), 6)
        openai = next(row for row in all_families if row["label"] == "OpenAI")
        self.assertEqual(openai["filters"], {"family": ["OpenAI"]})
        self.assertEqual((openai["models"], openai["versions"], openai["regions"], openai["subscriptions"],
                          openai["entries"]), (4, 5, 2, 2, 7))
        models = self.store.groups({
            "family": ["OpenAI"], "group_by": "model", "page_size": "1",
            "sort": "entries", "direction": "desc",
        })
        self.assertEqual(models["total"], 4)
        self.assertEqual(models["summary"]["rows"], 7)
        self.assertEqual(models["rows"][0]["filters"], {"model": ["A"]})
        self.assertEqual((models["rows"][0]["entries"], models["rows"][0]["versions"],
                          models["rows"][0]["quota_pools"]), (4, 2, 3))
        variants = self.store.groups({"group_by": "model_version", "model": ["A"]})
        first = next(row for row in variants["rows"] if row["version"] == "v1")
        self.assertEqual(first["filters"], {"model_version": [["OpenAI", "A", "v1"]]})
        self.assertEqual(first["value"], '["OpenAI","A","v1"]')
        self.assertEqual((first["entries"], first["regions"], first["subscriptions"],
                          first["quota_pools"]), (3, 2, 2, 3))
        self.assertEqual(first["models"], 1)
        self.assertEqual(first["versions"], 1)

    def test_group_models_share_names_but_variants_keep_providers_and_drill_targets(self):
        self.ingest([
            report_row(Model="Shared", Version="v1"),
            report_row(Model="Shared", Version="v1", Format="OtherProvider"),
            report_row(Model="Single", Version=""),
        ])
        models = self.store.groups({"group_by": "model", "sort": "models", "direction": "desc"})
        shared = models["rows"][0]
        self.assertEqual(shared["label"], "Shared")
        self.assertEqual(shared["models"], 2)
        self.assertEqual(shared["versions"], 2)
        self.assertEqual(len(self.rows(**shared["filters"])), 2)
        variants = self.store.groups({"group_by": "model_version"})
        self.assertEqual(variants["total"], 3)
        for variant in variants["rows"]:
            rows = self.rows(**variant["filters"])
            self.assertEqual(len(rows), 1)
            self.assertEqual([rows[0][field] for field in ("format", "model", "version")],
                             json.loads(variant["value"]))
        missing = next(row for row in variants["rows"] if row["model"] == "Single")
        self.assertEqual(missing["label"], "Single (version not reported)")
        self.assertEqual(missing["version"], "")

    def test_group_shared_pool_unit_conflicts_stay_unknown(self):
        self.ingest([
            report_row(Model="A", Version="v1", Unit="RPM"),
            report_row(Model="A", Version="v2", Unit="PTU"),
        ])
        model = self.store.groups({"group_by": "model"})["rows"][0]
        self.assertEqual((model["quota_pools"], model["with_headroom"],
                          model["zero_quota"], model["unknown_quota"]), (1, 0, 0, 1))

    def test_group_sorting_pagination_and_chart_limit(self):
        self.ingest([report_row(Model=f"model-{index:04d}") for index in range(1005)])
        first = self.store.groups({"group_by": "model", "page_size": "1000"})
        second = self.store.groups({"group_by": "model", "page_size": "1000", "page": "2"})
        self.assertEqual((first["total"], len(first["rows"]), len(second["rows"])), (1005, 1000, 5))
        self.assertEqual(first["rows"][0]["label"], "model-0000")
        self.assertEqual(second["rows"][0]["label"], "model-1000")
        self.assertEqual(second["summary"], first["summary"])
        descending = self.store.groups({"group_by": "model", "sort": "model", "direction": "desc",
                                        "page_size": "2"})
        self.assertEqual([row["label"] for row in descending["rows"]], ["model-1004", "model-1003"])
        self.assertEqual(self.store.groups({"group_by": "model", "page": "10000"})["rows"], [])

    def test_group_numeric_sorts_are_numbers_and_have_deterministic_ties(self):
        self.ingest([
            report_row(Model="A", Version=str(index), Region=f"region-{index}", QuotaName=str(index),
                       SubscriptionId=f"sub-{index}")
            for index in range(12)
        ] + [report_row(Model="B"), report_row(Model="B", Format="OtherProvider"), report_row(Model="C")])
        for sort in ("models", "versions", "regions", "subscriptions", "entries", "with_headroom"):
            for direction in ("asc", "desc"):
                with self.subTest(sort=sort, direction=direction):
                    result = self.store.groups({"group_by": "model", "sort": sort, "direction": direction})
                    values = [row[sort] for row in result["rows"]]
                    self.assertEqual(values, sorted(values, reverse=direction == "desc"))
                    self.assertEqual(result, self.store.groups({
                        "group_by": "model", "sort": sort, "direction": direction,
                    }))

    def test_group_empty_snapshots_filters_and_invalid_controls(self):
        for group_by in ("family", "model", "model_version"):
            result = self.store.groups({"group_by": group_by})
            self.assertIsNone(result["snapshot"])
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["total"], 0)
            self.assertTrue(all(value == 0 for value in result["summary"].values()))
        self.assertEqual(self.store.facets()["model_version"], [])
        snapshot = self.ingest([status_row("Empty")])
        self.assertEqual(self.store.groups({})["snapshot"]["id"], snapshot)
        self.assertEqual(self.store.groups({})["total"], 0)
        self.ingest([report_row()])
        result = self.store.groups({"model": ["missing"], "group_by": "model_version"})
        self.assertEqual(result["total"], 0)
        self.assertTrue(all(value == 0 for value in result["summary"].values()))
        for filters in (
            {"group_by": "version"}, {"group_by": "family;DROP TABLE scans"},
            {"sort": "quota_limit"}, {"sort": "models DESC;DROP TABLE scans"},
            {"page_size": "1001"}, {"page_size": "0"}, {"page": "0"},
            {"direction": "backward"}, {"group_by": ["family"]}, {"page_size": ["1"]},
        ):
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    self.store.groups(filters)


if __name__ == "__main__":
    unittest.main()
