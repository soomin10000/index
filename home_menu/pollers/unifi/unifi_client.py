"""
UnifiClient — minimal client for UniFi OS Console (UDM/UDM Pro/Dream Machine-class)
local API, confirmed working against console at 192.168.1.1.

Auth: API key passed as X-API-KEY header. Generate one in UniFi OS →
Settings → API Keys. No login, no session, no CSRF token needed.
"""

import logging
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class UnifiAuthError(Exception):
    pass


class UnifiClient:
    def __init__(self, base_url, api_key, site="default", timeout=10):
        self.base = base_url.rstrip("/")
        self.site = site
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"X-API-KEY": api_key})

    def _get(self, path):
        url = f"{self.base}/proxy/network/api/{path}"
        r = self.s.get(url, timeout=self.timeout)
        if r.status_code == 401:
            raise UnifiAuthError("API key rejected (401). Check key is valid and not expired.")
        r.raise_for_status()
        payload = r.json()
        if payload.get("meta", {}).get("rc") != "ok":
            raise RuntimeError(f"UniFi API returned non-ok response: {payload.get('meta')}")
        return payload["data"]

    def _post(self, path, payload):
        url = f"{self.base}/proxy/network/api/{path}"
        r = self.s.post(url, json=payload, timeout=self.timeout)
        if r.status_code == 401:
            raise UnifiAuthError("API key rejected (401).")
        r.raise_for_status()
        return r.json()

    def _get_v2(self, path):
        url = f"{self.base}/proxy/network/v2/api/{path}"
        r = self.s.get(url, timeout=self.timeout)
        if r.status_code == 401:
            raise UnifiAuthError("API key rejected (401).")
        r.raise_for_status()
        return r.json()

    def get_clients(self):
        return self._get(f"s/{self.site}/stat/sta")

    def get_devices(self):
        return self._get(f"s/{self.site}/stat/device")

    def get_wlans(self):
        return self._get(f"s/{self.site}/rest/wlanconf")

    def trigger_speedtest(self):
        return self._post(f"s/{self.site}/cmd/devmgr", {"cmd": "speedtest"})

    def get_speedtest_history(self):
        data = self._get_v2(f"site/{self.site}/speedtest")
        return [
            {
                "ts":            r["time"] // 1000,
                "ping_ms":       r["latency_ms"],
                "download_mbps": r["download_mbps"],
                "upload_mbps":   r["upload_mbps"],
                "interface":     r.get("interface_name", ""),
            }
            for r in data.get("data", [])
        ]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    api_key = os.environ.get("UNIFI_API_KEY")
    if not api_key:
        print("Set UNIFI_API_KEY env var before running this smoke test.", file=sys.stderr)
        sys.exit(1)

    client = UnifiClient("https://192.168.1.1", api_key)
    devices = client.get_devices()
    clients = client.get_clients()
    print(f"Devices: {len(devices)} | Clients: {len(clients)}")
