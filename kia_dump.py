#!/usr/bin/env python3
"""
kia_dump.py - one-shot: print EVERY non-null attribute the vehicle returns.
Reuses the saved token (no OTP needed). Run inside the container:

    docker exec -it kia-exporter python kia_dump.py

Run it right after a drive for the fullest picture.
"""
import os, sys, pickle, datetime, pprint
from hyundai_kia_connect_api import VehicleManager

KIA_USERNAME = os.environ["KIA_USERNAME"]
KIA_PASSWORD = os.environ["KIA_PASSWORD"]
KIA_PIN = os.environ.get("KIA_PIN", "")
REGION = int(os.environ.get("REGION", "3"))
BRAND = int(os.environ.get("BRAND", "1"))
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/data/kia_token.pkl")

mgr = VehicleManager(region=REGION, brand=BRAND,
                     username=KIA_USERNAME, password=KIA_PASSWORD, pin=KIA_PIN)

if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "rb") as f:
        mgr.token = pickle.load(f)
    print(f"[loaded saved token, valid until {getattr(mgr.token,'valid_until','?')}]")

mgr.check_and_refresh_token()
mgr.update_all_vehicles_with_cached_state()

def is_empty(v):
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    return False

for v in mgr.vehicles.values():
    print("\n" + "=" * 70)
    print(f"VEHICLE: {v.name}  ({getattr(v,'model','?')} {getattr(v,'year','?')})")
    print("=" * 70)

    attrs = [a for a in dir(v) if not a.startswith("_")]
    populated, empty, complex_fields = {}, [], {}

    for a in attrs:
        try:
            val = getattr(v, a)
        except Exception as e:
            continue
        if callable(val):
            continue
        if is_empty(val):
            empty.append(a)
        elif isinstance(val, (dict, list)) or hasattr(val, "__dict__"):
            complex_fields[a] = val
        else:
            populated[a] = val

    print(f"\n--- SIMPLE POPULATED FIELDS ({len(populated)}) ---")
    for k in sorted(populated):
        print(f"  {k:42s} = {populated[k]!r}")

    print(f"\n--- COMPLEX/STRUCTURED FIELDS ({len(complex_fields)}) ---")
    for k in sorted(complex_fields):
        print(f"\n  >>> {k}:")
        pprint.pprint(complex_fields[k], indent=6, width=100, depth=4)

    print(f"\n--- EMPTY / NULL FIELDS ({len(empty)}) ---")
    print("  " + ", ".join(sorted(empty)))
