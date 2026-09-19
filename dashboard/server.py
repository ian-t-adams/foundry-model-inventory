"""A bounded, same-origin HTTP API that can bind only to IPv4 loopback."""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import CATEGORICAL_FILTERS


_LOGGER = logging.getLogger(__name__)
_LOCAL_TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 64 * 1024
MAX_QUERY = 32 * 1024
MAX_QUERY_FIELDS = 1024
MAX_STATIC = 2 * 1024 * 1024
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/controls.js": ("controls.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_GET_ROUTES = {
    "/api/status", "/api/scans", "/api/inventory", "/api/facets", "/api/coverage",
    "/api/compare", "/api/history", "/api/export.csv", "/api/subscriptions", "/api/groups",
    "/api/quota", "/api/quota.csv",
}
_POST_ROUTES = {"/api/config", "/api/scan", "/api/schedule"}


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, store, collector, static_dir: Path, port: int):
        self.store = store
        self.collector = collector
        self.static_dir = Path(static_dir).resolve()
        self.csrf_token = _LOCAL_TOKEN
        self._slots = threading.BoundedSemaphore(32)
        self._status_lock = threading.Lock()
        self._status_cache = None
        self._status_time = 0.0
        super().__init__(("127.0.0.1", port), _Handler)
        port = self.server_address[1]
        self.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            try:
                self.store.close()
            finally:
                self._slots.release()

    def invalidate_status(self):
        with self._status_lock:
            self._status_cache = None

    def status(self) -> dict:
        with self._status_lock:
            if self._status_cache is None or time.monotonic() - self._status_time > 15:
                config = self.collector.load_config()
                try:
                    schedule = self.collector.schedule_status()
                except (RuntimeError, OSError):
                    _LOGGER.exception("Local scheduler status is unavailable.")
                    schedule = {
                        "available": False, "enabled": None,
                        "time": config.get("morning_time"), "task_name": None,
                        "next_run": None, "last_run": None, "last_result": None,
                        "note": "Schedule status is unavailable; check the local server log.",
                        "error": "Unable to read the local scheduler.",
                    }
                self._status_cache = {"config": config, "schedule": schedule}
                self._status_time = time.monotonic()
            cached = dict(self._status_cache)
        collection = dict(self.collector.state())
        scans = self.store.scans()
        latest = next((scan for scan in scans if scan["status"] == "complete"), None)
        if scans and scans[0]["status"] in {"failed", "partial"} and not collection.get("running"):
            attempt = scans[0]
            collection["scan_id"] = attempt["id"]
            collection["message"] = (
                f"Most recent snapshot attempt is {attempt['status']}. "
                + ("The last complete snapshot remains selected." if latest else
                   "There is no complete snapshot yet.")
            )
            collection["last_error"] = "; ".join(
                item["message"] for item in attempt["errors"]
            ) or collection.get("last_error", "")
        return {
            "configured": bool(cached["config"].get("tenant_id") and
                               cached["config"].get("subscriptions")),
            "config": cached["config"], "latest": latest, "collection": collection,
            "schedule": cached["schedule"], "csrf_token": self.csrf_token,
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FoundryInventory"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        # Do not log private query values, request headers, or routine polling.
        pass

    def send_error(self, code, message=None, explain=None):
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Invalid request"
        self._json(code, {"error": phrase})

    def _respond(self, status: int, body: bytes, content_type: str,
                 headers: dict | None = None):
        self.close_connection = True
        self.send_response(status)
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _one_header(self, name: str, required: bool = False) -> str:
        values = self.headers.get_all(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            raise _HTTPError(400, f"A single {name} header is required.")
        return values[0] if values else ""

    def _security(self, mutation: bool = False):
        host = self._one_header("Host", required=True)
        if host not in self.server.allowed_hosts:
            raise _HTTPError(403, "Host must be localhost or 127.0.0.1 with the server's exact port.")
        origin = self._one_header("Origin")
        expected = "http://" + host
        if ((self.headers.get_all("Origin") and origin != expected)
                or (mutation and origin != expected)):
            raise _HTTPError(403, "This request requires the exact same local Origin.")
        site = self._one_header("Sec-Fetch-Site")
        if site and site not in {"same-origin", "none"}:
            raise _HTTPError(403, "Cross-site requests are not allowed.")
        if mutation:
            token = self._one_header("X-Local-Token")
            if len(token) > 512 or not hmac.compare_digest(
                token.encode("utf-8"), self.server.csrf_token.encode("ascii")
            ):
                raise _HTTPError(403, "A valid X-Local-Token from /api/status is required.")

    def _target(self) -> tuple[str, dict]:
        if len(self.path) > MAX_QUERY:
            raise _HTTPError(400, "Request URL is too long.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise _HTTPError(400, "Only local, origin-form request paths are allowed.")
        try:
            values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False,
                              max_num_fields=MAX_QUERY_FIELDS, encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise _HTTPError(400, "Invalid query parameters.") from exc
        if any(len(items) != 1 and key not in CATEGORICAL_FILTERS for key, items in values.items()):
            raise _HTTPError(400, "Only categorical filter parameters may be repeated.")
        return parsed.path, {key: items[0] if len(items) == 1 else items
                             for key, items in values.items()}

    @staticmethod
    def _only(parameters: dict, allowed: set[str]):
        if set(parameters) - allowed:
            raise _HTTPError(400, "Unsupported query parameters.")

    def _body(self) -> dict:
        if self.headers.get_all("Transfer-Encoding"):
            raise _HTTPError(400, "Transfer-Encoding is not supported; use Content-Length.")
        content_type = self._one_header("Content-Type", required=True)
        if not re.fullmatch(r"application/json(?:;\s*charset=utf-8)?", content_type, re.I):
            raise _HTTPError(400, "Content-Type must be application/json with UTF-8 encoding.")
        length = self._one_header("Content-Length", required=True)
        if not re.fullmatch(r"[0-9]+", length, re.ASCII):
            raise _HTTPError(400, "Content-Length must be a nonnegative integer.")
        if len(length) > 8 or int(length) > MAX_BODY:
            raise _HTTPError(400, f"JSON request bodies are limited to {MAX_BODY} bytes.")
        count = int(length)
        raw = self.rfile.read(count)
        if len(raw) != count:
            raise _HTTPError(400, "The JSON request body is incomplete.")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key.")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError("Non-finite JSON value.")

        try:
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _HTTPError(400, "Body must be valid UTF-8 JSON without duplicate keys.") from exc
        if not isinstance(body, dict):
            raise _HTTPError(400, "The JSON body must be an object.")
        return body

    def _get(self, path: str, parameters: dict):
        store, collector = self.server.store, self.server.collector
        if path in _STATIC:
            name, content_type = _STATIC[path]
            try:
                file = (self.server.static_dir / name).resolve(strict=True)
                if file.parent != self.server.static_dir or not file.is_file():
                    raise _HTTPError(404, "Static file not found.")
                with file.open("rb") as handle:
                    body = handle.read(MAX_STATIC + 1)
                if len(body) > MAX_STATIC:
                    raise _HTTPError(500, "Static asset exceeds the local server's size limit.")
            except (FileNotFoundError, NotADirectoryError):
                raise _HTTPError(404, "Static file not found.") from None
            self._respond(200, body, content_type)
            return
        if path not in _GET_ROUTES:
            raise _HTTPError(404, "Route not found.")
        if path in {"/api/status", "/api/scans", "/api/subscriptions"}:
            self._only(parameters, set())
        if path in {"/api/facets", "/api/coverage"}:
            self._only(parameters, {"snapshot"})
        if path == "/api/status":
            result = self.server.status()
        elif path == "/api/scans":
            result = {"scans": store.scans()}
        elif path == "/api/inventory":
            result = store.inventory(parameters)
        elif path == "/api/groups":
            result = store.groups(parameters)
        elif path == "/api/quota":
            result = store.quota(parameters)
        elif path == "/api/facets":
            result = store.facets(parameters.get("snapshot", "latest"))
        elif path == "/api/coverage":
            result = store.coverage(parameters.get("snapshot", "latest"))
        elif path == "/api/history":
            result = store.history(parameters)
        elif path == "/api/compare":
            if "from" not in parameters or "to" not in parameters:
                raise _HTTPError(400, "Comparison requires from and to snapshot IDs.")
            result = store.compare(parameters.pop("from"), parameters.pop("to"), parameters)
        elif path in {"/api/export.csv", "/api/quota.csv"}:
            quota_export = path == "/api/quota.csv"
            content = store.export_quota_csv(parameters) if quota_export else store.export_csv(parameters)
            filename = "foundry-quota.csv" if quota_export else "foundry-inventory.csv"
            self._respond(
                200, content.encode("utf-8"), "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{filename}"'},
            )
            return
        else:
            result = {"subscriptions": collector.discover_subscriptions()}
        self._json(200, result)

    def _post(self, path: str, parameters: dict):
        if path not in _POST_ROUTES:
            raise _HTTPError(404, "Route not found.")
        self._only(parameters, set())
        body = self._body()
        collector = self.server.collector
        if path == "/api/config":
            result = {"config": collector.save_config(body)}
        elif path == "/api/scan":
            if body:
                raise _HTTPError(400, "Scan accepts an empty JSON object only.")
            try:
                result = collector.start_scan(source="manual")
            except RuntimeError as exc:
                raise _HTTPError(409, "A scan is already running or cannot be started; "
                                 "check collection status.") from exc
            self.server.invalidate_status()
            self._json(202, result)
            return
        else:
            if set(body) != {"enabled", "time"} or not isinstance(body["enabled"], bool):
                raise _HTTPError(400, "Schedule requires a boolean enabled and a time.")
            if not isinstance(body["time"], str) or not re.fullmatch(
                r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", body["time"]
            ):
                raise _HTTPError(400, "Schedule time must be HH:MM in local 24-hour time.")
            result = collector.set_schedule(enabled=body["enabled"], time=body["time"])
        self.server.invalidate_status()
        self._json(200, result)

    def _handle(self):
        try:
            self._security(mutation=self.command == "POST")
            path, parameters = self._target()
            if self.command == "GET":
                self._get(path, parameters)
            elif self.command == "POST":
                self._post(path, parameters)
            else:
                raise _HTTPError(405, "Only GET and POST are supported.")
        except _HTTPError as exc:
            self._json(exc.status, {"error": exc.message})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:
            self._json(408, {"error": "The local request timed out."})
        except Exception:
            _LOGGER.exception("Local dashboard request failed.")
            self._json(500, {"error": "The local operation failed; check the server log."})

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle
    do_PATCH = _handle
    do_TRACE = _handle
    do_CONNECT = _handle


def create_server(store, collector, static_dir: Path, port: int = 8765) -> ThreadingHTTPServer:
    """Create, but do not run, a loopback-only server; port 0 supports offline tests."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535.")
    return _LocalServer(store, collector, static_dir, port)


def serve(store, collector, static_dir: Path, port: int = 8765) -> None:
    """Block serving local browser requests until interrupted."""
    server = create_server(store, collector, static_dir, port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
