"""Read-only hosted mode for Azure App Service with built-in authentication.

One container serves the dashboard and collects the configured scope once a day.
App Service authentication signs people in with Microsoft Entra; this module
re-checks the identity headers the platform injects, accepts only configured
Host names and refuses every change over HTTP. The live SQLite database stays on
the container's local disk. After each finished collection a verified copy is
written to the persistent /home share, and restored when a new container starts.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta, timezone, tzinfo
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .collector import COMMAND_TIMEOUT, Collector, _clock, _detail, _guid, _redact
from .server import _Handler, _HTTPError, _LocalServer
from .store import Store


_LOG = logging.getLogger(__name__)
HOSTED_PORT = 8000
PLATFORM_PING = "/robots933456.txt"
DEFAULT_BACKUP_DIR = "/home/foundry-inventory"
BACKUP_NAME = "inventory.sqlite3"
KEEP_RUNS = 7
RETENTION_DAYS = 90
RETENTION_RANGE = (7, 3650)
MAX_PRINCIPAL = 32 * 1024
MAX_SCOPE = 64 * 1024
TENANT_CLAIMS = frozenset({"http://schemas.microsoft.com/identity/claims/tenantid", "tid"})
NAME_CLAIMS = (
    "name", "preferred_username",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)
READ_ONLY = (
    "The hosted dashboard is read-only. Its collection scope and schedule are "
    "managed through the deployment, not the browser."
)
SIGN_IN_FAILED = (
    "Hosted collection could not sign in with the web app's system-assigned managed identity"
)
_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PAGES = frozenset({"/", "/index.html"})
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?\Z")
_COMMIT = re.compile(r"[0-9a-f]{7,40}\Z")
_STANDARD_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
_URLSAFE_B64 = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normal_host(value: str) -> str:
    value = value.strip().lower()
    return value[:-4] if value.endswith(":443") else value


def _single_line(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
    return text[:limit]


@dataclass(frozen=True)
class HostedSettings:
    tenant_id: str
    scope: dict
    allowed_hosts: frozenset
    timezone_name: str
    zone: tzinfo
    backup_dir: Path
    commit: str
    retention_days: int = RETENTION_DAYS


def parse_scope(raw: str | None) -> dict:
    """Parse FOUNDRY_INVENTORY_SCOPE; the Collector validates the values when saving."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("FOUNDRY_INVENTORY_SCOPE must hold the hosted collection scope as JSON.")
    if len(raw) > MAX_SCOPE:
        raise ValueError("FOUNDRY_INVENTORY_SCOPE exceeds the supported size.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise ValueError("FOUNDRY_INVENTORY_SCOPE is not valid JSON without duplicate keys.") from None
    if not isinstance(payload, dict):
        raise ValueError("FOUNDRY_INVENTORY_SCOPE must be a JSON object.")
    if set(payload) - {"tenant_id", "subscriptions", "morning_time"}:
        raise ValueError(
            "FOUNDRY_INVENTORY_SCOPE supports only tenant_id, subscriptions and morning_time; "
            "the hosted service manages its own Azure CLI profile."
        )
    tenant = _guid(payload.get("tenant_id"), "FOUNDRY_INVENTORY_SCOPE tenant_id")
    for item in payload.get("subscriptions") or []:
        other = item.get("tenant_id") if isinstance(item, dict) else None
        if isinstance(other, str) and other.lower() != tenant:
            raise ValueError(
                "The hosted service collects one tenant with its managed identity; "
                "remove subscriptions that belong to other tenants."
            )
    return payload


def parse_hosts(environ: Mapping[str, str]) -> frozenset:
    values = [environ.get("WEBSITE_HOSTNAME", "")]
    values += environ.get("FOUNDRY_INVENTORY_ALLOWED_HOSTS", "").split(",")
    hosts = set()
    for value in values:
        value = value.strip().lower()
        if not value:
            continue
        if not _HOST.fullmatch(value) or ".." in value:
            raise ValueError(
                "Allowed host names must be DNS names with an optional port, "
                f"not {value[:80]!r}."
            )
        hosts.add(_normal_host(value))
    if not hosts:
        raise ValueError(
            "Set WEBSITE_HOSTNAME (App Service sets it) or FOUNDRY_INVENTORY_ALLOWED_HOSTS "
            "so the hosted dashboard can check Host headers."
        )
    return frozenset(hosts)


def load_zone(value: str | None) -> tuple[str, tzinfo]:
    name = (value or "").strip().removeprefix(":")
    if not name or name.upper() in {"UTC", "ETC/UTC"}:
        return "UTC", timezone.utc
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "TZ must be an IANA time zone available in the image, for example America/Chicago."
        ) from None


def build_commit(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if _COMMIT.fullmatch(value) else "unknown"


def parse_retention(value: str | None) -> int:
    """Days of snapshots the hosted database keeps; the latest complete one is always kept."""
    text = (value or "").strip()
    if not text:
        return RETENTION_DAYS
    low, high = RETENTION_RANGE
    if not re.fullmatch(r"[0-9]{1,5}", text) or not low <= int(text) <= high:
        raise ValueError(f"FOUNDRY_INVENTORY_RETENTION_DAYS must be a whole number of days from {low} to {high}.")
    return int(text)


def load_settings(environ: Mapping[str, str]) -> HostedSettings:
    """Read hosted settings from App Service application settings; fail closed."""
    tenant = _guid(environ.get("FOUNDRY_INVENTORY_AUTH_TENANT_ID"), "FOUNDRY_INVENTORY_AUTH_TENANT_ID")
    backup = environ.get("FOUNDRY_INVENTORY_BACKUP_DIR", DEFAULT_BACKUP_DIR)
    if (not backup or any(ord(char) < 32 for char in backup)
            or not Path(backup).is_absolute()):
        raise ValueError("FOUNDRY_INVENTORY_BACKUP_DIR must be an absolute directory path.")
    zone_name, zone = load_zone(environ.get("TZ"))
    return HostedSettings(
        tenant_id=tenant,
        scope=parse_scope(environ.get("FOUNDRY_INVENTORY_SCOPE")),
        allowed_hosts=parse_hosts(environ),
        timezone_name=zone_name,
        zone=zone,
        backup_dir=Path(backup),
        commit=build_commit(environ.get("FOUNDRY_INVENTORY_COMMIT")),
        retention_days=parse_retention(environ.get("FOUNDRY_INVENTORY_RETENTION_DAYS")),
    )


def _decode_principal(value: str) -> dict:
    malformed = "The App Service sign-in header is malformed; sign in again."
    text = value.strip()
    if not text or len(text) > MAX_PRINCIPAL:
        raise _HTTPError(401, malformed)
    padded = text + "=" * (-len(text) % 4)
    try:
        if _STANDARD_B64.fullmatch(padded):
            raw = base64.b64decode(padded, validate=True)
        elif _URLSAFE_B64.fullmatch(padded):
            raw = base64.urlsafe_b64decode(padded)
        else:
            raise ValueError("Not Base64.")
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise _HTTPError(401, malformed) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        raise _HTTPError(401, malformed)
    return payload


def authenticate(headers, tenant_id: str) -> str:
    """Return the caller's display name, or raise unless App Service signed in the tenant."""
    providers = headers.get_all("X-MS-CLIENT-PRINCIPAL-IDP") or []
    principals = headers.get_all("X-MS-CLIENT-PRINCIPAL") or []
    if not providers and not principals:
        raise _HTTPError(401, "Sign in with Microsoft Entra through App Service authentication.")
    if len(providers) != 1 or len(principals) != 1:
        raise _HTTPError(401, "App Service authentication headers are missing or repeated.")
    if providers[0].strip().lower() != "aad":
        raise _HTTPError(403, "Only Microsoft Entra accounts can use this dashboard.")
    payload = _decode_principal(principals[0])
    tenants = set()
    names = {}
    for claim in payload["claims"][:1000]:
        if not isinstance(claim, dict):
            continue
        kind, value = claim.get("typ"), claim.get("val")
        if not isinstance(kind, str) or not isinstance(value, str):
            continue
        if kind in TENANT_CLAIMS:
            tenants.add(value.strip().lower())
        elif kind in NAME_CLAIMS:
            names.setdefault(kind, value)
    if tenants != {tenant_id.lower()}:
        raise _HTTPError(403, "Your Microsoft Entra tenant is not allowed to use this dashboard.")
    header_names = headers.get_all("X-MS-CLIENT-PRINCIPAL-NAME") or []
    candidates = header_names[:1] + [names[kind] for kind in NAME_CLAIMS if kind in names]
    return next((name for name in map(_single_line, candidates) if name), "")


def scheduled_slots(moment: datetime, morning_time: str, zone: tzinfo) -> tuple[datetime, datetime]:
    """Return the latest scheduled time at or before moment and the next one, in UTC."""
    if moment.tzinfo is None:
        raise ValueError("Scheduling needs a timezone-aware time.")
    hour, minute = (int(part) for part in _clock(morning_time).split(":"))
    day = moment.astimezone(zone).date()
    instant = moment.astimezone(timezone.utc)

    def at(value):
        # Comparing in UTC keeps daylight-saving transitions unambiguous.
        return datetime.combine(value, wall_time(hour, minute), tzinfo=zone).astimezone(timezone.utc)

    today = at(day)
    if today <= instant:
        return today, at(day + timedelta(days=1))
    return at(day - timedelta(days=1)), today


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DailyScheduler:
    """Run one collection per scheduled local time; never two at once."""

    def __init__(self, attempt: Callable[[], Any], latest_complete: Callable[[], str | None],
                 morning_time: str, zone: tzinfo, *, now: Callable[[], datetime] = _utcnow,
                 wait: Callable[[float], bool] | None = None):
        self.morning_time = _clock(morning_time)
        self.zone = zone
        self._attempt = attempt
        self._latest_complete = latest_complete
        self._now = now
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self.running = False

    def slots(self, moment: datetime | None = None) -> tuple[datetime, datetime]:
        return scheduled_slots(moment or self._now(), self.morning_time, self.zone)

    def next_run(self) -> str:
        return self.slots()[1].astimezone(self.zone).isoformat()

    def startup_due(self) -> bool:
        """Collect at startup unless a complete snapshot began after the latest scheduled time."""
        previous, _ = self.slots()
        latest = self._latest_complete()
        return not latest or _parse_instant(latest) < previous

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            due = self.startup_due()
        except Exception:
            _LOG.exception("Snapshot history is unreadable; collecting now.")
            due = True
        anchor = self._run_once() if due else self._now()
        while not self._stop.is_set():
            _, target = self.slots(anchor)
            if self._sleep_until(target):
                return
            anchor = self._run_once()

    def _sleep_until(self, target: datetime) -> bool:
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return self._stop.is_set()
            # Short waits re-read the wall clock after sleep or clock adjustments.
            if self._wait(min(remaining, 300)):
                return True

    def _run_once(self) -> datetime:
        self.running = True
        try:
            self._attempt()
        except Exception:
            _LOG.exception("Hosted collection attempt failed unexpectedly.")
        finally:
            self.running = False
        return self._now()


def _verify_database(path: Path) -> str | None:
    try:
        with closing(sqlite3.connect(path)) as db:
            result = db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                return "integrity check failed"
            db.execute("SELECT COUNT(*) FROM scans").fetchone()
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {_detail(exc, 200)}"
    return None


def _copy_file(source: Path, target: Path) -> None:
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _remove_database(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class DatabaseBackup:
    """Keep a verified copy of the local database on persistent storage.

    SQLite never opens files on the network share: the backup API writes a local
    staging copy, which is verified and then copied and atomically renamed.
    """

    def __init__(self, database: Path, backup_dir: Path, staging_dir: Path):
        self.database = Path(database).resolve()
        self.directory = Path(backup_dir)
        self.target = self.directory / BACKUP_NAME
        self.staging = Path(staging_dir)
        if self.target.resolve() == self.database:
            raise ValueError("The backup must not replace the live database.")
        self.enabled = True
        self._lock = threading.Lock()
        self._state = {"saved_at": None, "restored_at": None, "bytes": None, "error": None}

    def state(self) -> dict:
        with self._lock:
            return {**self._state, "enabled": self.enabled}

    def _record(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def _pause(self, message: str) -> None:
        self.enabled = False
        self._record(error=message)
        _LOG.error("%s", message)

    def restore_if_missing(self) -> bool:
        """Restore the persisted copy when this container has no local database."""
        if self.database.exists():
            return False
        try:
            if not self.target.is_file():
                _LOG.info("No persisted database backup was found; starting a new inventory.")
                return False
        except OSError as exc:
            self._pause(f"Persistent storage is unavailable; backups are paused until the next "
                        f"restart: {_detail(exc, 300)}")
            return False
        self.staging.mkdir(parents=True, exist_ok=True)
        candidate = self.staging / f"restore-{uuid4().hex}.sqlite3"
        try:
            try:
                _copy_file(self.target, candidate)
            except OSError as exc:
                self._pause(f"The database backup could not be read; backups are paused until "
                            f"the next restart so it is not overwritten: {_detail(exc, 300)}")
                return False
            problem = _verify_database(candidate)
            if problem:
                aside = self.target.with_name(
                    f"{BACKUP_NAME}.invalid-{_utcnow().strftime('%Y%m%dT%H%M%SZ')}"
                )
                try:
                    os.replace(self.target, aside)
                except OSError as exc:
                    self._pause(f"The database backup is invalid ({problem}) and could not be set "
                                f"aside; backups are paused: {_detail(exc, 300)}")
                    return False
                self._record(error=f"The database backup was invalid ({problem}); it was kept as "
                                   f"{aside.name} and a new inventory was started.")
                _LOG.error("Set aside an invalid database backup as %s.", aside.name)
                return False
            for suffix in ("-journal", "-wal", "-shm"):
                Path(f"{self.database}{suffix}").unlink(missing_ok=True)
            os.replace(candidate, self.database)
            size = self.database.stat().st_size
            self._record(restored_at=_utcnow().isoformat(), bytes=size)
            _LOG.info("Restored the inventory database from persistent storage (%d bytes).", size)
            return True
        finally:
            _remove_database(candidate)

    def save(self) -> bool:
        """Write a consistent, verified copy and atomically replace the previous backup."""
        if not self.enabled:
            return False
        with self._lock:
            self.staging.mkdir(parents=True, exist_ok=True)
            snapshot = self.staging / f"backup-{uuid4().hex}.sqlite3"
            pending = self.target.with_name(f".{BACKUP_NAME}.{uuid4().hex}.tmp")
            try:
                with closing(sqlite3.connect(self.database)) as source, \
                        closing(sqlite3.connect(snapshot)) as copy:
                    source.backup(copy)
                    copy.execute("PRAGMA journal_mode=DELETE").fetchone()
                problem = _verify_database(snapshot)
                if problem:
                    raise RuntimeError(f"the new copy failed verification ({problem})")
                self.directory.mkdir(parents=True, exist_ok=True)
                _copy_file(snapshot, pending)
                os.replace(pending, self.target)
                size = self.target.stat().st_size
                self._state.update(saved_at=_utcnow().isoformat(), bytes=size, error=None)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                self._state["error"] = f"The latest database backup failed: {_detail(exc, 300)}"
                _LOG.error("%s", self._state["error"])
                return False
            finally:
                _remove_database(snapshot)
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    _LOG.warning("A partial backup file could not be removed.")
        _LOG.info("Saved a verified database backup to persistent storage (%d bytes).", size)
        return True


def prune_runs(runs: Path, keep: int = KEEP_RUNS) -> None:
    """Keep only the most recent collection diagnostics on the container's disk."""
    try:
        entries = sorted(
            (path for path in runs.iterdir()
             if path.name.isdigit() and path.is_dir() and not path.is_symlink()),
            key=lambda path: int(path.name), reverse=True,
        )
    except FileNotFoundError:
        return
    for old in entries[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _cli_error(text: str, limit: int = 480) -> str:
    """Summarize Azure CLI failure output without its traceback."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    errors = [line.removeprefix("ERROR:").strip() for line in lines if line.startswith("ERROR:")]
    specific = [line for line in errors if "unexpected error" not in line.lower()]
    exceptions = [line for line in lines if re.match(r"[A-Za-z_][\w.]*(?:Error|Exception)\b", line)]
    summary = next(iter(specific), None) or (exceptions[-1] if exceptions else None) \
        or next(iter(errors), None) or (lines[-1] if lines else "No diagnostic output.")
    summary = _redact(summary).strip()
    return summary if len(summary) <= limit else summary[:limit - 3].rstrip() + "..."


class HostedCollector(Collector):
    """Collect with the App Service managed identity; nothing is started over HTTP."""

    def __init__(self, store, repo_root: Path, data_dir: Path):
        super().__init__(store, repo_root, data_dir)
        self.profile_dir = self.data_dir / "azure-cli"

    def start_scan(self, source: str = "manual") -> dict:
        raise RuntimeError(READ_ONLY)

    def set_schedule(self, enabled: bool, time: str) -> dict:
        raise RuntimeError(READ_ONLY)

    def discover_subscriptions(self, azure_config_dir: str | None = None) -> list[dict]:
        raise RuntimeError("Subscription discovery is disabled in the hosted dashboard.")

    def _sign_in(self, config: dict) -> None:
        executable = shutil.which("az")
        if not executable:
            raise RuntimeError("Azure CLI is not installed in the container image.")
        command = [
            executable, "login", "--identity", "--allow-no-subscriptions",
            "--output", "none", "--only-show-errors",
        ]
        result = self._run_command(
            command, COMMAND_TIMEOUT, self._azure_environment(config["azure_config_dir"])
        )
        if result.returncode:
            reason = _cli_error(result.stderr or result.stdout)
            raise RuntimeError(f"az login --identity exited {result.returncode}: {reason}")

    def run_scan(self, source: str = "scheduled") -> dict:
        lock, config, status, run_dir = self._begin_scan(source)
        try:
            status["message"] = "Signing in with the web app's managed identity."
            self._write_status(status)
            self._sign_in(config)
        except Exception as exc:
            try:
                detail = _detail(exc, 900).strip()
                ending = "" if detail.endswith(".") else "."
                self._fail(status, f"{SIGN_IN_FAILED}: {detail}{ending} It retries at the next "
                                   "scheduled collection or when the web app restarts.")
            finally:
                try:
                    close = getattr(self.store, "close", None)
                    if callable(close):
                        close()
                finally:
                    lock.release()
            raise RuntimeError(f"{SIGN_IN_FAILED}.") from None
        return self._collect(lock, config, status, run_dir)


def apply_scope(collector: HostedCollector, scope: dict) -> dict:
    """Validate the environment's scope with the Collector and save it for this container."""
    collector.profile_dir.mkdir(mode=0o700, exist_ok=True)
    return collector.save_config({**scope, "azure_config_dir": str(collector.profile_dir)})


def latest_complete(store) -> str | None:
    try:
        return next((scan["started_at"] for scan in store.scans() if scan["status"] == "complete"), None)
    finally:
        store.close()


def apply_retention(store, days: int, now: Callable[[], datetime] = _utcnow) -> int:
    """Keep the hosted database, and so its /home copy, within a bounded size."""
    try:
        removed = store.delete_snapshots_before(now() - timedelta(days=days))
    except Exception as exc:
        # Clean-up must never stop the backup that follows it.
        _LOG.error("Snapshots older than %d days could not be removed: %s", days, _detail(exc))
        return 0
    finally:
        store.close()
    if removed:
        _LOG.info("Removed %d snapshot(s) older than %d days.", removed, days)
    return removed


def collect_once(collector: HostedCollector, backup: DatabaseBackup | None,
                 retention_days: int = RETENTION_DAYS) -> None:
    try:
        result = collector.run_scan(source="scheduled")
        _LOG.info("Hosted collection finished with status %s.", result.get("status"))
    except Exception as exc:
        _LOG.error("Hosted collection did not complete: %s", _detail(exc))
    finally:
        apply_retention(collector.store, retention_days)
        if backup is not None:
            backup.save()
        prune_runs(collector.data_dir / "runs")


class _HostedHandler(_Handler):
    principal = ""

    def _respond(self, status: int, body: bytes, content_type: str, headers: dict | None = None):
        extra = {"Strict-Transport-Security": "max-age=31536000"}
        extra.update(headers or {})
        super()._respond(status, body, content_type, extra)

    def _security(self, mutation: bool = False):
        host = self._one_header("Host", required=True)
        if _normal_host(host) not in self.server.allowed_hosts:
            raise _HTTPError(403, "This host name is not configured for the hosted dashboard.")
        self.principal = authenticate(self.headers, self.server.settings.tenant_id)
        if self.headers.get_all("Origin"):
            scheme = "https" if self._one_header("X-Forwarded-Proto").lower() == "https" else "http"
            origin = self._one_header("Origin").lower()
            if origin not in {f"{scheme}://{host.strip().lower()}", f"{scheme}://{_normal_host(host)}"}:
                raise _HTTPError(403, "Cross-origin requests are not allowed.")
        site = self._one_header("Sec-Fetch-Site")
        if site and site not in {"same-origin", "none"} and not self._page_navigation():
            raise _HTTPError(403, "Cross-site requests are not allowed.")
        if mutation:
            raise _HTTPError(403, READ_ONLY)

    def _page_navigation(self) -> bool:
        """Top-level GET navigations may open the dashboard page from another site.

        Signing in returns through login.microsoftonline.com, so the page load after it
        is cross-site. Framing stays blocked, and the API and assets stay same-origin.
        """
        return (self.command == "GET"
                and self._one_header("Sec-Fetch-Mode") == "navigate"
                and self._one_header("Sec-Fetch-Dest") == "document"
                and urlsplit(self.path).path in _PAGES)

    def _dispatch(self):
        # The platform's container ping arrives without identity; it learns nothing.
        if urlsplit(self.path).path == PLATFORM_PING:
            raise _HTTPError(404, "Route not found.")
        self._security(mutation=self.command in _MUTATIONS)
        if self.command != "GET":
            raise _HTTPError(405, "The hosted dashboard supports GET requests only.")
        path, parameters = self._target()
        self._get(path, parameters)

    def _get(self, path: str, parameters: dict):
        if path == "/api/subscriptions":
            raise _HTTPError(403, "Subscription discovery is disabled. " + READ_ONLY)
        if path == "/api/status":
            self._only(parameters, set())
            result = self.server.status()
            result["hosted"]["user"] = self.principal
            self._json(200, result)
            return
        super()._get(path, parameters)

    def _post(self, path: str, parameters: dict):
        raise _HTTPError(403, READ_ONLY)


class _HostedServer(_LocalServer):
    bind_address = "0.0.0.0"

    def __init__(self, store, collector, static_dir: Path, port: int, settings: HostedSettings,
                 scheduler: DailyScheduler | None, backup: DatabaseBackup | None,
                 bind_address: str = "0.0.0.0"):
        self.settings = settings
        self.scheduler = scheduler
        self.backup = backup
        self.bind_address = bind_address
        super().__init__(store, collector, static_dir, port)
        self.allowed_hosts = settings.allowed_hosts
        self.csrf_token = ""

    @staticmethod
    def handler_class():
        return _HostedHandler

    def schedule_status(self, config: dict) -> dict:
        clock = config.get("morning_time")
        return {
            "available": True, "enabled": True, "time": clock, "task_name": None,
            "next_run": self.scheduler.next_run() if self.scheduler else None,
            "last_run": None, "last_result": None,
            "note": f"The hosted service collects every day at {clock} ({self.settings.timezone_name}).",
        }

    def status(self) -> dict:
        result = super().status()
        config = {key: value for key, value in result["config"].items() if key != "azure_config_dir"}
        scans = self.store.scans()
        attempt = scans[0] if scans else None
        try:
            database_bytes = Path(self.store.db_path).stat().st_size
        except OSError:
            database_bytes = None
        result.update(config=config, csrf_token="", read_only=True)
        result["hosted"] = {
            "commit": self.settings.commit,
            "timezone": self.settings.timezone_name,
            "collection_time": config.get("morning_time"),
            "next_run": result["schedule"]["next_run"],
            "last_attempt": {
                key: attempt[key]
                for key in ("status", "source", "started_at", "completed_at", "error_count")
            } if attempt else None,
            "database_bytes": database_bytes,
            "retention_days": self.settings.retention_days,
            "backup": self.backup.state() if self.backup else None,
            "user": "",
        }
        return result


def create_hosted_server(store, collector, static_dir: Path, settings: HostedSettings,
                         scheduler: DailyScheduler | None = None,
                         backup: DatabaseBackup | None = None, port: int = HOSTED_PORT,
                         bind_address: str = "0.0.0.0"):
    """Create, but do not run, the hosted server; tests may bind loopback on port 0."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535.")
    return _HostedServer(store, collector, static_dir, port, settings, scheduler, backup,
                         bind_address)


def run(repo_root: Path, data_dir: Path, port: int = HOSTED_PORT,
        environ: Mapping[str, str] | None = None) -> int:
    """Serve the read-only dashboard on 0.0.0.0 and collect daily until stopped."""
    settings = load_settings(os.environ if environ is None else environ)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "inventory.sqlite3"
    backup = DatabaseBackup(database, settings.backup_dir, data_dir / "backup-staging")
    backup.restore_if_missing()
    store = Store(database)
    scheduler = server = None
    try:
        collector = HostedCollector(store, repo_root, data_dir)
        config = apply_scope(collector, settings.scope)
        store.close()
        scheduler = DailyScheduler(
            lambda: collect_once(collector, backup, settings.retention_days), lambda: latest_complete(store),
            config["morning_time"], settings.zone,
        )
        server = create_hosted_server(
            store, collector, Path(repo_root) / "dashboard" / "static", settings,
            scheduler, backup, port=port,
        )

        def stop(signum, frame):
            raise SystemExit(0)

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, stop)
        threading.Thread(target=scheduler.run, name="foundry-hosted-scheduler", daemon=True).start()
        _LOG.info(
            "Hosted Foundry inventory %s is listening on 0.0.0.0:%d; it collects %d "
            "subscription(s) daily at %s (%s) and keeps %d days of snapshots.", settings.commit,
            server.server_address[1], len(config["subscriptions"]), config["morning_time"],
            settings.timezone_name, settings.retention_days,
        )
        server.serve_forever(poll_interval=0.5)
        return 0
    finally:
        if scheduler is not None:
            scheduler.stop()
        if server is not None:
            server.server_close()
        store.close()
