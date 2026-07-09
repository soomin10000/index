"""
Moisture monitor endpoint — receives POSTs from the XIAO ESP32C6,
validates the bearer token, logs readings to SQLite.

Run on steve:
    pip install flask
    python3 moisture_endpoint.py

Test it:
    curl -X POST http://localhost:8082/moisture \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer your-shared-token" \
      -d '{"plant":"monstera","raw":1900,"moisture_pct":55}'

Later: add to systemd like your other services once you're happy with it.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify

# ---- Config ----
ENV_FILE = Path.home() / ".config" / "home-menu.env"


def load_token():
    """MOISTURE_TOKEN from the environment (systemd EnvironmentFile),
    falling back to parsing the env file for manual runs."""
    token = os.environ.get("MOISTURE_TOKEN")
    if not token and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("MOISTURE_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        raise SystemExit(f"MOISTURE_TOKEN not set (env var or {ENV_FILE})")
    return token


BEARER_TOKEN = load_token()  # must match the sketch
DB_PATH = "moisture.db"
HOST = "0.0.0.0"
PORT = 8082

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moisture")

app = Flask(__name__)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant TEXT NOT NULL,
            raw INTEGER NOT NULL,
            moisture_pct INTEGER NOT NULL,
            battery_v REAL,
            recorded_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


@app.route("/moisture", methods=["POST"])
def moisture():
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {BEARER_TOKEN}":
        log.warning("Rejected request: bad or missing auth header")
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid or missing JSON body"}), 400

    plant = data.get("plant")
    raw = data.get("raw")
    moisture_pct = data.get("moisture_pct")
    battery_v = data.get("battery_v")  # optional, present once you add battery monitoring

    if plant is None or raw is None or moisture_pct is None:
        return jsonify({"error": "missing required fields: plant, raw, moisture_pct"}), 400

    recorded_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO readings (plant, raw, moisture_pct, battery_v, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (plant, raw, moisture_pct, battery_v, recorded_at),
    )
    conn.commit()
    conn.close()

    log.info(f"{plant}: {moisture_pct}% (raw={raw}, battery={battery_v})")

    # Placeholder for the osascript alert step, e.g.:
    # if moisture_pct < 20:
    #     notify_low_moisture(plant, moisture_pct)

    return jsonify({"status": "ok"}), 200


@app.route("/moisture/latest", methods=["GET"])
def latest():
    """Quick way to check the most recent reading per plant from a browser or curl."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT plant, raw, moisture_pct, battery_v, recorded_at
        FROM readings
        WHERE id IN (SELECT MAX(id) FROM readings GROUP BY plant)
        ORDER BY plant
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    log.info(f"Starting moisture endpoint on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT)
