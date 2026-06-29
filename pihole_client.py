"""Pi-hole v6 API client."""

import os
import requests
import urllib3

urllib3.disable_warnings()

PIHOLE_URL = os.environ.get("PIHOLE_URL", "http://192.168.1.246")
PIHOLE_PASSWORD = os.environ.get("PIHOLE_PASSWORD", "")


class PiholeClient:
    def __init__(self, url=PIHOLE_URL, password=PIHOLE_PASSWORD):
        self.url = url.rstrip("/")
        self.password = password
        self._sid = None
        self._login()

    def _login(self):
        r = requests.post(f"{self.url}/api/auth",
                          json={"password": self.password}, timeout=10)
        r.raise_for_status()
        self._sid = r.json()["session"]["sid"]

    def _get(self, path, **params):
        r = requests.get(f"{self.url}/api/{path}",
                         headers={"X-FTL-SID": self._sid},
                         params=params, timeout=10)
        if r.status_code == 401:
            self._login()
            r = requests.get(f"{self.url}/api/{path}",
                             headers={"X-FTL-SID": self._sid},
                             params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def summary(self):
        return self._get("stats/summary")

    def history(self):
        return self._get("history").get("history", [])

    def top_domains(self, count=10):
        return self._get("stats/top_domains", count=count).get("domains", [])

    def top_blocked(self, count=10):
        return self._get("stats/top_domains", blocked="true", count=count).get("domains", [])

    def top_clients(self, count=10):
        return self._get("stats/top_clients", count=count).get("clients", [])

    def upstreams(self):
        return self._get("stats/upstreams").get("upstreams", [])

    def client_top_domains(self, ip, count=5):
        return self._get("stats/top_domains", client=ip, count=count).get("domains", [])

    def blocked_clients(self, count=200):
        return self._get("stats/top_clients", blocked="true", count=count).get("clients", [])
