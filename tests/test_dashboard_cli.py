from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dashboard.__main__ import import_snapshot, parser


class CommandLineTests(unittest.TestCase):
    def test_command_arguments_match_scheduler(self):
        args = parser().parse_args(
            ["collect", "--source", "scheduled", "--data-dir", r"D:\example\data"]
        )
        self.assertEqual(args.command, "collect")
        self.assertEqual(args.source, "scheduled")
        self.assertEqual(args.data_dir, Path(r"D:\example\data"))

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


if __name__ == "__main__":
    unittest.main()
