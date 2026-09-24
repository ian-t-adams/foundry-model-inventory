from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from dashboard.__main__ import import_snapshot, main, parser


class CommandLineTests(unittest.TestCase):
    PRIMARY = "10000000-0000-4000-8000-000000000001"
    OTHER = "10000000-0000-4000-8000-000000000002"
    SUB_A = "20000000-0000-4000-8000-000000000001"
    SUB_B = "20000000-0000-4000-8000-000000000002"

    def test_command_arguments_match_scheduler(self):
        args = parser().parse_args(
            ["collect", "--source", "scheduled", "--data-dir", r"D:\example\data"]
        )
        self.assertEqual(args.command, "collect")
        self.assertEqual(args.source, "scheduled")
        self.assertEqual(args.data_dir, Path(r"D:\example\data"))

    def test_configure_discovers_and_saves_the_explicit_cli_profile(self):
        tenant = "10000000-0000-4000-8000-000000000001"
        subscription = "20000000-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "data" / "azure-cli"
            with (
                patch("dashboard.__main__.ROOT", root),
                patch("dashboard.__main__.Store"),
                patch("dashboard.__main__.Collector") as collector,
                redirect_stdout(io.StringIO()),
            ):
                collector.return_value.discover_subscriptions.return_value = [{
                    "id": subscription, "tenant_id": tenant, "name": "Fixture", "state": "Enabled",
                }]
                collector.return_value.save_config.return_value = {}
                result = main([
                    "configure", "--tenant-id", tenant, "--subscription-id", subscription,
                    "--azure-config-dir", str(profile),
                ])
            self.assertEqual(result, 0)
            collector.return_value.discover_subscriptions.assert_called_once_with(str(profile.resolve()))
            collector.return_value.save_config.assert_called_once_with({
                "tenant_id": tenant, "subscriptions": [{"id": subscription, "name": "Fixture"}],
                "morning_time": "07:00", "azure_config_dir": str(profile.resolve()),
            })

    def test_configure_adds_a_verified_other_tenant_without_replacing_existing_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "data" / "other-profile"
            existing = {
                "tenant_id": self.PRIMARY,
                "subscriptions": [{"id": self.SUB_A, "name": "Primary"}],
                "azure_config_dir": str(root / "data" / "primary-profile"),
                "morning_time": "08:15",
            }
            with (
                patch("dashboard.__main__.ROOT", root),
                patch("dashboard.__main__.Store"),
                patch("dashboard.__main__.Collector") as collector,
                redirect_stdout(io.StringIO()),
            ):
                collector.return_value.load_config.return_value = existing
                collector.return_value.discover_subscriptions.return_value = [{
                    "id": self.SUB_B, "tenant_id": self.OTHER, "name": "Other",
                    "state": "Enabled",
                }]
                collector.return_value.save_config.return_value = {}
                args = [
                    "configure", "--add", "--tenant-id", self.OTHER,
                    "--subscription-id", self.SUB_B, "--azure-config-dir", str(profile),
                ]
                self.assertEqual(main(args), 0)
            collector.return_value.discover_subscriptions.assert_called_once_with(str(profile.resolve()))
            collector.return_value.save_config.assert_called_once_with({
                **existing,
                "subscriptions": [
                    *existing["subscriptions"],
                    {"id": self.SUB_B, "name": "Other", "tenant_id": self.OTHER},
                ],
                "tenant_profiles": {self.OTHER: str(profile.resolve())},
            }, expected_config=existing)

    def test_configure_add_requires_a_profile_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("dashboard.__main__.ROOT", root),
                patch("dashboard.__main__.Store"),
                patch("dashboard.__main__.Collector") as collector,
                self.assertLogs(level="ERROR") as logs,
            ):
                result = main([
                    "configure", "--add", "--tenant-id", self.OTHER,
                    "--subscription-id", self.SUB_B,
                ])
            self.assertEqual(result, 1)
            self.assertIn("--azure-config-dir", logs.output[0])
            collector.return_value.discover_subscriptions.assert_not_called()
            collector.return_value.save_config.assert_not_called()

    def test_import_uses_original_observation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snapshot.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["ScanStartedUtc", "Model"])
                writer.writeheader()
                writer.writerow({"ScanStartedUtc": "2026-01-02T03:04:05Z", "Model": "test-model"})
            store = Mock()
            store.start_scan.return_value = 17
            store.ingest_csvs.return_value = {"id": 17, "status": "complete"}
            result = import_snapshot(store, [source])
            store.start_scan.assert_called_once_with("import", started_at="2026-01-02T03:04:05Z")
            store.ingest_csvs.assert_called_once_with(17, [source.resolve()])
            self.assertEqual(result["status"], "complete")

    def test_failed_import_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snapshot.csv"
            source.write_text("Model\nfixture\n", encoding="utf-8")
            store = Mock()
            store.start_scan.return_value = 8
            store.ingest_csvs.side_effect = ValueError("Invalid numeric quota")
            with self.assertRaisesRegex(ValueError, "Invalid numeric quota"):
                import_snapshot(store, [source], "2026-01-02T00:00:00Z")
            store.fail_scan.assert_called_once_with(8, "Invalid numeric quota")

    def test_empty_or_missing_csv_does_not_start_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "empty.csv"
            source.write_text("Model\n", encoding="utf-8")
            store = Mock()
            with self.assertRaisesRegex(ValueError, "no inventory rows"):
                import_snapshot(store, [source])
            store.start_scan.assert_not_called()
            with self.assertRaises(FileNotFoundError):
                import_snapshot(store, [Path(directory) / "missing.csv"])
            store.start_scan.assert_not_called()

    def test_rejected_data_directories_do_not_create_files(self):
        commands = [
            ["serve"],
            ["collect"],
            ["import", "--csv", "missing.csv"],
            ["configure", "--tenant-id", "fixture", "--subscription-id", "fixture"],
            ["schedule"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            with patch("dashboard.__main__.ROOT", root), patch("dashboard.__main__.Store") as store:
                for command in commands:
                    for destination in (root, root / "data-other", root / "data" / ".." / "private"):
                        with self.subTest(command=command[0], destination=destination):
                            with self.assertLogs(level="ERROR") as logs:
                                result = main([*command, "--data-dir", str(destination)])
                            self.assertEqual(result, 1)
                            self.assertIn("data directory", logs.output[0])
                            self.assertFalse(destination.resolve().exists())
                            store.assert_not_called()

    def test_invalid_serve_port_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            with patch("dashboard.__main__.ROOT", root), patch("dashboard.__main__.Store") as store:
                for port in ("0", "1023", "65536"):
                    with self.subTest(port=port):
                        with self.assertLogs(level="ERROR") as logs:
                            result = main(["serve", "--port", port])
                        self.assertEqual(result, 1)
                        self.assertIn("port between 1024 and 65535", logs.output[0])
                        self.assertFalse((root / "data").exists())
                        store.assert_not_called()

    def test_main_imports_into_nested_data_directory_and_releases_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "data" / "archive"
            source = root / "snapshot.csv"
            source.write_text(
                "SubscriptionId,Region,Model,Version,SKU,Catalog,Limit,Allocated,"
                "Remaining,Unit,QuotaStatus,QuotaName\n"
                "fixture-subscription,eastus,fixture-model,1,GlobalStandard,Listed,"
                "100,20,80,1K TPM,Reported,fixture-pool\n",
                encoding="utf-8",
            )
            with patch("dashboard.__main__.ROOT", root), redirect_stdout(io.StringIO()) as output:
                result = main(["import", "--csv", str(source), "--data-dir", str(destination)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            database = destination / "inventory.sqlite3"
            self.assertEqual(set(destination.iterdir()), {database})
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT model FROM inventory").fetchall(),
                    [("fixture-model",)],
                )
            database.unlink()

    def test_main_closes_store_after_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status, expected in (("complete", 0), ("partial", 1), ("failed", 1)):
                with (
                    self.subTest(status=status),
                    patch("dashboard.__main__.ROOT", root),
                    patch("dashboard.__main__.Store") as store,
                    patch("dashboard.__main__.Collector") as collector,
                    redirect_stdout(io.StringIO()),
                ):
                    collector.return_value.run_scan.return_value = {"status": status}
                    self.assertEqual(main(["collect"]), expected)
                    store.assert_called_once_with(root.resolve() / "data" / "inventory.sqlite3")
                    store.return_value.close.assert_called_once_with()

    def test_main_closes_store_when_collection_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("dashboard.__main__.ROOT", Path(directory)),
                patch("dashboard.__main__.Store") as store,
                patch("dashboard.__main__.Collector") as collector,
                self.assertLogs(level="ERROR") as logs,
            ):
                collector.return_value.run_scan.side_effect = RuntimeError("fixture failure")
                self.assertEqual(main(["collect"]), 1)
                self.assertIn("fixture failure", logs.output[0])
                store.return_value.close.assert_called_once_with()

    def test_main_closes_store_when_collector_initialization_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("dashboard.__main__.ROOT", Path(directory)),
                patch("dashboard.__main__.Store") as store,
                patch("dashboard.__main__.Collector", side_effect=OSError("fixture failure")),
                self.assertLogs(level="ERROR") as logs,
            ):
                self.assertEqual(main(["schedule"]), 1)
                self.assertIn("fixture failure", logs.output[0])
                store.return_value.close.assert_called_once_with()

    def test_main_closes_store_when_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("dashboard.__main__.ROOT", Path(directory)),
                patch("dashboard.__main__.Store") as store,
                patch("dashboard.__main__.Collector") as collector,
                redirect_stderr(io.StringIO()) as error,
            ):
                collector.return_value.run_scan.side_effect = KeyboardInterrupt()
                self.assertEqual(main(["collect"]), 130)
                self.assertIn("Dashboard stopped.", error.getvalue())
                store.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
