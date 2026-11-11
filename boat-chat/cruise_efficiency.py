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


def matched_sample(row, rpm_threshold=200, engines=None):
    """Match all configured fuel-capable engines, without inventing zeroes.

    ``engines`` accepts shared-profile engine records.  Omitting it preserves
    the historical port/starboard contract for existing callers.
    """
    if engines is None:
        engines = [{"id": "port", "path": "propulsion.port", "fuel_rate_supported": True},
                   {"id": "starboard", "path": "propulsion.starboard", "fuel_rate_supported": True}]
    capable = [e for e in engines if e.get("fuel_rate_supported")]
    if not capable:
        return None
    samples = []
    for engine in capable:
        path = engine["path"]
        rpm = number(row.get(path + ".revolutions"))
        fuel = number(row.get(path + ".fuel.rate"))
        if rpm is None or fuel is None or rpm < 0 or fuel < 0:
            return None
        samples.append((engine["id"], rpm * 60, fuel))
    speed = number(row.get("navigation.speedOverGround"))
    if speed is None or speed < 0 or max(x[1] for x in samples) < rpm_threshold:
        return None
    result = {"knots": speed * 3600 / 1852,
              "gph": sum(x[2] for x in samples) * 3600 * GALLONS_PER_CUBIC_METRE,
              "time": row.get("_time", ""), "engine_count": len(samples),
              "engine_rpm": {x[0]: x[1] for x in samples},
              "engine_fuel_rate": {x[0]: x[2] for x in samples}}
    return result


def speed_bands(rows, minimum=15, maximum=40, step=2.5, engines=None):
    if not all(math.isfinite(x) for x in (minimum, maximum, step)) or not (0 <= minimum < maximum and step > 0):
        raise ValueError('Use finite speeds with 0 <= minimum < maximum and step > 0')
    count = math.ceil((maximum - minimum) / step)
    if count > 200:
        raise ValueError('Use at most 200 speed bands')
    groups = defaultdict(list)
    rejected = 0
    for row in rows:
        sample = matched_sample(row, rpm_threshold=600, engines=engines)
        if sample is None:
            rejected += 1
            continue
        rpms = list(sample.get('engine_rpm', {}).values())
        if rpms and (min(rpms) < 600 or (len(rpms) > 1 and max(rpms) - min(rpms) > 150)):
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
                          average_rpm=sum(sum(s.get('engine_rpm', {}).values()) / max(1, len(s.get('engine_rpm', {}))) for s in samples) / n if n else None,
                          total_gph=fuel, nm_per_gallon=speed / fuel if fuel else None,
                          gallons_per_100nm=100 * fuel / speed if speed else None,
                          first_sample_utc=min((s['time'] for s in samples), default=None),
                          last_sample_utc=max((s['time'] for s in samples), default=None),
                          sample_support='broader' if n >= 60 and days >= 3 else 'limited'))
    return dict(bands=bands, incomplete_or_invalid_rows=rejected,
                notes=['One-minute averages; configured engines >=600 RPM and within 150 RPM.',
                       'Speed over ground includes current effects; transitions are included.',
                       'Broader support means >=60 sampled minutes across >=3 UTC days; it is not statistical confidence.',
                       'Fuel burn is configured engines combined, in US gallons. No interpolation.'])
