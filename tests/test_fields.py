"""Field extraction against a payload shaped like a real 2026 Sportage HEV dump."""
import os
import sys
import calendar
import unittest

os.environ.setdefault("KIA_USERNAME", "driver@example.com")
os.environ.setdefault("KIA_PASSWORD", "hunter2")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kia_influx as ki  # noqa: E402

VS = {
    "batteryStatus": {"stateOfCharge": 97, "warning": 100},
    "brakeOilStatus": False, "engineOilStatus": True,
    "climate": {"airCtrl": False, "airTemp": {"unit": 1, "value": "72"}, "defrost": False},
    "doorLock": True, "engine": False, "fuelLevel": 100, "ign3": True,
    "doorStatus": {"backLeft": 0, "backRight": 0, "frontLeft": 1, "frontRight": 0,
                   "hood": 0, "trunk": 1},
    "lampWireStatus": {"headLamp": {"headLampStatus": False, "lampLB": False},
                       "stopLamp": {"leftLamp": True}},
    "lightStatus": {"hazardStatus": 0, "tailLampStatus": 0},
    "lowFuelLight": False, "smartKeyBatteryWarning": False, "sunroofOpen": False,
    "syncDate": {"offset": -8.0, "utc": "20261109161749"},
    "tirePressure": {"all": 0, "frontLeft": 0},
    "tirePressureDetail": {"row1": {"Left": {"tire": {"pressure": 34}},
                                    "Right": {"tire": {"pressure": 35}}},
                           "row2": {"Left": {"tire": {"pressure": 33}},
                                    "Right": {"tire": {"pressure": 33}}}},
    "valetParkingMode": 0, "washerFluidStatus": False,
    "windowStatus": {"windowFL": 0, "windowFR": 0, "windowRL": 3, "windowRR": 0},
}
RAW = {
    "lastVehicleInfo": {
        "activeDTC": {"dtcActiveCount": "0"},
        "location": {"coord": {"lat": 44.048444, "lon": -123.015855}, "head": 358,
                     "speed": {"unit": 1, "value": 0},
                     "syncDate": {"offset": -8.0, "utc": "20261109171422.244"}},
        "vehicleStatusRpt": {"vehicleStatus": VS},
    },
    "vehicleConfig": {
        "maintenance": {"maintenanceSchedule": [8000, 16000, 24000, 32000],
                        "nextServiceMile": 14707},
        "vehicleDetail": {"vehicle": {"mileage": "9293"}},
    },
}


# 2026-11-09 16:17:49 UTC and 17:14:22 UTC, as epoch milliseconds
STATUS_MS = calendar.timegm((2026, 11, 9, 16, 17, 49, 0, 0, 0)) * 1000.0
LOCATION_MS = calendar.timegm((2026, 11, 9, 17, 14, 22, 0, 0, 0)) * 1000.0


class Car:
    """What the library hands the exporter: raw ints for the flags."""
    data = RAW
    odometer = 9293.0
    fuel_level = 100
    is_locked = True
    engine_is_running = False
    hood_is_open = 0
    trunk_is_open = 1
    front_left_door_is_open = 1
    front_right_door_is_open = 0
    tire_pressure_all_warning_is_on = 0
    brake_fluid_warning_is_on = None     # library reads the misspelled key
    dtc_count = "0"
    VIN = "KNDPXDDG8T7341487"
    name = "My SPORTAGE HYBRID"
    model = "SPORTAGE HYBRID"


class FlagTests(unittest.TestCase):
    def test_to_flag(self):
        self.assertEqual(ki.to_flag(True), 1.0)
        self.assertEqual(ki.to_flag(0), 0.0)
        self.assertEqual(ki.to_flag(1), 1.0)
        self.assertEqual(ki.to_flag("1"), 1.0)
        self.assertIsNone(ki.to_flag(3))
        self.assertIsNone(ki.to_flag(None))
        self.assertIsNone(ki.to_flag("open"))

    def test_kia_utc_ms(self):
        self.assertEqual(ki.kia_utc_ms("20261109161749"), STATUS_MS)
        self.assertEqual(ki.kia_utc_ms("20261109171422.244"), LOCATION_MS)
        self.assertIsNone(ki.kia_utc_ms("2026"))
        self.assertIsNone(ki.kia_utc_ms(None))

    def test_scheduled_service(self):
        self.assertEqual(ki.scheduled_service([8000, 16000, 24000], 9293.0),
                         {"scheduled_service_at": 16000.0, "miles_to_scheduled_service": 6707.0})
        self.assertEqual(ki.scheduled_service([8000], 9293.0), {})
        self.assertEqual(ki.scheduled_service(None, 9293.0), {})


class PointTests(unittest.TestCase):
    def setUp(self):
        self.fields = ki.build_point(Car())["fields"]

    def test_doors_and_tpms_survive_as_ints(self):
        f = self.fields
        self.assertEqual(f["hood_open"], 0.0)
        self.assertEqual(f["trunk_open"], 1.0)
        self.assertEqual(f["door_fl_open"], 1.0)
        self.assertEqual(f["door_fr_open"], 0.0)
        self.assertEqual(f["tpms_warn"], 0.0)

    def test_raw_fallbacks_and_new_fields(self):
        f = self.fields
        self.assertEqual(f["brake_fluid_warn"], 0.0)     # from brakeOilStatus, not the library
        self.assertEqual(f["engine_oil_warn"], 1.0)
        self.assertEqual(f["sunroof_open"], 0.0)
        self.assertEqual(f["defrost_on"], 0.0)
        self.assertEqual(f["ignition_on"], 1.0)
        self.assertEqual(f["lamp_fault"], 1.0)           # stopLamp.leftLamp is True
        self.assertEqual(f["window_rl"], 3.0)
        self.assertEqual(f["window_fl"], 0.0)
        self.assertEqual(f["heading"], 358.0)
        self.assertEqual(f["speed"], 0.0)
        self.assertEqual(f["dtc_count"], 0.0)
        self.assertEqual(f["tire_psi_fr"], 35.0)

    def test_timestamps_and_schedule(self):
        f = self.fields
        self.assertEqual(f["status_synced_at"], STATUS_MS)
        self.assertEqual(f["location_synced_at"], LOCATION_MS)
        self.assertEqual(f["scheduled_service_at"], 16000.0)
        self.assertEqual(f["miles_to_scheduled_service"], 6707.0)

    def test_every_field_is_a_float(self):
        for k, v in self.fields.items():
            self.assertIsInstance(v, float, k)

    def test_dump_knows_every_raw_path_the_exporter_reads(self):
        import kia_dump as kd
        flat = kd.flatten(RAW)
        unused = kd.unused_raw(flat, kd.library_paths() | kd.EXPORTER_RAW_PATHS)
        for must_be_used in (
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.brakeOilStatus",
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.windowStatus.windowRL",
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.lampWireStatus.stopLamp.leftLamp",
            "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.syncDate.utc",
            "vehicleConfig.maintenance.maintenanceSchedule[1]",
        ):
            self.assertNotIn(must_be_used, unused)
        self.assertIn("lastVehicleInfo.vehicleStatusRpt.vehicleStatus.batteryStatus.warning", unused)


if __name__ == "__main__":
    unittest.main()
