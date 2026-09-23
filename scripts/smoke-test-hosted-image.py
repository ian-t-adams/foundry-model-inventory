"""Smoke-test the hosted container image offline; it never contacts Azure.

Starts the image with synthetic settings and simulated App Service identity
headers, checks sign-in enforcement, read-only routes and the bundled tools,
then proves that the /home backup written after the first (necessarily failed)
collection is restored by a new container. Usage:

    python scripts/smoke-test-hosted-image.py --image foundry-inventory:ci --commit <sha>
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4


TENANT = "10000000-0000-4000-8000-000000000001"
OTHER_TENANT = "10000000-0000-4000-8000-000000000002"
SCOPE = {
    "tenant_id": TENANT,
    "subscriptions": [{"id": "20000000-0000-4000-8000-000000000001", "name": "Smoke test"}],
    "morning_time": "07:00",
}
HOST = "inventory.smoke.test"
ROOT = Path(__file__).resolve().parents[1]


def principal(tenant: str) -> dict:
    claims = [{"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
              {"typ": "name", "val": "Smoke Test"}]
    encoded = base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()
    return {"Host": HOST, "X-MS-CLIENT-PRINCIPAL-IDP": "aad", "X-MS-CLIENT-PRINCIPAL": encoded,
            "X-MS-CLIENT-PRINCIPAL-NAME": "smoke@example.test"}


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise AssertionError(f"docker {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


class Container:
    def __init__(self, image: str, home: Path, port: int):
        self.name = f"foundry-inventory-smoke-{uuid4().hex[:8]}"
        self.port = port
        docker("run", "-d", "--name", self.name, "-p", f"127.0.0.1:{port}:8000",
               "-e", f"FOUNDRY_INVENTORY_AUTH_TENANT_ID={TENANT}",
               "-e", f"FOUNDRY_INVENTORY_SCOPE={json.dumps(SCOPE)}",
               "-e", f"WEBSITE_HOSTNAME={HOST}",
               "-v", f"{home}:/home", image)

    def request(self, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {"Host": HOST})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, path: str) -> dict:
        status, _, body = self.request("GET", path, principal(TENANT))
        if status != 200:
            raise AssertionError(f"GET {path} returned {status}: {body[:300]!r}")
        return json.loads(body)

    def wait_ready(self, timeout: float = 90) -> None:
        wait_for(lambda: self._ping() == 404, timeout, "the server to answer the platform ping")

    def _ping(self):
        try:
            return self.request("GET", "/robots933456.txt", {"Host": "127.0.0.1"})[0]
        except OSError:
            return None

    def logs(self) -> str:
        result = docker("logs", self.name, check=False)
        return (result.stdout + result.stderr)[-6000:]

    def remove(self) -> None:
        docker("rm", "-f", self.name, check=False)


def wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(2)
    raise AssertionError(f"Timed out after {timeout:.0f} seconds waiting for {description}.")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}", flush=True)


def check_first_container(box: Container, commit: str | None, home: Path) -> dict:
    status, _, body = box.request("GET", "/")
    expect(status == 401 and "error" in json.loads(body), "unauthenticated requests get 401 JSON")
    status, _, _ = box.request("GET", "/api/status", principal(OTHER_TENANT))
    expect(status == 403, "another tenant is forbidden")
    status, _, _ = box.request("GET", "/api/status", {**principal(TENANT), "Host": "evil.example.test"})
    expect(status == 403, "an unconfigured Host is forbidden")
    status, headers, body = box.request("GET", "/", principal(TENANT))
    expect(status == 200 and b"<title>Foundry inventory" in body
           and headers.get("Content-Type", "").startswith("text/html"), "a signed-in tenant user gets the dashboard")
    result = box.json("/api/status")
    expect(result.get("read_only") is True and result["hosted"]["timezone"] == "America/Chicago",
           "status reports read-only hosted details and the image time zone")
    if commit:
        expect(result["hosted"]["commit"] == commit, "status reports the baked-in commit")
    status, _, body = box.request("POST", "/api/scan", {**principal(TENANT), "Content-Type": "application/json"}, b"{}")
    expect(status == 403 and b"read-only" in body, "POST is refused as read-only")
    status, _, _ = box.request("GET", "/api/subscriptions", principal(TENANT))
    expect(status == 403, "subscription discovery is disabled")

    version = docker("exec", box.name, "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                     "$PSVersionTable.PSVersion.Major").stdout.strip()
    expect(version.isdigit() and int(version) >= 7, f"PowerShell {version} is present")
    az = json.loads(docker("exec", box.name, "az", "version", "--output", "json").stdout)
    expect("azure-cli" in az, f"Azure CLI {az.get('azure-cli')} is present")

    def failed_scan():
        scans = box.json("/api/scans")["scans"]
        return next((scan for scan in scans if scan["status"] in ("failed", "partial")), None)

    scan = wait_for(failed_scan, 240, "the startup collection to record its attempt")
    message = " ".join(error.get("message", "") for error in scan["errors"])
    expect(scan["status"] == "failed" and "managed identity" in message,
           "without an identity the startup collection records a hosted sign-in failure")
    backup = home / "foundry-inventory" / "inventory.sqlite3"
    wait_for(lambda: backup.is_file() and box.json("/api/status")["hosted"]["backup"]["saved_at"],
             60, "the database backup on /home")
    header = backup.read_bytes()[:100]
    expect(header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x01\x01",
           "a self-contained SQLite backup was written to /home")
    return scan


def main(argv: list[str] | None = None) -> int:
    options = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    options.add_argument("--image", required=True)
    options.add_argument("--commit", help="Expected commit baked into the image")
    options.add_argument("--home", type=Path, help="Empty directory to mount at /home")
    options.add_argument("--port", type=int, default=18000)
    args = options.parse_args(argv)
    home = (args.home or ROOT / "data" / "smoke-home" / uuid4().hex).resolve()
    home.mkdir(parents=True, exist_ok=True)
    if any(home.iterdir()):
        raise SystemExit("--home must be an empty directory.")
    box = Container(args.image, home, args.port)
    try:
        box.wait_ready()
        scan = check_first_container(box, args.commit, home)
        box.remove()
        box = Container(args.image, home, args.port)
        box.wait_ready()
        scans = box.json("/api/scans")["scans"]
        expect(any(item["id"] == scan["id"] and item["started_at"] == scan["started_at"] for item in scans),
               "a new container restores snapshot history from /home")
        expect(bool(box.json("/api/status")["hosted"]["backup"]["restored_at"]),
               "status reports the restore")
        print("Hosted image smoke test passed.", flush=True)
        return 0
    except AssertionError as exc:
        print(f"FAILED: {exc}\n--- container log (synthetic data only) ---\n{box.logs()}", file=sys.stderr)
        return 1
    finally:
        box.remove()
        if args.home is None:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
