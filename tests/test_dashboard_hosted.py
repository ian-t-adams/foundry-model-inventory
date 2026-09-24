"""Offline hosted-mode tests: fake identity headers, fake clocks, no Azure calls."""

import base64
from contextlib import closing
import copy
import csv
from datetime import datetime, timedelta, timezone
import http.client
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest import mock
from uuid import uuid4

from dashboard import hosted
from dashboard.__main__ import main, parser
from dashboard.hosted import (
    DailyScheduler, DatabaseBackup, HostedCollector, apply_retention, apply_scope, authenticate,
    collect_once, create_hosted_server, load_settings, prune_runs, scheduled_slots,
)
from dashboard.server import _HTTPError
from dashboard.store import Store


ROOT = Path(__file__).resolve().parents[1]
TENANT = "10000000-0000-4000-8000-000000000001"
OTHER_TENANT = "10000000-0000-4000-8000-000000000002"
SUB_A = "20000000-0000-4000-8000-000000000001"
SUB_B = "20000000-0000-4000-8000-000000000002"
HOST = "inventory.example.test"
MAPPED_TENANT = "http://schemas.microsoft.com/identity/claims/tenantid"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CENTRAL = timezone(timedelta(hours=-5), "UTC-05:00")
FIELDS = [
    "Subscription", "SubscriptionId", "TenantId", "ScanStartedUtc", "Model", "Version",
    "Region", "Type", "SKU", "Catalog", "Lifecycle", "Limit", "Allocated", "Remaining",
    "Unit", "QuotaStatus", "QuotaName", "QuotaDescription", "Kind", "Format",
    "InferenceDeprecation", "SkuDeprecation", "Notes",
]


def principal(tenant=TENANT, claim=MAPPED_TENANT, name="Ada Example", urlsafe=False):
    payload = {
        "auth_typ": "aad",
        "claims": [{"typ": claim, "val": tenant}, {"typ": "name", "val": name}],
        "name_typ": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "role_typ": "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    }
    raw = json.dumps(payload).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    return encoded.decode("ascii")


def identity(tenant=TENANT, **options):
    return {
        "Host": HOST, "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": principal(tenant, **options),
        "X-MS-CLIENT-PRINCIPAL-NAME": "ada@example.test",
    }


def message(items):
    headers = http.client.HTTPMessage()
    for name, value in items:
        headers[name] = value
    return headers


def scope(**changes):
    value = {"tenant_id": TENANT, "subscriptions": [{"id": SUB_A, "name": "Fixture"}],
             "morning_time": "07:00"}
    value.update(changes)
    return value


def environment(**changes):
    value = {
        "FOUNDRY_INVENTORY_AUTH_TENANT_ID": TENANT,
        "FOUNDRY_INVENTORY_SCOPE": json.dumps(scope()),
        "WEBSITE_HOSTNAME": HOST,
        "FOUNDRY_INVENTORY_BACKUP_DIR": str(ROOT / "data" / "hosted-tests" / "backup"),
        "FOUNDRY_INVENTORY_COMMIT": COMMIT,
        "TZ": "UTC",
    }
    value.update(changes)
    return {key: item for key, item in value.items() if item is not None}


def snapshot(store, path, started_at=None):
    """Record one complete snapshot with a single inventory row."""
    scan = store.start_scan("scheduled", started_at=started_at)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        row = dict.fromkeys(FIELDS, "")
        row.update(Subscription="Fixture", SubscriptionId=SUB_A, TenantId=TENANT, Region="eastus",
                   Model="example-model", Version="1", SKU="GlobalStandard", Catalog="Listed",
                   Limit="10", Allocated="1", Remaining="9", Unit="1K TPM", QuotaStatus="Reported",
                   QuotaName="pool", Format="OpenAI", Kind="OpenAI", Lifecycle="GenerallyAvailable")
        writer.writerow(row)
    store.ingest_csvs(scan, [path])
    return scan


class Workspace(unittest.TestCase):
    def setUp(self):
        # Keep fixtures under the ignored data directory, never OS temp folders.
        self.workspace = ROOT / "data" / "hosted-tests" / uuid4().hex
        self.repo = self.workspace / "repo"
        self.data = self.repo / "data"
        self.data.mkdir(parents=True)
        for name in ("run-foundry-subscription-inventory.ps1", "check-foundry-model-availability.ps1"):
            (self.repo / name).write_text("# Mocked trusted script\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def settings(self, **changes):
        return load_settings(environment(
            FOUNDRY_INVENTORY_BACKUP_DIR=str(self.workspace / "home"), **changes
        ))


class AuthenticationTests(unittest.TestCase):
    def test_accepts_the_configured_tenant_with_mapped_or_short_claims(self):
        for claim in (MAPPED_TENANT, "tid"):
            with self.subTest(claim=claim):
                headers = message([
                    ("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                    ("X-MS-CLIENT-PRINCIPAL", principal(claim=claim)),
                    ("X-MS-CLIENT-PRINCIPAL-NAME", "ada@example.test"),
                ])
                self.assertEqual(authenticate(headers, TENANT), "ada@example.test")
        headers = message([("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                           ("X-MS-CLIENT-PRINCIPAL", principal(TENANT.upper(), urlsafe=True))])
        self.assertEqual(authenticate(headers, TENANT), "Ada Example")

    def test_missing_malformed_or_repeated_identity_is_unauthenticated(self):
        cases = {
            "missing": [],
            "provider only": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad")],
            "principal only": [("X-MS-CLIENT-PRINCIPAL", principal())],
            "repeated": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"), ("X-MS-CLIENT-PRINCIPAL", principal()),
                         ("X-MS-CLIENT-PRINCIPAL", principal())],
            "not base64": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"), ("X-MS-CLIENT-PRINCIPAL", "%%%")],
            "not json": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                         ("X-MS-CLIENT-PRINCIPAL", base64.b64encode(b"not json").decode())],
            "no claims": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                          ("X-MS-CLIENT-PRINCIPAL", base64.b64encode(b'{"claims": {}}').decode())],
            "oversized": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"), ("X-MS-CLIENT-PRINCIPAL", "A" * 40000)],
        }
        for label, items in cases.items():
            with self.subTest(label), self.assertRaises(_HTTPError) as caught:
                authenticate(message(items), TENANT)
            self.assertEqual(caught.exception.status, 401)

    def test_other_providers_and_tenants_are_forbidden(self):
        cases = {
            "provider": [("X-MS-CLIENT-PRINCIPAL-IDP", "github"), ("X-MS-CLIENT-PRINCIPAL", principal())],
            "tenant": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                       ("X-MS-CLIENT-PRINCIPAL", principal(OTHER_TENANT))],
            "no tenant": [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                          ("X-MS-CLIENT-PRINCIPAL", principal(claim="unrelated"))],
        }
        conflicting = json.dumps({"claims": [
            {"typ": "tid", "val": TENANT}, {"typ": MAPPED_TENANT, "val": OTHER_TENANT},
        ]}).encode()
        cases["conflicting tenants"] = [("X-MS-CLIENT-PRINCIPAL-IDP", "aad"),
                                        ("X-MS-CLIENT-PRINCIPAL", base64.b64encode(conflicting).decode())]
        for label, items in cases.items():
            with self.subTest(label), self.assertRaises(_HTTPError) as caught:
                authenticate(message(items), TENANT)
            self.assertEqual(caught.exception.status, 403)


class SettingsTests(Workspace):
    def test_valid_environment(self):
        settings = self.settings(
            WEBSITE_HOSTNAME="Inventory.Example.Test",
            FOUNDRY_INVENTORY_ALLOWED_HOSTS=" dashboard.example.test , localhost:18000,alias.example.test:443",
        )
        self.assertEqual(settings.tenant_id, TENANT)
        self.assertEqual(settings.allowed_hosts, frozenset({
            HOST, "dashboard.example.test", "localhost:18000", "alias.example.test",
        }))
        self.assertEqual(settings.scope["subscriptions"][0]["id"], SUB_A)
        self.assertEqual((settings.timezone_name, settings.zone), ("UTC", timezone.utc))
        self.assertEqual(settings.commit, COMMIT)
        self.assertEqual(settings.backup_dir, self.workspace / "home")
        self.assertEqual(settings.retention_days, 90)

    def test_commit_and_timezone_fall_back_or_fail_closed(self):
        self.assertEqual(self.settings(FOUNDRY_INVENTORY_COMMIT="main; rm -rf").commit, "unknown")
        self.assertEqual(self.settings(FOUNDRY_INVENTORY_COMMIT=None).commit, "unknown")
        self.assertEqual(self.settings(TZ=None).timezone_name, "UTC")
        with self.assertRaisesRegex(ValueError, "TZ must be an IANA time zone"):
            self.settings(TZ="Not/AZone")
        self.assertEqual(self.settings(FOUNDRY_INVENTORY_RETENTION_DAYS=" 30 ").retention_days, 30)
        self.assertEqual(self.settings(FOUNDRY_INVENTORY_RETENTION_DAYS="").retention_days, 90)

    def test_invalid_settings_are_rejected(self):
        cases = {
            "missing tenant": {"FOUNDRY_INVENTORY_AUTH_TENANT_ID": None},
            "empty tenant": {"FOUNDRY_INVENTORY_AUTH_TENANT_ID": "00000000-0000-0000-0000-000000000000"},
            "missing scope": {"FOUNDRY_INVENTORY_SCOPE": None},
            "invalid json": {"FOUNDRY_INVENTORY_SCOPE": "{"},
            "duplicate keys": {"FOUNDRY_INVENTORY_SCOPE": '{"tenant_id": "a", "tenant_id": "b"}'},
            "array": {"FOUNDRY_INVENTORY_SCOPE": "[]"},
            "profile field": {"FOUNDRY_INVENTORY_SCOPE": json.dumps(scope(azure_config_dir="/tmp/x"))},
            "tenant profiles": {"FOUNDRY_INVENTORY_SCOPE": json.dumps(scope(tenant_profiles={}))},
            "other tenant": {"FOUNDRY_INVENTORY_SCOPE": json.dumps(scope(subscriptions=[
                {"id": SUB_B, "name": "Elsewhere", "tenant_id": OTHER_TENANT}]))},
            "no host": {"WEBSITE_HOSTNAME": None},
            "bad host": {"FOUNDRY_INVENTORY_ALLOWED_HOSTS": "evil.example/test"},
            "relative backup": {"FOUNDRY_INVENTORY_BACKUP_DIR": "home/backup"},
            "retention too short": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "6"},
            "retention too long": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "3651"},
            "retention negative": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "-30"},
            "retention fraction": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "30.5"},
            "retention words": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "ninety"},
            "retention non-ASCII digits": {"FOUNDRY_INVENTORY_RETENTION_DAYS": "\u0663\u0660"},
        }
        for label, changes in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                load_settings(environment(**{
                    "FOUNDRY_INVENTORY_BACKUP_DIR": str(self.workspace / "home"), **changes,
                }))


class ScopeTests(Workspace):
    def setUp(self):
        super().setUp()
        self.store = Store(self.data / "inventory.sqlite3")
        self.addCleanup(self.store.close)
        self.collector = HostedCollector(self.store, self.repo, self.data)

    def test_environment_scope_is_validated_by_the_collector_and_saved(self):
        settings = self.settings(FOUNDRY_INVENTORY_SCOPE=json.dumps(scope(
            subscriptions=[{"id": SUB_A.upper(), "name": " Fixture ", "tenant_id": TENANT}],
            morning_time="06:30",
        )))
        config = apply_scope(self.collector, settings.scope)
        self.assertEqual(config, {
            "tenant_id": TENANT, "subscriptions": [{"id": SUB_A, "name": "Fixture"}],
            "morning_time": "06:30", "azure_config_dir": str(self.data / "azure-cli"),
        })
        self.assertTrue((self.data / "azure-cli").is_dir())
        self.assertEqual(self.collector.load_config(), config)

    def test_collector_rejects_invalid_values_from_the_environment(self):
        for label, value in {
            "no subscriptions": scope(subscriptions=[]),
            "bad subscription": scope(subscriptions=[{"id": "not-a-guid", "name": "x"}]),
            "duplicate": scope(subscriptions=[{"id": SUB_A, "name": "a"}, {"id": SUB_A, "name": "b"}]),
            "bad time": scope(morning_time="7am"),
            "extra subscription field": scope(subscriptions=[{"id": SUB_A, "name": "a", "state": "x"}]),
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                apply_scope(self.collector, hosted.parse_scope(json.dumps(value)))
        self.assertFalse(self.collector.config_path.exists())

    def test_profile_outside_the_data_directory_is_impossible(self):
        with self.assertRaises(ValueError):
            self.collector.save_config({**scope(), "azure_config_dir": str(self.workspace / "elsewhere")})


class SchedulerTests(unittest.TestCase):
    def test_slots_around_the_configured_local_time(self):
        before = datetime(2026, 9, 23, 11, 59, tzinfo=timezone.utc)  # 06:59 at UTC-5
        self.assertEqual(scheduled_slots(before, "07:00", CENTRAL), (
            datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        ))
        exact = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(scheduled_slots(exact, "07:00", CENTRAL)[0], exact)
        late = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)  # 23:30 local on Sept 23
        self.assertEqual(scheduled_slots(late, "07:00", CENTRAL)[1],
                         datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            scheduled_slots(datetime(2026, 9, 23), "07:00", CENTRAL)
        with self.assertRaises(ValueError):
            scheduled_slots(exact, "25:00", CENTRAL)

    def test_daylight_saving_keeps_the_local_wall_time(self):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("America/Chicago")
        except Exception:
            self.skipTest("IANA time zone data is not installed on this machine.")
        winter = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(scheduled_slots(winter, "07:00", zone)[1],
                         datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))

    def test_startup_collects_only_without_a_complete_snapshot_since_the_latest_slot(self):
        now = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)  # 10:00 local, slot at 07:00
        for latest, due in ((None, True), ("2026-09-23T11:59:00+00:00", True),
                            ("2026-09-23T12:00:00+00:00", False), ("2026-09-23T14:00:00Z", False)):
            with self.subTest(latest=latest):
                scheduler = DailyScheduler(lambda: None, lambda: latest, "07:00", CENTRAL,
                                           now=lambda: now)
                self.assertEqual(scheduler.startup_due(), due)
        self.assertEqual(scheduler.next_run(), "2026-09-24T07:00:00-05:00")

    def run_clock(self, start, *, latest=None, duration=timedelta(minutes=20), attempts=3, fail=False):
        clock = {"now": start}
        started = []

        def attempt():
            started.append(clock["now"])
            clock["now"] += duration
            if fail:
                raise RuntimeError("fixture failure")

        def wait(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 300)
            clock["now"] += timedelta(seconds=seconds)
            if len(started) >= attempts:
                scheduler.stop()
                return True
            return False

        scheduler = DailyScheduler(attempt, lambda: latest, "07:00", CENTRAL,
                                   now=lambda: clock["now"], wait=wait)
        worker = threading.Thread(target=scheduler.run, daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        return started

    def test_collects_at_startup_then_once_per_scheduled_time(self):
        start = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(self.run_clock(start), [
            start,
            datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        ])

    def test_waits_for_the_next_slot_when_a_fresh_snapshot_exists(self):
        start = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
        started = self.run_clock(start, latest="2026-09-23T12:05:00+00:00", attempts=1)
        self.assertEqual(started, [datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)])

    def test_a_long_collection_does_not_repeat_immediately_and_failures_continue(self):
        start = datetime(2026, 9, 23, 11, 50, tzinfo=timezone.utc)  # 06:50 local
        with self.assertLogs("dashboard.hosted", level="ERROR") as logs:
            started = self.run_clock(start, duration=timedelta(minutes=45), attempts=2, fail=True)
        self.assertEqual(started, [start, datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)])
        self.assertEqual(len(logs.records), 2)


class BackupTests(Workspace):
    def setUp(self):
        super().setUp()
        self.database = self.data / "inventory.sqlite3"
        self.home = self.workspace / "home"
        self.backup = DatabaseBackup(self.database, self.home, self.data / "backup-staging")

    def populate(self, message="first"):
        store = Store(self.database)
        try:
            scan = store.start_scan("scheduled")
            store.fail_scan(scan, message)
        finally:
            store.close()

    def messages(self, path):
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute("SELECT errors FROM scans ORDER BY id").fetchall()
        return [json.loads(row[0])[0]["message"] for row in rows]

    def test_backup_is_a_verified_rollback_journal_copy_that_restores(self):
        self.populate()
        self.assertTrue(self.backup.save())
        target = self.home / "inventory.sqlite3"
        header = target.read_bytes()[:100]
        self.assertEqual(header[:16], b"SQLite format 3\x00")
        self.assertEqual(header[18:20], b"\x01\x01", "the persisted copy must not need WAL files")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["inventory.sqlite3"])
        self.assertEqual(list((self.data / "backup-staging").iterdir()), [])
        state = self.backup.state()
        self.assertEqual(state["bytes"], target.stat().st_size)
        self.assertIsNone(state["error"])

        self.populate("second")
        self.assertTrue(self.backup.save())
        self.assertEqual(self.messages(target), ["first", "second"])

        fresh = self.workspace / "fresh" / "data"
        fresh.mkdir(parents=True)
        restored = DatabaseBackup(fresh / "inventory.sqlite3", self.home, fresh / "backup-staging")
        self.assertTrue(restored.restore_if_missing())
        self.assertEqual(self.messages(fresh / "inventory.sqlite3"), ["first", "second"])
        self.assertIsNotNone(restored.state()["restored_at"])
        store = Store(fresh / "inventory.sqlite3")
        try:
            self.assertEqual([scan["status"] for scan in store.scans()], ["failed", "failed"])
        finally:
            store.close()
        self.assertFalse(restored.restore_if_missing(), "an existing local database is kept")

    def test_restore_without_backup_starts_empty(self):
        self.assertFalse(self.backup.restore_if_missing())
        self.assertFalse(self.database.exists())
        self.assertIsNone(self.backup.state()["error"])

    def test_invalid_backup_is_set_aside_not_overwritten(self):
        self.home.mkdir()
        (self.home / "inventory.sqlite3").write_bytes(b"not a database" * 100)
        with self.assertLogs("dashboard.hosted", level="ERROR"):
            self.assertFalse(self.backup.restore_if_missing())
        kept = [path.name for path in self.home.iterdir()]
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].startswith("inventory.sqlite3.invalid-"))
        self.assertIn("invalid", self.backup.state()["error"])
        self.assertTrue(self.backup.enabled)
        self.assertFalse(self.database.exists())

    def test_unreadable_backup_pauses_backups(self):
        self.home.mkdir()
        (self.home / "inventory.sqlite3").write_bytes(b"x")
        with (mock.patch("dashboard.hosted._copy_file", side_effect=PermissionError("denied")),
              self.assertLogs("dashboard.hosted", level="ERROR")):
            self.assertFalse(self.backup.restore_if_missing())
        self.assertFalse(self.backup.enabled)
        self.populate()
        self.assertFalse(self.backup.save())
        self.assertEqual((self.home / "inventory.sqlite3").read_bytes(), b"x")

    def test_failed_backup_keeps_the_previous_copy(self):
        self.populate()
        self.assertTrue(self.backup.save())
        previous = (self.home / "inventory.sqlite3").read_bytes()
        self.populate("second")
        with (mock.patch("dashboard.hosted.os.replace", side_effect=OSError("share unavailable")),
              self.assertLogs("dashboard.hosted", level="ERROR")):
            self.assertFalse(self.backup.save())
        self.assertEqual((self.home / "inventory.sqlite3").read_bytes(), previous)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["inventory.sqlite3"])
        self.assertIn("share unavailable", self.backup.state()["error"])

    def test_backup_never_targets_the_live_database(self):
        with self.assertRaises(ValueError):
            DatabaseBackup(self.database, self.data, self.data / "staging")

    def test_prune_keeps_recent_run_diagnostics(self):
        runs = self.data / "runs"
        for scan_id in range(1, 11):
            (runs / str(scan_id)).mkdir(parents=True)
        (runs / "notes").mkdir()
        prune_runs(runs, keep=3)
        self.assertEqual(sorted(path.name for path in runs.iterdir()), ["10", "8", "9", "notes"])
        prune_runs(self.data / "missing")


class RetentionTests(Workspace):
    def test_snapshots_older_than_the_window_are_removed(self):
        store = Store(self.data / "inventory.sqlite3")
        self.addCleanup(store.close)
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        snapshot(store, self.workspace / "old.csv", (now - timedelta(days=91)).isoformat())
        failed = store.start_scan("scheduled", started_at=(now - timedelta(days=95)).isoformat())
        store.fail_scan(failed, "Sign-in failed.")
        kept = snapshot(store, self.workspace / "kept.csv", (now - timedelta(days=89)).isoformat())
        with self.assertLogs("dashboard.hosted", level="INFO") as logs:
            self.assertEqual(apply_retention(store, 90, now=lambda: now), 2)
        self.assertIn("Removed 2 snapshot(s) older than 90 days.", logs.output[0])
        self.assertEqual([scan["id"] for scan in store.scans()], [kept])
        self.assertEqual(apply_retention(store, 90, now=lambda: now + timedelta(days=400)), 0)
        self.assertEqual([scan["id"] for scan in store.scans()], [kept],
                         "the latest complete snapshot is kept even when it is older")

    def test_failures_are_logged_not_raised(self):
        for error in (sqlite3.OperationalError("database is locked"), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                store = mock.Mock()
                store.delete_snapshots_before.side_effect = error
                with self.assertLogs("dashboard.hosted", level="ERROR"):
                    self.assertEqual(apply_retention(store, 90), 0)
                store.close.assert_called_once_with()

    def test_each_collection_applies_retention_before_the_backup(self):
        calls = []
        collector = mock.Mock(store="store", data_dir=self.data)
        collector.run_scan.side_effect = RuntimeError("Sign-in failed.")
        backup = mock.Mock()
        backup.save.side_effect = lambda: calls.append("backup")
        with (mock.patch("dashboard.hosted.apply_retention",
                         side_effect=lambda store, days: calls.append(("retention", store, days))),
              self.assertLogs("dashboard.hosted", level="ERROR")):
            collect_once(collector, backup, 30)
        self.assertEqual(calls, [("retention", "store", 30), "backup"])


class FakeStore:
    def __init__(self):
        self.started, self.failed, self.ingested = [], [], []

    def start_scan(self, source, started_at=None):
        self.started.append(source)
        return len(self.started)

    def fail_scan(self, scan_id, message):
        self.failed.append((scan_id, message))

    def ingest_csvs(self, scan_id, paths, failures=None, expected_subscriptions=None):
        self.ingested.append((scan_id, [path.name for path in paths], failures))
        return {"id": scan_id, "status": "partial" if failures else "complete"}

    def close(self):
        pass


class HostedCollectorTests(Workspace):
    def setUp(self):
        super().setUp()
        self.store = FakeStore()
        self.collector = HostedCollector(self.store, self.repo, self.data)
        apply_scope(self.collector, scope())
        patcher = mock.patch.object(self.collector, "_powershell", return_value=str(Path(sys.executable)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commands = []

    def outputs(self, command):
        directory = Path(command[command.index("-OutputDirectory") + 1])
        subscription = command[command.index("-SubscriptionId") + 1]
        row = dict.fromkeys(FIELDS, "")
        row.update(Subscription="Fixture", SubscriptionId=subscription, TenantId=TENANT,
                   Model="example-model", Version="1", Region="eastus", SKU="GlobalStandard",
                   Catalog="Listed", QuotaStatus="Reported", Limit="10", Allocated="1", Remaining="9")
        with (directory / f"{subscription}.csv").open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerow(row)
        coverage = {"SubscriptionId": subscription, "Region": "eastus", "CatalogStatus": "Read",
                    "Rows": 1, "QuotaErrors": 0, "QuotaUnknown": 0}
        with (directory / f"{subscription}-coverage.csv").open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=list(coverage))
            writer.writeheader()
            writer.writerow(coverage)

    def respond(self, login_code=0):
        def run(command, timeout, env=None):
            self.commands.append((command, env))
            if command[1:3] == ["login", "--identity"]:
                return subprocess.CompletedProcess(command, login_code, "", "identity endpoint unavailable")
            self.outputs(command)
            return subprocess.CompletedProcess(command, 0, "{}", "")
        return run

    def test_signs_in_with_the_managed_identity_before_collecting(self):
        with (mock.patch("dashboard.hosted.shutil.which", return_value="/usr/bin/az"),
              mock.patch.object(self.collector, "_run_command", side_effect=self.respond())):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "complete")
        login, env = self.commands[0]
        self.assertEqual(login, ["/usr/bin/az", "login", "--identity", "--allow-no-subscriptions",
                                 "--output", "none", "--only-show-errors"])
        self.assertEqual(env["AZURE_CONFIG_DIR"], str(self.data / "azure-cli"))
        collect, collect_env = self.commands[1]
        self.assertIn(SUB_A, collect)
        self.assertEqual(collect_env["AZURE_CONFIG_DIR"], str(self.data / "azure-cli"))
        self.assertEqual(self.store.started, ["scheduled"])
        self.assertFalse(self.collector.state()["running"])

    def test_sign_in_failure_records_a_failed_scan_and_releases_the_lock(self):
        for which, code in (("/usr/bin/az", 1), (None, 0)):
            with self.subTest(az=which):
                with (mock.patch("dashboard.hosted.shutil.which", return_value=which),
                      mock.patch.object(self.collector, "_run_command", side_effect=self.respond(code))):
                    with self.assertRaisesRegex(RuntimeError, "managed identity"):
                        self.collector.run_scan()
                scan_id, recorded = self.store.failed[-1]
                self.assertEqual(scan_id, len(self.store.started))
                self.assertIn("system-assigned managed identity", recorded)
                self.assertIn("next scheduled collection", recorded)
                state = self.collector.state()
                self.assertFalse(state["running"])
                self.assertIn("managed identity", state["last_error"])
        self.assertEqual(self.store.ingested, [])

    def test_http_driven_operations_are_refused(self):
        with self.assertRaises(RuntimeError):
            self.collector.start_scan()
        with self.assertRaises(RuntimeError):
            self.collector.set_schedule(True, "07:00")
        with self.assertRaises(RuntimeError):
            self.collector.discover_subscriptions()

    def test_cli_failures_are_summarized_without_tracebacks(self):
        traceback = "\n".join([
            "ERROR: The command failed with an unexpected error. Here is the traceback:",
            "ERROR: HTTPConnectionPool(host='169.254.169.254', port=80): Connection refused",
            "Traceback (most recent call last):",
            '  File "/opt/az/lib/site-packages/requests/adapters.py", line 678, in send',
            "requests.exceptions.ConnectionError: HTTPConnectionPool(host='169.254.169.254')",
            "To check existing issues, please visit: https://github.com/Azure/azure-cli/issues",
        ])
        self.assertEqual(hosted._cli_error(traceback),
                         "HTTPConnectionPool(host='169.254.169.254', port=80): Connection refused")
        self.assertEqual(hosted._cli_error("Traceback (most recent call last):\n  File x\nValueError: bad\n"),
                         "ValueError: bad")
        self.assertEqual(hosted._cli_error("ERROR: Identity not found"), "Identity not found")
        self.assertEqual(hosted._cli_error(""), "No diagnostic output.")
        self.assertEqual(hosted._cli_error("ERROR: access_token=abc123 was rejected"),
                         "access_token=[REDACTED] was rejected")
        self.assertLessEqual(len(hosted._cli_error("ERROR: " + "x" * 2000)), 480)
        self.assertTrue(hosted._cli_error("ERROR: " + "x" * 2000).endswith("..."))


class FakeCollector:
    def __init__(self):
        self.config = {**scope(), "azure_config_dir": "/app/data/azure-cli"}
        self.calls = []

    def load_config(self):
        return copy.deepcopy(self.config)

    def state(self):
        return {"running": False, "scan_id": None, "message": "Idle",
                "progress": {"completed": 0, "total": 0, "subscription": None}, "last_error": None}

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"The hosted server must not call {name}.")
        return refuse


class FakeScheduler:
    def next_run(self):
        return "2026-09-24T07:00:00+00:00"


class HostedServerTests(Workspace):
    def setUp(self):
        super().setUp()
        self.static = self.workspace / "static"
        self.static.mkdir()
        for name, text in (("index.html", "<!doctype html><title>Hosted fixture</title>"),
                           ("app.js", "'use strict';"), ("styles.css", "body { color: black; }")):
            (self.static / name).write_text(text, encoding="utf-8")
        self.store = Store(self.data / "inventory.sqlite3")
        self.collector = FakeCollector()
        backup = DatabaseBackup(self.data / "inventory.sqlite3", self.workspace / "home", self.data / "staging")
        self.server = create_hosted_server(self.store, self.collector, self.static, self.settings(),
                                           FakeScheduler(), backup, port=0, bind_address="127.0.0.1")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.collector.calls, [])

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {"Host": HOST})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def error(self, response, status):
        code, headers, body = response
        self.assertEqual(code, status, body)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        return payload["error"]

    def ingest(self):
        snapshot(self.store, self.workspace / "fixture.csv")

    def test_binds_all_interfaces_by_default(self):
        self.assertEqual(inspect.signature(create_hosted_server).parameters["bind_address"].default, "0.0.0.0")
        self.assertEqual(inspect.signature(create_hosted_server).parameters["port"].default, 8000)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_every_route_requires_app_service_identity(self):
        for path in ("/", "/index.html", "/app.js", "/api/status", "/api/scans", "/api/inventory",
                     "/api/export.csv", "/missing"):
            with self.subTest(path=path):
                self.assertIn("Sign in", self.error(self.request("GET", path), 401))
        self.error(self.request("POST", "/api/scan", body=b"{}"), 401)
        headers = identity(OTHER_TENANT)
        self.assertIn("tenant", self.error(self.request("GET", "/api/status", headers), 403))

    def test_platform_ping_answers_without_identity_or_detail(self):
        for host in (HOST, "169.254.0.1:8000"):
            status, _, body = self.request("GET", "/robots933456.txt", {"Host": host})
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "Route not found."})

    def test_only_configured_hosts_are_accepted(self):
        headers = identity()
        for host in ("evil.example.test", f"127.0.0.1:{self.port}", "inventory.example.test.evil"):
            with self.subTest(host=host):
                self.error(self.request("GET", "/api/status", {**headers, "Host": host}), 403)
        self.assertEqual(self.request("GET", "/", {**headers, "Host": "INVENTORY.example.test:443"})[0], 200)

    def test_signed_in_tenant_reads_the_dashboard_and_hosted_status(self):
        self.ingest()
        status, headers, body = self.request("GET", "/", identity())
        self.assertEqual(status, 200)
        self.assertIn(b"Hosted fixture", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        status, _, body = self.request("GET", "/api/inventory?snapshot=latest", identity())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"][0]["model"], "example-model")
        status, _, body = self.request("GET", "/api/status", identity())
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["csrf_token"], "")
        self.assertNotIn("azure_config_dir", result["config"])
        self.assertTrue(result["configured"])
        hosted_status = result["hosted"]
        self.assertEqual(hosted_status["commit"], COMMIT)
        self.assertEqual(hosted_status["timezone"], "UTC")
        self.assertEqual(hosted_status["collection_time"], "07:00")
        self.assertEqual(hosted_status["next_run"], "2026-09-24T07:00:00+00:00")
        self.assertEqual(hosted_status["user"], "ada@example.test")
        self.assertEqual(hosted_status["last_attempt"]["status"], "complete")
        self.assertGreater(hosted_status["database_bytes"], 0)
        self.assertEqual(hosted_status["retention_days"], 90)
        self.assertTrue(hosted_status["backup"]["enabled"])
        self.assertEqual(result["schedule"]["time"], "07:00")
        self.assertIn("every day at 07:00 (UTC)", result["schedule"]["note"])

    def test_changes_and_discovery_are_refused(self):
        headers = {**identity(), "Content-Type": "application/json", "Origin": f"http://{HOST}"}
        for path in ("/api/scan", "/api/config", "/api/schedule", "/api/subscriptions"):
            with self.subTest(path=path):
                self.assertIn("read-only", self.error(self.request("POST", path, headers, b"{}"), 403))
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertIn("read-only", self.error(self.request(method, "/api/config", headers, b"{}"), 403))
        status, headers_out, body = self.request("HEAD", "/", identity())
        self.assertEqual((status, body), (405, b""))
        self.assertIn("GET", self.error(self.request("OPTIONS", "/", identity()), 405))
        self.assertIn("discovery", self.error(self.request("GET", "/api/subscriptions", identity()), 403))

    def test_cross_origin_requests_are_refused(self):
        headers = identity()
        self.error(self.request("GET", "/api/status", {**headers, "Origin": "https://evil.example.test"}), 403)
        self.error(self.request("GET", "/api/status", {**headers, "Sec-Fetch-Site": "cross-site"}), 403)
        self.error(self.request("GET", "/api/status", {**headers, "Origin": f"https://{HOST}"}), 403)
        forwarded = {**headers, "Origin": f"https://{HOST}", "X-Forwarded-Proto": "https"}
        self.assertEqual(self.request("GET", "/api/status", forwarded)[0], 200)

    def test_cross_site_navigation_opens_only_the_page(self):
        # After sign-in, the redirect back from Microsoft Entra reaches the page as cross-site.
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                      "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1"}
        same_site = {**navigation, "Sec-Fetch-Site": "same-site"}
        for path, headers in (("/", navigation), ("/index.html", navigation),
                              ("/?view=quota", navigation), ("/", same_site)):
            with self.subTest(path=path, site=headers["Sec-Fetch-Site"]):
                status, response_headers, body = self.request("GET", path, {**identity(), **headers})
                self.assertEqual(status, 200, body)
                self.assertIn(b"Hosted fixture", body)
                self.assertEqual(response_headers["X-Frame-Options"], "DENY")
        refused = (
            ("/api/status", navigation), ("/app.js", navigation), ("/api/status", same_site),
            ("/", {**navigation, "Sec-Fetch-Dest": "iframe"}),
            ("/", {**navigation, "Sec-Fetch-Dest": "embed"}),
            ("/", {**navigation, "Sec-Fetch-Mode": "no-cors"}),
            ("/", {**navigation, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}),
        )
        for path, headers in refused:
            with self.subTest(path=path, headers=headers):
                self.assertIn("Cross-site", self.error(self.request("GET", path, {**identity(), **headers}), 403))
        self.assertEqual(self.request("HEAD", "/", {**identity(), **navigation})[0], 403)
        self.assertIn("Sign in", self.error(self.request("GET", "/", {"Host": HOST, **navigation}), 401))


class CommandLineTests(unittest.TestCase):
    def test_hosted_command_defaults_and_port_validation(self):
        args = parser().parse_args(["hosted"])
        self.assertEqual((args.command, args.port), ("hosted", 8000))
        with mock.patch("dashboard.hosted.run", return_value=0) as run:
            self.assertEqual(main(["hosted", "--port", "8000"]), 0)
        root, data_dir = run.call_args.args
        self.assertEqual(data_dir, root / "data")
        self.assertEqual(run.call_args.kwargs, {"port": 8000})
        with mock.patch("dashboard.hosted.run") as run, self.assertLogs(level="ERROR"):
            self.assertEqual(main(["hosted", "--port", "80"]), 1)
        run.assert_not_called()

    def test_hosted_command_fails_closed_without_settings(self):
        with (mock.patch.dict("os.environ", {"FOUNDRY_INVENTORY_AUTH_TENANT_ID": ""}),
              mock.patch("dashboard.hosted.Store") as store, self.assertLogs(level="ERROR")):
            self.assertEqual(main(["hosted"]), 1)
        store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
