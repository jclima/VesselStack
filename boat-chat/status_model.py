"""Pure owner-facing status normalization; never turn absent measurements into zero."""
import datetime as dt
import math
import re
import data_age
try:
    import profile_runtime
except ImportError:  # direct module loading in older tests
    profile_runtime = None

FRESH_SECONDS = data_age.FRESH_SECONDS
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


def normalize(cached, profile=None):
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
    runtime = profile_runtime.current() if profile is None and profile_runtime else {"profile": profile or {}, "diagnostic": None}
    equipment = runtime.get("profile", profile or {})
    engines = equipment.get("engines", [])
    banks = equipment.get("battery_banks", [])
    tanks = equipment.get("fuel_tanks", [])
    mode = 'Underway' if underway == 'on' else f'{watch} watch' if watch.lower() in {'dock','away','anchor'} else 'Unavailable'
    card('mode', 'Watch mode', mode, 'normal' if mode != 'Unavailable' else 'unknown', 'Give me a current boat status briefing')
    charging = state('binary_sensor.shore_power_connected')
    card('shore', 'Charging', {'on':'Charging detected','off':'No charging detected'}.get(charging,'Unavailable'), 'normal' if charging in {'on','off'} else 'unknown', 'What is the current shore power status?')
    primary = next((b for b in banks if b.get('id') == equipment.get('primary_battery_bank')), None)
    battery_prefix = primary.get('path') if primary else None
    soc = measured(f'{battery_prefix}.capacity.stateOfCharge', True) if battery_prefix else None
    voltage = measured(f'{battery_prefix}.voltage') if battery_prefix else None
    bank_values = []
    for bank in banks:
        prefix = bank['path']
        bank_soc = measured(f'{prefix}.capacity.stateOfCharge', True)
        bank_voltage = measured(f'{prefix}.voltage')
        bank_values.append(f"{bank.get('label', bank.get('id', 'Battery'))}: {display(bank_soc,'%')} · {display(bank_voltage,' V','.2f')}")
    battery_value = ' | '.join(bank_values) if bank_values else 'Not configured'
    battery_tone = 'unknown' if banks and any(measured(f"{b['path']}.capacity.stateOfCharge", True) is None or measured(f"{b['path']}.voltage") is None for b in banks) else 'normal'
    if soc is not None and soc < 20: battery_tone = 'warning'
    card('battery','Battery',battery_value, battery_tone, 'Tell me about the battery right now')
    engine_values = [(e, measured(f"{e['path']}.revolutions")) for e in engines]
    engine_text = ' / '.join(display(v, '') for _e, v in engine_values) if engine_values else 'Not configured'
    engine_label = 'Engine RPM' if not engines else 'Engine RPM (' + ', '.join(str(e.get('label', e.get('id', 'Engine'))) for e in engines) + ')'
    card('engines',engine_label,engine_text, 'unknown' if engine_values and any(v is None for _e, v in engine_values) else 'normal', 'Compare the engines right now')
    fuel = [measured(f"{t['path']}.currentLevel",True) for t in tanks]
    fuel_label = 'Fuel tanks' if not tanks else 'Fuel tanks (' + ', '.join(str(t.get('label', t.get('id', 'Tank'))) for t in tanks) + ')'
    card('fuel',fuel_label, ' / '.join(display(x,'%') for x in fuel) if fuel else 'Not configured', 'unknown' if fuel and None in fuel else 'normal', 'How much fuel is left?')
    timestamps = (cached.get('current_telemetry') or {}).get('timestamps') or {}
    measurements = {
        'battery': [path for b in banks for path in (f"{b['path']}.capacity.stateOfCharge", f"{b['path']}.voltage")],
        'engines': [f"{e['path']}.revolutions" for e in engines],
        'fuel': [f"{t['path']}.currentLevel" for t in tanks],
    }
    now = dt.datetime.now(dt.timezone.utc)
    for c in cards:
        paths = measurements.get(c['id'], [])
        ages = []
        for path in paths:
            stamp = data_age.parse_timestamp(timestamps.get(path))
            ages.append((now - stamp).total_seconds() if stamp else None)
        if paths:
            c['source_age_seconds'] = max(ages) if all(x is not None for x in ages) else None
            if any(x is None or x > FRESH_SECONDS or x < -data_age.FUTURE_TOLERANCE_SECONDS for x in ages):
                c['tone'] = 'unknown'
                c['value'] += ' · source age unverified' if c['source_age_seconds'] is None else ' · older reading'
    attention = [a for a in alerts if a['severity'] != 'ok']
    if runtime.get('diagnostic'):
        attention.append(dict(label='Boat profile', value='Unavailable', severity='unknown', reason=runtime['diagnostic'], prompt='How is the boat profile configured?'))
    for c in cards[1:]:
        if c['tone'] == 'unknown':
            attention.append(dict(label=c['label'], value=c['value'], severity='unknown', reason='Measurement missing, outdated, or source age unverified; check its source', prompt=c['prompt']))
        elif c['tone'] == 'warning':
            reason = 'Battery charge is below 20%' if c['id'] == 'battery' else 'Review this boat status'
            attention.append(dict(label=c['label'], value=c['value'], severity='warning', reason=reason, prompt=c['prompt']))
    warnings = sum(a['severity']=='warning' for a in attention)
    unknown = sum(a['severity']=='unknown' for a in attention)
    return dict(cards=cards, alerts=alerts, attention=attention, warning_count=warnings, unavailable_count=unknown,
                condition='Needs attention' if warnings else 'Status incomplete' if unknown else 'No reported issues',
                watch_summary=state('sensor.boat_watch_summary'), age_seconds=age, observed_at=cache.get('updated_at'),
                freshness='current' if fresh else 'outdated' if age is not None else 'unavailable',
                source='Cached boat observations', system_status=state('sensor.audit_health_summary'))
