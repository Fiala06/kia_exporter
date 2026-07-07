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

Kia US requires a one-time OTP (SMS/email) at login, so auth is split:

```bash
docker build -t kia-exporter:latest .

# One-time interactive auth (token pickled to /data):
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

If the refresh token ever dies you'll get an ntfy push (if configured) and
need to re-run the `--auth` step.

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
