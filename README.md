# kia_exporter

Polls Kia Connect (USA, OTP-based auth) every 30 minutes and writes vehicle
telemetry to InfluxDB 1.8 for Grafana. Built for a 2026 Kia Sportage HEV;
should work for any Kia US vehicle (EV/charging fields simply won't appear
on non-EVs).

## What it collects

- Odometer, fuel level %, fuel/total range
- 12V battery %, HEV battery state-of-health %
- Cabin setpoint + outside temperature
- Per-tire PSI, DTC count, service distances
- Lock state, doors/hood/trunk, TPMS / fuel / washer / brake / key-fob warnings
- GPS lat/lon, heading, speed
- **Derived:** `mpg_segment` (fill-up to fill-up fuel economy) and `fillup`
  events — the Kia API reports no fuel economy on the HEV, so the exporter
  detects fill-ups as an upward jump in fuel level and computes
  miles-driven / gallons-burned since the last one.

## Requirements

- **Docker** (the only supported run method; or Python 3.12 + `requirements.txt` if
  you run it bare).
- An active **Kia Connect account + subscription** with this vehicle enrolled in the
  Kia app. If Kia Connect doesn't work in the app, it won't work here.
- Your Kia login, password, and Connect **PIN**.
- A reachable **InfluxDB 1.8** instance. The exporter auto-creates the database
  (`vehicle` by default) on first connect, but you must stand up the server yourself.
  InfluxDB 2.x is *not* supported (this uses the 1.x InfluxQL client).
- **Grafana 12+** to import the dashboard — `grafana/Kia_Sportage_HEV_V2.json` uses the
  newer dashboard schema (`RowsLayout` / `elements`) and will not import into Grafana 10/11.
- Non-US Kia or a Hyundai? Set `REGION` / `BRAND` accordingly (see table below); this is
  only tested on Kia USA.

## Setup

Kia US requires a one-time code (SMS/email) at login. Once a code is verified
Kia remembers the device with a long-lived token; the exporter keeps that
token in `/data` and uses it to open a fresh session each day without asking
you again.

```bash
docker build -t kia-exporter:latest .

# One-time interactive auth (token saved to /data):
docker run --rm -it \
  -e KIA_USERNAME=you@example.com -e KIA_PASSWORD=... -e KIA_PIN=... \
  -v /path/to/data:/data \
  kia-exporter:latest python kia_influx.py --auth

# Normal 24/7 loop:
docker run -d --name kia-exporter \
  -e KIA_USERNAME=... -e KIA_PASSWORD=... -e KIA_PIN=... \
  -e INFLUX_HOST=192.168.1.101 \
  -v /path/to/data:/data \
  kia-exporter:latest
```

The loop never exits over authentication. If Kia stops accepting the saved
login it sends an ntfy push (if configured), keeps polling for a token, and
reloads the token file the moment it changes, so you can re-run the `--auth`
step from another shell (`docker exec -it kia-exporter python kia_influx.py
--auth`) without restarting anything.

## Keeping it logged in

Kia's "remember this device" token is not forever. It expires on Kia's side
on a schedule Kia doesn't publish, and users of the Home Assistant
integration (same library) report that another login to the **same account**
(the Kia Access app on a phone, the owner's portal) can kick a client off.
Two things make the exporter self-sufficient:

**1. Give the exporter its own Kia account.** In the Kia Access app, add a
secondary driver with a different e-mail address (Android: More › Manage all
vehicles › additional drivers; the iOS menus differ). The exporter logs in as
that driver, so your own phone logins never touch its session. A fresh mailbox for that account (a
Gmail address works) also gives you step 2 for free.

**2. Let it read the code itself (`OTP_IMAP_*`).** When Kia wants a new
code, the exporter asks for it by e-mail, watches the mailbox over IMAP,
pulls the 6-digit code out of the mail, finishes the login and carries on.
Nobody types anything, and the first start needs no `--auth` step either:

```bash
docker run -d --name kia-exporter \
  -e KIA_USERNAME=sportage-exporter@gmail.com -e KIA_PASSWORD=... -e KIA_PIN=... \
  -e OTP_IMAP_HOST=imap.gmail.com \
  -e OTP_IMAP_USER=sportage-exporter@gmail.com \
  -e OTP_IMAP_PASS='xxxx xxxx xxxx xxxx' \
  -e INFLUX_HOST=192.168.1.101 \
  -v /path/to/data:/data \
  kia-exporter:latest
```

For Gmail, turn on 2-step verification and create an *App password*; that
is `OTP_IMAP_PASS`. Any IMAP mailbox works (Fastmail, iCloud, a self-hosted
box). Microsoft 365 / Outlook.com no longer allow password IMAP logins, so
use a forwarding rule into a mailbox that does. The code goes to the e-mail
on the Kia account, so that mailbox (or one it forwards to) is the one to
point `OTP_IMAP_*` at. Only e-mail can be automated, not SMS.

Mail matching is deliberately loose: the sender must contain
`OTP_EMAIL_FROM` (`kia`), only mail that arrives after the code was
requested is read, and the first 6-digit number in the subject or body wins
(`OTP_CODE_REGEX`). If Kia changes its mail, adjust those two variables.
Automatic attempts are at most one per `REAUTH_COOLDOWN` (1h) so a broken
mailbox can't hammer Kia's login, which rate-limits hard.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `KIA_USERNAME` / `KIA_PASSWORD` | — | required |
| `KIA_PIN` | `` | Kia Connect PIN |
| `INFLUX_HOST` | — | required for the loop |
| `INFLUX_PORT` / `INFLUX_DB` | `8086` / `vehicle` | |
| `INFLUX_USER` / `INFLUX_PASS` | `` | only if your InfluxDB requires auth |
| `POLL_INTERVAL` | `1800` | seconds; Kia rate-limits hard, keep >= 900 |
| `FORCE_REFRESH` | `false` | `true` wakes the car and drains the 12V — leave false |
| `MEASUREMENT` | `kia` | |
| `REGION` / `BRAND` | `3` / `1` | USA / Kia |
| `TOKEN_FILE` | `/data/kia_token.pkl` | |
| `NTFY_URL` / `NTFY_TOPIC` | `` | re-auth alerts; both must be set to enable |
| `NTFY_ON_START` | `false` | `true` sends a ping when polling starts |
| `OTP_IMAP_HOST` / `OTP_IMAP_USER` / `OTP_IMAP_PASS` | `` | all three set = read Kia's e-mailed code automatically |
| `OTP_IMAP_PORT` / `OTP_IMAP_FOLDER` | `993` / `INBOX` | IMAP over TLS |
| `OTP_EMAIL_FROM` | `kia` | the code mail's From: must contain this (case-insensitive IMAP search) |
| `OTP_CODE_REGEX` | `\b(\d{6})\b` | first match in subject, then body, is the code |
| `OTP_WAIT_SECONDS` | `180` | how long to wait for the mail after asking Kia |
| `REAUTH_COOLDOWN` | `3600` | minimum seconds between re-auth attempts |
| `TANK_GALLONS` | `13.7` | Sportage HEV tank; set for your vehicle |
| `FILLUP_JUMP_PCT` | `8` | fuel % jump that counts as a fill-up |
| `MIN_BURN_PCT` | `2` | gauge-wobble guard before emitting MPG |
| `MPG_STATE_FILE` | `/data/mpg_state.json` | segment anchors, survives restarts |

## Grafana

`grafana/Kia_Sportage_HEV_V2.json` — import and point the `$datasource`
variable at an InfluxDB (InfluxQL) datasource for the `vehicle` database.
Includes fuel/range/MPG trends, daily miles, 12V and HEV-SOH trends, tire
pressure, warnings, doors, a location trail map, and a data-staleness stat.

The `$prometheus` variable is optional — it only feeds the "Segment MPG vs
Outside Temp" overlay, which expects a separate weather exporter publishing
`weather_temperature` to Prometheus. Leave it unset if you don't run one; the
rest of the dashboard is InfluxDB-only. The geomap also centers on a fixed
lat/lon (edit the panel's map view to your area).

## Utilities

`kia_dump.py` — one-shot dump of every non-null attribute the API returns
for your vehicle. Run inside the container right after a drive:

```bash
docker exec -it kia-exporter python kia_dump.py
```

## Tests

`tests/test_auth.py` covers the login, re-auth and mailbox code with a fake
Kia and a fake IMAP server (nothing talks to Kia):

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
```
