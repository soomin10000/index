"""Eufy cloud auth -> Tuya local key retrieval for the X8 Pro robovac.

The vacuum isn't a device eufy-security-ws knows about at all (that library only
speaks the P2P camera/doorbell protocol) — it's a Tuya-derived device, so getting
data out of it means: log in to Eufy's cloud once, find the vacuum in the account's
device list, then ask Tuya's mobile API for that device's current local_key. From
there tinytuya talks to it directly over LAN (port 6668, protocol v3.5 confirmed
by hand against this specific X8 Pro Hybrid — v3.3/v3.4 both fail on this unit).

Crypto/signing constants ported from https://github.com/8none1/eufy-x8
(tools/get_local_keys.py, Apache-2.0) — reverse-engineered from the Eufy/Tuya
Android app, not documented anywhere official.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid

import requests

EUFY_LOGIN_URL = "https://home-api.eufylife.com/v1/user/email/login"
EUFY_DEVICES_URL = "https://api.eufylife.com/v1/device/list/devices-and-groups"
EUFY_UA = "EufyHome-Android-3.1.3-753"

TUYA_CLIENT_ID = "yx5v9uc3ef9wg3v9atje"
TUYA_APP_SECRET = "s8x78u7xwymasd9kqa7a73pjhxqsedaj"
TUYA_BMP_SECRET = "cepev5pfnhua4dkqkdpmnrdxx378mpjr"
TUYA_HMAC_KEY = f"A_{TUYA_BMP_SECRET}_{TUYA_APP_SECRET}".encode()
TUYA_BASE_URL = "https://a1.tuyaeu.com/api.json"

TUYA_PASSWORD_KEY = bytes([36, 78, 109, 138, 86, 172, 135, 145, 36, 67, 45, 139, 108, 188, 162, 196])
TUYA_PASSWORD_IV = bytes([119, 36, 86, 242, 167, 102, 76, 243, 57, 44, 53, 151, 233, 62, 87, 71])

TUYA_SIGN_KEYS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid",
    "isH5", "h5Token", "os", "clientId", "postData", "time", "requestId",
    "et", "n4h5", "sid", "sp",
}


class EufyAuthError(Exception):
    pass


def _shuffled_md5(value: str) -> str:
    h = hashlib.md5(value.encode()).hexdigest()
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]


def _sign(params: dict) -> str:
    parts = []
    for k in sorted(params.keys()):
        if k not in TUYA_SIGN_KEYS:
            continue
        v = params[k]
        if v is None or v == "":
            continue
        parts.append(f"postData={_shuffled_md5(str(v))}" if k == "postData" else f"{k}={v}")
    return hmac.new(TUYA_HMAC_KEY, "||".join(parts).encode(), hashlib.sha256).hexdigest()


def _tuya_post(action: str, data: dict | None = None, version: str = "1.0",
               sid: str | None = None, base_url: str = TUYA_BASE_URL) -> dict:
    p: dict = {
        "appVersion": "2.4.0",
        "deviceId": "abcdef1234567890abcdef1234567890abcdef12345",
        "platform": "sdk_gphone64_arm64",
        "clientId": TUYA_CLIENT_ID,
        "lang": "en", "osSystem": "12", "os": "Android",
        "timeZoneId": "Europe/London", "ttid": "android",
        "et": "0.0.1", "sdkVersion": "3.0.8cAnker",
        "time": str(int(time.time())),
        "requestId": str(uuid.uuid4()).replace("-", ""),
        "a": action, "v": version,
    }
    if sid:
        p["sid"] = sid
    if data:
        p["postData"] = json.dumps(data, separators=(",", ":"))
    p["sign"] = _sign(p)
    r = requests.post(base_url, data=p, timeout=15)
    r.raise_for_status()
    return r.json()


def _derive_password(uid: str) -> str:
    from cryptography.hazmat.backends.openssl import backend as openssl_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padded = uid.zfill(16 * math.ceil(max(len(uid), 1) / 16))
    cipher = Cipher(algorithms.AES(TUYA_PASSWORD_KEY), modes.CBC(TUYA_PASSWORD_IV), backend=openssl_backend)
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded.encode("utf8")) + encryptor.finalize()
    return hashlib.md5(encrypted.hex().upper().encode()).hexdigest()


def _rsa_encrypt(exponent: str, modulus: str, message: str) -> str:
    n, e = int(modulus), int(exponent)
    c = pow(int(message.encode().hex(), 16), e, n)
    return hex(c)[2:].zfill((n.bit_length() + 7) // 8 * 2)


def _eufy_login(email: str, password: str) -> tuple[str, str]:
    """Returns (access_token, user_id)."""
    r = requests.post(
        EUFY_LOGIN_URL,
        headers={
            "User-Agent": EUFY_UA, "category": "Home", "Accept": "*/*",
            "openudid": "abcdef1234567890",
            "Content-Type": "application/json", "clientType": "1",
        },
        json={
            "email": email, "password": password,
            "client_id": "eufyhome-app", "client_secret": "GQCpr9dSp3uQpsOMgJ4xQ",
        },
        timeout=15,
    )
    data = r.json()
    user_id = str(data.get("user_id", ""))
    access_token = data.get("access_token", "")
    if not user_id or not access_token:
        raise EufyAuthError(f"Eufy login failed: {data.get('msg', data)}")
    return access_token, user_id


def _find_vacuum_device_id(access_token: str, user_id: str) -> tuple[str, str]:
    """Returns (device_id, display_name) for the first 'Cleaning' appliance on the account."""
    headers = {
        "User-Agent": EUFY_UA, "timezone": "Europe/London", "category": "Home",
        "token": access_token, "uid": user_id,
        "openudid": "sdk_gphone64_arm64", "clientType": "2", "language": "en",
        "country": "GB", "Accept-Encoding": "gzip",
    }
    r = requests.get(EUFY_DEVICES_URL, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    for item in data.get("items", []):
        dev = item.get("device") or {}
        if (dev.get("product") or {}).get("appliance") == "Cleaning":
            return dev["id"], dev.get("alias_name") or dev.get("name", dev["id"])
    raise EufyAuthError("No 'Cleaning' appliance found on this Eufy account")


def _acquire_tuya_session(user_id: str) -> tuple[str, str]:
    """Returns (sid, api_url)."""
    uid = f"eh-{user_id}"
    resp = _tuya_post("tuya.m.user.uid.token.create", data={"uid": uid, "countryCode": "44"})
    if not resp.get("success"):
        raise EufyAuthError(f"Tuya token create failed: {resp}")
    result = resp["result"]
    encrypted = _rsa_encrypt(result.get("exponent", "65537"), result["publicKey"], _derive_password(uid))
    login_data = {
        "uid": uid, "passwd": encrypted, "countryCode": "44",
        "createGroup": True, "ifencrypt": 1, "options": {"group": 1}, "token": result["token"],
    }
    resp2 = _tuya_post("tuya.m.user.uid.password.login.reg", data=login_data)
    if not resp2.get("success"):
        resp2 = _tuya_post("tuya.m.user.uid.password.login", data=login_data)
    if not resp2.get("success"):
        raise EufyAuthError(f"Tuya login failed: {resp2}")
    sid = resp2["result"]["sid"]
    domain = resp2["result"].get("domain", {})
    api_url = domain.get("mobileApiUrl") or TUYA_BASE_URL
    if not api_url.endswith("/api.json"):
        api_url = api_url.rstrip("/") + "/api.json"
    return sid, api_url


def get_vacuum_credentials(email: str, password: str) -> dict:
    """Full flow: Eufy login -> find vacuum -> Tuya session -> current local_key.

    Returns {"device_id", "local_key", "name"}.
    """
    access_token, user_id = _eufy_login(email, password)
    device_id, name = _find_vacuum_device_id(access_token, user_id)
    sid, api_url = _acquire_tuya_session(user_id)
    resp = _tuya_post("tuya.m.device.get", data={"devId": device_id}, sid=sid, base_url=api_url)
    result = resp.get("result") or {}
    local_key = result.get("localKey", "")
    if not local_key:
        raise EufyAuthError(f"No localKey in Tuya device.get response: {resp}")
    return {"device_id": device_id, "local_key": local_key, "name": name}
