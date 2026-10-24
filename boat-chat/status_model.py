"""Pure owner-facing status normalization; never turn absent measurements into zero."""
import datetime as dt
import math
import re

FRESH_SECONDS = 600
SOURCES = [
    ('Boat health', 'sensor.boat_health_summary', '/lovelace/health'),
    ('Boat watch', 'sensor.boat_watch_summary', '/lovelace/anchor'),
    ('Engine alarms', 'sensor.engine_alarm_status', '/lovelace/engines'),
    ('Weather risk', 'sensor.weather_risk_level', '/lovelace/environment'),
]

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def classify(label, value):
    text = str(value or '').strip().lower()
    if text in {'', 'unknown', 'unavailable', 'none'}:
        return 'unknown'
    normal = {'Boat health': {'ok'}, 'Engine alarms': {'all clear', 'normal'},
              'Weather risk': {'normal', 'low'}, 'Boat watch': {'clear'}}
    if text in normal.get(label, {'ok', 'healthy'}):
        return 'ok'
    if label == 'Boat watch' and re.fullmatch(r'(dock|away|anchor): clear(?:, (?:dock|drift) [\d.]+ ft)?', text):
        return 'ok'
    return 'warning'


def normalize(cached):
    cache = cached.get('cache') or {}
    age = number(cache.get('age_seconds'))
    fresh = age is not None and age <= FRESH_SECONDS
    ha = cached.get('ha_states') or {}
    state = lambda key: str((ha.get(key) or {}).get('state', 'Unavailable'))
    values = (cached.get('current_telemetry') or {}).get('values') or {}
    alerts = []
    for label, entity, path in SOURCES:
        value = state(entity)
        severity = classify(label, value) if fresh else 'unknown'
        reason = value if fresh else 'Status snapshot is outdated or unavailable'
        alerts.append(dict(label=label, value=value, severity=severity, reason=reason,
                           source='Home Assistant', path=path))
    if state('binary_sensor.boat_ok') == 'off' and alerts[0]['severity'] == 'ok':
        alerts[0].update(severity='warning', reason='Health indicators disagree; review boat status')
    if state('binary_sensor.boat_ok') not in {'on', 'off'} and alerts[0]['severity'] == 'ok':
        alerts[0].update(severity='unknown', reason='Boat OK indicator is unavailable')
    cards = []
    def card(key, label, value, tone, prompt):
        cards.append(dict(id=key, label=label, value=value, tone=tone if fresh else 'unknown', prompt=prompt))
    def measured(key, percent=False):
        value = number(values.get(key))
        if value is None or value < 0 or (percent and value > 100):
            return None
        return value
    def display(value, unit, precision='g'):
        return 'Unavailable' if value is None else f'{value:{precision}}{unit}'
    card('health', 'Boat', state('sensor.boat_health_summary'), alerts[0]['severity'], 'Is the boat OK?')
    watch = state('input_select.boat_watch_mode')
    underway = state('input_boolean.underway_mode')
    port, starboard = [measured(f'propulsion.{side}.revolutions') for side in ('port', 'starboard')]
    mode = 'Underway' if underway == 'on' else f'{watch} watch' if watch.lower() in {'dock','away','anchor'} else 'Unavailable'
    card('mode', 'Watch mode', mode, 'normal' if mode != 'Unavailable' else 'unknown', 'Give me a current boat status briefing')
    charging = state('binary_sensor.shore_power_connected')
    card('shore', 'Charging proxy', {'on':'Charging detected','off':'No charging detected'}.get(charging,'Unavailable'), 'normal' if charging in {'on','off'} else 'unknown', 'What is the current shore power status?')
    soc = measured('electrical.batteries.shunt.capacity.stateOfCharge', True)
    voltage = measured('electrical.batteries.shunt.voltage')
    card('battery','Battery',display(soc,'%')+' · '+display(voltage,' V','.2f'), 'unknown' if soc is None or voltage is None else 'warning' if soc < 20 else 'normal', 'Tell me about the battery right now')
    card('engines','Engine RPM',display(port,'')+' / '+display(starboard,''), 'unknown' if port is None or starboard is None else 'normal', 'Compare both engines right now')
    fuel = [measured(f'tanks.fuel.{side}.currentLevel',True) for side in (0,1)]
    card('fuel','Fuel tanks', ' / '.join(display(x,'%') for x in fuel), 'unknown' if None in fuel else 'normal', 'How much fuel is left?')
    timestamps = (cached.get('current_telemetry') or {}).get('timestamps') or {}
    measurements = {
        'battery': ['electrical.batteries.shunt.capacity.stateOfCharge', 'electrical.batteries.shunt.voltage'],
        'engines': ['propulsion.port.revolutions', 'propulsion.starboard.revolutions'],
        'fuel': ['tanks.fuel.0.currentLevel', 'tanks.fuel.1.currentLevel'],
    }
    now = dt.datetime.now(dt.timezone.utc)
    for c in cards:
        paths = measurements.get(c['id'], [])
        ages = []
        for path in paths:
            try:
                stamp = dt.datetime.fromisoformat(str(timestamps[path]).replace('Z', '+00:00'))
                ages.append((now - stamp).total_seconds())
            except (KeyError, ValueError, TypeError):
                ages.append(None)
        if paths:
            c['source_age_seconds'] = max(ages) if all(x is not None for x in ages) else None
            if any(x is None or x > FRESH_SECONDS or x < -60 for x in ages):
                c['tone'] = 'unknown'
                c['value'] += ' · source age unverified' if c['source_age_seconds'] is None else ' · older reading'
    attention = [a for a in alerts if a['severity'] != 'ok']
    for c in cards[1:]:
        if c['tone'] == 'unknown':
            attention.append(dict(label=c['label'], value=c['value'], severity='unknown', reason='Measurement missing, outdated, or source age unverified; check its source', prompt=c['prompt']))
        elif c['tone'] == 'warning':
            attention.append(dict(label=c['label'], value=c['value'], severity='warning', reason='Battery charge is below 20%', prompt=c['prompt']))
    warnings = sum(a['severity']=='warning' for a in attention)
    unknown = sum(a['severity']=='unknown' for a in attention)
    return dict(cards=cards, alerts=alerts, attention=attention, warning_count=warnings, unavailable_count=unknown,
                condition='Needs attention' if warnings else 'Status incomplete' if unknown else 'No reported issues',
                watch_summary=state('sensor.boat_watch_summary'), age_seconds=age, observed_at=cache.get('updated_at'),
                freshness='current' if fresh else 'outdated' if age is not None else 'unavailable',
                source='Cached boat observations', system_status=state('sensor.audit_health_summary'))
