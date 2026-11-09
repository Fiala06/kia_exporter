#!/usr/bin/env python3
"""
kia_dump.py - what does the car report, and is the exporter writing all of it?

Prints, for each vehicle:

  1. RAW PAYLOAD      every non-empty value Kia Connect returned, as dotted
                      paths. This is the complete list of what Kia sends for
                      this car.
  2. EXPORTER WRITES  the InfluxDB fields kia_influx.py derives from it.
  3. UNUSED RAW       raw values that neither the library nor the exporter
                      reads. Anything Kia adds shows up here.
  4. PARSED, NOT WRITTEN  library attributes the car fills in that the
                      exporter does not map to a field.

It also saves the raw payload to <data dir>/kia_raw_<vehicle>_<time>.json
and, when an older snapshot is there, lists the paths that appeared or
disappeared since. Reuses the saved token (no OTP). Run it inside the
container, right after a drive for the fullest picture:

    docker exec -it kia-exporter python kia_dump.py
"""
import os
import re
import sys
import glob
import json
import inspect
import datetime as dt

import kia_influx as ki

# Raw paths kia_influx.extra_fields() reads directly (not via the library).
EXPORTER_RAW_PATHS = {
    "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.tirePressureDetail",
    "lastVehicleInfo.location.head",
    "lastVehicleInfo.location.speed.value",
}

# Library attributes that are bookkeeping, not vehicle metrics.
META_ATTRS = {
    "id", "name", "model", "key", "data", "VIN", "timezone", "engine_type",
    "enabled", "supports_window_control", "supports", "last_updated_at",
    "location_last_updated_at", "geocode", "location", "daily_stats",
    "month_trip_info", "day_trip_info",
}


def flatten(obj, prefix=""):
    """Dotted path -> leaf value, skipping empty leaves."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif obj is not None and obj != "":
        out[prefix] = obj
    return out


def library_paths():
    """Raw-payload paths the Kia USA module of hyundai_kia_connect_api reads."""
    import hyundai_kia_connect_api.KiaUvoApiUSA as mod
    src = inspect.getsource(mod)
    return set(re.findall(r'get_child_value\(\s*\w+,\s*"([^"]+)"', src, re.S))


def is_read(path, known):
    """True if some known path equals this leaf or is one of its parents."""
    dotted = re.sub(r"\[(\d+)\]", r".\1", path)
    bare = re.sub(r"\[\d+\]", "", path)
    for k in known:
        for cand in (dotted, bare):
            if cand == k or cand.startswith(k + "."):
                return True
    return False


def unused_raw(flat, known):
    return {p: v for p, v in flat.items() if not is_read(p, known)}


def parsed_not_written(vehicle):
    """Library attributes with a value that the exporter doesn't map."""
    mapped = set(ki.NUMERIC_FIELDS) | set(ki.BOOL_FIELDS) | {"dtc_count"}
    out = {}
    for a in dir(vehicle):
        if a.startswith("_") or a in META_ATTRS or a in mapped:
            continue
        try:
            v = getattr(vehicle, a)
        except Exception:  # noqa: BLE001
            continue
        if callable(v) or v is None or v == "" or v == [] or v == {}:
            continue
        out[a] = v
    return out


def snapshot(data_dir, vehicle_id, data):
    """Save the raw payload; return paths added/removed since the last save."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(vehicle_id))
    pattern = os.path.join(data_dir, f"kia_raw_{safe}_*.json")
    previous = sorted(glob.glob(pattern))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(data_dir, f"kia_raw_{safe}_{stamp}.json")
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)
    added = removed = None
    if previous:
        with open(previous[-1]) as f:
            old = set(flatten(json.load(f)))
        new = set(flatten(data))
        added, removed = sorted(new - old), sorted(old - new)
    return path, previous[-1] if previous else None, added, removed


def show(title, items, fmt=lambda k, v: f"  {k:70s} = {v!r}"):
    print(f"\n--- {title} ({len(items)}) ---")
    for k in sorted(items):
        print(fmt(k, items[k]))


def report(vehicle, data_dir):
    print("\n" + "=" * 78)
    print(f"VEHICLE: {vehicle.name}  ({getattr(vehicle, 'model', '?')} "
          f"{getattr(vehicle, 'year', '?')})")
    print("=" * 78)

    data = getattr(vehicle, "data", None)
    if not isinstance(data, dict):
        print("No raw payload on this vehicle object.")
        return
    flat = flatten(data)
    known = library_paths() | EXPORTER_RAW_PATHS
    point = ki.build_point(vehicle) or {"fields": {}}

    show("1. RAW PAYLOAD: everything Kia sent", flat)
    show("2. EXPORTER WRITES to InfluxDB", point["fields"])
    show("3. UNUSED RAW: sent by Kia, read by nothing (new metrics land here)",
         unused_raw(flat, known))
    show("4. PARSED BY LIBRARY, NOT WRITTEN by the exporter",
         parsed_not_written(vehicle))

    path, prev, added, removed = snapshot(data_dir, vehicle.id, data)
    print(f"\nRaw payload saved to {path}")
    if prev is None:
        print("No earlier snapshot to compare with. Run again after a drive "
              "or in a few months to see what changed.")
    else:
        print(f"Compared with {os.path.basename(prev)}:")
        print(f"  paths that appeared:    {len(added)}")
        for p in added:
            print(f"    + {p} = {flat.get(p)!r}")
        print(f"  paths that disappeared: {len(removed)}")
        for p in removed:
            print(f"    - {p}")


def main():
    data_dir = os.path.dirname(ki.TOKEN_FILE) or "."
    try:
        mgr = ki.open_session(persist=False)
    except ki.AuthenticationOTPRequired as e:
        print(f"Kia needs a new one-time code ({e}). Run --auth first.")
        sys.exit(1)
    mgr.update_all_vehicles_with_cached_state()
    for v in mgr.vehicles.values():
        report(v, data_dir)


if __name__ == "__main__":
    main()
