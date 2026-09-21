"""Bounded localhost HTTP tests with a fake collector, never live Azure calls."""

import copy
import csv
import http.client
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from dashboard.server import MAX_BODY, MAX_QUERY, MAX_QUERY_FIELDS, create_server
from dashboard.store import MAX_FILTER_ITEMS, Store


class FakeCollector:
    def __init__(self):
        self.config = {"tenant_id": "", "subscriptions": [], "morning_time": "07:00"}
        self.collection = {
            "running": False, "scan_id": None, "message": "Idle",
            "progress": {"completed": 0, "total": 0, "subscription": ""}, "last_error": "",
        }
        self.schedule = {
            "enabled": False, "time": "07:00", "task_name": "FakeMorningInventory",
            "next_run": None, "last_run": None, "last_result": None, "note": "Test only",
        }
        self.calls = []
        self.conflict = False
        self.fail_discovery = False
        self.schedule_error = None

    def load_config(self):
        self.calls.append("load_config")
        return copy.deepcopy(self.config)

    def save_config(self, payload):
        self.calls.append("save_config")
        if set(payload) != {"tenant_id", "subscriptions", "morning_time"}:
            raise ValueError("Configuration has unsupported fields.")
        self.config = copy.deepcopy(payload)
        return self.load_config()

    def state(self):
        self.calls.append("state")
        return copy.deepcopy(self.collection)

    def start_scan(self, source="manual"):
        self.calls.append(("start_scan", source))
        if self.conflict:
            raise RuntimeError("private internal diagnostic")
        self.collection["running"] = True
        return self.state()

    def schedule_status(self):
        self.calls.append("schedule_status")
        if self.schedule_error is not None:
            raise self.schedule_error
        return copy.deepcopy(self.schedule)

    def set_schedule(self, enabled, time):
        self.calls.append(("set_schedule", enabled, time))
        self.schedule.update(enabled=enabled, time=time)
        return self.schedule_status()

    def discover_subscriptions(self):
        self.calls.append("discover_subscriptions")
        if self.fail_discovery:
            raise RuntimeError("secret-diagnostic-must-not-appear")
        return [{"id": "sub-a", "name": "Development", "tenant_id": "tenant-a", "state": "Enabled"}]


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(
            prefix=".dashboard-server-test-", dir=Path(__file__).resolve().parent
        )
        self.root = Path(self.workspace.name)
        self.static = self.root / "static"
        self.static.mkdir()
        for name, text in (("index.html", "<!doctype html><title>Offline fixture</title>"),
                           ("app.js", "'use strict';"), ("styles.css", "body { color: black; }")):
            (self.static / name).write_text(text, encoding="utf-8")
        self.store = Store(self.root / "data" / "inventory.sqlite3")
        self.collector = FakeCollector()
        self.server = create_server(self.store, self.collector, self.static, port=0)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        status, headers, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.token = json.loads(body)["csrf_token"]
        self.collector.calls.clear()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.workspace.cleanup()
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, payload=None, **headers):
        request_headers = {
            "Origin": self.origin, "X-Local-Token": self.token,
            "Content-Type": "application/json",
        }
        request_headers.update(headers)
        return self.request("POST", path, json.dumps(payload if payload is not None else {}).encode(),
                            request_headers)

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(request)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def ingest(self, models=("first", "second"), *, failed=False, rows=None):
        scan_id = self.store.start_scan("fixture")
        if failed:
            self.store.fail_scan(scan_id, "Collector could not read a subscription.")
            return scan_id
        fields = ["Subscription", "SubscriptionId", "TenantId", "Region", "Model", "Version",
                  "SKU", "Catalog", "Limit", "Allocated", "Remaining", "Unit", "QuotaStatus",
                  "QuotaName", "Format", "Kind", "Lifecycle"]
        path = self.root / f"scan-{scan_id}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for updates in rows if rows is not None else [{"Model": model} for model in models]:
                row = dict(
                    Subscription="Development", SubscriptionId="sub-a", TenantId="tenant-a",
                    Region="eastus", Model="", Version="v1", SKU="GlobalStandard",
                    Catalog="Listed", Limit="10", Allocated="3", Remaining="7",
                    Unit="RPM", QuotaStatus="Reported", QuotaName="shared",
                    Format="OpenAI", Kind="OpenAI", Lifecycle="GenerallyAvailable",
                )
                row.update(updates)
                writer.writerow(row)
        self.store.ingest_csvs(scan_id, [path])
        return scan_id

    def assert_json_error(self, response, expected):
        status, headers, body = response
        self.assertEqual(status, expected)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"error"})
        self.assertIsInstance(payload["error"], str)
        self.assertNotIn(self.token, body.decode("utf-8"))
        self.assertNotIn("Traceback", body.decode("utf-8"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        return payload

    def test_server_binds_only_ipv4_loopback_and_validates_ports(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertTrue(self.thread.is_alive())
        for port in (-1, 65536, True, "8765", 1.5):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    create_server(self.store, self.collector, self.static, port)

    def test_status_contract_token_and_security_headers(self):
        status, headers, body = self.request("GET", "/api/status")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"configured", "config", "latest", "collection",
                                      "schedule", "csrf_token"})
        self.assertFalse(result["configured"])
        self.assertIsNone(result["latest"])
        self.assertEqual(result["csrf_token"], self.token)
        self.assertGreaterEqual(len(self.token), 32)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-eval", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Python", headers["Server"])

    def test_later_failure_is_visible_without_replacing_complete_snapshot(self):
        complete = self.ingest()
        failure = self.ingest(failed=True)
        self.collector.collection["scan_id"] = complete
        self.collector.collection["last_error"] = "An older, stale diagnostic."
        result = json.loads(self.request("GET", "/api/status")[2])
        self.assertEqual(result["latest"]["id"], complete)
        self.assertEqual(result["collection"]["scan_id"], failure)
        self.assertIn("failed", result["collection"]["message"])
        self.assertIn("could not read", result["collection"]["last_error"])
        scans = json.loads(self.request("GET", "/api/scans")[2])["scans"]
        self.assertEqual([scan["status"] for scan in scans], ["failed", "complete"])

    def test_historical_cli_auth_errors_have_one_recovery_message_without_rewriting_history(self):
        tenant = "10000000-0000-4000-8000-000000000001"
        self.collector.config["tenant_id"] = tenant
        self.collector.config["subscriptions"] = [
            {"id": subscription, "name": subscription} for subscription in ("sub-a", "sub-b", "sub-c")
        ]
        self.server.invalidate_status()
        complete = self.ingest()
        failed = self.store.start_scan("fixture")
        failures = [{
            "subscription_id": subscription,
            "message": (
                "Collector exited 1: ERROR: User 'fixture@example.invalid' does not exist "
                "in MSAL token cache. Run `az login`.; Inventory CSV is unavailable or "
                f"invalid: No such file or directory: '{subscription}.csv'"
            ),
        } for subscription in ("sub-a", "sub-b", "sub-c")]
        self.store.ingest_csvs(
            failed, [], failures=failures, expected_subscriptions=["sub-a", "sub-b", "sub-c"],
        )
        result = json.loads(self.request("GET", "/api/status")[2])
        message = result["collection"]["last_error"]
        self.assertEqual(result["latest"]["id"], complete)
        self.assertEqual(result["collection"]["scan_id"], failed)
        self.assertIn("failed", result["collection"]["message"])
        self.assertEqual(message.count("az login"), 1)
        self.assertIn(f'az login --tenant "{tenant}"', message)
        self.assertNotIn("No such file", message)
        self.assertNotIn("fixture@example.invalid", message)
        scans = json.loads(self.request("GET", "/api/scans")[2])["scans"]
        self.assertEqual([item["message"] for item in scans[0]["errors"]],
                         [item["message"] for item in failures])
        self.assertEqual(scans[0]["status"], "failed")

    def test_auth_summary_preserves_other_errors_and_keeps_recovery_visible(self):
        self.collector.config["tenant_id"] = "10000000-0000-4000-8000-000000000001"
        self.server.invalidate_status()
        failed = self.store.start_scan("fixture")
        self.store.ingest_csvs(failed, [], failures=[
            {"subscription_id": "sub-a",
             "message": "User 'fixture' does not exist in MSAL token cache. Run `az login`."},
            {"subscription_id": "sub-b", "message": "x" * 5000 + " AuthorizationFailed: denied"},
        ])
        message = json.loads(self.request("GET", "/api/status")[2])["collection"]["last_error"]
        self.assertLessEqual(len(message), 1412)
        self.assertIn("az login --tenant", message)
        self.assertIn("AuthorizationFailed: denied", message)

    def test_historical_auth_failure_does_not_recommend_a_different_configured_tenant(self):
        tenant = "10000000-0000-4000-8000-000000000001"
        self.collector.config.update(tenant_id=tenant, subscriptions=[{"id": "new-sub"}])
        self.server.invalidate_status()
        failed = self.store.start_scan("fixture")
        self.store.ingest_csvs(failed, [], failures=[{
            "subscription_id": "old-sub",
            "message": "User 'fixture' does not exist in MSAL token cache. Run `az login`.",
        }])
        message = json.loads(self.request("GET", "/api/status")[2])["collection"]["last_error"]
        self.assertIn('az login --tenant "<affected-tenant-id>"', message)
        self.assertNotIn(tenant, message)

    def test_correct_localhost_host_and_origin_are_allowed(self):
        status = self.request(
            "GET", "/api/status", headers={
                "Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}",
                "Sec-Fetch-Site": "same-origin",
            },
        )[0]
        self.assertEqual(status, 200)

    def test_host_rebinding_and_wrong_ports_are_forbidden(self):
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}",
                     "127.0.0.1", "localhost", f"localhost.:{self.port}",
                     f"localhost.evil:{self.port}", f"[::1]:{self.port}",
                     f"127.0.0.1:{self.port}@evil.example"):
            with self.subTest(host=host):
                self.assert_json_error(self.request("GET", "/api/status", headers={"Host": host}), 403)

    def test_missing_and_duplicate_host_headers_are_rejected(self):
        self.assert_json_error(self.raw(b"GET /api/status HTTP/1.1\r\nConnection: close\r\n\r\n"), 400)
        request = (
            f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Host: localhost:{self.port}\r\nConnection: close\r\n\r\n"
        ).encode()
        self.assert_json_error(self.raw(request), 400)

    def test_private_gets_reject_foreign_null_or_wrong_local_origin(self):
        for origin in ("", "null", "https://example.invalid", f"http://localhost:{self.port}",
                       self.origin + "/", self.origin.replace("http:", "https:"),
                       f"http://127.0.0.1:{self.port + 1}"):
            with self.subTest(origin=origin):
                self.assert_json_error(self.request("GET", "/api/status", headers={"Origin": origin}), 403)

    def test_cross_site_fetch_metadata_is_blocked_even_without_origin(self):
        for site in ("cross-site", "same-site"):
            for path in ("/api/status", "/api/export.csv", "/api/subscriptions", "/api/groups"):
                with self.subTest(site=site, path=path):
                    self.assert_json_error(
                        self.request("GET", path, headers={"Sec-Fetch-Site": site}), 403
                    )
        self.assertNotIn("discover_subscriptions", self.collector.calls)
        self.assertEqual(self.request("GET", "/api/status",
                                     headers={"Sec-Fetch-Site": "none"})[0], 200)

    def test_mutations_require_origin_token_and_json(self):
        headers = {"Origin": self.origin, "X-Local-Token": self.token,
                   "Content-Type": "application/json"}
        for missing in ("Origin", "X-Local-Token"):
            with self.subTest(missing=missing):
                current = {key: value for key, value in headers.items() if key != missing}
                self.assert_json_error(self.request("POST", "/api/scan", b"{}", current), 403)
        self.assert_json_error(self.post("/api/scan", **{"X-Local-Token": "wrong"}), 403)
        self.assert_json_error(self.post("/api/scan", **{"X-Local-Token": "é"}), 403)
        self.assert_json_error(self.post("/api/scan", **{"X-Local-Token": "x" * 513}), 403)
        self.assert_json_error(self.post("/api/scan", **{"Origin": "null"}), 403)
        self.assert_json_error(self.post("/api/scan", **{"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertFalse(any(isinstance(call, tuple) for call in self.collector.calls))

    def test_config_scan_schedule_and_subscription_contracts(self):
        payload = {"tenant_id": "tenant-a", "subscriptions": [{"id": "sub-a", "name": "Development"}],
                   "morning_time": "07:30"}
        status, headers, body = self.post("/api/config", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"config": payload})
        self.assertTrue(json.loads(self.request("GET", "/api/status")[2])["configured"])
        status, headers, body = self.post("/api/scan")
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)["running"])
        self.assertIn(("start_scan", "manual"), self.collector.calls)
        status, headers, body = self.post("/api/schedule", {"enabled": True, "time": "06:45"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["enabled"])
        self.assertIn(("set_schedule", True, "06:45"), self.collector.calls)
        self.assertEqual(len(json.loads(self.request("GET", "/api/subscriptions")[2])["subscriptions"]), 1)

    def test_scan_conflict_is_409_without_internal_diagnostics(self):
        self.collector.conflict = True
        response = self.post("/api/scan")
        self.assert_json_error(response, 409)
        self.assertNotIn(b"private internal", response[2])

    def test_scan_and_schedule_reject_arbitrary_command_inputs(self):
        for body in ({"source": "evil"}, {"command": "powershell"}, {"path": "data"}):
            self.assert_json_error(self.post("/api/scan", body), 400)
        for body in ({"enabled": "true", "time": "07:00"}, {"enabled": True, "time": "24:00"},
                     {"enabled": True, "time": "07:60"}, {"enabled": True, "time": "7:00"},
                     {"enabled": True, "time": "07:00;cmd"}, {"enabled": True},
                     {"enabled": True, "time": "07:00", "command": "whoami"}):
            with self.subTest(body=body):
                self.assert_json_error(self.post("/api/schedule", body), 400)
        self.assertFalse(any(isinstance(call, tuple) for call in self.collector.calls))

    def test_content_type_enforced_but_utf8_charset_supported(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "application/json; charset=utf-16", "application/jsonp"):
            self.assert_json_error(self.post("/api/scan", **{"Content-Type": content_type}), 400)
        self.assertEqual(self.post("/api/scan", **{"Content-Type": "application/json; charset=utf-8"})[0], 202)

    def test_json_must_be_object_finite_unique_and_valid_utf8(self):
        for body in (b"[]", b"null", b"1", b'"text"', b'{"bad":NaN}', b'{"bad":Infinity}',
                     b'{"x":1,"x":2}', b'{"x":"\xff"}', b"{", b""):
            with self.subTest(body=body):
                self.assert_json_error(self.request("POST", "/api/scan", body, {
                    "Origin": self.origin, "X-Local-Token": self.token,
                    "Content-Type": "application/json",
                }), 400)

    def test_content_length_is_required_bounded_and_not_ambiguous(self):
        prefix = (
            f"POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Origin: {self.origin}\r\nX-Local-Token: {self.token}\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n"
        )
        for lengths in ("", "Content-Length: -1\r\n", "Content-Length: +2\r\n",
                        f"Content-Length: {MAX_BODY + 1}\r\n",
                        "Content-Length: 2\r\nContent-Length: 2\r\n",
                        "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n"):
            with self.subTest(lengths=lengths):
                self.assert_json_error(self.raw((prefix + lengths + "\r\n{}").encode()), 400)

    def test_duplicate_origin_and_token_headers_are_rejected(self):
        for duplicate in (f"Origin: {self.origin}", f"X-Local-Token: {self.token}"):
            request = (
                f"POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Origin: {self.origin}\r\nX-Local-Token: {self.token}\r\n"
                f"{duplicate}\r\nContent-Type: application/json\r\n"
                "Content-Length: 2\r\nConnection: close\r\n\r\n{}"
            ).encode()
            self.assert_json_error(self.raw(request), 400)

    def test_fixed_static_allowlist_and_no_file_traversal(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertTrue(body)
        for path in ("/store.py", "/data/inventory.sqlite3", "/config.json", "/../data/inventory.sqlite3",
                     "/%2e%2e/data/inventory.sqlite3", "/%2Findex.html", "/static/app.js",
                     "/..\\data\\inventory.sqlite3", "/app.js/../config.json", "/favicon.ico"):
            with self.subTest(path=path):
                self.assert_json_error(self.request("GET", path), 404)

    def test_static_symlinks_cannot_escape_allowlisted_directory(self):
        css = self.static / "styles.css"
        css.unlink()
        try:
            css.symlink_to(self.store.db_path)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {type(exc).__name__}")
        self.assert_json_error(self.request("GET", "/styles.css"), 404)

    def test_favicon_svg_uses_exact_allowlist_mime_and_unchanged_csp(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"></svg>'
        (self.static / "favicon.svg").write_bytes(svg)
        (self.static / "other.svg").write_bytes(svg)
        status, headers, body = self.request("GET", "/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(body, svg)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("img-src 'self';", headers["Content-Security-Policy"])
        self.assertNotIn("data:", headers["Content-Security-Policy"])
        for path in ("/other.svg", "/static/favicon.svg", "/../favicon.svg", "/favicon.svg/"):
            with self.subTest(path=path):
                self.assert_json_error(self.request("GET", path), 404)

    def test_unknown_routes_and_methods_never_invoke_collector(self):
        self.assert_json_error(self.request("GET", "/api/shell"), 404)
        self.assert_json_error(self.post("/api/shell"), 404)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            self.assert_json_error(self.request(method, "/api/scan"), 405)
        self.assertEqual(self.collector.calls, [])

    def test_api_filters_bounds_and_duplicate_query_rejected(self):
        queries = (
            "/api/inventory?page_size=501",
            "/api/inventory?sort=model%3BDROP%20TABLE%20scans",
            "/api/inventory?capacity_type=unbounded",
            "/api/inventory?page=1&page=2",
            "/api/inventory?unknown=value",
            "/api/facets?model=forbidden",
            "/api/status?csrf_token=guess",
            "/api/compare?from=1",
            "/api/compare?from=1&to=oops",
            "/api/inventory?q=%FF",
            "/api/inventory?" + "&".join(f"a{index}=x" for index in range(MAX_QUERY_FIELDS + 1)),
            "/api/inventory?q=" + "x" * MAX_QUERY,
        )
        for path in queries:
            with self.subTest(path=path[:90]):
                self.assert_json_error(self.request("GET", path), 400)

    def test_absolute_form_request_targets_are_not_accepted(self):
        self.assert_json_error(self.request("GET", self.origin + "/api/status"), 400)
        self.assert_json_error(self.request("GET", "/api/status#fragment"), 400)

    def test_inventory_facets_coverage_compare_and_history_routes(self):
        before = self.ingest(models=("first",))
        after = self.ingest(models=("first", "second"))
        inventory = json.loads(self.request("GET", "/api/inventory?model=second&page_size=1")[2])
        self.assertEqual(inventory["total"], 1)
        self.assertEqual(inventory["rows"][0]["model"], "second")
        facets = json.loads(self.request("GET", f"/api/facets?snapshot={before}")[2])
        self.assertEqual(facets["model"], ["first"])
        coverage = json.loads(self.request("GET", "/api/coverage")[2])
        self.assertEqual(coverage["snapshot"]["id"], after)
        self.assertEqual(coverage["rows"][0]["record_count"], 2)
        comparison = json.loads(self.request("GET", f"/api/compare?from={before}&to={after}")[2])
        self.assertEqual(comparison["summary"]["added"], 1)
        history = json.loads(self.request("GET", "/api/history")[2])
        self.assertEqual(len(history["points"]), 2)

    def test_export_is_complete_safe_and_not_json(self):
        self.ingest(models=("=formula", "+cmd", "ordinary"))
        status, headers, body = self.request("GET", "/api/export.csv?page=3&page_size=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["model"] for row in rows}, {"'=formula", "'+cmd", "ordinary"})

    def test_slow_schedule_status_is_cached_and_not_called_for_tables(self):
        self.ingest()
        self.server.invalidate_status()
        for _ in range(3):
            self.request("GET", "/api/status")
        self.assertEqual(self.collector.calls.count("schedule_status"), 1)
        self.collector.calls.clear()
        for path in ("/api/inventory", "/api/facets", "/api/coverage", "/api/scans",
                     "/api/history", "/api/groups"):
            self.request("GET", path)
        self.assertEqual(self.collector.calls, [])

    def test_scheduler_failure_keeps_status_and_browsing_available_without_leaking_details(self):
        complete = self.ingest()
        self.collector.config = {
            "tenant_id": "tenant-a", "subscriptions": [{"id": "sub-a", "name": "Development"}],
            "morning_time": "06:45",
        }
        for error_type in (RuntimeError, OSError):
            with self.subTest(error_type=error_type.__name__):
                self.collector.calls.clear()
                self.collector.schedule_error = error_type("private-scheduler-error-detail")
                self.server.invalidate_status()
                with self.assertLogs("dashboard.server", level="ERROR") as logs:
                    status, headers, body = self.request("GET", "/api/status")
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertTrue(result["configured"])
                self.assertEqual(result["config"], self.collector.config)
                self.assertEqual(result["collection"], self.collector.collection)
                self.assertEqual(result["latest"]["id"], complete)
                self.assertEqual(result["csrf_token"], self.token)
                self.assertIs(result["schedule"]["available"], False)
                self.assertIsNone(result["schedule"]["enabled"])
                self.assertEqual(result["schedule"]["time"], "06:45")
                self.assertIn("unavailable", result["schedule"]["note"].lower())
                self.assertTrue(result["schedule"]["error"])
                self.assertNotIn(b"private-scheduler-error-detail", body)
                self.assertNotIn(b"Traceback", body)
                self.assertTrue(any("private-scheduler-error-detail" in message
                                    for message in logs.output))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertEqual(self.request("GET", "/")[0], 200)
                inventory = self.request("GET", "/api/inventory")
                self.assertEqual(inventory[0], 200)
                self.assertEqual(json.loads(inventory[2])["total"], 2)
                cached = self.request("GET", "/api/status")
                self.assertEqual(cached[0], 200)
                self.assertIs(json.loads(cached[2])["schedule"]["available"], False)
                self.assertEqual(self.collector.calls.count("schedule_status"), 1)
        self.collector.schedule_error = None
        self.collector.schedule["enabled"] = True
        self.server.invalidate_status()
        recovered = json.loads(self.request("GET", "/api/status")[2])
        self.assertIs(recovered["schedule"]["enabled"], True)
        self.assertIsNot(recovered["schedule"].get("available"), False)

    def test_scheduler_fallback_does_not_mask_configuration_failures(self):
        self.server.invalidate_status()
        with patch.object(self.collector, "load_config",
                          side_effect=OSError("private-config-error-detail")):
            with self.assertLogs("dashboard.server", level="ERROR"):
                response = self.request("GET", "/api/status")
        self.assert_json_error(response, 500)
        self.assertNotIn(b"private-config-error-detail", response[2])
        self.assertNotIn("schedule_status", self.collector.calls)

    def test_genuine_server_failures_log_but_do_not_expose_internal_exception(self):
        self.collector.fail_discovery = True
        with self.assertLogs("dashboard.server", level="ERROR") as logs:
            response = self.request("GET", "/api/subscriptions")
        self.assert_json_error(response, 500)
        self.assertNotIn(b"secret-diagnostic", response[2])
        self.assertTrue(any("Local dashboard request failed" in message for message in logs.output))

    def test_repeated_categorical_filters_or_values_and_intersect_fields(self):
        self.ingest(rows=[
            {"Model": "A"},
            {"Model": "B", "Region": "westus", "Limit": "", "Allocated": "", "Remaining": "",
             "QuotaStatus": "Unknown"},
            {"Model": "C", "Limit": "0", "Allocated": "0", "Remaining": "0"},
            {"Model": "D", "Region": "north"},
        ])
        query = urlencode([
            ("region", "eastus"), ("region", "westus"),
            ("model", "A"), ("model", "B"), ("model", "C"),
            ("availability", "available"), ("availability", "unknown"),
        ])
        response = self.request("GET", "/api/inventory?" + query)
        self.assertEqual(response[0], 200)
        self.assertEqual({row["model"] for row in json.loads(response[2])["rows"]}, {"A", "B"})
        grouped = self.request("GET", "/api/groups?group_by=model&" + query)
        self.assertEqual(grouped[0], 200)
        self.assertEqual({row["label"] for row in json.loads(grouped[2])["rows"]}, {"A", "B"})

    def test_repeated_literal_commas_and_legacy_single_comma_selection(self):
        self.ingest(models=("A,B", "A", "B", "C"))
        literal_query = urlencode([("model", "A,B"), ("model", "C")])
        literal = json.loads(self.request("GET", "/api/inventory?" + literal_query)[2])
        self.assertEqual({row["model"] for row in literal["rows"]}, {"A,B", "C"})
        legacy = json.loads(self.request("GET", "/api/inventory?" + urlencode({"model": "A,B"}))[2])
        self.assertEqual({row["model"] for row in legacy["rows"]}, {"A", "B"})

    def test_repeated_scalars_are_rejected_before_route_dispatch(self):
        for key in ("page", "page_size", "snapshot", "sort", "direction", "minimum", "group_by",
                    "model_version", "q", "csrf_token", "from", "to"):
            with self.subTest(key=key):
                query = urlencode([(key, "1"), (key, "2")])
                payload = self.assert_json_error(self.request("GET", "/api/inventory?" + query), 400)
                self.assertIn("Only categorical", payload["error"])

    def test_model_version_http_filter_applies_to_all_read_apis(self):
        rows = [{"Model": model, "Version": version}
                for model in ("A", "B") for version in ("1", "2")]
        rows.append({"Model": "A", "Version": "1", "Format": "OtherProvider"})
        before = self.ingest(rows=rows)
        after = self.ingest(rows=[dict(row, Limit="20", Remaining="17") for row in rows])
        pairs = [["OpenAI", "A", "1"], ["OpenAI", "B", "2"]]
        query = urlencode([
            ("model_version", json.dumps(pairs)), ("region", "eastus"), ("region", "westus"),
            ("availability", "available"), ("availability", "unknown"),
        ])
        inventory = self.request("GET", "/api/inventory?" + query)
        self.assertEqual(inventory[0], 200)
        self.assertEqual({(row["format"], row["model"], row["version"])
                          for row in json.loads(inventory[2])["rows"]},
                         {tuple(triple) for triple in pairs})
        export = self.request("GET", "/api/export.csv?page=2&page_size=1&" + query)
        self.assertEqual(export[0], 200)
        exported = list(csv.DictReader(io.StringIO(export[2].decode("utf-8-sig"))))
        self.assertEqual(len(exported), 2)
        groups = self.request("GET", "/api/groups?group_by=model_version&page_size=1000&" + query)
        self.assertEqual(groups[0], 200)
        self.assertEqual(json.loads(groups[2])["total"], 2)
        comparison = self.request("GET", f"/api/compare?from={before}&to={after}&" + query)
        self.assertEqual(comparison[0], 200)
        self.assertEqual(json.loads(comparison[2])["summary"]["changed"], 2)
        history = self.request("GET", "/api/history?" + query)
        self.assertEqual(history[0], 200)
        self.assertEqual([point["record_count"] for point in json.loads(history[2])["points"]], [2, 2])
        facets = json.loads(self.request("GET", "/api/facets")[2])
        self.assertEqual(len(facets["model_version"]), 5)
        self.assertTrue({"model", "version", "family", "subscription"} <= set(facets))

    def test_model_version_http_json_bounds_and_injection(self):
        injected = "A' OR 1=1 --"
        self.ingest(models=(injected, "ordinary"))
        query = urlencode({"model_version": json.dumps([["OpenAI", injected, "v1"]])})
        response = self.request("GET", "/api/inventory?" + query)
        self.assertEqual(response[0], 200)
        self.assertEqual([row["model"] for row in json.loads(response[2])["rows"]], [injected])
        for selection in ("", "{}", "null", "invalid", '[["OpenAI","A"]]',
                          '[["OpenAI","A",false]]', '[["OpenAI","A","\\ud800"]]',
                          json.dumps([["OpenAI", "A", "v1"]] * (MAX_FILTER_ITEMS + 1))):
            with self.subTest(selection=selection[:80]):
                query = urlencode({"model_version": selection})
                self.assert_json_error(self.request("GET", "/api/groups?" + query), 400)
        repeated = urlencode([("model_version", "[]"), ("model_version", "[]")])
        self.assert_json_error(self.request("GET", "/api/inventory?" + repeated), 400)

    def test_several_dozen_long_variant_choices_fit_bounded_request_uri(self):
        models = ["variant-" + "m" * 100 + str(index) for index in range(64)]
        self.ingest(models=models)
        selection = [["OpenAI", model, "v1"] for model in models]
        query = urlencode({"model_version": json.dumps(selection), "page_size": "500"})
        path = "/api/inventory?" + query
        self.assertGreater(len(path), 8192)
        self.assertLess(len(path), MAX_QUERY)
        response = self.request("GET", path)
        self.assertEqual(response[0], 200)
        self.assertEqual(json.loads(response[2])["total"], 64)
        self.assertEqual(len(json.loads(response[2])["rows"]), 64)
        self.assert_json_error(self.request("GET", "/" + "x" * MAX_QUERY), 400)

    def test_query_field_and_per_filter_item_limits_are_enforced(self):
        self.ingest(models=("first",))
        fields = [
            (key, value) for key, value in
            (("region", "eastus"), ("family", "OpenAI"), ("model", "first"), ("unit", "RPM"))
            for _ in range(MAX_FILTER_ITEMS)
        ]
        self.assertEqual(len(fields), MAX_QUERY_FIELDS)
        response = self.request("GET", "/api/inventory?" + urlencode(fields))
        self.assertEqual(response[0], 200)
        self.assertEqual(json.loads(response[2])["total"], 1)
        self.assert_json_error(self.request("GET", "/api/inventory?" + urlencode(fields + [("unit", "RPM")])), 400)
        excessive = urlencode([("region", "eastus")] * (MAX_FILTER_ITEMS + 1))
        payload = self.assert_json_error(self.request("GET", "/api/inventory?" + excessive), 400)
        self.assertIn("at most 256", payload["error"])

    def test_groups_http_full_rollup_pagination_sort_and_bounds(self):
        self.ingest(rows=[
            {"Model": "A", "Version": "1"}, {"Model": "A", "Version": "2"},
            {"Model": "B", "Version": "1"},
        ])
        response = self.request("GET", "/api/groups?group_by=model&page_size=1&sort=versions&direction=desc")
        self.assertEqual(response[0], 200)
        result = json.loads(response[2])
        self.assertEqual(set(result), {"rows", "total", "page", "page_size", "snapshot", "group_by", "summary"})
        self.assertEqual((result["total"], result["page"], result["page_size"]), (2, 1, 1))
        self.assertEqual(result["summary"]["rows"], 3)
        self.assertEqual(result["rows"][0]["label"], "A")
        self.assertEqual(result["rows"][0]["entries"], 2)
        self.assertEqual(result["rows"][0]["versions"], 2)
        self.assertEqual(result["rows"][0]["quota_pools"], 1)
        self.assertEqual(result["rows"][0]["filters"], {"model": ["A"]})
        self.assertEqual(self.request("GET", "/api/groups?page_size=1000")[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/groups")[2])["group_by"], "family")
        for query in ("page_size=1001", "group_by=version", "sort=quota_limit", "direction=random",
                      "group_by=family%3BDROP%20TABLE%20scans", "unit=RPM&unit=PTU&minimum=0"):
            with self.subTest(query=query):
                self.assert_json_error(self.request("GET", "/api/groups?" + query), 400)

    def test_legacy_mistral_family_links_and_chart_facets_share_one_canonical_family(self):
        scan_id = self.ingest(rows=[
            {"Model": "alpha", "Format": "Mistral AI"},
            {"Model": "beta", "Format": "Mistral"},
        ])
        self.store._db().execute(
            "UPDATE inventory SET family='Mistral AI' WHERE scan_id=? AND format='Mistral AI'",
            (scan_id,),
        )
        query = urlencode({"family": "Mistral AI"})
        response = self.request("GET", "/api/groups?group_by=family&" + query)
        self.assertEqual(response[0], 200)
        grouped = json.loads(response[2])
        self.assertEqual(grouped["total"], 1)
        self.assertEqual(grouped["rows"][0]["label"], "Mistral")
        self.assertEqual(grouped["rows"][0]["filters"], {"family": ["Mistral"]})
        self.assertEqual(grouped["rows"][0]["entries"], 2)
        self.assertEqual(grouped["rows"][0]["quota_pools"], 1)
        self.assertEqual(grouped["rows"][0]["with_headroom"], 1)
        facets = json.loads(self.request("GET", "/api/facets")[2])
        self.assertEqual(facets["family"], ["Mistral"])
        self.assertEqual({option["family"] for option in facets["model_version"]}, {"Mistral"})
        inventory = json.loads(self.request("GET", "/api/inventory?" + query)[2])
        self.assertEqual(inventory["total"], 2)
        self.assertEqual({row["family"] for row in inventory["rows"]}, {"Mistral"})
        self.assertEqual({row["format"] for row in inventory["rows"]}, {"Mistral", "Mistral AI"})
        raw = self.store._db().execute(
            "SELECT family FROM inventory WHERE scan_id=? AND format='Mistral AI'", (scan_id,)
        ).fetchone()
        self.assertEqual(raw["family"], "Mistral AI")

    def test_inventory_combined_model_version_sort_is_shared_with_export(self):
        self.ingest(rows=[
            {"Model": "B", "Version": "2"}, {"Model": "A", "Version": "2"},
            {"Model": "A", "Version": "1"}, {"Model": "B", "Version": "1"},
        ])
        query = "sort=model&direction=desc&page=2&page_size=2"
        response = self.request("GET", "/api/inventory?" + query)
        self.assertEqual(response[0], 200)
        rows = json.loads(response[2])["rows"]
        self.assertEqual([(row["model"], row["version"]) for row in rows], [("A", "2"), ("A", "1")])
        response = self.request("GET", "/api/export.csv?" + query)
        self.assertEqual(response[0], 200)
        rows = list(csv.DictReader(io.StringIO(response[2].decode("utf-8-sig"))))
        self.assertEqual([(row["model"], row["version"]) for row in rows],
                         [("B", "2"), ("B", "1"), ("A", "2"), ("A", "1")])

    def test_controls_and_theme_are_fixed_safe_javascript_assets(self):
        expected_csp = self.request("GET", "/index.html")[1]["Content-Security-Policy"]
        for name in ("controls.js", "theme.js"):
            content = b"'use strict'; export const fixture = true;"
            (self.static / name).write_bytes(content)
            response = self.request("GET", "/" + name)
            self.assertEqual(response[0], 200)
            self.assertEqual(response[2], content)
            self.assertEqual(response[1]["Content-Type"], "text/javascript; charset=utf-8")
            self.assertEqual(response[1]["Content-Security-Policy"], expected_csp)
            self.assertEqual(response[1]["Cache-Control"], "no-store")
            self.assertEqual(response[1]["X-Content-Type-Options"], "nosniff")
            self.assert_json_error(self.request("GET", "/static/" + name), 404)
        (self.static / "arbitrary.js").write_text("must not be served", encoding="utf-8")
        self.assert_json_error(self.request("GET", "/arbitrary.js"), 404)

    def test_quota_routes_deduplicate_and_preserve_security_boundaries(self):
        self.ingest()
        status, headers, body = self.request("GET", "/api/quota?page_size=1")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["remaining"], 7)
        self.assertEqual(result["rows"][0]["sharing_choices"], 2)
        status, headers, body = self.request("GET", "/api/quota.csv?page_size=1")
        self.assertEqual(status, 200)
        self.assertIn("foundry-quota.csv", headers["Content-Disposition"])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(body.decode())))), 1)
        self.assert_json_error(self.request("GET", "/api/quota", headers={"Origin": "https://example.invalid"}), 403)
        self.assert_json_error(self.request("GET", "/api/quota?sort=unsupported"), 400)


if __name__ == "__main__":
    unittest.main()
