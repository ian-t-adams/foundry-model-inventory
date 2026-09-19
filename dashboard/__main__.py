"""Command-line entry point for the local Foundry inventory workbench."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
import sys

from .collector import Collector
from .store import Store


ROOT = Path(__file__).resolve().parent.parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local Foundry inventory and snapshot history")
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in [
        ("serve", "Start the loopback-only dashboard"),
        ("collect", "Collect the configured subscriptions and store a snapshot"),
        ("import", "Import existing inventory CSV files as one historical snapshot"),
        ("configure", "Select the tenant and subscriptions to collect"),
        ("schedule", "Manage the daily Windows collection task"),
    ]:
        command = commands.add_parser(name, help=description)
        command.add_argument("--data-dir", type=Path, default=ROOT / "data")
        if name == "serve":
            command.add_argument("--port", type=int, default=8765)
        elif name == "collect":
            command.add_argument("--source", choices=["manual", "scheduled"], default="manual")
        elif name == "import":
            command.add_argument("--csv", type=Path, nargs="+", required=True)
            command.add_argument("--started-at", help="ISO 8601 timestamp; defaults to the CSV scan timestamp")
        elif name == "configure":
            command.add_argument("--tenant-id", required=True)
            command.add_argument("--subscription-id", nargs="+", required=True)
            command.add_argument("--morning-time", default="07:00")
        elif name == "schedule":
            action = command.add_mutually_exclusive_group()
            action.add_argument("--enable", action="store_true")
            action.add_argument("--disable", action="store_true")
            command.add_argument("--time", default="07:00")
    return result


def import_snapshot(store: Store, paths: list[Path], started_at: str | None = None) -> dict:
    paths = [path.resolve(strict=True) for path in paths]
    if any(not path.is_file() for path in paths):
        raise ValueError("Every --csv path must be an existing file.")
    if not started_at:
        with paths[0].open(encoding="utf-8-sig", newline="") as handle:
            first = next(csv.DictReader(handle), None)
        if not first:
            raise ValueError("The first CSV has no inventory rows.")
        started_at = first.get("ScanStartedUtc") or None
    scan_id = store.start_scan("import", started_at=started_at)
    try:
        return store.ingest_csvs(scan_id, paths)
    except Exception as exc:
        store.fail_scan(scan_id, str(exc))
        raise


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = args.data_dir.resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / "inventory.sqlite3")
        collector = Collector(store, ROOT, data_dir)
        if args.command == "serve":
            if not 1024 <= args.port <= 65535:
                raise ValueError("Choose a port between 1024 and 65535.")
            from .server import serve

            print(f"Foundry inventory: http://127.0.0.1:{args.port}", flush=True)
            print(f"Local data: {data_dir}", flush=True)
            print("Press Ctrl+C to stop the dashboard. Scheduled collection is independent.", flush=True)
            serve(store, collector, ROOT / "dashboard" / "static", port=args.port)
            return 0
        if args.command == "import":
            summary = import_snapshot(store, args.csv, args.started_at)
        elif args.command == "collect":
            summary = collector.run_scan(source=args.source)
        elif args.command == "configure":
            subscriptions = collector.discover_subscriptions()
            requested = {item.lower() for item in args.subscription_id}
            selected = [
                {"id": item["id"], "name": item["name"]}
                for item in subscriptions
                if item["id"].lower() in requested
                and item["tenant_id"].lower() == args.tenant_id.lower()
                and item["state"] == "Enabled"
            ]
            if len(selected) != len(requested):
                raise ValueError("One or more subscriptions are unavailable, disabled, or belong to another tenant.")
            summary = collector.save_config(
                {"tenant_id": args.tenant_id, "subscriptions": selected, "morning_time": args.morning_time}
            )
        else:
            summary = (
                collector.set_schedule(args.enable, args.time)
                if args.enable or args.disable
                else collector.schedule_status()
            )
        print(json.dumps(summary, indent=2, ensure_ascii=True, default=str))
        if isinstance(summary, dict) and summary.get("status") in ("partial", "failed"):
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nDashboard stopped.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
