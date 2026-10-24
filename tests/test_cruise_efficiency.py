import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import copy
import json
import math
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
import app
from cruise_efficiency import matched_sample, speed_bands


def row(speed=20, fuel=10, rpm=4000, time='2026-09-01T12:00:00Z'):
    return {'_time': time, 'navigation.speedOverGround': speed * 1852 / 3600,
            'propulsion.port.revolutions': rpm / 60,
            'propulsion.starboard.revolutions': rpm / 60,
            'propulsion.port.fuel.rate': fuel / (3600 * 264.172052358),
            'propulsion.starboard.fuel.rate': fuel / (3600 * 264.172052358)}


class EconomyCoverageTests(unittest.TestCase):
    def summary(self, rows, **kwargs):
        with patch.object(app, 'query_influx', return_value=rows), patch.object(app.telemetry_cache, 'put_summary'):
            return app.fuel_economy_summary(use_cache=False, **kwargs)

    def test_missing_fuel_does_not_inflate_economy(self):
        broken = row();del broken['propulsion.port.fuel.rate']
        s = self.summary([row(), broken])
        self.assertAlmostEqual(s['economy']['nm_per_gallon'], 1.0, places=3)
        self.assertEqual(s['sample_counts']['matched_minutes'], 1)
        self.assertEqual(s['sample_counts']['excluded_running_minutes'], 1)
        self.assertEqual(s['sample_counts']['coverage_percent'], 50)

    def test_missing_speed_does_not_add_unmatched_fuel(self):
        broken = row();del broken['navigation.speedOverGround']
        s = self.summary([row(), broken])
        self.assertAlmostEqual(s['economy']['nm_per_gallon'], 1.0, places=3)
        self.assertAlmostEqual(s['economy']['avg_speed_knots_running'], 20)

    def test_missing_rpm_is_excluded(self):
        broken = row();del broken['propulsion.port.revolutions']
        self.assertIsNone(matched_sample(broken))

    def test_nonfinite_negative_and_boolean_inputs_are_rejected(self):
        for key in row():
            if key == '_time':continue
            for value in ('NaN', 'inf', '-inf', -1, True):
                with self.subTest(key=key, value=value):
                    broken = row();broken[key] = value
                    self.assertIsNone(matched_sample(broken))

    def test_zero_speed_is_valid_and_burns_fuel(self):
        s = self.summary([row(speed=0), row()])
        self.assertEqual(s['sample_counts']['matched_minutes'], 2)
        self.assertAlmostEqual(s['economy']['nm_per_gallon'], .5, places=3)

    def test_no_complete_samples_has_no_economy(self):
        broken = row();del broken['propulsion.port.fuel.rate']
        s = self.summary([broken])
        self.assertIsNone(s['economy']['nm_per_gallon'])
        self.assertIsNone(s['economy']['avg_speed_knots_running'])

    def test_require_both_engines_without_rpm_band(self):
        broken = row();broken['propulsion.starboard.revolutions'] = 0
        s = self.summary([broken], require_both_engines=True)
        self.assertEqual(s['running_minutes'], 0)

    def test_answer_discloses_matched_coverage(self):
        broken = row();del broken['navigation.speedOverGround']
        s = self.summary([row(), broken])
        answer = app.answer_from_fuel_economy({'question_type': {'fuel_economy':True}, 'fuel_economy':s})
        self.assertIn('1 of 2 retained running minutes', answer)

    def test_cache_keys_separate_both_scope(self):
        keys=[]
        with patch.object(app, 'query_influx', return_value=[]), patch.object(app.telemetry_cache, 'put_summary', side_effect=lambda category,key,payload: keys.append(key)):
            app.fuel_economy_summary(use_cache=False)
            app.fuel_economy_summary(use_cache=False, require_both_engines=True)
        self.assertNotEqual(keys[0], keys[1])

    def test_old_cached_calculation_is_recomputed(self):
        with patch.object(app.telemetry_cache, 'get_summary', return_value={'economy': {'nm_per_gallon':99}}), patch.object(app, 'query_influx', return_value=[row()]) as query, patch.object(app.telemetry_cache, 'put_summary'):
            s = app.fuel_economy_summary()
        query.assert_called_once()
        self.assertEqual(s['calculation_version'], 2)
        self.assertAlmostEqual(s['economy']['nm_per_gallon'],1)


class CruiseBandTests(unittest.TestCase):
    def test_boundaries_and_empty_bands(self):
        s = speed_bands([row(15),row(17.5),row(40),row(40.1)])
        self.assertEqual([1,1,0,0,0,0,0,0,0,1], [b['minutes'] for b in s['bands']])
        self.assertIsNone(s['bands'][2]['nm_per_gallon'])
        json.dumps(s, allow_nan=False)

    def test_economy_is_ratio_of_totals_not_mean_of_ratios(self):
        s = speed_bands([row(20,5), row(20,15)])
        self.assertAlmostEqual(s['bands'][2]['nm_per_gallon'],1)

    def test_reject_invalid_ranges(self):
        for args in [(40,15,2.5),(15,40,0),(15,math.inf,2.5),(15,40,.001)]:
            with self.assertRaises(ValueError):speed_bands([], *args)

    def test_limited_sample_support_is_explicit(self):
        s = speed_bands([row(25)])
        self.assertEqual(s['bands'][4]['sample_support'],'limited')
        self.assertEqual(s['bands'][4]['days'],1)
