#!/usr/bin/env python3
"""
kia_influx.py - poll Kia Connect (USA, OTP) and write to InfluxDB 1.8.

Kia USA requires an OTP (SMS/email code) at login. Once a code is verified,
Kia "remembers" the device with a long-lived token (rmtoken) that this
script keeps in TOKEN_FILE and uses to open a fresh session whenever the
23h session id lapses. Kia can still drop that rmtoken at any time: it
expires server-side, and another login to the same account (the phone app,
the owner's portal) has been seen to kill it. So there are three ways in:

  --auth (interactive, one-time):  prompts for the OTP and saves the token.

  Automatic (hands-free):          if OTP_IMAP_* are set, the loop asks Kia
      to e-mail the code, reads it from that mailbox over IMAP, finishes
      the login and carries on. Nobody has to type anything, ever.

  Manual while running:            run --auth from another shell; the loop
      notices the new TOKEN_FILE and reloads it. No restart needed.

The loop never exits over authentication: it keeps polling for a token and
alerts over ntfy (if configured) so the dashboard tells you what it needs.
"""

import os
import re
import sys
import time
import json
import html
import email
import pickle
import logging
import imaplib
import datetime as dt
from email.header import decode_header, make_header

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.ApiImpl import OTPRequest
from hyundai_kia_connect_api.Token import Token
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

# --- Automatic OTP by e-mail ---------------------------------------------
# When Kia wants a new one-time code, ask for it by e-mail and read it out
# of this mailbox over IMAP. Set all three of HOST/USER/PASS to enable.
OTP_IMAP_HOST = env("OTP_IMAP_HOST", "")
OTP_IMAP_PORT = int(env("OTP_IMAP_PORT", "993"))
OTP_IMAP_USER = env("OTP_IMAP_USER", "")
OTP_IMAP_PASS = env("OTP_IMAP_PASS", "")
OTP_IMAP_FOLDER = env("OTP_IMAP_FOLDER", "INBOX")
OTP_EMAIL_FROM = env("OTP_EMAIL_FROM", "kia")        # From: must contain this
OTP_CODE_REGEX = env("OTP_CODE_REGEX", r"\b(\d{6})\b")
OTP_WAIT_SECONDS = int(env("OTP_WAIT_SECONDS", "180"))
REAUTH_COOLDOWN = int(env("REAUTH_COOLDOWN", "3600"))  # between re-auth attempts
AUTO_OTP = bool(OTP_IMAP_HOST and OTP_IMAP_USER and OTP_IMAP_PASS)

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

AUTH_HELP = (
    "Run the one-time auth from another shell (the loop picks the new token "
    "up by itself):\n"
    "    docker exec -it kia-exporter python kia_influx.py --auth\n"
    "Set OTP_IMAP_HOST/USER/PASS to have this happen automatically."
)


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


# --- Token file -----------------------------------------------------------
# _token_seen is the token file's mtime as we last read or wrote it. If it
# changes under us, someone ran --auth and we should reload.
_token_seen = None
_token_saved = None   # (sid, rmtoken) last written, to skip no-op writes


def token_mtime():
    try:
        return os.stat(TOKEN_FILE).st_mtime
    except OSError:
        return None


def token_file_changed():
    return token_mtime() != _token_seen


def save_token(mgr):
    global _token_seen, _token_saved
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(mgr.token, f)
    os.replace(tmp, TOKEN_FILE)
    _token_seen = token_mtime()
    _token_saved = (mgr.token.access_token, mgr.token.refresh_token)
    log.info("Token saved to %s (session valid until %s)", TOKEN_FILE,
             getattr(mgr.token, "valid_until", "?"))


def save_token_if_changed(mgr):
    """The library swaps session ids in place when Kia drops one mid-poll;
    persist that so a restart doesn't start from a dead session."""
    if (mgr.token.access_token, mgr.token.refresh_token) != _token_saved:
        save_token(mgr)


def load_token(mgr):
    global _token_seen, _token_saved
    if not os.path.exists(TOKEN_FILE):
        return False
    with open(TOKEN_FILE, "rb") as f:
        mgr.token = pickle.load(f)
    _token_seen = token_mtime()
    _token_saved = (mgr.token.access_token, mgr.token.refresh_token)
    log.info("Loaded saved token (session valid until %s)",
             getattr(mgr.token, "valid_until", "?"))
    return True


def wait_for_token_file(seconds, step=30):
    """Sleep up to `seconds`, returning early (True) if TOKEN_FILE changes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(step, max(0, deadline - time.monotonic())))
        if token_file_changed():
            return True
    return token_file_changed()


# --- OTP by e-mail ----------------------------------------------------------
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d):
    """IMAP SEARCH date (dd-Mon-yyyy), independent of the locale."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def message_text(msg):
    """Plain text of an e-mail: text/plain parts, then text/html stripped."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain, rich = [], []
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (plain if ctype == "text/plain" else rich).append(text)
    return "\n".join(plain + [strip_html(t) for t in rich])


def extract_code(text):
    m = re.search(OTP_CODE_REGEX, text or "")
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


class OtpMailbox:
    """Watches a mailbox over IMAP for the one-time code Kia e-mails."""

    def __init__(self, host=None, port=None, user=None, password=None,
                 folder=None, sender=None, poll=10, imap_factory=None):
        self.host = host or OTP_IMAP_HOST
        self.port = port or OTP_IMAP_PORT
        self.user = user or OTP_IMAP_USER
        self.password = password or OTP_IMAP_PASS
        self.folder = folder or OTP_IMAP_FOLDER
        self.sender = OTP_EMAIL_FROM if sender is None else sender
        self.poll = poll
        self._imap_factory = imap_factory or imaplib.IMAP4_SSL

    def _open(self):
        imap = self._imap_factory(self.host, self.port)
        imap.login(self.user, self.password)
        typ, _ = imap.select(self.folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"IMAP folder {self.folder!r} not selectable")
        return imap

    def _search(self, imap):
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        criteria = ["SINCE", imap_date(since)]
        if self.sender:
            criteria += ["FROM", f'"{self.sender}"']
        typ, data = imap.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {data!r}")
        return set(data[0].split()) if data and data[0] else set()

    def _with_imap(self, fn):
        imap = self._open()
        try:
            return fn(imap)
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass

    def snapshot(self):
        """UIDs already in the mailbox, so only mail that arrives after we
        ask Kia for a code is considered."""
        return self._with_imap(self._search)

    def _find_code(self, imap, ignore):
        for uid in sorted(self._search(imap) - ignore, key=int, reverse=True):
            typ, data = imap.uid("FETCH", uid, "(RFC822)")
            raw = next((d[1] for d in data or [] if isinstance(d, tuple)), None)
            if typ != "OK" or raw is None:
                continue
            msg = email.message_from_bytes(raw)
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            code = extract_code(subject) or extract_code(message_text(msg))
            if code:
                log.info("Found the code in mail from %s: %r",
                         msg.get("From", "?"), subject)
                return code
            ignore.add(uid)   # some other mail from Kia; don't re-read it
        return None

    def wait_for_code(self, ignore, timeout):
        """Poll until a new mail with a code shows up, or `timeout` seconds."""
        ignore = set(ignore)
        deadline = time.monotonic() + timeout
        while True:
            code = self._with_imap(lambda imap: self._find_code(imap, ignore))
            if code or time.monotonic() >= deadline:
                return code
            time.sleep(self.poll)


# --- Login ------------------------------------------------------------------
def start_login(mgr):
    """Fresh login. Returns the OTPRequest Kia answers with, or None if it
    let us straight in (no OTP needed)."""
    result = mgr.login()
    if result is True:
        return None
    if isinstance(result, OTPRequest):
        return result
    raise RuntimeError(f"Unexpected login result: {result!r}")


def auto_otp_login():
    """Hands-free login: Kia e-mails the code, we read it over IMAP.
    Returns a manager with vehicles loaded; the token is saved."""
    mgr = new_manager()
    log.info("Logging in to Kia Connect (region=%s brand=%s)...", REGION, BRAND)
    request = start_login(mgr)
    if request is None:
        log.info("Logged in without OTP.")
        save_token(mgr)
        return mgr
    if request.has_email is False:
        raise RuntimeError("Kia has no e-mail on this account to send the code to")
    mailbox = OtpMailbox()
    already = mailbox.snapshot()
    mgr.send_otp(OTP_NOTIFY_TYPE.EMAIL)
    log.info("Asked Kia to e-mail the code (%s); watching %s/%s for up to %ss",
             request.email or "account e-mail", OTP_IMAP_HOST, OTP_IMAP_FOLDER,
             OTP_WAIT_SECONDS)
    code = mailbox.wait_for_code(already, OTP_WAIT_SECONDS)
    if not code:
        raise RuntimeError(
            f"no mail with a code from {OTP_EMAIL_FROM!r} arrived in "
            f"{OTP_WAIT_SECONDS}s (check OTP_EMAIL_FROM / OTP_CODE_REGEX)")
    mgr.verify_otp_and_complete_login(code)
    log.info("OTP verified. Vehicles: %s",
             ", ".join(v.name for v in mgr.vehicles.values()))
    save_token(mgr)
    return mgr


def interactive_otp_login():
    mgr = new_manager()
    log.info("Logging in to Kia Connect (region=%s brand=%s)...", REGION, BRAND)
    request = start_login(mgr)
    if request is None:
        log.info("Logged in without OTP.")
        save_token(mgr)
        return mgr

    print("\nKia requires a one-time code. Choose delivery method:")
    print("  1) SMS")
    print("  2) Email")
    choice = input("Enter 1 or 2: ").strip()
    method = OTP_NOTIFY_TYPE.SMS if choice == "1" else OTP_NOTIFY_TYPE.EMAIL
    mgr.send_otp(method)
    log.info("OTP sent via %s. Check your phone/email.", method.value)

    code = input("Enter the OTP code you received: ").strip()
    mgr.verify_otp_and_complete_login(code)
    log.info("OTP verified. Vehicles: %s",
             ", ".join(v.name for v in mgr.vehicles.values()))
    save_token(mgr)
    return mgr


def do_auth():
    """The --auth command."""
    if AUTO_OTP:
        try:
            auto_otp_login()
            print("\nAuth complete. Token saved. A running loop reloads it by itself.")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Automatic OTP failed (%s)", e)
            if not sys.stdin.isatty():
                sys.exit(1)
            log.info("Falling back to the prompts.")
    interactive_otp_login()
    print("\nAuth complete. Token saved. A running loop reloads it by itself.")


def renew_session(mgr):
    """Trade the saved rmtoken for a fresh session id and (re)load the
    vehicles. Raises AuthenticationOTPRequired if Kia wants a new code."""
    # Keywords, not positions: login(username, password, token, pin) in
    # this library version, (username, password, pin, token) in 5.x.
    result = mgr.api.login(username=KIA_USERNAME, password=KIA_PASSWORD,
                           token=mgr.token, pin=KIA_PIN)
    if isinstance(result, OTPRequest):
        raise AuthenticationOTPRequired(
            "Kia no longer accepts the saved login; it wants a new one-time code")
    if not isinstance(result, Token):
        raise RuntimeError(f"Unexpected login result: {result!r}")
    result.pin = KIA_PIN
    mgr.token = result
    mgr.vehicles.clear()
    mgr.initialize_vehicles()
    save_token(mgr)


def open_session():
    """Manager with a live session from the saved token, vehicles loaded.
    Raises AuthenticationOTPRequired if there is no usable token."""
    mgr = new_manager()
    if not load_token(mgr):
        raise AuthenticationOTPRequired(f"no saved token at {TOKEN_FILE}")
    # Always start from a fresh session id. Kia drops session ids well
    # before the 23h the library assumes, and a dead one is not told apart
    # from a dead rmtoken by this library version.
    renew_session(mgr)
    log.info("Session open. Vehicles: %s",
             ", ".join(v.name for v in mgr.vehicles.values()))
    return mgr


def is_otp_required(exc):
    """The library raises AuthenticationOTPRequired from its token check,
    but when Kia drops the rmtoken mid-poll the session-repair wrapper
    trips over the OTPRequest it gets back instead."""
    return isinstance(exc, AuthenticationOTPRequired) or (
        isinstance(exc, AttributeError) and "OTPRequest" in str(exc))


_last_reauth = None


def recover_session(reason):
    """The saved login is dead. Get a new one automatically (IMAP), or wait
    for someone to run --auth. Returns a ready manager, or None to retry."""
    global _last_reauth
    log.error("Kia needs a new one-time code: %s", reason)
    if AUTO_OTP:
        if _last_reauth is not None:
            wait = REAUTH_COOLDOWN - (time.monotonic() - _last_reauth)
            if wait > 0:
                log.info("Last automatic attempt was %ds ago; waiting %ds",
                         REAUTH_COOLDOWN - wait, wait)
                if wait_for_token_file(wait):
                    return None
        _last_reauth = time.monotonic()
        try:
            mgr = auto_otp_login()
            notify("Kia exporter: re-authenticated",
                   "Kia asked for a new one-time code. Read it from the mailbox "
                   "and logged back in. Polling continues.",
                   tags="white_check_mark,car")
            return mgr
        except Exception as e:  # noqa: BLE001
            log.exception("Automatic re-auth failed: %s", e)
            notify("Kia exporter: automatic re-auth failed",
                   f"{e}. Dashboard is stale. Check OTP_IMAP_* or run --auth.",
                   priority="high", tags="warning,car")
    else:
        log.error("%s", AUTH_HELP)
        notify("Kia exporter: re-auth needed",
               "Kia dropped the saved login. Dashboard is stale until you run "
               "the --auth step to enter a new OTP (no restart needed).",
               priority="high", tags="warning,car")
    if wait_for_token_file(REAUTH_COOLDOWN):
        log.info("Token file changed on disk; reloading it")
    return None


# --- Data -------------------------------------------------------------------
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


def poll_once(mgr, influx, mpg_state):
    mgr.check_and_refresh_token()
    if FORCE_REFRESH:
        log.info("Force-refreshing (waking car)")
        mgr.force_refresh_all_vehicles_states()
    else:
        mgr.update_all_vehicles_with_cached_state()
    save_token_if_changed(mgr)

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


def run_loop():
    if not INFLUX_HOST:
        log.error("INFLUX_HOST is required for the polling loop")
        sys.exit(1)

    influx = connect_influx()
    log.info("Connected to InfluxDB %s:%s db=%s", INFLUX_HOST, INFLUX_PORT, INFLUX_DB)
    log.info("Automatic OTP by e-mail: %s",
             f"on ({OTP_IMAP_USER} at {OTP_IMAP_HOST})" if AUTO_OTP else "off")

    mpg_state = load_mpg_state()
    if mpg_state:
        log.info("Loaded MPG segment state for %d vehicle(s)", len(mpg_state))

    mgr = None
    started = False
    while True:
        if mgr is None:
            try:
                mgr = open_session()
            except AuthenticationOTPRequired as e:
                mgr = recover_session(str(e))
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("Could not open a Kia session: %s", e)
                wait_for_token_file(POLL_INTERVAL)
                continue
            if not started and NTFY_ON_START:
                notify("Kia exporter started",
                       "Polling is live. You will be alerted here if re-auth is ever needed.",
                       tags="white_check_mark,car")
            started = True

        if token_file_changed():
            log.info("Token file changed on disk (someone ran --auth); reloading")
            mgr = None
            continue

        try:
            poll_once(mgr, influx, mpg_state)
        except Exception as e:  # noqa: BLE001
            if is_otp_required(e):
                mgr = recover_session(str(e))
                continue
            log.exception("Poll failed: %s", e)
            # A session Kia dropped early looks like a generic API error
            # in this library version; a fresh session id fixes that.
            try:
                renew_session(mgr)
            except AuthenticationOTPRequired as e2:
                mgr = recover_session(str(e2))
                continue
            except Exception as e2:  # noqa: BLE001
                log.warning("Session renewal failed too: %s", e2)
        wait_for_token_file(POLL_INTERVAL)


def main():
    if "--auth" in sys.argv:
        do_auth()
    else:
        run_loop()


if __name__ == "__main__":
    main()
