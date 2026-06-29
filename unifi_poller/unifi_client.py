"""
UnifiClient — minimal client for UniFi OS Console (UDM/UDM Pro/Dream Machine-class)
local API, confirmed working against console at 192.168.1.1 on 2026-06-29.

Auth flow (confirmed via curl):
  1. POST /api/auth/login with {"username", "password"} -> 200 OK
  2. Response carries:
       - Set-Cookie: TOKEN=<jwt>  (session cookie, ~2hr expiry)
       - X-CSRF-Token: <uuid>     (must be replayed on subsequent requests)
  3. Data endpoints live under /proxy/network/api/s/<site>/...
     and require both the session cookie AND the X-CSRF-Token header.

Confirmed site name: "default"
"""

import logging
import requests
import urllib3

# Suppress the InsecureRequestWarning noise from verify=False.
# This is intentional: local UDM consoles use a self-signed cert.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class UnifiAuthError(Exception):
    """Raised when login to the UniFi OS console fails."""
    pass


class UnifiClient:
    def __init__(self, base_url, username, password, site="default", timeout=10):
        self.base = base_url.rstrip("/")
        self.site = site
        self.timeout = timeout
        self._username = username
        self._password = password
        self.s = requests.Session()
        self.s.verify = False  # self-signed cert on local UDM console
        self._login(username, password)

    def _login(self, username, password):
        """
        Authenticate against the UniFi OS console (not the legacy /api/login).
        On success, the session cookie jar is populated automatically by
        requests.Session(), and we additionally capture the CSRF token to
        attach as a header on subsequent requests.
        """
        url = f"{self.base}/api/auth/login"
        try:
            r = self.s.post(
                url,
                json={"username": username, "password": password},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise UnifiAuthError(f"Could not reach UniFi console at {url}: {e}") from e

        if r.status_code == 401:
            raise UnifiAuthError("Login rejected: invalid username or password.")
        if r.status_code == 499:
            # UniFi OS returns 499 with a specific error code when 2FA is required.
            raise UnifiAuthError(
                "Login requires 2FA (HTTP 499). Use a local admin account "
                "without 2FA enabled for API/automation access."
            )
        r.raise_for_status()

        csrf = r.headers.get("X-CSRF-Token")
        if not csrf:
            raise UnifiAuthError(
                "Login succeeded but no X-CSRF-Token header was returned; "
                "cannot safely make follow-up requests."
            )
        self.s.headers.update({"X-CSRF-Token": csrf})
        logger.info("UniFi OS login succeeded; CSRF token captured.")

    def _get(self, path):
        """Internal helper: authenticated GET against /proxy/network/api/..."""
        url = f"{self.base}/proxy/network/api/{path}"
        r = self.s.get(url, timeout=self.timeout)
        if r.status_code == 401:
            logger.info("Session expired (401); re-authenticating.")
            self.s = requests.Session()
            self.s.verify = False
            self._login(self._username, self._password)
            r = self.s.get(url, timeout=self.timeout)
            if r.status_code == 401:
                raise UnifiAuthError("Still getting 401 after re-login; giving up.")
        r.raise_for_status()
        payload = r.json()
        if payload.get("meta", {}).get("rc") != "ok":
            raise RuntimeError(f"UniFi API returned non-ok response: {payload.get('meta')}")
        return payload["data"]

    def _post(self, path, payload):
        url = f"{self.base}/proxy/network/api/{path}"
        r = self.s.post(url, json=payload, timeout=self.timeout)
        if r.status_code == 401:
            self.s = requests.Session()
            self.s.verify = False
            self._login(self._username, self._password)
            r = self.s.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get_v2(self, path):
        url = f"{self.base}/proxy/network/v2/api/{path}"
        r = self.s.get(url, timeout=self.timeout)
        if r.status_code == 401:
            self.s = requests.Session()
            self.s.verify = False
            self._login(self._username, self._password)
            r = self.s.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_clients(self):
        """Returns stat/sta data: per-client stats (signal, rssi, retries, etc)."""
        return self._get(f"s/{self.site}/stat/sta")

    def get_devices(self):
        """Returns stat/device data: per-AP/switch stats (radio_table_stats, ports, etc)."""
        return self._get(f"s/{self.site}/stat/device")

    def trigger_speedtest(self):
        """Ask the gateway to run an on-demand speed test."""
        return self._post(f"s/{self.site}/cmd/devmgr", {"cmd": "speedtest"})

    def get_speedtest_history(self):
        """Return all gateway speed test results (daily + on-demand)."""
        data = self._get_v2(f"site/{self.site}/speedtest")
        return [
            {
                "ts":           r["time"] // 1000,
                "ping_ms":      r["latency_ms"],
                "download_mbps": r["download_mbps"],
                "upload_mbps":  r["upload_mbps"],
                "interface":    r.get("interface_name", ""),
            }
            for r in data.get("data", [])
        ]


if __name__ == "__main__":
    # Quick manual smoke test. Run with:
    #   UNIFI_PASSWORD=... python3 unifi_client.py
    import os
    import sys

    logging.basicConfig(level=logging.INFO)

    password = os.environ.get("UNIFI_PASSWORD")
    if not password:
        print("Set UNIFI_PASSWORD env var before running this smoke test.", file=sys.stderr)
        sys.exit(1)

    client = UnifiClient("https://192.168.1.1", "unifi", password)
    devices = client.get_devices()
    clients = client.get_clients()
    print(f"Devices: {len(devices)} | Clients: {len(clients)}")
