"""Local, read-only Azure collection and current-user morning scheduling.

Only the checked-in PowerShell collectors are executable from this interface.
Configuration, progress, and generated artifacts stay below the repository's
ignored data directory. No Azure CLI default account or credentials are changed.
"""

from __future__ import annotations

import base64
import csv
import ctypes
import errno
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Protocol
from uuid import UUID, uuid4
from datetime import datetime, timezone


TASK_NAME = "FoundryModelInventory-MorningScan"
DEFAULT_TIME = "07:00"
COMMAND_TIMEOUT = 60
SUBSCRIPTION_TIMEOUT = 50 * 60
SCAN_TIMEOUT = 170 * 60
SCHEDULE_NOTE = (
    "You must be signed into Windows and Azure CLI authentication must be valid. "
    "Times are local; missed runs start when the machine is available."
)
_WINDOWS = os.name == "nt"
_LOG = logging.getLogger(__name__)
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_SECRET = re.compile(
    r"""(?ix)
    (["']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|
       password|authorization|api[_-]?key|connectionstring|device[_-]?code|
       user[_-]?code|sig)["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;&}]+)
    """
)
_DISCOVER_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
& $env:FOUNDRY_INVENTORY_AZ_CLI account list --all --output json --only-show-errors
exit $LASTEXITCODE
""".strip()


class StoreInterface(Protocol):
    def start_scan(self, source: str, started_at: str | None = None) -> int: ...

    def ingest_csvs(
        self,
        scan_id: int,
        paths: list[Path],
        failures: list[dict] | None = None,
        expected_subscriptions: list[str] | None = None,
    ) -> dict: ...

    def fail_scan(self, scan_id: int, message: str) -> None: ...


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = _SECRET.sub(r"\1[REDACTED]", text)
    return re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED]",
        text,
    )


def _detail(value: Any, limit: int = 1400) -> str:
    text = _redact(value).strip()
    return text if len(text) <= limit else "[truncated] " + text[-limit:]


def _guid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _GUID.fullmatch(value):
        raise ValueError(f"{field} must be a UUID in hyphenated form.")
    result = str(UUID(value))
    if UUID(result).int == 0:
        raise ValueError(f"{field} must not be the empty UUID.")
    return result


def _clock(value: Any) -> str:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("Morning time must be HH:MM (00:00 through 23:59), local time.")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    staging = path.with_name(f".{path.name}.{uuid4().hex}.new")
    try:
        with staging.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


class _FileLock:
    """A held handle, not a PID or sentinel file, determines lock ownership."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> "_FileLock":
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError(
                    "A collection or configuration change is already running."
                ) from None
            raise
        self.handle = handle
        return self

    def release(self) -> None:
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "_FileLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


class _WindowsJob:
    """Ensure terminating the collector also terminates its PowerShell/az tree."""

    def __init__(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io_counters", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            if process.poll() is None:
                raise error

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


class Collector:
    def __init__(self, store: StoreInterface, repo_root: Path, data_dir: Path):
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        if not self.data_dir.is_relative_to(self.repo_root / "data"):
            raise ValueError("All local data must remain under the repository's data directory.")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.json"
        self.status_path = self.data_dir / "collection-status.json"
        self.lock_path = self.data_dir / "collection.lock"
        self._thread: threading.Thread | None = None

    @staticmethod
    def _validate_config(payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Configuration must be a JSON object.")
        if set(payload) - {"tenant_id", "subscriptions", "morning_time"}:
            raise ValueError("Configuration contains unsupported fields.")
        tenant = _guid(payload.get("tenant_id"), "tenant_id")
        subscriptions = payload.get("subscriptions")
        if not isinstance(subscriptions, list) or not 1 <= len(subscriptions) <= 100:
            raise ValueError("Configure between 1 and 100 subscriptions.")
        seen = set()
        scope = []
        for item in subscriptions:
            if not isinstance(item, dict) or set(item) - {"id", "name"}:
                raise ValueError("Each subscription must contain only id and name.")
            subscription = _guid(item.get("id"), "Subscription id")
            if subscription in seen:
                raise ValueError("Duplicate subscription ids are not allowed.")
            name = item.get("name", subscription)
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 256
                or any(ord(char) < 32 for char in name)
            ):
                raise ValueError("Subscription names must be nonempty, single-line text.")
            seen.add(subscription)
            scope.append({"id": subscription, "name": name.strip()})
        return {
            "tenant_id": tenant,
            "subscriptions": scope,
            "morning_time": _clock(payload.get("morning_time", DEFAULT_TIME)),
        }

    @staticmethod
    def _read_json(path: Path) -> Any:
        if path.stat().st_size > 256 * 1024:
            raise ValueError(f"{path.name} exceeds the supported size.")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed {path.name}; repair the local JSON file.") from exc

    def load_config(self) -> dict:
        if not self.config_path.exists():
            return {"tenant_id": "", "subscriptions": [], "morning_time": DEFAULT_TIME}
        return self._validate_config(self._read_json(self.config_path))

    def save_config(self, payload: dict) -> dict:
        config = self._validate_config(payload)
        with _FileLock(self.lock_path):
            _atomic_json(self.config_path, config)
        return config

    def _powershell(self) -> str:
        names = ("pwsh.exe", "powershell.exe") if _WINDOWS else ("pwsh", "powershell")
        for name in names:
            executable = shutil.which(name)
            if executable:
                path = Path(executable).resolve()
                if path.is_file():
                    return str(path)
        raise RuntimeError("PowerShell 7 (pwsh.exe) or Windows PowerShell is required.")

    def _script(self, relative: str) -> Path:
        path = (self.repo_root / relative).resolve()
        if not path.is_relative_to(self.repo_root) or not path.is_file():
            raise RuntimeError(f"Required checked-in script is unavailable: {relative}")
        return path

    def _ps_file(self, relative: str) -> list[str]:
        return [
            self._powershell(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(self._script(relative)),
        ]

    def _run_command(
        self, command: list[str], timeout: float, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        job = _WindowsJob() if _WINDOWS else None
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if _WINDOWS else 0,
                start_new_session=not _WINDOWS,
            )
            if job:
                job.assign(process)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if job:
                    job.close()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The process group exited at the timeout boundary.
                stdout, stderr = process.communicate(timeout=15)
                raise subprocess.TimeoutExpired(command, timeout, stdout, stderr) from None
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            if job:
                job.close()
            if process is not None:
                if process.poll() is None:
                    if not _WINDOWS:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=15)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def _json_command(self, command: list[str], label: str, env: dict | None = None) -> Any:
        try:
            result = self._run_command(command, COMMAND_TIMEOUT, env)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{label} timed out after {COMMAND_TIMEOUT} seconds.") from None
        except OSError as exc:
            raise RuntimeError(f"{label} could not start: {_detail(exc)}") from None
        if result.returncode:
            reason = _detail(result.stderr or result.stdout) or "No diagnostic output."
            raise RuntimeError(f"{label} failed (exit {result.returncode}): {reason}")
        try:
            return json.loads(result.stdout.lstrip("\ufeff"))
        except (ValueError, AttributeError):
            raise RuntimeError(f"{label} returned invalid JSON, not a successful response.") from None

    def discover_subscriptions(self) -> list[dict]:
        executable = shutil.which("az")
        if not executable:
            raise RuntimeError("Azure CLI is unavailable. Install it and sign in with az login.")
        # PowerShell invokes even az.cmd through a fixed command; no browser text
        # enters shell source or command-line arguments to the batch interpreter.
        encoded = base64.b64encode(_DISCOVER_SCRIPT.encode("utf-16-le")).decode("ascii")
        command = [
            self._powershell(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded,
        ]
        env = os.environ.copy()
        env["FOUNDRY_INVENTORY_AZ_CLI"] = str(Path(executable).resolve())
        accounts = self._json_command(command, "Azure subscription discovery", env)
        if not isinstance(accounts, list):
            raise RuntimeError("Azure subscription discovery returned an unexpected account list.")
        discovered = []
        for account in accounts:
            if not isinstance(account, dict):
                raise RuntimeError("Azure subscription discovery returned malformed account metadata.")
            try:
                subscription = _guid(account.get("id"), "Subscription id")
                tenant = _guid(account.get("tenantId"), "Tenant id")
            except ValueError:
                raise RuntimeError(
                    "Azure subscription discovery returned malformed account scope."
                ) from None
            name, state = account.get("name"), account.get("state")
            if not isinstance(name, str) or not isinstance(state, str) or not name or not state:
                raise RuntimeError("Azure subscription discovery returned incomplete account metadata.")
            discovered.append({
                "id": subscription, "name": name, "tenant_id": tenant, "state": state,
            })
        return discovered

    @staticmethod
    def _idle_state() -> dict:
        return {
            "running": False, "scan_id": None, "message": "No collection has run.",
            "progress": {"completed": 0, "total": 0, "subscription": None},
            "last_error": None, "pid": None,
        }

    def _read_status(self) -> dict:
        if not self.status_path.exists():
            return self._idle_state()
        status = self._read_json(self.status_path)
        if (
            not isinstance(status, dict)
            or type(status.get("running")) is not bool
            or not isinstance(status.get("progress"), dict)
            or not {"scan_id", "message", "last_error"}.issubset(status)
        ):
            raise ValueError("Malformed collection-status.json; repair the local status file.")
        return status

    @staticmethod
    def _interrupted(status: dict) -> dict:
        return {
            **status, "running": False,
            "message": "Previous collection was interrupted; no collector holds the lock.",
            "last_error": "The collector exited before recording completion. Start a new scan.",
        }

    def state(self) -> dict:
        lock = _FileLock(self.lock_path)
        try:
            lock.acquire()
        except RuntimeError:
            status = self._read_status()
            if not status["running"]:
                return {
                    **status, "running": True,
                    "message": "Another process is starting collection or updating configuration.",
                }
            return status
        try:
            status = self._read_status()
            # A GET can report stale status without mutating scan history.
            return self._interrupted(status) if status["running"] else status
        finally:
            lock.release()

    def _write_status(self, status: dict) -> None:
        status["updated_at"] = _utcnow()
        _atomic_json(self.status_path, status)

    def _begin_scan(self, source: str) -> tuple[_FileLock, dict, dict, Path]:
        if (
            not isinstance(source, str) or not source.strip() or len(source) > 64
            or any(ord(char) < 32 for char in source)
        ):
            raise ValueError("Collection source must be nonempty, single-line text (at most 64 characters).")
        lock = _FileLock(self.lock_path).acquire()
        scan_id = None
        status = None
        try:
            config = self.load_config()
            if not config["subscriptions"]:
                raise ValueError("Configure a tenant and subscriptions before collecting.")
            previous = self._read_status()
            if previous["running"]:
                interrupted = self._interrupted(previous)
                if type(previous["scan_id"]) is int:
                    read_scans = getattr(self.store, "scans", None)
                    # SQLite may have committed before the status-file rename.
                    finalized = callable(read_scans) and any(
                        scan.get("id") == previous["scan_id"]
                        and scan.get("status") in ("complete", "partial", "failed")
                        for scan in read_scans()
                    )
                    if not finalized:
                        self.store.fail_scan(previous["scan_id"], interrupted["last_error"])
                self._write_status(interrupted)
            started = _utcnow()
            scan_id = self.store.start_scan(source, started_at=started)
            if type(scan_id) is not int or scan_id <= 0:
                raise RuntimeError("The scan store returned an invalid scan id.")
            status = {
                "running": True, "scan_id": scan_id, "source": source,
                "started_at": started, "pid": os.getpid(), "last_error": None,
                "message": "Starting collection.",
                "progress": {
                    "completed": 0, "total": len(config["subscriptions"]), "subscription": None,
                },
            }
            run_dir = self.data_dir / "runs" / str(scan_id)
            run_dir.mkdir(parents=True, exist_ok=False)
            self._write_status(status)
            return lock, config, status, run_dir
        except Exception as exc:
            try:
                if status is not None:
                    self._fail(status, exc)
            finally:
                lock.release()
            raise

    def start_scan(self, source: str = "manual") -> dict:
        lock, config, status, run_dir = self._begin_scan(source)
        initial = {**status, "progress": dict(status["progress"])}
        try:
            self._thread = threading.Thread(
                target=self._background_scan,
                args=(lock, config, status, run_dir),
                name=f"foundry-collection-{status['scan_id']}",
                daemon=True,
            )
            self._thread.start()
        except Exception as exc:
            try:
                self._fail(status, exc)
            finally:
                lock.release()
            raise
        return initial

    def _background_scan(self, lock: _FileLock, config: dict, status: dict, run_dir: Path) -> None:
        try:
            self._collect(lock, config, status, run_dir)
        except Exception as exc:
            _LOG.error("Collection failed: %s", _detail(exc))

    def run_scan(self, source: str = "scheduled") -> dict:
        return self._collect(*self._begin_scan(source))

    def _fail(self, status: dict, error: Any) -> None:
        message = _detail(error) or "Collection failed."
        status.update(running=False, message="Collection failed.", last_error=message)
        try:
            self.store.fail_scan(status["scan_id"], message)
        finally:
            self._write_status(status)

    @staticmethod
    def _inspect_csv(path: Path, subscription: str, tenant: str) -> dict[str, dict]:
        regions: dict[str, dict] = {}
        required = {"SubscriptionId", "TenantId", "Region", "Model", "Catalog", "QuotaStatus"}
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("Inventory CSV is missing required columns.")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Inventory CSV contains an incomplete row.")
                if (
                    _guid(row["SubscriptionId"], "CSV subscription") != subscription
                    or _guid(row["TenantId"], "CSV tenant") != tenant
                ):
                    raise ValueError("Inventory CSV scope does not match the requested subscription/tenant.")
                region = row["Region"]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", region):
                    raise ValueError("Inventory CSV contains an invalid or empty region.")
                entry = regions.setdefault(region, {"rows": 0, "catalog": 0, "quota": 0, "unknown": 0})
                entry["rows"] += 1
                entry["catalog"] += row["Catalog"] == "ERROR"
                entry["quota"] += row["QuotaStatus"] == "ERROR"
                entry["unknown"] += row["QuotaStatus"] == "Unknown"
        if not regions:
            raise ValueError("Inventory CSV has no observations.")
        return regions

    @staticmethod
    def _inspect_coverage(path: Path, subscription: str, regions: dict) -> None:
        seen = set()
        required = {"SubscriptionId", "Region", "CatalogStatus", "Rows", "QuotaErrors", "QuotaUnknown"}
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("Coverage CSV is missing required columns.")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Coverage CSV contains an incomplete row.")
                region = row["Region"]
                if (
                    _guid(row["SubscriptionId"], "Coverage subscription") != subscription
                    or region not in regions or region in seen
                ):
                    raise ValueError("Coverage CSV scope does not match inventory.")
                actual = regions[region]
                if (
                    int(row["Rows"]) != actual["rows"]
                    or int(row["QuotaErrors"]) != actual["quota"]
                    or int(row["QuotaUnknown"]) != actual["unknown"]
                    or row["CatalogStatus"] not in ("Read", "Empty", "ERROR")
                    or (row["CatalogStatus"] == "ERROR") != bool(actual["catalog"])
                ):
                    raise ValueError("Coverage CSV counts do not match inventory.")
                seen.add(region)
        if seen != set(regions):
            raise ValueError("Coverage CSV omits observed regions.")

    def _subscription(
        self, subscription: dict, tenant: str, run_dir: Path, timeout: float
    ) -> tuple[Path | None, str | None]:
        subscription_id = subscription["id"]
        command = self._ps_file("run-foundry-subscription-inventory.ps1") + [
            "-TenantId", tenant, "-SubscriptionId", subscription_id,
            "-ReportScript", str(self._script("check-foundry-model-availability.ps1")),
            "-OutputDirectory", str(run_dir),
        ]
        problems = []
        stdout = stderr = ""
        try:
            result = self._run_command(command, timeout)
            stdout, stderr = result.stdout, result.stderr
            if result.returncode:
                reason = _detail(stderr or stdout) or "No diagnostic output."
                problems.append(f"Collector exited {result.returncode}: {reason}")
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout, exc.stderr
            problems.append(f"Subscription collection timed out after {int(timeout)} seconds.")
        except OSError as exc:
            problems.append(f"Could not launch subscription collector: {_detail(exc)}")
        for suffix, text in (("stdout.log", stdout), ("stderr.log", stderr)):
            (run_dir / f"{subscription_id}.{suffix}").write_text(_redact(text), encoding="utf-8")
        transcript = run_dir / f"{subscription_id}.log"
        if transcript.exists():
            text = transcript.read_text(encoding="utf-8-sig", errors="replace")
            transcript.write_text(_redact(text), encoding="utf-8")
        path: Path | None = run_dir / f"{subscription_id}.csv"
        try:
            regions = self._inspect_csv(path, subscription_id, tenant)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            problems.append(f"Inventory CSV is unavailable or invalid: {_detail(exc)}")
            path = None
        if path is not None:
            try:
                self._inspect_coverage(run_dir / f"{subscription_id}-coverage.csv", subscription_id, regions)
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                problems.append(f"Coverage CSV is unavailable or invalid: {_detail(exc)}")
        return path, _detail("; ".join(problems)) if problems else None

    def _collect(self, lock: _FileLock, config: dict, status: dict, run_dir: Path) -> dict:
        paths = []
        failures = []
        ingested = False
        deadline = time.monotonic() + SCAN_TIMEOUT
        try:
            for subscription in config["subscriptions"]:
                status["progress"]["subscription"] = subscription["id"]
                status["message"] = f"Collecting {subscription['name']}."
                self._write_status(status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    path, error = None, "The overall collection time limit was reached."
                else:
                    path, error = self._subscription(
                        subscription, config["tenant_id"], run_dir,
                        min(SUBSCRIPTION_TIMEOUT, remaining),
                    )
                if path is not None:
                    paths.append(path)
                if error:
                    failures.append({"subscription_id": subscription["id"], "message": error})
                    status["last_error"] = error
                status["progress"]["completed"] += 1
                self._write_status(status)
            result = self.store.ingest_csvs(
                status["scan_id"], paths, failures=failures,
                expected_subscriptions=[item["id"] for item in config["subscriptions"]],
            )
            ingested = True
            outcome = result.get("status")
            if outcome not in ("complete", "partial", "failed"):
                raise RuntimeError("The scan store returned an invalid completion status.")
            if failures and outcome == "complete":
                raise RuntimeError("The scan store marked an incomplete collection as complete.")
            status.update(
                running=False,
                message="Collection complete." if outcome == "complete" else f"Collection {outcome}; inspect scan errors.",
                last_error=None if outcome == "complete" else (
                    _detail("; ".join(item["message"] for item in failures))
                    or f"Collection {outcome}; catalog or quota errors are recorded in scan history."
                ),
            )
            status["progress"]["subscription"] = None
            self._write_status(status)
            return result
        except Exception as exc:
            if not ingested:
                self._fail(status, exc)
            raise RuntimeError(_detail(exc)) from None
        finally:
            try:
                close = getattr(self.store, "close", None)
                if callable(close):
                    close()
            finally:
                lock.release()

    def _schedule_command(self, switch: str, clock: str) -> list[str]:
        if switch not in ("-Status", "-Disable", "-Enabled"):
            raise ValueError("Unsupported scheduling operation.")
        command = self._ps_file(str(Path("scripts") / "register-morning-scan.ps1")) + [
            switch, "-Time", _clock(clock),
        ]
        # PythonPath selects the helper's Enable parameter set.
        if switch == "-Enabled":
            command.extend([
                "-PythonPath", str(Path(sys.executable).resolve()),
                "-RepoRoot", str(self.repo_root), "-DataDirectory", str(self.data_dir),
            ])
        return command

    @staticmethod
    def _schedule_result(result: Any) -> dict:
        fields = {"enabled", "time", "task_name", "next_run", "last_run", "last_result", "note"}
        if (
            not isinstance(result, dict) or not fields.issubset(result)
            or type(result["enabled"]) is not bool or result["task_name"] != TASK_NAME
            or result["last_result"] is not None and type(result["last_result"]) is not int
            or not isinstance(result["note"], str)
            or any(result[key] is not None and not isinstance(result[key], str) for key in ("next_run", "last_run"))
        ):
            raise RuntimeError("Task Scheduler returned an unexpected status schema.")
        try:
            _clock(result["time"])
        except ValueError:
            raise RuntimeError("Task Scheduler returned an invalid local time.") from None
        return {key: _detail(result[key]) if key == "note" else result[key] for key in fields}

    def schedule_status(self) -> dict:
        clock = self.load_config()["morning_time"]
        if not _WINDOWS:
            return {
                "enabled": False, "time": clock, "task_name": TASK_NAME,
                "next_run": None, "last_run": None, "last_result": None,
                "note": "Windows Task Scheduler is unavailable on this platform. " + SCHEDULE_NOTE,
            }
        return self._schedule_result(
            self._json_command(self._schedule_command("-Status", clock), "Task Scheduler query")
        )

    def set_schedule(self, enabled: bool, time: str) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Schedule enabled must be a JSON boolean.")
        clock = _clock(time)
        if not _WINDOWS:
            raise RuntimeError("Morning scheduling requires Windows Task Scheduler.")
        with _FileLock(self.lock_path):
            config = self.load_config()
            if enabled and not config["subscriptions"]:
                raise ValueError("Configure subscriptions before enabling morning collection.")
            result = self._schedule_result(self._json_command(
                self._schedule_command("-Enabled" if enabled else "-Disable", clock),
                "Task Scheduler update",
            ))
            if result["enabled"] != enabled:
                raise RuntimeError("Task Scheduler did not apply the requested enabled state.")
            if enabled and result["time"] != clock:
                raise RuntimeError("Task Scheduler did not apply the requested local time.")
            if config["subscriptions"]:
                config["morning_time"] = clock
                _atomic_json(self.config_path, config)
            return result
