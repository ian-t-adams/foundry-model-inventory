"""Isolated collector/scheduler tests: never contact Azure or change real tasks."""

import base64
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from dashboard.collector import Collector, TASK_NAME, _FileLock, _atomic_json


ROOT = Path(__file__).resolve().parents[1]
TENANT = "10000000-0000-4000-8000-000000000001"
SUB_A = "20000000-0000-4000-8000-000000000001"
SUB_B = "20000000-0000-4000-8000-000000000002"
FIELDS = [
    "Subscription", "SubscriptionId", "TenantId", "ScanStartedUtc", "Model", "Version",
    "Region", "Type", "SKU", "Catalog", "Lifecycle", "Limit", "Allocated", "Remaining",
    "Unit", "QuotaStatus", "QuotaName", "QuotaDescription", "Kind", "Format",
    "InferenceDeprecation", "SkuDeprecation", "Notes",
]


class FakeStore:
    def __init__(self):
        self.started = []
        self.ingested = []
        self.failed = []

    def start_scan(self, source, started_at=None):
        self.started.append((source, started_at))
        return len(self.started)

    def ingest_csvs(self, scan_id, paths, failures=None, expected_subscriptions=None):
        self.ingested.append((scan_id, paths, failures, expected_subscriptions))
        rows = []
        for path in paths:
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows.extend(csv.DictReader(source))
        errors = bool(failures) or any(
            row["Catalog"] == "ERROR" or row["QuotaStatus"] == "ERROR" for row in rows
        )
        status = "failed" if not rows else "partial" if errors else "complete"
        return {"id": scan_id, "status": status, "record_count": len(rows), "errors": failures or []}

    def fail_scan(self, scan_id, message):
        self.failed.append((scan_id, message))


class CollectorFixture(unittest.TestCase):
    def setUp(self):
        # Never use OS temporary directories, including on Windows.
        self.workspace = ROOT / "data" / "collector-tests" / uuid4().hex
        self.repo = self.workspace / "fixture repo & space"
        self.data = self.repo / "data"
        self.data.mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        for relative in (
            "run-foundry-subscription-inventory.ps1",
            "check-foundry-model-availability.ps1",
            str(Path("scripts") / "register-morning-scan.ps1"),
            str(Path("scripts") / "run-morning-scan.ps1"),
        ):
            (self.repo / relative).write_text("# Mocked trusted script\n", encoding="utf-8")
        self.store = FakeStore()
        self.collector = Collector(self.store, self.repo, self.data)
        self.ps_patch = mock.patch.object(
            self.collector, "_powershell", return_value=str(Path(sys.executable).resolve())
        )
        self.ps_patch.start()
        self.addCleanup(self.ps_patch.stop)
        self.addCleanup(self.cleanup_workspace)

    def cleanup_workspace(self):
        if self.collector._thread is not None:
            self.collector._thread.join(timeout=5)
            if self.collector._thread.is_alive():
                raise AssertionError("A collector test left a worker thread running.")
        shutil.rmtree(self.workspace)

    def config(self, second=False):
        subscriptions = [{"id": SUB_A, "name": "Production 日本語"}]
        if second:
            subscriptions.append({"id": SUB_B, "name": "Research; this is never command text"})
        return {"tenant_id": TENANT, "subscriptions": subscriptions, "morning_time": "07:00"}

    def configure(self, second=False):
        return self.collector.save_config(self.config(second))

    def outputs(self, command, *, catalog="Listed", quota="Reported", tenant=TENANT):
        directory = Path(command[command.index("-OutputDirectory") + 1])
        subscription = command[command.index("-SubscriptionId") + 1]
        row = dict.fromkeys(FIELDS, "")
        row.update(
            Subscription="Mock subscription", SubscriptionId=subscription, TenantId=tenant,
            ScanStartedUtc="2026-09-19T12:00:00+00:00", Model="example-model", Version="1",
            Region="eastus", Type="Global", SKU="GlobalStandard", Catalog=catalog,
            QuotaStatus=quota, Limit="100", Allocated="20", Remaining="80", Unit="1K TPM",
        )
        path = directory / f"{subscription}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerow(row)
        coverage = {
            "Subscription": "Mock subscription", "SubscriptionId": subscription, "Region": "eastus",
            "CatalogStatus": "ERROR" if catalog == "ERROR" else "Empty" if catalog == "Empty" else "Read",
            "Rows": 1, "QuotaErrors": int(quota == "ERROR"), "QuotaUnknown": int(quota == "Unknown"), "Notes": "",
        }
        with (directory / f"{subscription}-coverage.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as target:
            writer = csv.DictWriter(target, fieldnames=list(coverage))
            writer.writeheader()
            writer.writerow(coverage)
        return path

    def success(self, command, timeout, env=None):
        self.outputs(command)
        return subprocess.CompletedProcess(command, 0, '{"Rows": 1}', "")

    def schedule(self, enabled=True, clock="07:00"):
        return {
            "enabled": enabled, "time": clock, "task_name": TASK_NAME,
            "next_run": None, "last_run": None, "last_result": None,
            "note": "You must be signed into Windows; Azure CLI authentication must be valid.",
        }

    def active_status(self, scan_id=9):
        return {
            "running": True, "scan_id": scan_id, "pid": os.getpid(),
            "message": "Collecting.", "last_error": None,
            "progress": {"completed": 1, "total": 3, "subscription": SUB_A},
        }


class CollectorCase(CollectorFixture):
    def test_unconfigured_default_does_not_write_config(self):
        self.assertEqual(self.collector.load_config(), {
            "tenant_id": "", "subscriptions": [], "morning_time": "07:00",
        })
        self.assertFalse(self.collector.config_path.exists())
        with self.assertRaisesRegex(ValueError, "Configure"):
            self.collector.run_scan()
        self.assertFalse(self.store.started)
        with _FileLock(self.collector.lock_path):
            pass

    def test_config_is_normalized_and_utf8_atomic(self):
        config = self.config()
        config["tenant_id"] = TENANT.upper()
        config["subscriptions"][0]["id"] = SUB_A.upper()
        result = self.collector.save_config(config)
        self.assertEqual(result, self.config())
        self.assertEqual(self.collector.load_config(), result)
        content = self.collector.config_path.read_bytes()
        self.assertIn("日本語".encode("utf-8"), content)
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(list(self.data.glob("*.new")), [])

    def test_config_rejects_empty_duplicate_invalid_and_command_fields(self):
        invalid = [
            {},
            {"tenant_id": TENANT, "subscriptions": []},
            {"tenant_id": TENANT, "subscriptions": "all"},
            {**self.config(), "tenant_id": TENANT + ";whoami"},
            {**self.config(), "tenant_id": "00000000-0000-0000-0000-000000000000"},
            {**self.config(), "subscriptions": [{"id": SUB_A + "&whoami"}]},
            {**self.config(), "subscriptions": [{"id": SUB_A}, {"id": SUB_A.upper()}]},
            {**self.config(), "subscriptions": [{"id": SUB_A, "name": "\n"}]},
            {**self.config(), "subscriptions": [{"id": SUB_A, "script": "untrusted.ps1"}]},
            {**self.config(), "command": "whoami"},
            {**self.config(), "access_token": "must-not-be-stored"},
            {**self.config(), "morning_time": "7:00"},
            {**self.config(), "morning_time": "24:00"},
            {**self.config(), "morning_time": "07:00\n"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.collector.save_config(payload)
        self.assertFalse(self.collector.config_path.exists())

    def test_atomic_write_failure_preserves_previous_config_and_cleans_staging(self):
        original = self.configure()
        changed = {**original, "morning_time": "06:30"}
        with mock.patch("dashboard.collector.os.replace", side_effect=OSError("disk write denied")):
            with self.assertRaisesRegex(OSError, "disk write denied"):
                self.collector.save_config(changed)
        self.assertEqual(self.collector.load_config(), original)
        self.assertFalse(list(self.data.glob("*.new")))
        self.collector.save_config(changed)
        self.assertEqual(self.collector.load_config(), changed)

    def test_malformed_config_fails_explicitly_and_releases_scan_lock(self):
        self.collector.config_path.write_text('{"tenant_id": ', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Malformed config.json"):
            self.collector.load_config()
        with self.assertRaisesRegex(ValueError, "Malformed config.json"):
            self.collector.run_scan()
        with _FileLock(self.collector.lock_path):
            pass
        self.assertFalse(self.store.started)

    def test_data_cannot_escape_ignored_directory(self):
        for directory in (self.repo, self.repo / "reports", self.data / ".." / "outside"):
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "data directory"):
                Collector(self.store, self.repo, directory)

    def test_configuration_cannot_change_while_lock_is_held(self):
        self.configure()
        with _FileLock(self.collector.lock_path):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.collector.save_config(self.config())
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.collector.run_scan()

    def test_file_lock_releases_on_exception(self):
        with self.assertRaisesRegex(ValueError, "expected"):
            with _FileLock(self.collector.lock_path):
                raise ValueError("expected")
        with _FileLock(self.collector.lock_path):
            pass

    def test_cross_process_lock_is_released_when_process_dies(self):
        ready = self.workspace / "lock-ready"
        child_script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from dashboard.collector import _FileLock\n"
            "lock = _FileLock(Path(sys.argv[1])).acquire()\n"
            "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", child_script, str(self.collector.lock_path), str(ready)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "The child could not acquire the test lock.")
            _atomic_json(self.collector.status_path, self.active_status())
            self.assertTrue(self.collector.state()["running"])
            with self.assertRaisesRegex(RuntimeError, "already running"):
                _FileLock(self.collector.lock_path).acquire()
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        with _FileLock(self.collector.lock_path):
            pass
        self.assertFalse(self.collector.state()["running"])

    def test_stale_status_is_read_only_and_recovered_before_new_scan(self):
        self.configure()
        _atomic_json(self.collector.status_path, self.active_status())
        state = self.collector.state()
        self.assertFalse(state["running"])
        self.assertIn("interrupted", state["message"])
        self.assertTrue(json.loads(self.collector.status_path.read_text())["running"])
        self.assertFalse(self.store.failed)
        with mock.patch.object(self.collector, "_run_command", side_effect=self.success):
            self.collector.run_scan()
        self.assertEqual(self.store.failed[0][0], 9)
        self.assertIn("exited", self.store.failed[0][1])
        self.assertFalse(self.collector.state()["running"])

    def test_stale_status_after_a_committed_scan_does_not_mutate_terminal_history(self):
        self.configure()
        _atomic_json(self.collector.status_path, self.active_status())
        with (
            mock.patch.object(self.store, "scans", return_value=[{"id": 9, "status": "complete"}], create=True),
            mock.patch.object(self.store, "fail_scan", side_effect=ValueError("Terminal scans are immutable")) as fail,
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
        ):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "complete")
        fail.assert_not_called()
        self.assertFalse(self.collector.state()["running"])

    def test_status_write_failure_after_ingestion_preserves_committed_scan_and_allows_retry(self):
        self.configure()
        write_status = self.collector._write_status

        def fail_completion_write(status):
            if not status["running"]:
                raise OSError("Completion status file is unavailable")
            write_status(status)

        with (
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
            mock.patch.object(self.collector, "_write_status", side_effect=fail_completion_write),
            mock.patch.object(self.store, "fail_scan", side_effect=ValueError("Terminal scans are immutable")) as fail,
            self.assertRaisesRegex(RuntimeError, "Completion status file is unavailable"),
        ):
            self.collector.run_scan()
        fail.assert_not_called()
        self.assertEqual(len(self.store.ingested), 1)
        self.assertFalse(self.collector.state()["running"])
        with (
            mock.patch.object(self.store, "scans", return_value=[{"id": 1, "status": "complete"}], create=True),
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
        ):
            self.assertEqual(self.collector.run_scan()["status"], "complete")
        self.assertFalse(self.store.failed)

    def test_optional_store_connection_is_closed_in_the_worker_thread(self):
        self.configure()
        closed_threads = []
        with (
            mock.patch.object(
                self.store, "close", side_effect=lambda: closed_threads.append(threading.get_ident()), create=True,
            ),
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
        ):
            self.collector.start_scan()
            self.collector._thread.join(timeout=5)
            self.assertFalse(self.collector._thread.is_alive())
        self.assertEqual(closed_threads, [self.collector._thread.ident])
        self.assertFalse(self.collector.state()["running"])

    def test_optional_store_cleanup_error_still_releases_cross_process_lock(self):
        self.configure()
        with (
            mock.patch.object(self.store, "close", side_effect=RuntimeError("Connection close failed"), create=True),
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
            self.assertRaisesRegex(RuntimeError, "Connection close failed"),
        ):
            self.collector.run_scan()
        with _FileLock(self.collector.lock_path):
            pass
        self.assertFalse(self.collector.state()["running"])

    def test_malformed_status_is_not_silently_ignored(self):
        self.collector.status_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Malformed collection-status"):
            self.collector.state()

    def test_discovery_uses_fixed_powershell_wrapper_for_az_cmd(self):
        az_path = self.repo / "az & no interpolation.cmd"
        payload = [{
            "id": SUB_A, "name": "Subscription", "tenantId": TENANT, "state": "Enabled",
            "user": {"name": "not-returned"}, "accessToken": "not-returned",
            "isDefault": True,
        }]
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with (
            mock.patch("dashboard.collector.shutil.which", return_value=str(az_path)),
            mock.patch.object(self.collector, "_run_command", return_value=response) as command,
        ):
            result = self.collector.discover_subscriptions()
        self.assertEqual(result, [{
            "id": SUB_A, "name": "Subscription", "tenant_id": TENANT, "state": "Enabled",
        }])
        argv, timeout, env = command.call_args.args
        self.assertEqual(timeout, 60)
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertIn("-NonInteractive", argv)
        code = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertIn("account list --all --output json --only-show-errors", code)
        self.assertNotIn(str(az_path), code)
        self.assertEqual(env["FOUNDRY_INVENTORY_AZ_CLI"], str(az_path))
        self.assertNotIn("account set", code)
        self.assertNotIn("get-access-token", code)

    def test_discovery_reports_unknown_auth_failure_without_secrets(self):
        response = subprocess.CompletedProcess(
            [], 1, "", "Unrecognized authentication failure: password=synthetic-private access_token=synthetic-token"
        )
        with (
            mock.patch("dashboard.collector.shutil.which", return_value=str(self.repo / "az.cmd")),
            mock.patch.object(self.collector, "_run_command", return_value=response),
            self.assertRaisesRegex(RuntimeError, "authentication failure") as error,
        ):
            self.collector.discover_subscriptions()
        self.assertNotIn("synthetic-private", str(error.exception))
        self.assertNotIn("synthetic-token", str(error.exception))

    def test_discovery_rejects_unexpected_success_schema(self):
        for payload in ({}, [None], [{"id": SUB_A}], [{"id": SUB_A, "tenantId": TENANT}]):
            with (
                self.subTest(payload=payload),
                mock.patch("dashboard.collector.shutil.which", return_value=str(self.repo / "az.cmd")),
                mock.patch.object(
                    self.collector, "_run_command",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
                ),
                self.assertRaises(RuntimeError),
            ):
                self.collector.discover_subscriptions()

    def test_discovery_missing_cli_is_explicit(self):
        with mock.patch("dashboard.collector.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Azure CLI is unavailable"):
                self.collector.discover_subscriptions()

    def test_successful_scan_only_invokes_trusted_fixed_commands(self):
        self.configure(second=True)
        with mock.patch.object(self.collector, "_run_command", side_effect=self.success) as execute:
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.started[0][0], "scheduled")
        self.assertIsNotNone(self.store.started[0][1])
        self.assertEqual(execute.call_count, 2)
        for call, subscription in zip(execute.call_args_list, (SUB_A, SUB_B)):
            argv, timeout = call.args
            self.assertEqual(argv[:8], [
                str(Path(sys.executable).resolve()), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File",
                str(self.repo / "run-foundry-subscription-inventory.ps1"),
            ])
            self.assertEqual(argv[8:], [
                "-TenantId", TENANT, "-SubscriptionId", subscription,
                "-ReportScript", str(self.repo / "check-foundry-model-availability.ps1"),
                "-OutputDirectory", str(self.data / "runs" / "1"),
            ])
            self.assertLessEqual(timeout, 50 * 60)
            self.assertNotIn(self.config(True)["subscriptions"][1]["name"], argv)
        self.assertEqual(self.store.ingested[0][3], [SUB_A, SUB_B])
        self.assertEqual(self.store.ingested[0][2], [])
        self.assertEqual(self.collector.state()["progress"]["completed"], 2)
        self.assertIsNone(self.collector.state()["last_error"])
        self.assertTrue((self.data / "runs" / "1" / f"{SUB_A}.stdout.log").exists())

    def test_source_label_is_metadata_not_command_text(self):
        self.configure()
        with mock.patch.object(self.collector, "_run_command", side_effect=self.success) as execute:
            self.collector.run_scan(source="integration")
        self.assertEqual(self.store.started[0][0], "integration")
        self.assertNotIn("integration", execute.call_args.args[0])
        for source in ("", None, "manual\n", "x" * 65):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.collector.run_scan(source=source)

    def test_async_scan_reserves_lock_before_return_and_reports_external_progress(self):
        self.configure()
        entered = threading.Event()
        finish = threading.Event()

        def delayed(command, timeout, env=None):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("Test did not release collector.")
            return self.success(command, timeout, env)

        try:
            with mock.patch.object(self.collector, "_run_command", side_effect=delayed):
                initial = self.collector.start_scan()
                self.assertTrue(initial["running"])
                self.assertEqual(initial["scan_id"], 1)
                self.assertTrue(entered.wait(5))
                external = Collector(self.store, self.repo, self.data)
                self.assertTrue(external.state()["running"])
                self.assertEqual(external.state()["progress"]["subscription"], SUB_A)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    external.start_scan()
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    external.save_config(self.config())
                finish.set()
                self.collector._thread.join(timeout=5)
                self.assertFalse(self.collector._thread.is_alive())
        finally:
            finish.set()
        self.assertEqual(self.store.started[0][0], "manual")
        self.assertFalse(self.collector.state()["running"])
        self.assertEqual(len(self.store.ingested), 1)

    def test_thread_start_failure_records_error_and_releases_lock(self):
        self.configure()
        with mock.patch("dashboard.collector.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                self.collector.start_scan()
        self.collector._thread = None
        self.assertEqual(self.store.failed[0][0], 1)
        self.assertFalse(self.collector.state()["running"])
        with _FileLock(self.collector.lock_path):
            pass

    def test_nonzero_exit_still_ingests_valid_partial_files_and_failure(self):
        self.configure()

        def partial(command, timeout, env=None):
            self.outputs(command)
            return subprocess.CompletedProcess(command, 2, "", "Coverage generation failed")

        with mock.patch.object(self.collector, "_run_command", side_effect=partial):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(self.store.ingested[0][1]), 1)
        failure = self.store.ingested[0][2][0]
        self.assertEqual(failure["subscription_id"], SUB_A)
        self.assertIn("exited 2", failure["message"])
        self.assertIn("Coverage generation failed", self.collector.state()["last_error"])

    def test_zero_exit_without_inventory_is_failed_not_complete(self):
        self.configure()
        with mock.patch.object(
            self.collector, "_run_command", return_value=subprocess.CompletedProcess([], 0, "{}", "")
        ):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.ingested[0][1], [])
        self.assertIn("Inventory CSV", self.store.ingested[0][2][0]["message"])

    def test_malformed_subscription_does_not_discard_healthy_subscription(self):
        self.configure(second=True)

        def partial(command, timeout, env=None):
            path = self.outputs(command)
            if command[command.index("-SubscriptionId") + 1] == SUB_B:
                path.write_text('"SubscriptionId","Region"\n"unfinished', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with mock.patch.object(self.collector, "_run_command", side_effect=partial):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual([path.name for path in self.store.ingested[0][1]], [f"{SUB_A}.csv"])
        self.assertEqual(self.store.ingested[0][2][0]["subscription_id"], SUB_B)
        self.assertEqual(self.store.ingested[0][3], [SUB_A, SUB_B])

    def test_wrong_tenant_in_output_is_never_ingested(self):
        self.configure()

        def wrong_scope(command, timeout, env=None):
            self.outputs(command, tenant=SUB_B)
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with mock.patch.object(self.collector, "_run_command", side_effect=wrong_scope):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.ingested[0][1], [])
        self.assertIn("scope", self.store.ingested[0][2][0]["message"])

    def test_timeout_ingests_existing_partial_csv_and_logs_redacted_diagnostics(self):
        self.configure()

        def timeout(command, seconds, env=None):
            self.outputs(command)
            raise subprocess.TimeoutExpired(command, seconds, "access_token=synthetic-token", "password=synthetic-secret")

        with mock.patch.object(self.collector, "_run_command", side_effect=timeout):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertIn("timed out", self.collector.state()["last_error"])
        for log in (self.data / "runs" / "1").glob("*.log"):
            content = log.read_text(encoding="utf-8")
            self.assertNotIn("synthetic-token", content)
            self.assertNotIn("synthetic-secret", content)

    def test_catalog_and_quota_error_rows_are_not_successful_even_with_exit_zero(self):
        self.configure()

        def catalog_error(command, timeout, env=None):
            self.outputs(command, catalog="ERROR", quota="ERROR")
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with mock.patch.object(self.collector, "_run_command", side_effect=catalog_error):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.ingested[0][2], [])
        self.assertIsNotNone(self.collector.state()["last_error"])

    def test_unknown_quota_does_not_become_a_failure_or_fake_zero(self):
        self.configure()

        def unknown(command, timeout, env=None):
            self.outputs(command, quota="Unknown")
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with mock.patch.object(self.collector, "_run_command", side_effect=unknown):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.ingested[0][2], [])

    def test_missing_coverage_marks_subscription_failure_but_keeps_csv(self):
        self.configure()

        def no_coverage(command, timeout, env=None):
            path = self.outputs(command)
            path.with_name(f"{SUB_A}-coverage.csv").unlink()
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with mock.patch.object(self.collector, "_run_command", side_effect=no_coverage):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(self.store.ingested[0][1]), 1)
        self.assertIn("Coverage CSV", self.store.ingested[0][2][0]["message"])

    def test_failure_text_is_bounded_and_credentials_are_redacted_in_logs(self):
        self.configure()

        def failure(command, timeout, env=None):
            directory = Path(command[command.index("-OutputDirectory") + 1])
            (directory / f"{SUB_A}.log").write_text(
                "Authorization: Bearer synthetic-private\nclient_secret='other-private'",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command, 1, "", "x" * 12000 + "\nAuthentication failed password=synthetic-secret"
            )

        with mock.patch.object(self.collector, "_run_command", side_effect=failure):
            result = self.collector.run_scan()
        self.assertEqual(result["status"], "failed")
        self.assertLess(len(self.collector.state()["last_error"]), 1450)
        for log in (self.data / "runs" / "1").glob("*.log"):
            content = log.read_text(encoding="utf-8")
            for secret in ("synthetic-private", "other-private", "synthetic-secret"):
                self.assertNotIn(secret, content)

    def test_ingestion_failure_is_recorded_and_lock_released(self):
        self.configure()
        with (
            mock.patch.object(self.collector, "_run_command", side_effect=self.success),
            mock.patch.object(self.store, "ingest_csvs", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            self.collector.run_scan()
        self.assertEqual(self.store.failed, [(1, "database unavailable")])
        self.assertFalse(self.collector.state()["running"])
        with _FileLock(self.collector.lock_path):
            pass

    def test_missing_trusted_script_fails_explicitly(self):
        self.configure()
        (self.repo / "run-foundry-subscription-inventory.ps1").unlink()
        with self.assertRaisesRegex(RuntimeError, "checked-in script"):
            self.collector.run_scan()
        self.assertTrue(self.store.failed)
        self.assertFalse(self.collector.state()["running"])

    def test_overall_deadline_records_every_unobserved_subscription(self):
        self.configure(second=True)
        with (
            mock.patch("dashboard.collector.SCAN_TIMEOUT", 0),
            mock.patch.object(self.collector, "_run_command") as execute,
        ):
            result = self.collector.run_scan()
        execute.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual([item["subscription_id"] for item in self.store.ingested[0][2]], [SUB_A, SUB_B])

    def test_schedule_enable_is_fixed_and_config_time_updates_only_after_success(self):
        self.configure()
        response = subprocess.CompletedProcess([], 0, json.dumps(self.schedule(clock="06:30")), "")
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command", return_value=response) as execute,
        ):
            result = self.collector.set_schedule(True, "06:30")
        self.assertTrue(result["enabled"])
        self.assertEqual(self.collector.load_config()["morning_time"], "06:30")
        argv, timeout, env = execute.call_args.args
        self.assertEqual(timeout, 60)
        self.assertIsNone(env)
        self.assertEqual(argv[7], str(self.repo / "scripts" / "register-morning-scan.ps1"))
        self.assertEqual(argv[8:], [
            "-Enabled", "-Time", "06:30", "-PythonPath", str(Path(sys.executable).resolve()),
            "-RepoRoot", str(self.repo), "-DataDirectory", str(self.data),
        ])

    def test_schedule_disable_does_not_need_config(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.schedule(enabled=False)), "")
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command", return_value=response) as execute,
        ):
            result = self.collector.set_schedule(False, "07:00")
        self.assertFalse(result["enabled"])
        self.assertEqual(execute.call_args.args[0][8:], ["-Disable", "-Time", "07:00"])
        self.assertFalse(self.collector.config_path.exists())

    def test_schedule_rejects_injection_and_non_boolean_enabled(self):
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command") as execute,
        ):
            for value in ("07:00;whoami", "07:00\n", "7:00", "24:00", "12:60", None):
                with self.subTest(time=value), self.assertRaises(ValueError):
                    self.collector.set_schedule(True, value)
            for value in ("true", 1, [], None):
                with self.subTest(enabled=value), self.assertRaises(ValueError):
                    self.collector.set_schedule(value, "07:00")
        execute.assert_not_called()

    def test_schedule_command_rejects_unknown_operations_before_building_process(self):
        with (
            mock.patch.object(self.collector, "_ps_file") as script,
            self.assertRaisesRegex(ValueError, "Unsupported scheduling operation"),
        ):
            self.collector._schedule_command("-Status -Enabled", "07:00")
        script.assert_not_called()

    def test_schedule_mutation_is_rejected_mid_scan(self):
        self.configure()
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command") as execute,
            _FileLock(self.collector.lock_path),
            self.assertRaisesRegex(RuntimeError, "already running"),
        ):
            self.collector.set_schedule(True, "07:00")
        execute.assert_not_called()

    def test_schedule_failures_are_visible_and_preserve_config(self):
        self.configure()
        response = subprocess.CompletedProcess([], 1, "", "Access is denied")
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command", return_value=response),
        ):
            with self.assertRaisesRegex(RuntimeError, "Access is denied"):
                self.collector.set_schedule(True, "06:30")
            with self.assertRaisesRegex(RuntimeError, "Access is denied"):
                self.collector.schedule_status()
        self.assertEqual(self.collector.load_config()["morning_time"], "07:00")

    def test_schedule_status_is_read_only_and_validated(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.schedule()), "")
        with (
            mock.patch("dashboard.collector._WINDOWS", True),
            mock.patch.object(self.collector, "_run_command", return_value=response) as execute,
        ):
            self.assertEqual(self.collector.schedule_status(), self.schedule())
        self.assertEqual(execute.call_args.args[0][8:], ["-Status", "-Time", "07:00"])
        self.assertFalse(self.collector.config_path.exists())
        for value in ({}, {**self.schedule(), "enabled": "true"}, {**self.schedule(), "task_name": "other"}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.collector._schedule_result(value)

    def test_unsupported_scheduler_is_explicit(self):
        with mock.patch("dashboard.collector._WINDOWS", False):
            self.assertIn("unavailable", self.collector.schedule_status()["note"])
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                self.collector.set_schedule(False, "07:00")

    def test_native_process_is_bounded_and_uses_repo_cwd(self):
        result = self.collector._run_command(
            [sys.executable, "-c", "import os; print(os.getcwd())"], 10
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Path(result.stdout.strip()), self.repo)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collector._run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], 0.2
            )


class PowerShellScriptsCase(CollectorFixture):
    """Real PowerShell parser/runtime tests with all scheduler cmdlets replaced."""

    @classmethod
    def setUpClass(cls):
        cls.shells = [
            Path(path).resolve()
            for name in ("pwsh.exe", "powershell.exe")
            if (path := shutil.which(name))
        ]
        if not cls.shells or os.name != "nt":
            raise unittest.SkipTest("Windows PowerShell runtimes are not installed.")

    def ps(self, shell, code, extra_env=None):
        env = os.environ.copy()
        env.update({
            "FOUNDRY_TEST_REGISTER": str(ROOT / "scripts" / "register-morning-scan.ps1"),
            "FOUNDRY_TEST_RUNNER": str(ROOT / "scripts" / "run-morning-scan.ps1"),
            "FOUNDRY_TEST_REPO": str(self.repo),
            "FOUNDRY_TEST_DATA": str(self.data),
            "FOUNDRY_TEST_PYTHON": str(Path(sys.executable).resolve()),
            "PYTHONUTF8": "1",
        })
        env.update(extra_env or {})
        return subprocess.run(
            [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", code],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, shell=False,
        )

    def test_owned_scripts_parse_in_ps51_and_ps7(self):
        code = r"""
$ErrorActionPreference = 'Stop'
foreach ($path in @($env:FOUNDRY_TEST_REGISTER, $env:FOUNDRY_TEST_RUNNER)) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors -join "`n") }
    $ast.ParamBlock.Parameters.Name.VariablePath.UserPath -join ','
}
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Enabled,Disable,Status,Time,PythonPath,RepoRoot,DataDirectory", result.stdout)
                self.assertIn("PythonPath,RepoRoot,DataDirectory", result.stdout)

    def test_invalid_time_is_rejected_before_scheduler_query(self):
        code = r"""
function Get-ScheduledTask { throw 'SCHEDULER_MUST_NOT_BE_CALLED' }
& $env:FOUNDRY_TEST_REGISTER -Status -Time $env:FOUNDRY_TEST_TIME
"""
        for shell in self.shells:
            for clock in ("07:00;whoami", "24:00", "7:00", "07:00\n"):
                with self.subTest(shell=shell, clock=clock):
                    result = self.ps(shell, code, {"FOUNDRY_TEST_TIME": clock})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("SCHEDULER_MUST_NOT_BE_CALLED", result.stderr)

    def test_status_of_absent_task_does_not_register_anything(self):
        code = r"""
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask { return $null }
function Register-ScheduledTask { throw 'REGISTRATION_FORBIDDEN' }
function Disable-ScheduledTask { throw 'DISABLE_FORBIDDEN' }
& $env:FOUNDRY_TEST_REGISTER -Status -Time '07:00'
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertFalse(value["enabled"])
                self.assertEqual(value["task_name"], TASK_NAME)
                self.assertIsNone(value["next_run"])
                self.assertIn("not registered", value["note"])
                self.assertIn("signed into Windows", value["note"])

    def test_actual_status_helper_through_collector_is_read_only(self):
        collector = Collector(FakeStore(), ROOT, self.data)
        expected_fields = {
            "enabled", "time", "task_name", "next_run", "last_run", "last_result", "note",
        }
        for shell in self.shells:
            with (
                self.subTest(shell=shell.name),
                mock.patch.object(collector, "_powershell", return_value=str(shell)),
            ):
                status = collector.schedule_status()
                self.assertEqual(set(status), expected_fields)
                self.assertIs(type(status["enabled"]), bool)
                self.assertEqual(status["task_name"], TASK_NAME)
                self.assertRegex(status["time"], r"\A(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
                self.assertIn("signed into Windows", status["note"])
        self.assertFalse(collector.config_path.exists())
        self.assertFalse(collector.status_path.exists())
        self.assertFalse((self.data / "runs").exists())

    def test_file_entrypoint_resolves_default_root_without_starting_collection(self):
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = subprocess.run(
                    [
                        str(shell), "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "scripts" / "run-morning-scan.ps1"),
                        "-PythonPath", str(self.workspace / "nonexistent-python.exe"),
                    ],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30, shell=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PythonPath must point to an existing absolute Python executable", result.stderr)
                self.assertNotIn("Split-Path", result.stderr)

    def test_query_access_errors_are_not_treated_as_absent_tasks(self):
        code = r"""
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask { throw 'Access denied to the named task.' }
& $env:FOUNDRY_TEST_REGISTER -Status
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Access denied", result.stderr)

    def test_discovery_can_execute_a_fixed_cmd_shim_from_a_path_with_metacharacters(self):
        az_path = self.repo / "fake az & cli.cmd"
        payload = [{"id": SUB_A, "tenantId": TENANT, "name": "Mock subscription", "state": "Enabled"}]
        az_path.write_text(
            "@echo off\n"
            'if not "%1 %2 %3"=="account list --all" exit /b 9\n'
            "echo " + json.dumps(payload) + "\nexit /b 0\n",
            encoding="utf-8",
        )
        for shell in self.shells:
            with (
                self.subTest(shell=shell),
                mock.patch.object(self.collector, "_powershell", return_value=str(shell)),
                mock.patch("dashboard.collector.shutil.which", return_value=str(az_path)),
            ):
                result = self.collector.discover_subscriptions()
                self.assertEqual(result, [{
                    "id": SUB_A, "tenant_id": TENANT, "name": "Mock subscription", "state": "Enabled",
                }])

    def test_disable_only_modifies_the_named_current_user_task(self):
        code = r"""
$ErrorActionPreference = 'Stop'
$global:InventoryTestDisabled = 0
$global:InventoryTestTask = [pscustomobject]@{
    Principal=[pscustomobject]@{UserId=[Security.Principal.WindowsIdentity]::GetCurrent().Name}
    Settings=[pscustomobject]@{Enabled=$true}
    Triggers=@([pscustomobject]@{StartBoundary='2026-09-19T07:00:00'})
}
function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'FoundryModelInventory-MorningScan' -or $TaskPath -ne '\') { throw 'Wrong task queried' }
    $global:InventoryTestTask
}
function Disable-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'FoundryModelInventory-MorningScan' -or $TaskPath -ne '\') { throw 'Wrong task changed' }
    $global:InventoryTestDisabled++
    $global:InventoryTestTask.Settings.Enabled = $false
}
function Register-ScheduledTask { throw 'REGISTRATION_FORBIDDEN' }
function Get-ScheduledTaskInfo {
    [pscustomobject]@{NextRunTime=[datetime]::MinValue;LastRunTime=[datetime]'1999-11-30';LastTaskResult=267011}
}
$json = & $env:FOUNDRY_TEST_REGISTER -Disable
[pscustomobject]@{status=($json | ConvertFrom-Json);changes=$global:InventoryTestDisabled} | ConvertTo-Json -Compress
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["changes"], 1)
                self.assertFalse(payload["status"]["enabled"])
                self.assertIsNone(payload["status"]["last_run"])
                self.assertEqual(payload["status"]["last_result"], 267011)

    def owner_functions_script(self):
        return r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:FOUNDRY_TEST_REGISTER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors -join "`n") }
$functions = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Resolve-TaskOwnerSid', 'Assert-CurrentUserTask')
}, $true))
foreach ($definition in $functions) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
"""

    def test_owner_guard_accepts_unqualified_account_only_via_matching_sid(self):
        code = self.owner_functions_script() + r"""
$global:OwnerResolutionCount = 0
function Resolve-TaskOwnerSid([string]$Account) {
    if ($Account -ne 'test-operator') { throw 'Unexpected fixture account.' }
    $global:OwnerResolutionCount++
    'S-1-5-21-101-202-303-1001'
}
$identity = [pscustomobject]@{
    Name='EXAMPLE\test-operator'
    User=[pscustomobject]@{Value='S-1-5-21-101-202-303-1001'}
}
$task = [pscustomobject]@{Principal=[pscustomobject]@{UserId='test-operator'}}
Assert-CurrentUserTask $task $identity
if ($global:OwnerResolutionCount -ne 1) { throw 'The owner SID was not resolved.' }
'OWNER_SID_MATCHED'
"""
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OWNER_SID_MATCHED", result.stdout)

    def test_owner_guard_refuses_different_sid_even_when_account_names_match(self):
        code = self.owner_functions_script() + r"""
function Resolve-TaskOwnerSid([string]$Account) { 'S-1-5-21-101-202-303-1002' }
$identity = [pscustomobject]@{
    Name='EXAMPLE\test-operator'
    User=[pscustomobject]@{Value='S-1-5-21-101-202-303-1001'}
}
$task = [pscustomobject]@{Principal=[pscustomobject]@{UserId='EXAMPLE\test-operator'}}
Assert-CurrentUserTask $task $identity
"""
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = self.ps(shell, code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("belongs to another user", result.stderr)

    def test_owner_guard_fails_closed_when_account_cannot_be_resolved(self):
        code = self.owner_functions_script() + r"""
function Resolve-TaskOwnerSid([string]$Account) {
    throw [Security.Principal.IdentityNotMappedException]::new('Fixture account mapping failure.')
}
$identity = [pscustomobject]@{
    Name='EXAMPLE\test-operator'
    User=[pscustomobject]@{Value='S-1-5-21-101-202-303-1001'}
}
$task = [pscustomobject]@{Principal=[pscustomobject]@{UserId='EXAMPLE\test-operator'}}
Assert-CurrentUserTask $task $identity
"""
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = self.ps(shell, code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Fixture account mapping failure", result.stderr)

    def test_owner_sid_resolver_canonicalizes_sids_and_windows_accounts(self):
        code = self.owner_functions_script() + r"""
$syntheticSid = 'S-1-5-21-101-202-303-1001'
if ((Resolve-TaskOwnerSid $syntheticSid) -ne $syntheticSid) { throw 'SID parsing failed.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ((Resolve-TaskOwnerSid $identity.Name) -ne $identity.User.Value) { throw 'Native account resolution failed.' }
'OWNER_RESOLVER_VERIFIED'
"""
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OWNER_RESOLVER_VERIFIED", result.stdout)

    def test_owner_sid_resolver_rejects_blank_and_malformed_sids(self):
        code = self.owner_functions_script() + r"""
$identity = [pscustomobject]@{
    Name='EXAMPLE\test-operator'
    User=[pscustomobject]@{Value='S-1-5-21-101-202-303-1001'}
}
$task = [pscustomobject]@{Principal=[pscustomobject]@{UserId=$env:FOUNDRY_TEST_OWNER}}
Assert-CurrentUserTask $task $identity
"""
        for shell in self.shells:
            for owner in ("", "S-1-invalid"):
                with self.subTest(shell=shell.name, owner=owner):
                    result = self.ps(shell, code, {"FOUNDRY_TEST_OWNER": owner})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("could not be resolved to a Windows SID", result.stderr)

    def registration_script(self):
        return r"""
$ErrorActionPreference = 'Stop'
$global:InventoryTestTask = $null
$global:InventoryTestHostLookups = 0
function Get-Process { throw 'The scheduled host must not come from the current process path.' }
function Get-Command {
    [CmdletBinding()]
    param($Name, $CommandType)
    if ($env:FOUNDRY_TEST_NO_SYSTEM_HOST -ne '1') { throw 'Fallback queried while the system host is available.' }
    if ($Name -ne 'pwsh.exe' -or $CommandType -ne 'Application') { throw 'Unexpected fallback lookup.' }
    $global:InventoryTestHostLookups++
    [pscustomobject]@{Source=$env:FOUNDRY_TEST_FALLBACK_HOST}
}
function Test-Path {
    [CmdletBinding()]
    param($LiteralPath, $PathType)
    $systemHost = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ($env:FOUNDRY_TEST_NO_SYSTEM_HOST -eq '1' -and $LiteralPath -ieq $systemHost) { return $false }
    Microsoft.PowerShell.Management\Test-Path @PSBoundParameters
}
function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'FoundryModelInventory-MorningScan' -or $TaskPath -ne '\') { throw 'Wrong task queried' }
    $global:InventoryTestTask
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    $global:InventoryTestAction = [pscustomobject]@{execute=$Execute;arguments=$Argument;cwd=$WorkingDirectory}
    $global:InventoryTestAction
}
function New-ScheduledTaskTrigger {
    param([switch]$Daily, $At)
    if (-not $Daily) { throw 'Not daily' }
    [pscustomobject]@{StartBoundary=$At.ToString('o')}
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    $global:InventoryTestPrincipal = [pscustomobject]@{UserId=$UserId;logon=$LogonType;level=$RunLevel}
    $global:InventoryTestPrincipal
}
function New-ScheduledTaskSettingsSet {
    param([switch]$StartWhenAvailable, $MultipleInstances, $ExecutionTimeLimit)
    $global:InventoryTestSettings = [pscustomobject]@{
        Enabled=$true;available=[bool]$StartWhenAvailable;instances=$MultipleInstances;hours=$ExecutionTimeLimit.TotalHours
    }
    $global:InventoryTestSettings
}
function Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    if ($env:FOUNDRY_TEST_REJECT_REGISTRATION -eq '1') { throw 'Unexpected task registration after an invalid fallback.' }
    if ($TaskName -ne 'FoundryModelInventory-MorningScan' -or $TaskPath -ne '\') { throw 'Wrong task modified' }
    if (-not $Force) { throw 'Expected idempotent registration' }
    $global:InventoryTestTask = [pscustomobject]@{Settings=$Settings;Triggers=@($Trigger);Principal=$Principal}
}
function Get-ScheduledTaskInfo {
    [pscustomobject]@{NextRunTime=[datetime]::Today.AddDays(1);LastRunTime=[datetime]::MinValue;LastTaskResult=0}
}
$json = & $env:FOUNDRY_TEST_REGISTER -Enabled -Time '06:05' -PythonPath $env:FOUNDRY_TEST_PYTHON `
    -RepoRoot $env:FOUNDRY_TEST_REPO -DataDirectory $env:FOUNDRY_TEST_DATA
[pscustomobject]@{
    status=($json | ConvertFrom-Json);action=$global:InventoryTestAction;
    settings=$global:InventoryTestSettings;principal=$global:InventoryTestPrincipal;
    host_lookups=$global:InventoryTestHostLookups
} | ConvertTo-Json -Depth 5 -Compress
"""

    def test_registration_uses_interactive_limited_named_task_with_no_password(self):
        code = self.registration_script()
        system_host = Path(os.environ["SystemRoot"]) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertTrue(value["status"]["enabled"])
                self.assertEqual(value["status"]["time"], "06:05")
                self.assertEqual(value["principal"]["logon"], "Interactive")
                self.assertEqual(value["principal"]["level"], "Limited")
                self.assertTrue(value["settings"]["available"])
                self.assertEqual(value["settings"]["instances"], "IgnoreNew")
                self.assertEqual(value["settings"]["hours"], 3)
                self.assertEqual(Path(value["action"]["cwd"]), self.repo)
                self.assertEqual(Path(value["action"]["execute"]), system_host)
                self.assertNotIn("windowsapps", value["action"]["execute"].lower())
                self.assertEqual(value["host_lookups"], 0)
                self.assertIn(f'-PythonPath "{Path(sys.executable).resolve()}"', value["action"]["arguments"])
                self.assertIn(f'-DataDirectory "{self.data}"', value["action"]["arguments"])
                self.assertIn("run-morning-scan.ps1", value["action"]["arguments"])

    def test_registration_uses_validated_non_store_fallback_only_without_system_host(self):
        fallback = self.workspace / "standalone PowerShell" / "pwsh.exe"
        fallback.parent.mkdir()
        fallback.write_text("Mock executable; never launched.", encoding="utf-8")
        for shell in self.shells:
            with self.subTest(shell=shell.name):
                result = self.ps(shell, self.registration_script(), {
                    "FOUNDRY_TEST_NO_SYSTEM_HOST": "1",
                    "FOUNDRY_TEST_FALLBACK_HOST": str(fallback),
                })
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(Path(value["action"]["execute"]), fallback)
                self.assertEqual(value["host_lookups"], 1)
                self.assertEqual(value["principal"]["logon"], "Interactive")
                self.assertEqual(value["principal"]["level"], "Limited")
                self.assertEqual(value["settings"]["instances"], "IgnoreNew")

    def test_registration_rejects_store_relative_missing_or_wrong_fallback_hosts(self):
        store_host = self.workspace / "WindowsApps" / "Microsoft.PowerShell_99.0_x64" / "pwsh.exe"
        store_host.parent.mkdir(parents=True)
        store_host.write_text("Mock executable; never launched.", encoding="utf-8")
        wrong_host = self.workspace / "not-powershell.exe"
        wrong_host.write_text("Mock executable; never launched.", encoding="utf-8")
        cases = {
            "Store package": str(store_host),
            "relative path": "pwsh.exe",
            "drive-relative path": "C:pwsh.exe",
            "root-relative path": r"\pwsh.exe",
            "missing file": str(self.workspace / "missing" / "pwsh.exe"),
            "wrong executable": str(wrong_host),
        }
        for shell in self.shells:
            for label, fallback in cases.items():
                with self.subTest(shell=shell.name, fallback=label):
                    result = self.ps(shell, self.registration_script(), {
                        "FOUNDRY_TEST_NO_SYSTEM_HOST": "1",
                        "FOUNDRY_TEST_FALLBACK_HOST": fallback,
                        "FOUNDRY_TEST_REJECT_REGISTRATION": "1",
                    })
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PowerShell fallback must be", result.stderr)
                    self.assertNotIn("Unexpected task registration", result.stderr)

    def test_runner_sets_cwd_writes_redacted_utf8_log_and_preserves_python_exit(self):
        module = self.repo / "dashboard"
        module.mkdir()
        (module / "__init__.py").write_text("", encoding="utf-8")
        (module / "__main__.py").write_text(
            "import json,os,sys\n"
            "print(json.dumps({'cwd':os.getcwd(),'argv':sys.argv[1:]}))\n"
            "print('password=synthetic-private', file=sys.stderr)\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        code = r"""
& $env:FOUNDRY_TEST_RUNNER -PythonPath $env:FOUNDRY_TEST_PYTHON `
    -RepoRoot $env:FOUNDRY_TEST_REPO -DataDirectory $env:FOUNDRY_TEST_DATA
exit $LASTEXITCODE
"""
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.ps(shell, code)
                self.assertEqual(result.returncode, 7, result.stderr + result.stdout)
                self.assertNotIn("synthetic-private", result.stdout + result.stderr)
                json_line = next(line for line in result.stdout.splitlines() if line.startswith("{"))
                payload = json.loads(json_line)
                self.assertEqual(Path(payload["cwd"]), self.repo)
                self.assertEqual(payload["argv"], [
                    "collect", "--data-dir", str(self.data), "--source", "scheduled",
                ])
        logs = list((self.data / "logs").glob("morning-*.log"))
        self.assertEqual(len(logs), len(self.shells))
        for log in logs:
            text = log.read_text(encoding="utf-8")
            self.assertIn("exited with code 7", text)
            self.assertNotIn("synthetic-private", text)


if __name__ == "__main__":
    unittest.main()
