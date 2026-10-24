#!/usr/bin/env python3
"""Read-only cruise economy report. No model calls or cache writes."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'boat-chat'))
import app
from cruise_efficiency import speed_bands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--minimum', type=float, default=15)
    parser.add_argument('--maximum', type=float, default=40)
    parser.add_argument('--step', type=float, default=2.5)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error('--days must be between 1 and 365 (retention limit)')
    try:
        speed_bands([], args.minimum, args.maximum, args.step)
    except ValueError as exc:
        parser.error(str(exc))
    query = f'''from(bucket: "{app.INFLUX_HISTORY_BUCKET}")
      |> range(start: -{args.days}d)
      |> filter(fn: (r) => r._measurement == "propulsion.port.fuel.rate" or r._measurement == "propulsion.starboard.fuel.rate" or r._measurement == "propulsion.port.revolutions" or r._measurement == "propulsion.starboard.revolutions" or r._measurement == "navigation.speedOverGround")
      |> filter(fn: (r) => r._field == "value")
      |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
      |> keep(columns: ["_time", "_measurement", "_value"])
      |> pivot(rowKey: ["_time"], columnKey: ["_measurement"], valueColumn: "_value")
      |> sort(columns: ["_time"])'''
    result = speed_bands(app.query_influx(query), args.minimum, args.maximum, args.step)
    result['lookback_days'] = args.days
    if args.json:
        print(json.dumps(result, indent=2, allow_nan=False))
        return
    print(f'Retained history, last {args.days} days; speed over ground; US gallons')
    print('| Speed kn | Avg kn | RPM | Total gal/h | nm/gal | gal/100 nm | Minutes | Days | Support |')
    print('|---|---:|---:|---:|---:|---:|---:|---:|---|')
    def fmt(value, digits):
        return '—' if value is None else f'{value:.{digits}f}'
    for b in result['bands']:
        print(f"| {b['minimum_knots']:g}–{b['maximum_knots']:g} | {fmt(b['average_knots'],1)} | {fmt(b['average_rpm'],0)} | {fmt(b['total_gph'],1)} | {fmt(b['nm_per_gallon'],3)} | {fmt(b['gallons_per_100nm'],1)} | {b['minutes']} | {b['days']} | {b['sample_support']} |")
    for note in result['notes']:
        print(note)


if __name__ == '__main__':
    main()
