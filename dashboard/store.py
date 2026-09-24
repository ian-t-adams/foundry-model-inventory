"""Atomic, historical inventory snapshots backed by thread-local SQLite connections.

Catalogue presence is not a promise of deployment capacity. Quota summaries count
distinct pools, never add their capacities, and treat inconsistent pool readings
as unknown. Unmapped rows are also included in ``unknown_quota``.
Family aliases are projected on reads without rewriting historical snapshots.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROW_FIELDS = (
    "id", "key", "subscription", "subscription_id", "tenant_id", "model",
    "version", "region", "family", "format", "deployment_type", "capacity_type",
    "sku", "catalog", "lifecycle", "quota_limit", "allocated", "remaining",
    "unit", "quota_status", "quota_name", "quota_description", "account_kinds",
    "inference_deprecation", "sku_deprecation", "notes",
)
_DATA_FIELDS = ROW_FIELDS[2:]
_NUMERIC_FIELDS = {"quota_limit", "allocated", "remaining"}
_QUOTA_FIELDS = _NUMERIC_FIELDS | {
    "unit", "quota_status", "quota_name", "quota_description",
}
_SUMMARY_FIELDS = (
    "models", "versions", "regions", "subscriptions", "rows", "quota_pools",
    "with_headroom", "zero_quota", "unknown_quota",
)
_REQUIRED_CSV = {
    "SubscriptionId", "Region", "Model", "Version", "SKU", "Catalog",
    "Limit", "Allocated", "Remaining", "Unit", "QuotaStatus", "QuotaName",
}
_REPORT_COLUMNS = {
    "Subscription", "SubscriptionId", "TenantId", "ScanStartedUtc", "Model",
    "Version", "Region", "Type", "SKU", "Catalog", "Lifecycle", "Limit",
    "Allocated", "Remaining", "Unit", "QuotaStatus", "QuotaName",
    "QuotaDescription", "Kind", "Format", "InferenceDeprecation",
    "SkuDeprecation", "Notes",
}
_FILTER_COLUMNS = {
    "tenant": "tenant_id",
    "subscription": "subscription_id",
    "region": "region",
    "family": "family",
    "model": "model",
    "version": "version",
    "deployment_type": "deployment_type",
    "capacity_type": "capacity_type",
    "lifecycle": "lifecycle",
    "unit": "unit",
    "quota_name": "quota_name",
    "sku": "sku",
}
CATEGORICAL_FILTERS = frozenset(_FILTER_COLUMNS) | {"availability"}
MAX_FILTER_ITEMS = 256
MAX_FILTER_FIELD = 512
_FILTER_KEYS = set(_FILTER_COLUMNS) | {
    "snapshot", "q", "availability", "minimum", "sort", "direction", "page",
    "page_size", "model_version",
}
_SORT_COLUMNS = set(ROW_FIELDS) - {"key", "account_kinds", "notes"}
_GROUP_COLUMNS = {
    "family": ("family",),
    "model": ("model",),
    "model_version": ("format", "model", "version"),
}
_GROUP_SORT_COLUMNS = {
    "label": "label", "models": "models", "versions": "versions", "regions": "regions",
    "subscriptions": "subscriptions", "entries": "rows", "with_headroom": "with_headroom",
}
_VARIANT_LABEL_SQL = (
    "model || ' ' || CASE WHEN version='' THEN '(version not reported)' ELSE version END"
)
_FAMILY_SQL = "CASE WHEN family COLLATE NOCASE='Mistral AI' THEN 'Mistral' ELSE family END"
_POOL_KEY_SQL = "CASE WHEN quota_name='' THEN 'entry:' || key ELSE 'pool:' || quota_name END"
_POOL_KNOWN_SQL = """
    MIN(CASE WHEN quota_status='Reported' AND quota_limit IS NOT NULL
             AND allocated IS NOT NULL AND remaining IS NOT NULL THEN 1 ELSE 0 END)
    AND COUNT(DISTINCT quota_limit)=1 AND COUNT(DISTINCT allocated)=1
    AND COUNT(DISTINCT remaining)=1 AND COUNT(DISTINCT unit)=1
"""
_ENUMS = {
    "deployment_type": {"Global", "DataZone", "Regional", "Unknown"},
    "capacity_type": {"PAYG", "PTU", "Batch", "Other"},
    "availability": {"available", "exhausted", "unknown"},
}
_NUMBER = re.compile(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z", re.ASCII)
_MAX_NUMBER = Decimal("9007199254740991")
_CATALOG = {
    "listed": "Listed", "no skus": "No SKUs", "not listed": "Not listed",
    "empty": "Empty", "error": "ERROR",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _text(value: Any, field: str, *, required: bool = False,
          maximum: int = 65536) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    value = value.strip()
    if "\x00" in value or len(value) > maximum:
        raise ValueError(f"{field} contains invalid or excessive text.")
    if required and not value:
        raise ValueError(f"{field} is required.")
    return value


def _number(value: str, field: str) -> int | float | None:
    value = value.strip()
    if not value:
        return None
    if not _NUMBER.fullmatch(value):
        raise ValueError(f"{field} must be a nonnegative finite number or blank.")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a valid number.") from exc
    if not decimal.is_finite() or decimal < 0 or decimal > _MAX_NUMBER:
        raise ValueError(f"{field} is outside the supported nonnegative numeric range.")
    if decimal == decimal.to_integral_value():
        return int(decimal)
    result = float(decimal)
    if not math.isfinite(result) or (result == 0 and decimal != 0):
        raise ValueError(f"{field} is outside the supported numeric precision.")
    return result


def _positive_int(value: Any, field: str, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value), re.ASCII):
        raise ValueError(f"{field} must be a positive integer.")
    result = int(value)
    if not 1 <= result <= maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}.")
    return result


def _unit(raw: str, description: str) -> str:
    for text in (description, raw):
        text = re.sub(r"\s+", " ", text.strip())
        for noun, label in (("tokens?", "TPM"), ("requests?", "RPM")):
            for scale, words in (
                ("1M", r"(?:one )?million|1[,]?000[,]?000|1m"),
                ("1K", r"(?:one )?thousand|1[,]?000|1k"),
            ):
                if re.search(
                    rf"(?:{words}) {noun} per minute"
                    rf"|{noun} per minute\s*\((?:{words})s?\)"
                    rf"|\b{scale}\s*{label}\b", text, re.I
                ):
                    return f"{scale} {label}"
        if re.search(r"provisioned.*throughput units?|\bPTUs?\b", text, re.I):
            return "PTU"
        if re.search(r"requests? per minute|\bRPM\b", text, re.I):
            return "RPM"
        if re.search(r"tokens? per minute|\bTPM\b", text, re.I):
            return "TPM"
        if re.search(r"tokens? per second|\bTPS\b", text, re.I):
            return "TPS"
        if re.search(r"requests? per day|\bRPD\b", text, re.I):
            return "RPD"
        if re.search(r"tokens? per day|\bTPD\b", text, re.I):
            return "TPD"
        if re.search(r"million (?:enqueued )?tokens|tokens\s*\(millions?\)"
                     r"|enqueued tokens.*million|\b1m tokens\b", text, re.I):
            return "1M tokens"
    return raw


def _canonical_family(value: str) -> str:
    return "Mistral" if value.casefold() == "mistral ai" else value


def _family(model: str, provider: str) -> str:
    name, vendor = model.casefold(), _canonical_family(provider).casefold()
    if "claude" in name or vendor == "anthropic":
        return "Claude"
    if vendor == "openai" or re.match(
        r"(?:gpt-|chatgpt-|o[134](?:-|$)|text-embedding-|dall-e|whisper|sora)", name
    ):
        return "OpenAI"
    for family, vendors, prefixes in (
        ("Microsoft", {"microsoft"}, ("phi-", "mai-")),
        ("Meta", {"meta"}, ("llama-", "meta-llama")),
        ("Mistral", {"mistral", "mistralai"}, ("mistral", "mixtral", "codestral")),
        ("DeepSeek", {"deepseek"}, ("deepseek",)),
        ("Cohere", {"cohere"}, ("cohere", "command-r")),
        ("xAI", {"xai"}, ("grok",)),
    ):
        if vendor in vendors or name.startswith(prefixes):
            return family
    return provider or "Other"


def _deployment(raw: str, sku: str) -> str:
    if raw:
        values = {item.casefold(): item for item in _ENUMS["deployment_type"]}
        if raw.casefold() not in values:
            raise ValueError("Type must be Global, DataZone, Regional, or Unknown.")
        return values[raw.casefold()]
    if sku.casefold().startswith("global"):
        return "Global"
    if sku.casefold().startswith("datazone"):
        return "DataZone"
    if sku.casefold() in {"standard", "provisionedmanaged", "provisioned", "batch"}:
        return "Regional"
    return "Unknown"


def _capacity(sku: str) -> str:
    value = sku.casefold()
    if "batch" in value:
        return "Batch"
    if "provisioned" in value or value == "ptu":
        return "PTU"
    if "standard" in value:
        return "PAYG"
    return "Other"


def _identity(row: dict) -> str:
    values = [row[field] for field in
              ("subscription_id", "region", "format", "model", "version", "sku")]
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _public_row(row: sqlite3.Row | dict) -> dict:
    result = {field: row[field] for field in ROW_FIELDS}
    result["family"] = _canonical_family(result["family"])
    if isinstance(result["account_kinds"], str):
        result["account_kinds"] = json.loads(result["account_kinds"])
    return result


def _spreadsheet(value: Any) -> Any:
    if isinstance(value, list):
        value = "; ".join(value)
    if value is None:
        return ""
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def _choices(value: Any, field: str) -> list[str]:
    """Strings retain legacy comma splitting; list items are literal choices."""
    if isinstance(value, list):
        supplied = value
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = _text(str(value), field, maximum=MAX_FILTER_ITEMS * (MAX_FILTER_FIELD + 1))
        supplied = text.split(",") if text else []
    else:
        raise ValueError(f"{field} must be text or a list of text choices.")
    if len(supplied) > MAX_FILTER_ITEMS:
        raise ValueError(f"{field} supports at most {MAX_FILTER_ITEMS} choices.")
    choices = [_text(item, field, required=True, maximum=MAX_FILTER_FIELD) for item in supplied]
    if field == "family":
        choices = [_canonical_family(item) for item in choices]
    if field in _ENUMS and any(item not in _ENUMS[field] for item in choices):
        raise ValueError(f"{field} has an unsupported value.")
    return list(dict.fromkeys(choices))


def _model_versions(value: Any) -> list[list[str]]:
    if isinstance(value, str):
        if len(value) > MAX_FILTER_ITEMS * (3 * MAX_FILTER_FIELD + 16):
            raise ValueError("model_version JSON is too long.")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("model_version must be a JSON array of [format,model,version] triples.") from exc
    if not isinstance(value, list) or len(value) > MAX_FILTER_ITEMS:
        raise ValueError(f"model_version must be a list of at most {MAX_FILTER_ITEMS} triples.")
    choices, seen = [], set()
    for triple in value:
        if not isinstance(triple, list) or len(triple) != 3:
            raise ValueError("Each model_version selection must contain format, model, and version.")
        for item in triple:
            if not isinstance(item, str) or len(item) > MAX_FILTER_FIELD or "\x00" in item:
                raise ValueError(f"model_version fields must be text of at most {MAX_FILTER_FIELD} characters.")
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("model_version fields must contain valid Unicode text.") from exc
        if not triple[1].strip():
            raise ValueError("model_version selections require a model name.")
        key = tuple(triple)
        if key not in seen:
            seen.add(key)
            choices.append(list(triple))
    return choices


def _variant_option(provider: str, model: str, version: str, family: str) -> dict:
    return {
        "value": _json([provider, model, version]),
        "label": model + " " + (version or "(version not reported)"),
        "model": model, "version": version, "format": provider, "family": _canonical_family(family),
    }


class Store:
    """One durable database; connections are never shared between threads."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        db = self._db()
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('running','complete','partial','failed')),
                record_count INTEGER NOT NULL DEFAULT 0,
                subscription_count INTEGER NOT NULL DEFAULT 0,
                region_count INTEGER NOT NULL DEFAULT 0,
                model_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                errors TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS scans_status_time
                ON scans(status, started_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER NOT NULL REFERENCES scans(id),
                key TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                subscription TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                model TEXT NOT NULL,
                version TEXT NOT NULL,
                region TEXT NOT NULL,
                family TEXT NOT NULL,
                format TEXT NOT NULL,
                deployment_type TEXT NOT NULL,
                capacity_type TEXT NOT NULL,
                sku TEXT NOT NULL,
                catalog TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                quota_limit NUMERIC,
                allocated NUMERIC,
                remaining NUMERIC,
                unit TEXT NOT NULL,
                quota_status TEXT NOT NULL,
                quota_name TEXT NOT NULL,
                quota_description TEXT NOT NULL,
                account_kinds TEXT NOT NULL,
                inference_deprecation TEXT NOT NULL,
                sku_deprecation TEXT NOT NULL,
                notes TEXT NOT NULL,
                metadata TEXT NOT NULL,
                UNIQUE(scan_id, key)
            );
            CREATE INDEX IF NOT EXISTS inventory_scope
                ON inventory(scan_id, subscription_id, region);
            CREATE INDEX IF NOT EXISTS inventory_model
                ON inventory(scan_id, model COLLATE NOCASE, version COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS inventory_family
                ON inventory(scan_id, family COLLATE NOCASE, region COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS inventory_capacity
                ON inventory(scan_id, capacity_type, deployment_type);
            CREATE INDEX IF NOT EXISTS inventory_pool
                ON inventory(scan_id, subscription_id, region, quota_name);
            CREATE INDEX IF NOT EXISTS inventory_identity_history
                ON inventory(identity_key, scan_id DESC);
            CREATE TABLE IF NOT EXISTS coverage (
                scan_id INTEGER NOT NULL REFERENCES scans(id),
                subscription TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                region TEXT NOT NULL,
                catalog_status TEXT NOT NULL,
                quota_status TEXT NOT NULL,
                status TEXT NOT NULL,
                successful INTEGER NOT NULL,
                record_count INTEGER NOT NULL,
                model_count INTEGER NOT NULL,
                errors TEXT NOT NULL,
                notes TEXT NOT NULL,
                PRIMARY KEY(scan_id, subscription_id, region)
            );
            PRAGMA user_version=1;
        """)

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "connection", None)
        if db is None:
            db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA temp_store=MEMORY")
            self._local.connection = db
        return db

    def close(self) -> None:
        """Close only the calling thread's connection."""
        db = getattr(self._local, "connection", None)
        if db is not None:
            db.close()
            del self._local.connection

    @contextmanager
    def _transaction(self, write: bool = False):
        db = self._db()
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _scan_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["errors"] = json.loads(result["errors"])
        return result

    def _snapshot(self, db: sqlite3.Connection, selection: str | int = "latest") -> dict | None:
        if selection in ("latest", "", None):
            row = db.execute(
                "SELECT * FROM scans WHERE status='complete' "
                "ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
        else:
            scan_id = _positive_int(selection, "snapshot")
            row = db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if row is None:
                raise ValueError("The requested snapshot does not exist.")
        return self._scan_dict(row) if row is not None else None

    def start_scan(self, source: str, started_at: str | None = None) -> int:
        source = _text(source, "source", required=True, maximum=128)
        if started_at is not None:
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    raise ValueError("A timezone is required.")
                started_at = started.astimezone(timezone.utc).isoformat(timespec="microseconds")
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("started_at must be an ISO 8601 timestamp with a timezone.") from exc
        with self._transaction(write=True) as db:
            cursor = db.execute(
                "INSERT INTO scans(source,started_at,status) VALUES(?,?,'running')",
                (source, started_at or _now()),
            )
            return cursor.lastrowid

    @staticmethod
    def _require_running(db: sqlite3.Connection, scan_id: int) -> None:
        row = db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row is None:
            raise ValueError("The requested scan does not exist.")
        if row["status"] != "running":
            raise RuntimeError("A finalized snapshot cannot be changed.")

    def fail_scan(self, scan_id: int, message: str) -> None:
        scan_id = _positive_int(scan_id, "scan_id")
        message = _text(message, "message", required=True)
        with self._transaction(write=True) as db:
            self._require_running(db, scan_id)
            errors = [{"subscription_id": "", "region": "", "kind": "scan", "message": message}]
            db.execute(
                "UPDATE scans SET status='failed',completed_at=?,error_count=1,errors=? WHERE id=?",
                (_now(), _json(errors), scan_id),
            )

    @staticmethod
    def _parse_row(raw: dict) -> tuple[dict | None, dict]:
        raw = {key: _text(value, key) for key, value in raw.items()}
        subscription_id = _text(raw["SubscriptionId"], "SubscriptionId", required=True)
        region = _text(raw["Region"], "Region", required=True)
        catalog = _CATALOG.get(raw["Catalog"].casefold())
        if catalog is None:
            raise ValueError("Catalog must be Listed, No SKUs, Not listed, Empty, or ERROR.")
        model = raw["Model"]
        if catalog not in {"ERROR", "Empty"} and not model:
            raise ValueError("A model name is required for a catalogue entry.")
        if catalog == "Listed" and not raw["SKU"]:
            raise ValueError("A Listed catalogue entry must identify its SKU.")
        if catalog in {"No SKUs", "Not listed"} and raw["SKU"]:
            raise ValueError("A No SKUs or Not listed entry cannot contain a SKU.")
        if catalog == "Empty" and model:
            raise ValueError("An Empty catalogue observation cannot contain a model.")
        numbers = {
            target: _number(raw[source], source)
            for source, target in (("Limit", "quota_limit"), ("Allocated", "allocated"),
                                   ("Remaining", "remaining"))
        }
        quota_status = raw["QuotaStatus"]
        if quota_status:
            states = {"reported": "Reported", "unknown": "Unknown", "error": "ERROR"}
            if quota_status.casefold() not in states:
                raise ValueError("QuotaStatus must be Reported, Unknown, ERROR, or blank.")
            quota_status = states[quota_status.casefold()]
        if quota_status == "Reported" and any(value is None for value in numbers.values()):
            raise ValueError("Reported quota requires Limit, Allocated, and Remaining.")
        if not quota_status and any(value is not None for value in numbers.values()):
            raise ValueError("Numeric quota must have an explicit QuotaStatus.")
        if numbers["remaining"] is not None:
            if numbers["quota_limit"] is None or numbers["allocated"] is None:
                raise ValueError("Remaining requires both Limit and Allocated.")
            expected = max(0, numbers["quota_limit"] - numbers["allocated"])
            if not math.isclose(numbers["remaining"], expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Remaining is inconsistent with Limit and Allocated.")
        coverage = {
            "subscription": raw.get("Subscription") or subscription_id,
            "subscription_id": subscription_id,
            "tenant_id": raw.get("TenantId", ""),
            "region": region,
            "catalog": catalog,
            "quota_status": quota_status or "Not queried",
            "notes": raw.get("Notes", ""),
        }
        if catalog in {"Empty", "ERROR"}:
            return None, coverage
        description = raw.get("QuotaDescription", "")
        provider = raw.get("Format", "")
        row = {
            **{field: coverage[field] for field in
               ("subscription", "subscription_id", "tenant_id", "region")},
            "model": model,
            "version": raw["Version"],
            "family": _family(model, provider),
            "format": provider,
            "deployment_type": _deployment(raw.get("Type", ""), raw["SKU"]),
            "capacity_type": _capacity(raw["SKU"]),
            "sku": raw["SKU"],
            "catalog": catalog,
            "lifecycle": raw.get("Lifecycle", ""),
            **numbers,
            "unit": _unit(raw["Unit"], description),
            "quota_status": quota_status or "Unknown",
            "quota_name": raw["QuotaName"],
            "quota_description": description,
            "account_kinds": [raw["Kind"]] if raw.get("Kind") else [],
            "inference_deprecation": raw.get("InferenceDeprecation", ""),
            "sku_deprecation": raw.get("SkuDeprecation", ""),
            "notes": raw.get("Notes", ""),
            "metadata": _json({key: value for key, value in raw.items()
                               if key not in _REPORT_COLUMNS}),
        }
        row["identity_key"] = _identity(row)
        return row, coverage

    @staticmethod
    def _read_csvs(paths: list[Path]) -> tuple[list[dict], list[dict]]:
        entries, observations = [], []
        for supplied in paths:
            path = Path(supplied)
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle, strict=True)
                    headers = reader.fieldnames
                    if not headers or len(set(headers)) != len(headers):
                        raise ValueError("CSV headers are missing or duplicated.")
                    missing = _REQUIRED_CSV - set(headers)
                    if missing:
                        raise ValueError("CSV is missing required columns: " + ", ".join(sorted(missing)))
                    for raw in reader:
                        if None in raw or any(value is None for value in raw.values()):
                            raise ValueError(f"CSV record at line {reader.line_num} has the wrong field count.")
                        try:
                            entry, observation = Store._parse_row(raw)
                        except ValueError as exc:
                            raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
                        if entry is not None:
                            entries.append(entry)
                        observations.append(observation)
            except (csv.Error, UnicodeError) as exc:
                raise ValueError(f"{path.name}: invalid UTF-8 CSV.") from exc
            except ValueError as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
        return entries, observations

    @staticmethod
    def _deduplicate(entries: list[dict]) -> list[dict]:
        unique: dict[str, dict] = {}
        for entry in entries:
            signature = _json({key: value for key, value in entry.items() if key != "account_kinds"})
            if signature not in unique:
                unique[signature] = dict(entry)
            else:
                unique[signature]["account_kinds"] = sorted(
                    set(unique[signature]["account_kinds"]) | set(entry["account_kinds"])
                )
        return list(unique.values())

    @staticmethod
    def _assign_keys(db: sqlite3.Connection, entries: list[dict]) -> None:
        previous: dict[str, list[dict]] = defaultdict(list)
        groups: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            groups[entry["identity_key"]].append(entry)
        identities = list(groups)
        # A partial scan may omit a scope entirely. Its absence must not erase
        # that scope's last row identities when the next full scan succeeds.
        for offset in range(0, len(identities), 500):
            batch = identities[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for old in db.execute(
                "WITH latest AS (SELECT i.identity_key,MAX(i.scan_id) AS scan_id "
                "FROM inventory i JOIN scans s ON s.id=i.scan_id "
                f"WHERE i.identity_key IN ({placeholders}) "
                "AND s.status IN ('complete','partial') GROUP BY i.identity_key) "
                "SELECT i.* FROM inventory i JOIN latest l ON "
                "i.identity_key=l.identity_key AND i.scan_id=l.scan_id", batch,
            ):
                item = dict(old)
                item["account_kinds"] = json.loads(item["account_kinds"])
                previous[item["identity_key"]].append(item)
        for identity, group in groups.items():
            group.sort(key=lambda item: (_json(item["account_kinds"]), _json(item)))
            old_group = previous[identity]
            candidates = []
            for index, item in enumerate(group):
                for old_index, old in enumerate(old_group):
                    same_kinds = item["account_kinds"] == old["account_kinds"]
                    overlap = bool(set(item["account_kinds"]) & set(old["account_kinds"]))
                    differences = sum(item[field] != old[field] for field in _DATA_FIELDS)
                    candidates.append((not same_kinds, not overlap, differences,
                                       old["key"], index, old_index))
            assigned, used_old = set(), set()
            for _, _, _, key, index, old_index in sorted(candidates):
                if index not in assigned and old_index not in used_old:
                    group[index]["key"] = key
                    assigned.add(index)
                    used_old.add(old_index)
            reserved = {item["key"] for item in old_group}
            reserved.update(item["key"] for item in group if "key" in item)
            for item in group:
                if "key" not in item:
                    key = identity
                    if key in reserved:
                        suffix = hashlib.sha256(_json(item).encode("utf-8")).hexdigest()
                        key = identity + ":" + suffix
                    item["key"] = key
                    reserved.add(key)

    def ingest_csvs(self, scan_id: int, paths: list[Path],
                    failures: list[dict] | None = None,
                    expected_subscriptions: list[str] | None = None) -> dict:
        """Publish every row and all coverage in one transaction, or publish nothing.

        Parsing failures leave the scan running so its caller can record a failure
        with ``fail_scan``. Terminal snapshots are immutable.
        """
        scan_id = _positive_int(scan_id, "scan_id")
        with self._transaction() as db:
            self._require_running(db, scan_id)
        expected = None
        if expected_subscriptions is not None:
            expected = {_text(value, "expected subscription", required=True)
                        for value in expected_subscriptions}
            if len(expected) != len(expected_subscriptions):
                raise ValueError("Expected subscriptions must be unique.")
        parsed, observations = self._read_csvs(paths)
        entries = self._deduplicate(parsed)
        errors: dict[tuple, dict] = {}

        def error(subscription_id: str, region: str, kind: str, message: str):
            key = (subscription_id, region, kind, message)
            errors[key] = dict(subscription_id=subscription_id, region=region,
                               kind=kind, message=message)

        failed_subscriptions = set()
        for failure in failures or []:
            if not isinstance(failure, dict):
                raise ValueError("Each failure must contain subscription_id and message.")
            subscription_id = _text(failure.get("subscription_id"), "failure subscription_id",
                                    required=True)
            message = _text(failure.get("message"), "failure message", required=True)
            failed_subscriptions.add(subscription_id)
            error(subscription_id, "", "subscription", message)
        seen = {item["subscription_id"] for item in observations}
        if expected is not None:
            if (seen | failed_subscriptions) - expected:
                raise ValueError("Report or failure contains an unexpected subscription.")
            for subscription_id in expected - seen - failed_subscriptions:
                failed_subscriptions.add(subscription_id)
                error(subscription_id, "", "subscription", "No report was received for this subscription.")
        tenants: dict[str, set[str]] = defaultdict(set)
        scopes: dict[tuple, list[dict]] = defaultdict(list)
        for observation in observations:
            if observation["tenant_id"]:
                tenants[observation["subscription_id"]].add(observation["tenant_id"])
            scopes[(observation["subscription_id"], observation["region"])].append(observation)
        if any(len(values) > 1 for values in tenants.values()):
            raise ValueError("A subscription cannot belong to multiple tenants in one snapshot.")
        counts: dict[tuple, list[dict]] = defaultdict(list)
        for row in entries:
            counts[(row["subscription_id"], row["region"])].append(row)
        coverage = []
        for (subscription_id, region), group in sorted(scopes.items()):
            catalogs = {item["catalog"] for item in group}
            if "Empty" in catalogs and catalogs - {"Empty", "ERROR"}:
                raise ValueError("A region cannot be both empty and contain catalogue entries.")
            catalog_status = "ERROR" if "ERROR" in catalogs else (
                "Empty" if catalogs == {"Empty"} else "Read"
            )
            quota_states = {item["quota_status"] for item in group}
            quota_status = next((state for state in ("ERROR", "Unknown", "Reported")
                                 if state in quota_states), "Not queried")
            for item in group:
                if item["catalog"] == "ERROR":
                    error(subscription_id, region, "catalog", item["notes"] or "Catalogue request failed.")
                if item["quota_status"] == "ERROR":
                    error(subscription_id, region, "quota", item["notes"] or "Quota request failed.")
            successful = catalog_status != "ERROR" and subscription_id not in failed_subscriptions
            regional_errors = [item for item in errors.values()
                               if item["subscription_id"] == subscription_id
                               and item["region"] in ("", region)]
            regional = counts[(subscription_id, region)]
            coverage.append({
                **{field: group[0][field] for field in
                   ("subscription", "subscription_id", "tenant_id", "region")},
                "catalog_status": catalog_status,
                "quota_status": quota_status,
                "status": "failed" if not successful else
                          ("partial" if quota_status == "ERROR" else "complete"),
                "successful": int(successful),
                "record_count": len(regional),
                "model_count": len({(row["format"], row["model"]) for row in regional}),
                "errors": _json(regional_errors),
                "notes": "; ".join(sorted({item["notes"] for item in group if item["notes"]})),
            })
        for subscription_id in sorted(failed_subscriptions):
            subscription_errors = [item for item in errors.values()
                                   if item["subscription_id"] == subscription_id and item["region"] == ""]
            coverage.append({
                "subscription": subscription_id, "subscription_id": subscription_id,
                "tenant_id": "", "region": "", "catalog_status": "ERROR",
                "quota_status": "Not queried", "status": "failed", "successful": 0,
                "record_count": 0, "model_count": 0, "errors": _json(subscription_errors),
                "notes": "; ".join(item["message"] for item in subscription_errors),
            })
        if not observations and not errors:
            error("", "", "scan", "No report data was provided.")
        successful = any(item["successful"] for item in coverage)
        status = "complete" if successful and not errors else ("partial" if successful else "failed")
        with self._transaction(write=True) as db:
            self._require_running(db, scan_id)
            self._assign_keys(db, entries)
            columns = ("scan_id", "key", "identity_key", *_DATA_FIELDS, "metadata")
            sql = ("INSERT INTO inventory(" + ",".join(columns) + ") VALUES("
                   + ",".join("?" for _ in columns) + ")")
            db.executemany(sql, [
                tuple(scan_id if field == "scan_id" else
                      _json(row[field]) if field == "account_kinds" else row[field]
                      for field in columns)
                for row in entries
            ])
            if coverage:
                columns = ("scan_id", *coverage[0].keys())
                db.executemany(
                    "INSERT INTO coverage(" + ",".join(columns) + ") VALUES("
                    + ",".join("?" for _ in columns) + ")",
                    [tuple(scan_id if field == "scan_id" else item[field] for field in columns)
                     for item in coverage],
                )
            db.execute(
                "UPDATE scans SET completed_at=?,status=?,record_count=?,subscription_count=?,"
                "region_count=?,model_count=?,error_count=?,errors=? WHERE id=?",
                (_now(), status, len(entries), len(seen | failed_subscriptions),
                 len({item["region"] for item in observations}),
                 len({(row["format"], row["model"]) for row in entries}),
                 len(errors), _json(list(errors.values())), scan_id),
            )
            return self._snapshot(db, scan_id)

    def scans(self) -> list[dict]:
        with self._transaction() as db:
            return [self._scan_dict(row) for row in db.execute(
                "SELECT * FROM scans ORDER BY started_at DESC,id DESC"
            )]

    @staticmethod
    def _filters(filters: dict | None, *, grouped: bool = False) -> dict:
        if filters is None:
            filters = {}
        allowed = _FILTER_KEYS | ({"group_by"} if grouped else set())
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise ValueError("Unsupported inventory filter.")
        values = {}
        for key, value in filters.items():
            if key in CATEGORICAL_FILTERS:
                values[key] = _choices(value, key)
            elif key == "model_version":
                values[key] = _model_versions(value)
            else:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise ValueError(f"{key} must be a scalar filter value.")
                values[key] = _text(str(value), key, maximum=4096)
        values["page"] = _positive_int(values.get("page") or "1", "page", 1_000_000)
        values["page_size"] = _positive_int(
            values.get("page_size") or "50", "page_size", 1000 if grouped else 500
        )
        values["sort"] = values.get("sort") or ("label" if grouped else "model")
        values["direction"] = values.get("direction") or "asc"
        if grouped:
            values["group_by"] = values.get("group_by") or "family"
            if values["group_by"] not in _GROUP_COLUMNS:
                raise ValueError("group_by must be family, model, or model_version.")
            if values["sort"] == "model":
                values["sort"] = "label"
        if values["sort"] not in (_GROUP_SORT_COLUMNS if grouped else _SORT_COLUMNS):
            raise ValueError("Unsupported sort column.")
        if values["direction"] not in {"asc", "desc"}:
            raise ValueError("direction must be asc or desc.")
        snapshot = values.get("snapshot") or "latest"
        if snapshot != "latest":
            _positive_int(snapshot, "snapshot")
        values["snapshot"] = snapshot
        for key in (*CATEGORICAL_FILTERS, "model_version"):
            values.setdefault(key, [])
        if len(values.get("q", "")) > 256:
            raise ValueError("q must be at most 256 characters.")
        minimum = values.get("minimum", "")
        values["minimum"] = _number(minimum, "minimum") if minimum else None
        if values["minimum"] is not None and len({unit.casefold() for unit in values["unit"]}) > 1:
            raise ValueError("minimum requires at most one selected quota unit.")
        return values

    @staticmethod
    def _where(scan_id: int, filters: dict) -> tuple[str, list]:
        clauses, params = ["scan_id=?"], [scan_id]
        for key, column in _FILTER_COLUMNS.items():
            values = filters[key]
            if values:
                column = _FAMILY_SQL if key == "family" else column
                placeholders = ",".join("?" for _ in values)
                clause = f"{column} COLLATE NOCASE IN ({placeholders})"
                params.extend(values)
                if key == "subscription":
                    clause = f"({clause} OR subscription COLLATE NOCASE IN ({placeholders}))"
                    params.extend(values)
                clauses.append(clause)
        if filters["model_version"]:
            placeholders = ",".join("(?,?,?)" for _ in filters["model_version"])
            clauses.append(f"(format,model,version) IN (VALUES {placeholders})")
            params.extend(value for triple in filters["model_version"] for value in triple)
        if filters.get("q"):
            text = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            columns = ("model", "version", _FAMILY_SQL, "format", "subscription", "subscription_id",
                       "region", "sku", "quota_name", "quota_description")
            clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns) + ")")
            params.extend(["%" + text + "%"] * len(columns))
        if filters["availability"]:
            availability = (
                "CASE WHEN quota_status!='Reported' OR remaining IS NULL THEN 'unknown' "
                "WHEN remaining>0 THEN 'available' ELSE 'exhausted' END"
            )
            placeholders = ",".join("?" for _ in filters["availability"])
            clauses.append(f"({availability}) IN ({placeholders})")
            params.extend(filters["availability"])
        if filters["minimum"] is not None:
            clauses.append("quota_status='Reported' AND remaining>=?")
            params.append(filters["minimum"])
        return " AND ".join(clauses), params

    @staticmethod
    def _order(filters: dict) -> str:
        column = filters["sort"]
        direction = filters["direction"].upper()
        if column == "model":
            return (
                f"model COLLATE NOCASE {direction},version COLLATE NOCASE {direction},"
                "format COLLATE NOCASE,region COLLATE NOCASE,key"
            )
        if column == "family":
            column = _FAMILY_SQL
        return (f"{column} IS NULL ASC,{column} COLLATE NOCASE {direction},"
                "model COLLATE NOCASE,version COLLATE NOCASE,region COLLATE NOCASE,key")

    @staticmethod
    def _aggregate_sql(where: str, group_fields: tuple[str, ...] = ()) -> str:
        keys = ",".join(group_fields)
        prefix = keys + "," if keys else ""
        group = " GROUP BY " + keys if keys else ""
        join = " USING (" + keys + ")" if keys else " ON 1=1"
        models = ",".join(dict.fromkeys((*group_fields, "format", "model")))
        versions = ",".join(dict.fromkeys((*group_fields, "format", "model", "version")))
        output_keys = "".join("c." + field + "," for field in group_fields)
        return f"""
            WITH selected AS (
                SELECT format,model,version,region,subscription_id,quota_name,quota_status,
                    quota_limit,allocated,remaining,unit,{_FAMILY_SQL} AS family
                FROM inventory WHERE {where}
            ),
            model_counts AS (
                SELECT {prefix}COUNT(*) AS models
                FROM (SELECT DISTINCT {models} FROM selected){group}
            ),
            version_counts AS (
                SELECT {prefix}COUNT(*) AS versions
                FROM (SELECT DISTINCT {versions} FROM selected){group}
            ),
            inventory_counts AS (
                SELECT {prefix}COUNT(*) AS rows,COUNT(DISTINCT region) AS regions,
                    COUNT(DISTINCT subscription_id) AS subscriptions,
                    COUNT(CASE WHEN quota_name='' THEN 1 END) AS unmapped,
                    MIN(family) AS group_family
                FROM selected{group}
            ),
            pools AS (
                SELECT {prefix}subscription_id,region,quota_name,
                    {_POOL_KNOWN_SQL} AS known,
                    MIN(remaining) AS remaining
                FROM selected WHERE quota_name!=''
                GROUP BY {prefix}subscription_id,region,quota_name
            ),
            pool_counts AS (
                SELECT {prefix}COUNT(*) AS quota_pools,
                    COUNT(CASE WHEN known AND remaining>0 THEN 1 END) AS with_headroom,
                    COUNT(CASE WHEN known AND remaining=0 THEN 1 END) AS zero_quota,
                    COUNT(CASE WHEN NOT known THEN 1 END) AS unknown_quota
                FROM pools{group}
            )
            SELECT {output_keys}m.models,v.versions,c.regions,c.subscriptions,c.rows,
                COALESCE(p.quota_pools,0) AS quota_pools,
                COALESCE(p.with_headroom,0) AS with_headroom,
                COALESCE(p.zero_quota,0) AS zero_quota,
                COALESCE(p.unknown_quota,0)+c.unmapped AS unknown_quota,c.group_family
            FROM inventory_counts c
            JOIN model_counts m{join}
            JOIN version_counts v{join}
            LEFT JOIN pool_counts p{join}
        """

    @staticmethod
    def _summary(db: sqlite3.Connection, where: str, params: list) -> dict:
        row = db.execute(Store._aggregate_sql(where), params).fetchone()
        return {field: row[field] for field in _SUMMARY_FIELDS}

    def inventory(self, filters: dict) -> dict:
        filters = self._filters(filters)
        with self._transaction() as db:
            snapshot = self._snapshot(db, filters["snapshot"])
            if snapshot is None:
                summary = {field: 0 for field in _SUMMARY_FIELDS}
                rows = []
            else:
                where, params = self._where(snapshot["id"], filters)
                summary = self._summary(db, where, params)
                rows = [_public_row(row) for row in db.execute(
                    f"SELECT * FROM inventory WHERE {where} ORDER BY {self._order(filters)} LIMIT ? OFFSET ?",
                    [*params, filters["page_size"], (filters["page"] - 1) * filters["page_size"]],
                )]
            return dict(rows=rows, total=summary["rows"], page=filters["page"],
                        page_size=filters["page_size"], snapshot=snapshot, summary=summary)

    def groups(self, filters: dict) -> dict:
        """Roll up all matching entries before pagination; never sum quota units."""
        filters = self._filters(filters, grouped=True)
        group_by = filters["group_by"]
        fields = _GROUP_COLUMNS[group_by]
        rows, total = [], 0
        with self._transaction() as db:
            snapshot = self._snapshot(db, filters["snapshot"])
            if snapshot is None:
                summary = {field: 0 for field in _SUMMARY_FIELDS}
            else:
                where, params = self._where(snapshot["id"], filters)
                summary = self._summary(db, where, params)
                total = db.execute(
                    f"SELECT COUNT(*) FROM (SELECT 1 FROM inventory WHERE {where} "
                    f"GROUP BY {','.join(_FAMILY_SQL if field == 'family' else field for field in fields)})",
                    params,
                ).fetchone()[0]
                label = _VARIANT_LABEL_SQL if group_by == "model_version" else group_by
                sort = _GROUP_SORT_COLUMNS[filters["sort"]]
                order = (
                    f"{sort} COLLATE NOCASE {filters['direction'].upper()},"
                    f"label COLLATE NOCASE,{','.join(fields)}"
                )
                query = (
                    f"SELECT grouped.*,{label} AS label FROM "
                    f"({self._aggregate_sql(where, fields)}) grouped ORDER BY {order} LIMIT ? OFFSET ?"
                )
                for raw in db.execute(query, [
                    *params, filters["page_size"], (filters["page"] - 1) * filters["page_size"],
                ]):
                    row = {field: raw[field] for field in _SUMMARY_FIELDS if field != "rows"}
                    row["entries"] = raw["rows"]
                    if group_by == "model_version":
                        row.update(_variant_option(raw["format"], raw["model"], raw["version"],
                                                   raw["group_family"]))
                        row["filters"] = {"model_version": [[raw[field] for field in fields]]}
                    else:
                        row.update(value=raw[group_by], label=raw["label"],
                                   filters={group_by: [raw[group_by]]})
                        row[group_by] = raw[group_by]
                    rows.append(row)
            return dict(rows=rows, total=total, page=filters["page"], page_size=filters["page_size"],
                        snapshot=snapshot, group_by=group_by, summary=summary)

    def quota(self, filters: dict) -> dict:
        """Show each quota pool once; model filters cannot hide conflicting readings."""
        return self._quota_result(filters)

    def _quota_result(self, filters: dict, *, paginate: bool = True) -> dict:
        requested = filters or {}
        selected = self._filters(filters)
        selected["sort"] = requested.get("sort") or "remaining"
        selected["direction"] = requested.get("direction") or "desc"
        if selected["sort"] not in {"remaining", "allocated", "quota_limit", "region", "subscription", "unit"}:
            raise ValueError("Unsupported quota sort column.")
        scope = {**selected, "availability": [], "minimum": None}
        rows = []
        with self._transaction() as db:
            snapshot = self._snapshot(db, selected["snapshot"])
            if snapshot is not None:
                where, parameters = self._where(snapshot["id"], scope)
                query = f"""
                    WITH selected AS (
                        SELECT *,{_POOL_KEY_SQL} AS pool_key FROM inventory WHERE {where}
                    ), matches AS (
                        SELECT subscription_id,region,pool_key,MIN(subscription) AS subscription,
                            COUNT(*) AS matching_entries,
                            json_group_array(DISTINCT json_array(
                                format,model,version,family,lifecycle,catalog,sku
                            )) AS choices_json,
                            json_group_array(DISTINCT sku) AS skus_json,
                            json_group_array(DISTINCT deployment_type) AS types_json,
                            json_group_array(DISTINCT capacity_type) AS capacities_json
                        FROM selected GROUP BY subscription_id,region,pool_key
                    ), readings AS (
                        SELECT *,{_POOL_KEY_SQL} AS pool_key FROM inventory WHERE scan_id=?
                    ), pools AS (
                        SELECT subscription_id,region,pool_key,MIN(quota_name) AS quota_name,
                            {_POOL_KNOWN_SQL} AND MIN(unit!='') AS known,
                            MIN(quota_limit) AS quota_limit,MIN(allocated) AS allocated,
                            MIN(remaining) AS remaining,
                            json_group_array(DISTINCT unit) AS units_json,
                            COUNT(DISTINCT json_array(format,model,version)) AS sharing_choices,
                            SUM(CASE WHEN quota_status='ERROR' THEN 1 ELSE 0 END) AS errors
                        FROM readings JOIN matches USING(subscription_id,region,pool_key)
                        GROUP BY subscription_id,region,pool_key
                    )
                    SELECT pools.*,matches.subscription,matches.matching_entries,matches.choices_json,
                        matches.skus_json,matches.types_json,matches.capacities_json
                    FROM pools JOIN matches USING(subscription_id,region,pool_key)
                """
                for raw in db.execute(query, [*parameters, snapshot["id"]]):
                    known = bool(raw["known"] and raw["quota_name"])
                    availability = ("available" if raw["remaining"] > 0 else "exhausted") if known else "unknown"
                    if selected["availability"] and availability not in selected["availability"]:
                        continue
                    if selected["minimum"] is not None and (not known or raw["remaining"] < selected["minimum"]):
                        continue
                    choices = {}
                    for provider, model, version, family, lifecycle, catalog, sku in json.loads(raw["choices_json"]):
                        key = (provider, model, version)
                        if key not in choices:
                            choices[key] = {**_variant_option(provider, model, version, family),
                                            "lifecycles": set(), "catalogs": set()}
                        choices[key]["lifecycles"].add(lifecycle or "Not reported")
                        choices[key]["catalogs"].add(catalog)
                    for choice in choices.values():
                        choice["lifecycles"] = sorted(choice["lifecycles"])
                        choice["catalogs"] = sorted(choice["catalogs"])
                    units = sorted(unit for unit in json.loads(raw["units_json"]) if unit)
                    note = ""
                    if not raw["quota_name"]:
                        note = "No quota pool was reported. This entry cannot be combined with other unknown entries."
                    elif not known:
                        note = "Pool readings are incomplete or inconsistent; no quota amount is inferred."
                    current = sum(
                        "Listed" in choice["catalogs"] and bool(
                            {"GenerallyAvailable", "Stable", "Preview"} & set(choice["lifecycles"])
                        ) for choice in choices.values()
                    )
                    rows.append({
                        "key": _json([raw["subscription_id"], raw["region"], raw["pool_key"]]),
                        "subscription_id": raw["subscription_id"], "subscription": raw["subscription"],
                        "region": raw["region"], "quota_name": raw["quota_name"],
                        "quota_limit": raw["quota_limit"] if known else None,
                        "allocated": raw["allocated"] if known else None,
                        "remaining": raw["remaining"] if known else None,
                        "unit": units[0] if len(units) == 1 else "Mixed" if units else "",
                        "availability": availability, "notes": note,
                        "skus": sorted(json.loads(raw["skus_json"])),
                        "deployment_types": sorted(json.loads(raw["types_json"])),
                        "capacity_types": sorted(json.loads(raw["capacities_json"])),
                        "choices": sorted(choices.values(), key=lambda choice: (choice["label"].casefold(), choice["format"])),
                        "sharing_choices": raw["sharing_choices"], "current_choices": current,
                        "matching_entries": raw["matching_entries"],
                    })

            variants = {(choice["format"], choice["model"], choice["version"])
                        for row in rows for choice in row["choices"]}
            summary = {
                "models": len({(provider, model) for provider, model, version in variants}),
                "versions": len(variants), "regions": len({row["region"] for row in rows}),
                "subscriptions": len({row["subscription_id"] for row in rows}),
                "rows": sum(row["matching_entries"] for row in rows),
                "quota_pools": sum(bool(row["quota_name"]) for row in rows),
                "with_headroom": sum(row["availability"] == "available" for row in rows),
                "zero_quota": sum(row["availability"] == "exhausted" for row in rows),
                "unknown_quota": sum(row["availability"] == "unknown" for row in rows),
            }
            column, descending = selected["sort"], selected["direction"] == "desc"
            rows.sort(key=lambda row: (row["region"].casefold(), row["subscription"].casefold(), row["key"]))
            if column in _NUMERIC_FIELDS:
                rows.sort(key=lambda row: (
                    row[column] is None,
                    (-row[column] if descending else row[column]) if row[column] is not None else 0,
                ))
                rows.sort(key=lambda row: row["unit"].casefold())
            else:
                rows.sort(key=lambda row: row[column].casefold(), reverse=descending)
            ranks = {"available": 0, "exhausted": 2, "unknown": 3}
            rows.sort(key=lambda row: 1 if row["availability"] == "available" and not row["current_choices"]
                      else ranks[row["availability"]])
            total = len(rows)
            if paginate:
                start = (selected["page"] - 1) * selected["page_size"]
                rows = rows[start:start + selected["page_size"]]
            return dict(rows=rows, total=total, page=selected["page"], page_size=selected["page_size"],
                        snapshot=snapshot, summary=summary)

    def export_quota_csv(self, filters: dict) -> str:
        result = self._quota_result(filters, paginate=False)
        fields = ("subscription", "subscription_id", "region", "quota_name", "unit",
                  "quota_limit", "allocated", "remaining", "availability", "sharing_choices",
                  "matching_model_versions", "skus", "notes")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in result["rows"]:
            record = {key: row.get(key, "") for key in fields}
            record["matching_model_versions"] = "; ".join(choice["label"] for choice in row["choices"])
            record["skus"] = "; ".join(row["skus"])
            writer.writerow({key: _spreadsheet(value) for key, value in record.items()})
        return output.getvalue()

    def facets(self, snapshot: str | int = "latest") -> dict:
        with self._transaction() as db:
            selected = self._snapshot(db, snapshot)
            result = {field: [] for field in _FILTER_COLUMNS}
            result["model_version"] = []
            if selected is None:
                return result
            scan_id = selected["id"]
            result["subscription"] = [
                {"value": row["subscription_id"], "label": row["subscription"]}
                for row in db.execute(
                    "SELECT subscription_id,COALESCE("
                    "MIN(CASE WHEN subscription!=subscription_id THEN subscription END),"
                    "MIN(subscription)) AS subscription FROM coverage "
                    "WHERE scan_id=? GROUP BY subscription_id ORDER BY subscription COLLATE NOCASE",
                    (scan_id,),
                )
            ]
            for field in _FILTER_COLUMNS:
                if field == "subscription":
                    continue
                table = "coverage" if field in {"tenant", "region"} else "inventory"
                column = _FAMILY_SQL if field == "family" else _FILTER_COLUMNS[field]
                result[field] = [row[0] for row in db.execute(
                    f"SELECT DISTINCT {column} AS {field} FROM {table} WHERE scan_id=? AND {column}!='' "
                    f"ORDER BY {field} COLLATE NOCASE", (scan_id,),
                )]
            result["model_version"] = [
                _variant_option(row["format"], row["model"], row["version"], row["family"])
                for row in db.execute(
                    f"SELECT format,model,version,MIN(family) AS family,{_VARIANT_LABEL_SQL} AS label "
                    "FROM inventory WHERE scan_id=? GROUP BY format,model,version "
                    "ORDER BY label COLLATE NOCASE,format COLLATE NOCASE,format,model,version",
                    (scan_id,),
                )
            ]
            return result

    def coverage(self, snapshot: str | int = "latest") -> dict:
        """Return observed regions and explicit subscription failures.

        ``successful`` means the catalogue was observed: an Empty catalogue is
        successful, whereas a failed subscription has an empty region. Quota
        errors make a scope partial without making catalogue absence ambiguous.
        """
        with self._transaction() as db:
            selected = self._snapshot(db, snapshot)
            rows = []
            if selected is not None:
                for raw in db.execute(
                    "SELECT * FROM coverage WHERE scan_id=? "
                    "ORDER BY subscription COLLATE NOCASE,region COLLATE NOCASE", (selected["id"],),
                ):
                    row = dict(raw)
                    del row["scan_id"]
                    row["successful"] = bool(row["successful"])
                    row["errors"] = json.loads(row["errors"])
                    rows.append(row)
            return {"rows": rows, "snapshot": selected}

    def compare(self, older: int, newer: int, filters: dict) -> dict:
        older, newer = _positive_int(older, "from"), _positive_int(newer, "to")
        filters = self._filters(filters)
        with self._transaction() as db:
            before_snapshot = self._snapshot(db, older)
            after_snapshot = self._snapshot(db, newer)
            scopes = []
            quota_errors = False
            for scan_id in (older, newer):
                rows = list(db.execute("SELECT * FROM coverage WHERE scan_id=?", (scan_id,)))
                scopes.append({(row["subscription_id"], row["region"]) for row in rows
                               if row["successful"] and row["region"]})
                quota_errors |= any(row["quota_status"] == "ERROR" for row in rows)
            common = scopes[0] & scopes[1]
            warnings = []
            if scopes[0] != scopes[1]:
                warnings.append(
                    f"Scope differs: {len(scopes[0] - common)} older and "
                    f"{len(scopes[1] - common)} newer subscription/region scopes were excluded. "
                    "Only common successfully observed catalogue scopes are comparable."
                )
            if not common:
                warnings.append("No common successfully observed subscription/region scope; "
                                "no additions or removals can be inferred.")
            if before_snapshot["status"] != "complete" or after_snapshot["status"] != "complete":
                warnings.append("At least one snapshot is incomplete. Missing or failed scopes are "
                                "not evidence that models or quota were removed.")
            if quota_errors:
                warnings.append("Quota requests failed in at least one snapshot; unknown quota is "
                                "not zero capacity. Quota-status changes are shown explicitly.")
            matched_keys = set()
            inventories = []
            for scan_id in (older, newer):
                where, params = self._where(scan_id, filters)
                matched_keys.update(row[0] for row in db.execute(
                    f"SELECT key FROM inventory WHERE {where}", params
                ))
                inventories.append({row["key"]: _public_row(row) for row in db.execute(
                    "SELECT * FROM inventory WHERE scan_id=?", (scan_id,)
                )})
            changes, excluded = [], set()
            summary = dict(added=0, removed=0, changed=0, quota_changed=0, uncomparable=0)
            for key in sorted(matched_keys):
                before, after = inventories[0].get(key), inventories[1].get(key)
                representative = after or before
                scope = (representative["subscription_id"], representative["region"])
                if scope not in common:
                    excluded.add(key)
                    continue
                fields = []
                if before is None:
                    change = "added"
                elif after is None:
                    change = "removed"
                else:
                    fields = [field for field in _DATA_FIELDS if before[field] != after[field]]
                    if not fields:
                        continue
                    change = "changed"
                    if set(fields) & _QUOTA_FIELDS:
                        summary["quota_changed"] += 1
                summary[change] += 1
                changes.append(dict(change=change, key=key, before=before, after=after, fields=fields))
            summary["uncomparable"] = len(excluded)
            sort = filters["sort"]

            def sort_value(item):
                value = (item["after"] or item["before"])[sort]
                return (value or 0) if sort in _NUMERIC_FIELDS | {"id"} else str(value).casefold()

            changes.sort(key=lambda item: str((item["after"] or item["before"])["key"]))
            changes.sort(key=sort_value, reverse=filters["direction"] == "desc")
            changes.sort(key=lambda item: (item["after"] or item["before"])[sort] is None)
            offset = (filters["page"] - 1) * filters["page_size"]
            return {
                "from_snapshot": before_snapshot, "to_snapshot": after_snapshot,
                "summary": summary, "rows": changes[offset:offset + filters["page_size"]],
                "warnings": warnings, "total": len(changes), "page": filters["page"],
                "page_size": filters["page_size"],
            }

    def history(self, filters: dict) -> dict:
        """Trend complete snapshots only; partial observations are not a zero dip."""
        filters = self._filters(filters)
        points = []
        with self._transaction() as db:
            for snapshot in db.execute(
                "SELECT id,started_at FROM scans WHERE status='complete' ORDER BY started_at,id"
            ).fetchall():
                where, params = self._where(snapshot["id"], filters)
                summary = self._summary(db, where, params)
                points.append(dict(id=snapshot["id"], started_at=snapshot["started_at"],
                                   model_count=summary["models"], record_count=summary["rows"],
                                   quota_pools=summary["quota_pools"],
                                   with_headroom=summary["with_headroom"]))
        return {"points": points}

    def export_csv(self, filters: dict) -> str:
        filters = self._filters(filters)
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(ROW_FIELDS)
        with self._transaction() as db:
            snapshot = self._snapshot(db, filters["snapshot"])
            if snapshot is not None:
                where, params = self._where(snapshot["id"], filters)
                for raw in db.execute(
                    f"SELECT * FROM inventory WHERE {where} ORDER BY {self._order(filters)}", params
                ):
                    row = _public_row(raw)
                    writer.writerow([_spreadsheet(row[field]) for field in ROW_FIELDS])
        return output.getvalue()
