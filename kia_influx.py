#!/usr/bin/env python3
"""
kia_influx.py - poll Kia Connect (USA, OTP) and write to InfluxDB 1.8.

Kia USA now requires an OTP (SMS/email code) at login. A headless loop
can't read that code, so we split auth into two phases:

  PHASE 1 (one-time, interactive):  run with --auth
      Prompts for the OTP, completes login, and pickles the resulting
      token to TOKEN_FILE. Run this attached to a terminal.

  PHASE 2 (normal 24/7 loop):       run with no args (default CMD)
      Loads the pickled token and refreshes it automatically. Polls the
      cloud cache every POLL_INTERVAL and writes to InfluxDB. Only if the
      stored refresh token dies do you need to re-run --auth.
"""

import os
import sys
import time
import json
import pickle
import logging

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE
from hyundai_kia_connect_api.exceptions import AuthenticationOTPRequired
from influxdb import InfluxDBClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("kia_influx")


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        log.error("Missing required env var %s", name)
        sys.exit(1)
    return v


KIA_USERNAME = env("KIA_USERNAME", required=True)
KIA_PASSWORD = env("KIA_PASSWORD", required=True)
KIA_PIN = env("KIA_PIN", default="")
INFLUX_HOST = env("INFLUX_HOST", default="")
INFLUX_PORT = int(env("INFLUX_PORT", "8086"))
INFLUX_DB = env("INFLUX_DB", "vehicle")
INFLUX_USER = env("INFLUX_USER", "")
INFLUX_PASS = env("INFLUX_PASS", "")
POLL_INTERVAL = int(env("POLL_INTERVAL", "1800"))
FORCE_REFRESH = env("FORCE_REFRESH", "false").lower() == "true"
MEASUREMENT = env("MEASUREMENT", "kia")
REGION = int(env("REGION", "3"))
BRAND = int(env("BRAND", "1"))
TOKEN_FILE = env("TOKEN_FILE", "/data/kia_token.pkl")
NTFY_URL = env("NTFY_URL", "")          # e.g. https://ntfy.sh
NTFY_TOPIC = env("NTFY_TOPIC", "")      # e.g. kia-reauth-cory-7h3k9
NTFY_ON_START = env("NTFY_ON_START", "false").lower() == "true"

# --- Segment MPG (fill-up to fill-up) -----------------------------------
# The Kia cloud API does not report fuel economy on the HEV, so we derive
# it: detect fill-ups as an upward jump in fuel_level_pct, anchor a segment
# there, and compute miles-driven / gallons-burned since the anchor.
MPG_STATE_FILE = env("MPG_STATE_FILE", "/data/mpg_state.json")
TANK_GALLONS = float(env("TANK_GALLONS", "13.7"))     # 2023+ Sportage HEV tank
FILLUP_JUMP_PCT = float(env("FILLUP_JUMP_PCT", "8"))  # +pct jump = fill-up
MIN_BURN_PCT = float(env("MIN_BURN_PCT", "2"))        # gauge wobble guard

NUMERIC_FIELDS = {
    "odometer": "odometer",
    "fuel_level": "fuel_level_pct",
    "fuel_driving_range": "fuel_range",
    "total_driving_range": "total_range",
    "car_battery_percentage": "battery_12v_pct",
    "ev_battery_soh_percentage": "hev_battery_soh_pct",
    "air_temperature": "cabin_temp",
    "outside_temperature": "outside_temp",
    "last_service_distance": "last_service_distance",
    "next_service_distance": "next_service_distance",
    "location_latitude": "lat",
    "location_longitude": "lon",
}

BOOL_FIELDS = {
    "is_locked": "is_locked",
    "engine_is_running": "engine_running",
    "air_control_is_on": "climate_on",
    "hood_is_open": "hood_open",
    "trunk_is_open": "trunk_open",
    "fuel_level_is_low": "fuel_low",
    "tire_pressure_all_warning_is_on": "tpms_warn",
    "washer_fluid_warning_is_on": "washer_low",
    "brake_fluid_warning_is_on": "brake_fluid_warn",
    "smart_key_battery_warning_is_on": "key_batt_warn",
    "front_left_door_is_open": "door_fl_open",
    "front_right_door_is_open": "door_fr_open",
    "back_left_door_is_open": "door_rl_open",
    "back_right_door_is_open": "door_rr_open",
}


def notify(title, message, priority="default", tags=""):
    """Best-effort ntfy push. Silent no-op if NTFY not configured."""
    if not (NTFY_URL and NTFY_TOPIC):
        return
    try:
        import urllib.request
        url = NTFY_URL.rstrip("/") + "/" + NTFY_TOPIC
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Priority", priority)
        if tags:
            req.add_header("Tags", tags)
        urllib.request.urlopen(req, timeout=10)
        log.info("ntfy sent: %s", title)
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy failed: %s", e)


def new_manager():
    return VehicleManager(
        region=REGION, brand=BRAND,
        username=KIA_USERNAME, password=KIA_PASSWORD, pin=KIA_PIN,
    )


def save_token(mgr):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(mgr.token, f)
    log.info("Token saved to %s (valid until %s)", TOKEN_FILE,
             getattr(mgr.token, "valid_until", "?"))


def load_token(mgr):
    if not os.path.exists(TOKEN_FILE):
        return False
    with open(TOKEN_FILE, "rb") as f:
        mgr.token = pickle.load(f)
    log.info("Loaded saved token (valid until %s)",
             getattr(mgr.token, "valid_until", "?"))
    return True


def do_auth():
    mgr = new_manager()
    log.info("Logging in to Kia Connect (region=%s brand=%s)...", REGION, BRAND)
    result = mgr.login()
    if result is True:
        log.info("Logged in without OTP.")
        save_token(mgr)
        return

    print("\nKia requires a one-time code. Choose delivery method:")
    print("  1) SMS")
    print("  2) Email")
    choice = input("Enter 1 or 2: ").strip()
    notify = OTP_NOTIFY_TYPE.SMS if choice == "1" else OTP_NOTIFY_TYPE.EMAIL
    mgr.send_otp(notify)
    log.info("OTP sent via %s. Check your phone/email.", notify)

    code = input("Enter the OTP code you received: ").strip()
    mgr.verify_otp_and_complete_login(code)
    log.info("OTP verified. Vehicles: %s",
             ", ".join(v.name for v in mgr.vehicles.values()))
    save_token(mgr)
    print("\nAuth complete. Token saved. Start the container normally now.")


def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (ValueError, AttributeError):
            return None
    return None


def dig(d, *keys, default=None):
    """Safely walk nested dicts; return default if any key is missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def extra_fields(vehicle):
    """Pull values that the flat attributes leave null but exist in the raw blob."""
    out = {}
    data = getattr(vehicle, "data", None)
    if not isinstance(data, dict):
        return out
    vs = dig(data, "lastVehicleInfo", "vehicleStatusRpt", "vehicleStatus", default={})

    # Per-tire PSI from tirePressureDetail
    tpd = vs.get("tirePressureDetail", {})
    tire_map = {
        ("row1", "Left"): "tire_psi_fl",
        ("row1", "Right"): "tire_psi_fr",
        ("row2", "Left"): "tire_psi_rl",
        ("row2", "Right"): "tire_psi_rr",
    }
    for (row, side), fname in tire_map.items():
        psi = dig(tpd, row, side, "tire", "pressure")
        if isinstance(psi, (int, float)) and psi > 0:
            out[fname] = float(psi)

    # DTC count
    dtc = getattr(vehicle, "dtc_count", None)
    try:
        if dtc is not None:
            out["dtc_count"] = float(dtc)
    except (TypeError, ValueError):
        pass

    # Heading + speed from location block
    head = dig(data, "lastVehicleInfo", "location", "head")
    if isinstance(head, (int, float)):
        out["heading"] = float(head)
    spd = dig(data, "lastVehicleInfo", "location", "speed", "value")
    if isinstance(spd, (int, float)):
        out["speed"] = float(spd)

    return out


def load_mpg_state():
    try:
        with open(MPG_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_mpg_state(state):
    os.makedirs(os.path.dirname(MPG_STATE_FILE), exist_ok=True)
    tmp = MPG_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, MPG_STATE_FILE)


def mpg_fields(vin, odo, fuel, state):
    """Derive segment MPG (since last fill-up) from odometer + fuel level.

    Notes/limits:
    - Fuel gauge reports whole percent and wobbles +/-1; MIN_BURN_PCT keeps
      us from emitting garbage on tiny deltas.
    - A partial top-up smaller than FILLUP_JUMP_PCT will NOT reset the
      segment, which skews that segment optimistic. Rare enough to accept.
    - Emits `fillup=1.0` on the detection cycle (usable as a Grafana
      annotation source).
    """
    out = {}
    s = state.get(vin)
    if s is None:
        state[vin] = {"anchor_odo": odo, "anchor_fuel": fuel, "last_fuel": fuel}
        log.info("MPG: initialized segment anchor for %s (odo=%.0f fuel=%.0f%%)",
                 vin, odo, fuel)
        return out

    if fuel - s["last_fuel"] >= FILLUP_JUMP_PCT:
        log.info("MPG: fill-up detected for %s (%.0f%% -> %.0f%%); "
                 "new segment anchor at odo=%.0f",
                 vin, s["last_fuel"], fuel, odo)
        s["anchor_odo"] = odo
        s["anchor_fuel"] = fuel
        out["fillup"] = 1.0
    s["last_fuel"] = fuel

    burned_pct = s["anchor_fuel"] - fuel
    miles = odo - s["anchor_odo"]
    if burned_pct >= MIN_BURN_PCT and miles > 0:
        gallons = burned_pct / 100.0 * TANK_GALLONS
        mpg = miles / gallons
        if 3.0 <= mpg <= 99.0:  # sanity clamp
            out["mpg_segment"] = round(mpg, 1)
    return out


def build_point(vehicle):
    fields = {}
    for attr, fname in NUMERIC_FIELDS.items():
        num = to_number(getattr(vehicle, attr, None))
        if num is not None:
            fields[fname] = num
    for attr, fname in BOOL_FIELDS.items():
        val = getattr(vehicle, attr, None)
        if isinstance(val, bool):
            fields[fname] = 1.0 if val else 0.0
    fields.update(extra_fields(vehicle))
    if not fields:
        return None
    tags = {
        "vin": getattr(vehicle, "VIN", "") or "unknown",
        "name": getattr(vehicle, "name", "") or "kia",
        "model": getattr(vehicle, "model", "") or "Sportage",
    }
    return {"measurement": MEASUREMENT, "tags": tags, "fields": fields}


def connect_influx():
    kwargs = dict(host=INFLUX_HOST, port=INFLUX_PORT, database=INFLUX_DB)
    if INFLUX_USER:
        kwargs["username"] = INFLUX_USER
        kwargs["password"] = INFLUX_PASS
    client = InfluxDBClient(**kwargs)
    existing = {d["name"] for d in client.get_list_database()}
    if INFLUX_DB not in existing:
        log.info("Creating InfluxDB database %s", INFLUX_DB)
        client.create_database(INFLUX_DB)
    return client


def run_loop():
    if not INFLUX_HOST:
        log.error("INFLUX_HOST is required for the polling loop")
        sys.exit(1)

    mgr = new_manager()
    if not load_token(mgr):
        log.error(
            "No saved token at %s. Run the one-time auth first:\n"
            "    docker exec -it kia-exporter python kia_influx.py --auth",
            TOKEN_FILE,
        )
        sys.exit(1)

    influx = connect_influx()
    log.info("Connected to InfluxDB %s:%s db=%s", INFLUX_HOST, INFLUX_PORT, INFLUX_DB)

    mpg_state = load_mpg_state()
    if mpg_state:
        log.info("Loaded MPG segment state for %d vehicle(s)", len(mpg_state))

    try:
        mgr.check_and_refresh_token()
        mgr.update_all_vehicles_with_cached_state()
        save_token(mgr)
        log.info("Startup OK. Vehicles: %s",
                 ", ".join(v.name for v in mgr.vehicles.values()))
        if NTFY_ON_START:
            notify("Kia exporter started",
                   "Polling is live. You will be alerted here if re-auth is ever needed.",
                   tags="white_check_mark,car")
    except AuthenticationOTPRequired:
        log.error(
            "Stored token expired and needs a NEW OTP. Re-run:\n"
            "    docker exec -it kia-exporter python kia_influx.py --auth"
        )
        notify("Kia exporter: re-auth needed",
               "Stored refresh token expired. Run the --auth step to re-OTP.",
               priority="high", tags="warning,car")
        sys.exit(1)

    while True:
        try:
            mgr.check_and_refresh_token()
            save_token(mgr)
            if FORCE_REFRESH:
                log.info("Force-refreshing (waking car)")
                mgr.force_refresh_all_vehicles_states()
            else:
                mgr.update_all_vehicles_with_cached_state()

            points = []
            for v in mgr.vehicles.values():
                p = build_point(v)
                if p:
                    odo = p["fields"].get("odometer")
                    fuel = p["fields"].get("fuel_level_pct")
                    if odo is not None and fuel is not None:
                        p["fields"].update(
                            mpg_fields(p["tags"]["vin"], odo, fuel, mpg_state))
                    points.append(p)
                    log.info("%s: %s", v.name, p["fields"])
            if points:
                influx.write_points(points)
                save_mpg_state(mpg_state)
                log.info("Wrote %d point(s)", len(points))
            else:
                log.warning("No numeric fields this cycle")
        except AuthenticationOTPRequired:
            log.error("Refresh token died - re-run --auth. Sleeping 1h then retrying.")
            notify("Kia exporter: re-auth needed",
                   "Kia refresh token died. Dashboard is now stale until you "
                   "re-run the --auth step to enter a new OTP.",
                   priority="high", tags="warning,car")
            time.sleep(3600)
            continue
        except Exception as e:
            log.exception("Poll failed: %s", e)
        time.sleep(POLL_INTERVAL)


def main():
    if "--auth" in sys.argv:
        do_auth()
    else:
        run_loop()


if __name__ == "__main__":
    main()
