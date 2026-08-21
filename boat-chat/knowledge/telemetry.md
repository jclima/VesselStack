# Telemetry Knowledge

## Sources

- Current marine telemetry comes from SignalK.
- Nearby AIS traffic comes from the SignalK `/vessels` feed. Position timestamps older than 15 minutes are marked stale.
- Current health/watch state comes from Home Assistant.
- Historical fuel, runtime, speed, and trend calculations come from retained InfluxDB summaries or cached SQLite summaries.
- The SQLite telemetry cache stores derived summaries only; it is not the source of truth for raw telemetry.
- Forward solar inference is sampled every five minutes into `boat-chat/data/power_tracking.sqlite`. Positive SmartShunt power is integrated only when the boat is underway, beyond the dock radius, or the charging proxy is off; both engines are below 200 RPM; and adjacent samples are no more than 15 minutes apart.
- Inferred solar is net battery charging after loads. A compatible solar controller should provide direct `electrical.solar.<id>.panelPower` and `yieldToday` for gross production.

## Engine Running

- Engine-running samples use either port or starboard RPM >= 200 RPM.
- SignalK stores engine revolutions in revolutions per second; answers convert to RPM.
- Retained historical answers use the configured one-minute downsample bucket.
- Complex engine questions include both all-sample and engine-running views. Prefer the running view for oil pressure, fuel rate, temperature, load, and side comparisons.
- Historical summaries retain extrema timestamps and aligned values at those times. Port/starboard comparisons include all aligned samples and samples whose RPM differs by no more than 50 RPM.
- Correlation coefficients describe retained aligned samples only; they do not establish causation.

## Fuel

- Fuel rate is stored as SignalK cubic meters per second.
- Boat Chat converts fuel rate to US gallons/hour, then integrates by minute for fuel totals.
- All fuel answers use US units: gallons and gal/h. Engine temperatures are °F, oil pressure is psi, wind is knots, barometric pressure is inHg.
- Fuel totals include only minutes where at least one engine is above the running threshold.
- Fuel economy estimates distance from `navigation.speedOverGround`; current, tide, GPS noise, and docking maneuvers can affect the result.

## Shore Power

- `binary_sensor.shore_power_connected` is a Home Assistant template binary sensor.
- It is on when SmartShunt voltage/current indicate charging.
- It is a charging proxy, not proof of physical AC input. Solar charging can turn it on.
- It has a 2-minute on delay and 30-minute off delay to avoid false shore-power loss during charger float mode.

## Battery Voltage

- The boat uses a 12 V AGM battery reference for voltage-to-state-of-charge estimates.
- AGM reference points: 13.00 V = 100%, 12.75 V = 90%, 12.50 V = 80%, 12.30 V = 70%, 12.15 V = 60%, 12.05 V = 50%, 11.95 V = 40%, 11.81 V = 30%, 11.66 V = 20%, 11.51 V = 10%, 10.50 V = 0%.
- This chart is most useful for resting battery voltage. Charging, float mode, or active loads can make voltage-based SOC estimates misleading.
- SmartShunt SOC is preferred when available. Voltage history is still useful for detecting lows, charger behavior, and sustained drops.

## Health

- `binary_sensor.boat_ok` and `sensor.boat_health_summary` are concise current-health signals.
- Watch-mode issues are advisory unless the specific sensor says otherwise.
- `sensor.boat_watch_summary` gives the best human-readable dock/away watch summary.
- A Home Assistant `last_updated` age over six hours is a review signal. Stable sensor values may legitimately keep an old timestamp, so age alone is not proof of failure.
