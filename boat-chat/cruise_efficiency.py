"""Pure calculations for matched one-minute cruise observations (SignalK SI)."""
import math
from collections import defaultdict

GALLONS_PER_CUBIC_METRE = 264.172052358


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def matched_sample(row, rpm_threshold=200):
    """Require both RPMs, both fuel rates and SOG; never fill missing data."""
    values = [number(row.get(key)) for key in (
        'propulsion.port.revolutions', 'propulsion.starboard.revolutions',
        'propulsion.port.fuel.rate', 'propulsion.starboard.fuel.rate',
        'navigation.speedOverGround')]
    if any(x is None or x < 0 for x in values):
        return None
    port, starboard, port_fuel, starboard_fuel, speed = values
    if max(port, starboard) * 60 < rpm_threshold:
        return None
    return dict(port_rpm=port * 60, starboard_rpm=starboard * 60,
                knots=speed * 3600 / 1852,
                gph=(port_fuel + starboard_fuel) * 3600 * GALLONS_PER_CUBIC_METRE,
                time=row.get('_time', ''))


def speed_bands(rows, minimum=15, maximum=40, step=2.5):
    if not all(math.isfinite(x) for x in (minimum, maximum, step)) or not (0 <= minimum < maximum and step > 0):
        raise ValueError('Use finite speeds with 0 <= minimum < maximum and step > 0')
    count = math.ceil((maximum - minimum) / step)
    if count > 200:
        raise ValueError('Use at most 200 speed bands')
    groups = defaultdict(list)
    rejected = 0
    for row in rows:
        sample = matched_sample(row, rpm_threshold=600)
        if sample is None:
            rejected += 1
            continue
        if min(sample['port_rpm'], sample['starboard_rpm']) < 600 or abs(sample['port_rpm'] - sample['starboard_rpm']) > 150:
            continue
        if not minimum <= sample['knots'] <= maximum or sample['gph'] <= 0:
            continue
        band = min(int((sample['knots'] - minimum) / step), count - 1)
        groups[band].append(sample)
    bands = []
    for i in range(count):
        samples = groups[i]
        n = len(samples)
        speed = sum(s['knots'] for s in samples) / n if n else None
        fuel = sum(s['gph'] for s in samples) / n if n else None
        days = len({s['time'][:10] for s in samples if s['time']})
        bands.append(dict(minimum_knots=minimum + i * step,
                          maximum_knots=min(maximum, minimum + (i + 1) * step),
                          minutes=n, days=days, average_knots=speed,
                          average_rpm=sum((s['port_rpm'] + s['starboard_rpm']) / 2 for s in samples) / n if n else None,
                          total_gph=fuel, nm_per_gallon=speed / fuel if fuel else None,
                          gallons_per_100nm=100 * fuel / speed if speed else None,
                          first_sample_utc=min((s['time'] for s in samples), default=None),
                          last_sample_utc=max((s['time'] for s in samples), default=None),
                          sample_support='broader' if n >= 60 and days >= 3 else 'limited'))
    return dict(bands=bands, incomplete_or_invalid_rows=rejected,
                notes=['One-minute averages; both engines >=600 RPM and within 150 RPM.',
                       'Speed over ground includes current effects; transitions are included.',
                       'Broader support means >=60 sampled minutes across >=3 UTC days; it is not statistical confidence.',
                       'Fuel burn is both engines combined, in US gallons. No interpolation.'])
