import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import unittest
import datetime as dt
import status_model as model

class OwnerStatusTests(unittest.TestCase):
    def fixture(self):
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        values={'electrical.batteries.shunt.capacity.stateOfCharge':80,'electrical.batteries.shunt.voltage':13.1,'propulsion.port.revolutions':0,'propulsion.starboard.revolutions':0,'tanks.fuel.0.currentLevel':0,'tanks.fuel.1.currentLevel':50}
        states={'sensor.boat_health_summary':'OK','binary_sensor.boat_ok':'on','sensor.boat_watch_summary':'Dock: clear, dock 12 ft','sensor.engine_alarm_status':'All Clear','sensor.weather_risk_level':'Normal','input_select.boat_watch_mode':'Dock','input_boolean.underway_mode':'off','binary_sensor.shore_power_connected':'on'}
        return {'cache':{'age_seconds':10},'ha_states':{k:{'state':v} for k,v in states.items()},'current_telemetry':{'values':values,'timestamps':{k:now for k in values}}}
    def test_missing_is_not_safe_or_zero(self):
        result=model.normalize({})
        self.assertEqual('Status incomplete',result['condition'])
        self.assertTrue(all(x['severity']=='unknown' for x in result['alerts']))
        self.assertNotIn('0%',str(result['cards']))
        self.assertNotIn('Docked',str(result))
    def test_normal_states_and_real_zero(self):
        result=model.normalize(self.fixture())
        self.assertEqual('No reported issues',result['condition'])
        self.assertTrue(all(x['severity']=='ok' for x in result['alerts']))
        self.assertEqual('0% / 50%',result['cards'][-1]['value'])
    def test_stale_cache(self):
        cached=self.fixture();cached['cache']['age_seconds']=1801
        result=model.normalize(cached)
        self.assertEqual('outdated',result['freshness']);self.assertTrue(result['unavailable_count'])
    def test_old_source_in_fresh_cache(self):
        cached=self.fixture();cached['current_telemetry']['timestamps']['propulsion.port.revolutions']='2020-01-01T00:00:00Z'
        result=model.normalize(cached)
        self.assertEqual('unknown',result['cards'][4]['tone'])
    def test_conflicting_health(self):
        cached=self.fixture();cached['ha_states']['binary_sensor.boat_ok']['state']='off'
        self.assertEqual('Needs attention',model.normalize(cached)['condition'])
    def test_partial_and_invalid(self):
        cached=self.fixture();cached['current_telemetry']['values']['electrical.batteries.shunt.capacity.stateOfCharge']=float('nan')
        result=model.normalize(cached);self.assertEqual('unknown',result['cards'][3]['tone']);self.assertIn('Unavailable',result['cards'][3]['value'])
    def test_alarm_and_system_separation(self):
        cached=self.fixture();cached['ha_states']['sensor.audit_health_summary']={'state':'Issues: repository dirty'}
        self.assertEqual('No reported issues',model.normalize(cached)['condition'])
        cached['ha_states']['sensor.engine_alarm_status']['state']='Low oil pressure'
        self.assertEqual('Needs attention',model.normalize(cached)['condition'])
    def test_chat_health_uses_the_same_missing_data_rules(self):
        import app
        context = {'question_type': {'health': True}, 'ha_states': {'sensor.boat_health_summary': {'state': 'OK'}, 'binary_sensor.boat_ok': {'state': 'on'}}}
        answer = app.answer_from_health_state('Is the boat OK?', context)
        self.assertIn('Status incomplete', answer)
        self.assertNotIn('Yes.', answer)

    def test_unrecognized_watch_is_not_clear(self):
        self.assertEqual('warning',model.classify('Boat watch','Dock: clear, alarm active'))
        self.assertEqual('unknown',model.classify('Boat watch','unavailable'))

if __name__=='__main__':unittest.main()
