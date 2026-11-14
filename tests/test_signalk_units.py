import importlib.util
import json
import math
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[1]


def load_module(relative, name):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# Mismatches this module fixes (for the reviewer).  The old pair in
# boat-chat/app.py, convert_generic_value() and canonical_unit_for_measurement(),
# used different rules, so for many Signal K paths the stored number and its
# label disagreed:
#   * propulsion.port.oilTemperature stayed in kelvin but was labelled "F"
#     (only paths ending ".temperature" were converted).
#   * navigation.headingTrue stayed in radians but was labelled "deg"
#     (only headingMagnetic and courseOverGroundTrue were converted).
#   * tanks.fuel.0.capacity (m3) was labelled "%" (every tanks.* path was "%").
#   * non-fuel tank levels and relative humidity were ratios labelled "%"
#     without the x100.
#   * wind speeds stayed in m/s although the label said "kn".
#   * environment.current.drift / setTrue were labelled "A" (any "current").
#   * the "> 2000" pressure heuristic changed the unit depending on the value.
# Here one ordered table gives the conversion and the label from one lookup.

GOLDEN = [
    ("propulsion.port.oilTemperature", 358.15, 185.0, "F"),
    ("propulsion.port.temperature", 353.15, 176.0, "F"),
    ("environment.outside.dewPointTemperature", 283.15, 50.0, "F"),
    ("electrical.batteries.house.temperature", 298.15, 77.0, "F"),
    ("tanks.fuel.0.temperature", 293.15, 68.0, "F"),
    ("propulsion.port.transmission.oilTemperature", 363.15, 194.0, "F"),
    ("navigation.headingTrue", 1.5708, 90.0, "deg"),
    ("navigation.headingTrue", -0.1, 354.27, "deg"),
    ("environment.wind.angleApparent", -0.7854, -45.0, "deg"),
    ("environment.wind.directionTrue", 3.6652, 210.0, "deg"),
    ("navigation.rateOfTurn", 0.01, 34.38, "deg/min"),
    ("navigation.speedOverGround", 5.144, 10.0, "kn"),
    ("environment.wind.speedApparent", 10, 19.44, "kn"),
    ("tanks.fuel.0.currentLevel", 0.5, 50, "%"),
    ("tanks.freshWater.0.currentLevel", 0.25, 25, "%"),
    ("tanks.fuel.0.capacity", 0.378541, 100.0, "US gal"),
    ("tanks.fuel.0.currentVolume", 0.189271, 50.0, "US gal"),
    ("environment.inside.relativeHumidity", 0.65, 65, "%"),
    ("environment.outside.pressure", 101325, 29.92, "inHg"),
    ("propulsion.port.oilPressure", 300000, 43.51, "psi"),
    ("propulsion.port.coolantPressure", 1500, 0.22, "psi"),
    ("propulsion.port.fuel.rate", 1e-5, 9.51, "gal/h"),
    ("propulsion.port.revolutions", 30, 1800, "RPM"),
    ("propulsion.port.runTime", 36000, 10, "h"),
    ("environment.depth.belowTransducer", 3.0, 9.84, "ft"),
    ("navigation.log", 18520, 10, "NM"),
    ("electrical.batteries.house.capacity.consumedCharge", 36000, 10, "Ah"),
    ("electrical.batteries.house.capacity.stateOfCharge", 0.85, 85, "%"),
    ("electrical.batteries.house.capacity.stateOfCharge", 85, 85, "%"),
    ("electrical.batteries.house.voltage", 13.1, 13.1, "V"),
    ("electrical.batteries.house.current", -4.2, -4.2, "A"),
    ("electrical.ac.0.frequency", 60, 60, "Hz"),
    ("environment.current.drift", 1.0, 1.94, "kn"),
    ("propulsion.port.engineLoad", 0.4, 40, "%"),
]

US_CUSTOMARY_LABELS = {"F", "ft", "psi", "inHg", "US gal", "gal/h", "kn", "NM"}

# Labels display_units.py cannot convert to metric today (it either has no
# metric counterpart for them or does not know them).  They pass through
# display unchanged; the next slices add the ones that need a metric form
# (for example "US gal" is known, but "NM", "Ah", "deg/min" and "Hz" are not).
PASSTHROUGH_LABELS = {"deg", "kn", "%", "V", "A", "W", "RPM", "h", "NM", "Ah", "deg/min", "Hz"}

# Catalog paths (with "*" replaced) that resolve() does not recognise yet.
CATALOG_GAPS = {
    "electrical.batteries.0.capacity.consumed",
    "electrical.solar.0.panelPower",
    "electrical.ac.0.phase.A.lineNeutralVoltage",
}

CATALOG_SI = {
    "temperature": "K", "depth": "m", "pressure": "Pa", "barometric": "Pa",
    "volume": "m3", "flow": "m3/s", "speed": "m/s", "heading": "rad", "angle": "rad",
    "ratio": "ratio", "rpm": "Hz", "charge": "C", "voltage": "V", "current": "A",
    "power": "W", "distance": "m", "duration": "s",
}


def sample_path(pattern):
    if pattern.kind == "path":
        return pattern.text.replace("*", "port")
    if pattern.kind == "leaf":
        return (pattern.under or "x.") + "0." + pattern.text
    if pattern.kind == "suffix":
        return "x.dew" + pattern.text
    return "navigation.courseGreatCircle." + pattern.text


class SignalKUnitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = load_module("boat-chat/signalk_units.py", "signalk_units_test")
        cls.display = load_module("boat-chat/display_units.py", "display_units_for_signalk_test")

    def test_golden_table(self):
        for path, value, expected, label in GOLDEN:
            with self.subTest(path=path, value=value):
                got, got_label = self.units.canonical(path, value)
                self.assertAlmostEqual(got, expected, places=2)
                self.assertEqual(got_label, label)

    def test_heading_never_reaches_360(self):
        got, _ = self.units.canonical("navigation.headingTrue", -1e-20)
        self.assertLess(got, 360.0)
        self.assertGreaterEqual(got, 0.0)

    def test_fallbacks(self):
        u = self.units
        self.assertEqual(u.canonical("custom.thing", 7.0, "J"), (7.0, "J"))
        self.assertEqual(u.resolve("custom.thing", "J").origin, "meta")
        self.assertEqual(u.canonical("custom.thing", 7.0), (7.0, ""))
        self.assertEqual(u.resolve("custom.thing").origin, "unknown")
        self.assertEqual(u.resolve("custom.thing", "").origin, "unknown")
        r = u.resolve("propulsion.port.temperature", "K")
        self.assertEqual(r.origin, "table")
        self.assertAlmostEqual(u.canonical("propulsion.port.temperature", 353.15, "K")[0], 176.0, places=2)
        r = u.resolve("propulsion.port.temperature", "C")
        self.assertEqual(r.origin, "conflict")
        self.assertEqual(u.canonical("propulsion.port.temperature", 80.0, "C"), (80.0, "C"))

    def test_non_finite_values_do_not_raise(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            got, label = self.units.canonical("navigation.headingTrue", value)
            self.assertEqual(label, "deg")
            self.assertTrue(math.isnan(got) if math.isnan(value) else got == value)

    def test_case_sensitive_and_exact_segments(self):
        u = self.units
        self.assertEqual(u.resolve("navigation.headingtrue").origin, "unknown")
        self.assertEqual(u.resolve("environment.current.drift.extra").origin, "unknown")
        self.assertEqual(u.resolve("").origin, "unknown")
        self.assertEqual(u.resolve("electrical..voltage").origin, "unknown")
        # "current" is only an electrical current under electrical.
        self.assertEqual(u.resolve("environment.current.speed").origin, "unknown")

    def test_every_table_entry(self):
        u = self.units
        self.assertTrue(u.PATTERNS)
        for rule in u.PATTERNS:
            for pattern in rule.patterns:
                path = sample_path(pattern)
                with self.subTest(path=path):
                    r = u.resolve(path)
                    self.assertEqual(r.origin, "table")
                    self.assertTrue(r.unit)
                    self.assertTrue(r.si_unit)
                    # Shadowing check: an earlier rule must not capture this sample.
                    self.assertEqual((r.si_unit, r.quantity, r.unit), (rule.si_unit, rule.quantity, rule.unit))
                    self.assertEqual(u.canonical(path, 0.5), (r.convert(0.5), r.unit))
                    self.assertIn(r.unit, u.STORED_UNITS)
        self.assertEqual(u.STORED_UNITS, {rule.unit for rule in u.PATTERNS})

    def test_us_customary_labels_only_from_table(self):
        u = self.units
        paths = [sample_path(p) for rule in u.PATTERNS for p in rule.patterns]
        for path in paths:
            for meta in (None, "", "X", "C", "kn", "psi"):
                r = u.resolve(path, meta)
                if r.origin != "table":
                    self.assertNotIn(r.unit, US_CUSTOMARY_LABELS - {meta})
        for meta in US_CUSTOMARY_LABELS:
            r = u.resolve("unknown.path", meta)
            self.assertEqual(r.origin, "meta")
            self.assertEqual(r.convert(3.0), 3.0)

    def test_labels_known_to_display_units_or_listed_passthrough(self):
        d = self.display
        targets = ("C", "m", "kPa", "hPa", "L", "L/h")
        convertible = {label for label in self.units.STORED_UNITS
                       if any(d.conversion_supported(label, t) for t in targets)}
        self.assertEqual(convertible | PASSTHROUGH_LABELS, set(self.units.STORED_UNITS))
        self.assertFalse(convertible & PASSTHROUGH_LABELS)

    def test_parity_with_displays_catalog(self):
        u = self.units
        catalog = json.loads((ROOT / "displays" / "catalog.json").read_text())
        unknown = set()
        checked = 0
        for entry in catalog["entries"]:
            quantity = entry.get("quantity")
            if entry.get("kind") == "notification" or quantity not in CATALOG_SI:
                continue
            path = entry["path"].replace("*", "0")
            r = u.resolve(path)
            if r.origin == "unknown":
                unknown.add(path)
                continue
            checked += 1
            with self.subTest(path=path):
                self.assertEqual(r.si_unit, CATALOG_SI[quantity])
        self.assertGreater(checked, 30)
        self.assertEqual(unknown, CATALOG_GAPS)


if __name__ == "__main__":
    unittest.main()
