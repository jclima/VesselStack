# Power And Solar Monitoring

## Current Coverage

The boat currently exposes one Victron SmartShunt through the SignalK
`signalk-victron-ble` plugin. It measures whole-house-bank voltage, current,
power, state of charge, discharge since full, and time remaining. There is no
dedicated solar-controller, PV, charger-output, or AC shore-power measurement
in SignalK or Home Assistant.

Home Assistant's `binary_sensor.shore_power_connected` is not a physical
shore-input sensor. It is a delayed charging proxy derived from SmartShunt
voltage and current, so sufficient solar charging can turn it on.

Boat Chat records a sample every five minutes in
`boat-chat/data/power_tracking.sqlite`. It attributes positive SmartShunt power
to `inferred net solar or another uninstrumented source` only when:

- The boat is underway, is beyond the configured dock radius, or the charging
  proxy is off.
- Both engines report less than 200 RPM.
- SmartShunt battery power is available.
- The gap between adjacent observations is no longer than 15 minutes.

It separately integrates positive net charge and net battery discharge. This
is net energy at the battery after onboard loads, not gross panel production.
The database is ignored by Git, retained for 400 days, and included in the
normal VesselStack backup.

## Best Upgrade Order

1. Identify the existing solar charge controller.

   Record its manufacturer, exact model, panel count, panel wattage, panel
   `Voc` and `Isc`, battery chemistry, and controller-to-battery wiring. Do not
   select a replacement MPPT from panel wattage alone.

2. Connect an existing compatible controller before replacing it.

   The installed SignalK `signalk-victron-ble` plugin supports Victron
   SmartSolar and BlueSolar MPPT controllers. For SmartSolar, add its Bluetooth
   MAC address and Victron advertisement key as another plugin device. For a
   compatible controller with VE.Direct, a VE.Direct-to-USB interface to the
   Raspberry Pi is another local option. Never commit the advertisement key.

   The plugin can expose paths including:

   - `electrical.solar.<id>.panelPower`
   - `electrical.solar.<id>.loadCurrent`
   - `electrical.solar.<id>.yieldToday`

3. If the existing controller has no data interface, use a correctly sized
   Victron SmartSolar MPPT.

   SmartSolar provides direct PV power, battery charge current, charge stage,
   daily yield, peak PV power, and retained history. Size it from the array's
   cold-weather open-circuit voltage and maximum current, then select the
   correct AGM charging profile.

4. Add branch monitoring for appliance attribution.

   A Simarine PICO with SCQ25, SCQ25T, or SCQ50 modules measures four DC
   circuits per module. SCQ25 channels support 25 A continuous; SCQ50 channels
   support 50 A continuous. Start with refrigerator, electronics/network,
   lighting/accessories, and pumps. Put intermittent high-current loads on
   correctly rated channels or dedicated shunts. The optional NMEA 2000
   gateway can make measurements available to the marine data network, subject
   to confirming the emitted PGNs with SignalK.

5. Add physical AC shore-input metering.

   A compatible single-phase energy meter or current-transformer meter can
   measure shore voltage, current, real power, power factor, and kWh. Victron
   energy meters integrate through a GX device; the meter, enclosure, and
   installation must be appropriate for the boat's shore service and dry
   location. This removes the ambiguity in the current charging proxy and makes
   shore-versus-solar attribution reliable at the dock. AC work should be
   performed by a qualified marine electrician.

## Decision Metrics

Collect at least two to four representative weeks with some shore-power-off
days before deciding about the panel. Compare:

- Gross solar yield per day and peak panel power.
- Net solar energy reaching the battery.
- Overnight and baseline DC consumption.
- Refrigerator, electronics, lighting, and pump Wh per day.
- Battery SOC at sunset, before sunrise, and after solar recovery.
- Time below the desired AGM SOC floor.
- Days when the controller clips at its rated output.
- Solar yield versus the panel's weight, roof area, shading, wiring losses, and
  maintenance burden.

Keep the panel when its representative daily yield materially offsets the
boat's unattended and cruising loads or prevents damaging battery discharge.
Panel usefulness cannot be judged from peak watts alone.

## Reference Documentation

- Victron SmartSolar MPPT:
  <https://www.victronenergy.com/solar-charge-controllers/smartsolar-mppt-75-10-75-15-100-15-100-20>
- Victron GX connections:
  <https://www.victronenergy.com/media/pg/Cerbo_GX/en/connecting-victron-products.html>
- Victron energy meters:
  <https://www.victronenergy.com/meters-and-sensors/energy-meter>
- Simarine appliance shunts:
  <https://simarine.net/quadro-shunt-modules-scq25-scq25t-and-scq50/>
