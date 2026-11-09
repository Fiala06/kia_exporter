"""Tests for kia_dump.py's coverage analysis (no Kia, no network)."""
import os
import sys
import json
import tempfile
import unittest

os.environ.setdefault("KIA_USERNAME", "driver@example.com")
os.environ.setdefault("KIA_PASSWORD", "hunter2")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kia_dump as kd  # noqa: E402

RAW = {
    "lastVehicleInfo": {
        "location": {"coord": {"lat": 44.05, "lon": -123.01}, "head": 358,
                     "speed": {"unit": 1, "value": 0}},
        "vehicleStatusRpt": {"vehicleStatus": {
            "engine": False, "doorLock": True,
            "batteryStatus": {"stateOfCharge": 97},
            "tirePressureDetail": {"row1": {"Left": {"tire": {"pressure": 34}}}},
            "brandNewThing": {"value": 42},
            "emptyList": [], "nothing": None, "blank": "",
        }},
    },
    "vehicleConfig": {"maintenance": {"nextServiceMile": 14707}},
}


class FlattenTests(unittest.TestCase):
    def test_flatten_skips_empties_and_indexes_lists(self):
        flat = kd.flatten({"a": {"b": [1, {"c": None}, {"d": ""}, {"e": 2}]}, "f": 0})
        self.assertEqual(flat, {"a.b[0]": 1, "a.b[3].e": 2, "f": 0})

    def test_is_read_matches_parents_and_list_indices(self):
        known = {"lastVehicleInfo.vehicleStatusRpt.vehicleStatus.tirePressureDetail",
                 "lastVehicleInfo.location.coord.lat"}
        self.assertTrue(kd.is_read(
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.tirePressureDetail.row1.Left.tire.pressure",
            known))
        self.assertTrue(kd.is_read("lastVehicleInfo.location.coord.lat", known))
        self.assertFalse(kd.is_read("lastVehicleInfo.location.coord.lon", known))
        self.assertTrue(kd.is_read("a.b[2].c", {"a.b.2.c"}))
        self.assertTrue(kd.is_read("a.b[2].c", {"a.b.c"}))

    def test_library_paths_come_from_the_installed_library(self):
        paths = kd.library_paths()
        self.assertIn("lastVehicleInfo.vehicleStatusRpt.vehicleStatus.engine", paths)
        self.assertIn("lastVehicleInfo.location.coord.lat", paths)
        self.assertGreater(len(paths), 20)

    def test_unused_raw_surfaces_new_metrics_only(self):
        flat = kd.flatten(RAW)
        unused = kd.unused_raw(flat, kd.library_paths() | kd.EXPORTER_RAW_PATHS)
        self.assertIn("lastVehicleInfo.vehicleStatusRpt.vehicleStatus.brandNewThing.value", unused)
        self.assertNotIn("lastVehicleInfo.vehicleStatusRpt.vehicleStatus.engine", unused)
        self.assertNotIn("lastVehicleInfo.location.head", unused)
        self.assertNotIn(
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.tirePressureDetail.row1.Left.tire.pressure",
            unused)

    def test_parsed_not_written_ignores_mapped_and_meta(self):
        class V:  # a bare object, so no Mock bookkeeping attributes leak in
            pass
        v = V()
        v.odometer = 9293.0            # mapped by the exporter
        v.name = "Sportage"            # meta
        v.steering_wheel_heater_is_on = False   # parsed, not written
        v.ev_battery_percentage = None # empty
        out = kd.parsed_not_written(v)
        self.assertEqual(out, {"steering_wheel_heater_is_on": False})

    def test_snapshot_diffs_against_previous(self):
        d = tempfile.mkdtemp()
        path1, prev, added, removed = kd.snapshot(d, "veh 1", RAW)
        self.assertIsNone(prev)
        self.assertTrue(os.path.exists(path1))
        newer = json.loads(json.dumps(RAW))
        newer["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evenNewer"] = 1
        del newer["vehicleConfig"]
        import time; time.sleep(1.1)   # distinct timestamp in the filename
        path2, prev, added, removed = kd.snapshot(d, "veh 1", newer)
        self.assertEqual(prev, path1)
        self.assertEqual(added, ["lastVehicleInfo.vehicleStatusRpt.vehicleStatus.evenNewer"])
        self.assertEqual(removed, ["vehicleConfig.maintenance.nextServiceMile"])


if __name__ == "__main__":
    unittest.main()
