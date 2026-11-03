# VesselStack hardware bill of materials

This is a reference build for a Raspberry Pi 5 connected to an existing NMEA
2000 backbone. Confirm availability, compatibility, cable gender, and current
manufacturer instructions before ordering.

| Item | Baseline | Why / selection notes |
|---|---|---|
| Computer | Raspberry Pi 5, 8 GB (4 GB minimum) | 8 GB leaves headroom for Signal K, Home Assistant, databases, and dashboards. |
| Cooling | Raspberry Pi Active Cooler | The Pi 5 is intended to use active cooling under sustained workloads. |
| Boot media | 32 GB+ high-endurance microSD | Used to install Raspberry Pi OS; keep as recovery media if moving root to SSD. |
| Data disk | 256 GB+ high-endurance USB 3 SSD and UASP enclosure | USB avoids GPIO stacking conflicts with CAN/power HATs. Prefer known Linux/UASP compatibility. |
| Marine power | Isolated/regulating 10–32 V DC to stable Pi 5 supply, sized for Pi plus USB load | Use a dedicated fused branch, short conductors, strain relief, and the manufacturer's fuse recommendation. |
| Bench power | Official Raspberry Pi 27 W USB-C supply | For dry-land commissioning only; it provides the Pi 5's full USB power budget. |
| NMEA interface | Preferred: galvanically isolated SocketCAN interface; field-proven alternative: PiCAN-M (not isolated, see caveats) | Isolation is the safest choice. The PiCAN-M is the maintainer's field-proven board but is not isolated; see the caveats below. Verify Pi 5 support and power before purchase. |
| NMEA cabling | One compatible Micro-C/DeviceNet drop cable and T-piece | Connector families differ. Do not assume SeaTalkNG, SimNet, or proprietary connectors mate with Micro-C. |
| Enclosure | Ventilated, splash-resistant enclosure with cable glands | Mount dry, above likely bilge water, away from engine heat and ignition sources. Avoid condensation traps. |
| Network | Ethernet cable preferred; Wi-Fi optional | Use Ethernet for initial setup and stable telemetry where practical. |
| Backup | Separate USB SSD/flash drive | Must mount outside `VESSELSTACK_DATA`; remove or replicate off-boat periodically. |
| Optional AIS | Linux-compatible USB AIS receiver and antenna system | VesselStack supports AIS-catcher only when explicitly enabled. |
| Optional display (preview) | Waveshare ESP32-S3-Touch-LCD-2.8C, one per display | Shows Signal K values from the Pi over MQTT on a 2.8 inch round touch screen. It is separate from the NMEA interface and runs only with `DISPLAYS_ENABLE=true`; see [Instrument displays](DISPLAYS.md). |

## NMEA interface

These are compatibility paths, not endorsements or claims of NMEA
certification.

Galvanically isolated interfaces are the preferred, safest choice. The
PiCAN-M is the maintainer's field-proven option; it is not isolated and comes
with the caveats below.

### Preferred: isolated interfaces

- USB SocketCAN: a [PEAK PCAN-USB](https://www.peak-system.com/PCAN-USB.199.0.html?L=1),
  whose opto-decoupled version gives galvanic isolation up to 500 V. On
  Linux it is a SocketCAN `can*` interface through the mainline `peak_usb`
  driver, so it works with `SOCKETCAN_ENABLE`. It needs a Micro-C/DeviceNet
  adapter cable or wiring to the NMEA 2000 drop. This avoids HAT stacking.
- USB serial gateway: a [Yacht Devices YDNU-02](https://www.yachtd.com/products/usb_gateway.html)
  has high-voltage galvanic isolation between NMEA 2000 and USB. It appears
  as a USB serial device, not `can0`, so keep `SOCKETCAN_ENABLE=false` and add
  a Signal K serial connection. The Docker Signal K service has no `devices:`
  passthrough and uses host networking, so use Signal K's existing or native
  install mode, or map the device manually.
- Isolated HATs: Hat Labs Sailor Hat v2 with its isolated CAN/NMEA add-on
  (follow Hat Labs' current instructions), or a Pi 5-compatible isolated
  MCP2515 HAT such as the Waveshare two-channel isolated CAN HAT documented by
  Hat Labs (use the isolated NMEA channel, 3.3 V logic, termination normally
  off).

### Field-proven: PiCAN-M (not isolated)

The
[PiCAN-M](https://copperhilltech.com/pican-m-nmea-0183-nmea-2000-hat-for-raspberry-pi-with-smps/)
is an SK Pang Electronics HAT sold by Copperhill. It is not galvanically
isolated. It is the maintainer's field-proven board and has worked very well
on boats. Its NMEA 2000 port is a Micro-C connector, and the CAN channel
appears as `can0` through the mcp2515 overlay (see
[INSTALLATION.md](INSTALLATION.md) section 4).

- Use one Micro-C drop cable and a T-piece. A normal drop does not need a
  terminator, so leave the HAT's terminator option off; see the vendor user
  guide for the terminator setting.
- It also has an NMEA 0183 (RS422) screw terminal and a Qwiic (I2C)
  connector. VesselStack does not configure these; Signal K would.
- Copperhill sells a metal enclosure designed for the PiCAN-M with a
  Raspberry Pi 4. Check that it clears the Pi 5 Active Cooler before buying.

Power depends on the Pi model:

- Pi 4: the [SMPS variant](https://copperhilltech.com/pican-m-nmea-0183-nmea-2000-hat-for-raspberry-pi-with-smps/)
  can power the Pi and HAT from 12 V on the NMEA 2000 connection, within the
  backbone's current budget. Copperhill describes this as typically working
  but at the limit.
- Pi 5 (the reference build): use the
  [variant without SMPS](https://copperhilltech.com/pican-m-nmea-0183-nmea-2000-hat-for-raspberry-pi/),
  which cannot be powered from the NMEA 2000 connection, and power the Pi from
  a dedicated, fused 5 V / 5 A class marine supply. Copperhill rates the SMPS
  variant as not suitable for a Pi 5, which needs about 2.3-2.7 A at 12 V,
  most of a typical 3 A backbone budget. See Copperhill's
  [power supply article](https://copperhilltech.com/blog/raspberry-pi-and-picanm-in-marine-nmea-2000-systems-power-supply-considerations/).
- Never feed the Pi from two sources, for example the SMPS variant plus USB-C.

Caveats:

- Without isolation, the Pi's ground is tied to the NMEA 2000 network ground.
  Differences in ground potential can cause ground loops, CAN errors, or
  damage. Typical causes are the Pi being powered from a different battery or
  supply than the backbone, or a shore-powered monitor, laptop, or USB device
  connected to the Pi.
- Power the Pi from the same DC system and negative bus as the backbone,
  through a fused branch. Avoid extra ground paths while connected to the
  bus; for bench work with mains-powered peripherals, disconnect from the bus.
- Commission on the bench first. Watch `ip -details -statistics link show can0`
  for errors or bus-off.
- Choose an isolated option if you cannot guarantee a single common ground.

### Other connection types

[Hat Labs SH-ESP32](https://docs.hatlabs.fi/sh-esp32/) (12/24 V, can act as
an NMEA 2000 gateway) and [HALMET](https://docs.hatlabs.fi/halmet/)
(sensor/engine interface with a built-in NMEA 2000 interface) reach Signal K
over Wi-Fi/network or join the bus as nodes; add them as a Signal K data
connection. These are not the ESPHome display boards in this repository.
Check each vendor's documentation for its isolation design.

The MacArthur HAT documentation states that its NMEA 2000 circuit is not
electrically isolated. Treat it like the PiCAN-M: isolated interfaces are
preferred, and a non-isolated one works with the caveats above.

## Power and mechanical rules

- Never feed the Pi simultaneously from USB-C and a power HAT unless the
  hardware manufacturer explicitly documents that topology.
- Do not use an M.2 HAT and a CAN/power HAT together until pin use, stacking
  height, cooling, and enclosure clearance are verified. USB SSD is the safer
  reference layout.
- NMEA 2000 backbone power is not automatically an acceptable Pi supply. Use
  only a device designed to power the Pi from that bus.
- Keep the computer a drop device on the backbone. A normal drop does not add a
  terminator; the existing backbone should have exactly two end terminators.
- VesselStack is supplementary monitoring software, not certified navigation,
  collision avoidance, engine control, or a sole alarm system.

## Primary references

- [Raspberry Pi 5 power and cooling documentation](https://www.raspberrypi.com/documentation/hardware/raspberrypi/raspberry-pi-5.html)
- [Raspberry Pi SSD kit documentation](https://www.raspberrypi.com/documentation/accessories/ssd-kit.html)
- [Hat Labs Sailor Hat documentation](https://docs.hatlabs.fi/sh-rpi/print_page/)
- [Copperhill PiCAN-M with SMPS](https://copperhilltech.com/pican-m-nmea-0183-nmea-2000-hat-for-raspberry-pi-with-smps/)
- [Copperhill PiCAN-M without SMPS](https://copperhilltech.com/pican-m-nmea-0183-nmea-2000-hat-for-raspberry-pi/)
- [Copperhill PiCAN-M power supply considerations](https://copperhilltech.com/blog/raspberry-pi-and-picanm-in-marine-nmea-2000-systems-power-supply-considerations/)
- [PiCAN-M user guide (PDF)](https://copperhilltech.com/content/pican-m_UGB_30.pdf)
- [PEAK PCAN-USB](https://www.peak-system.com/PCAN-USB.199.0.html?L=1)
- [Yacht Devices YDNU-02 USB gateway](https://www.yachtd.com/products/usb_gateway.html)
- [Hat Labs SH-ESP32](https://docs.hatlabs.fi/sh-esp32/)
- [Hat Labs HALMET](https://docs.hatlabs.fi/halmet/)
- [Hat Labs isolated CAN HAT guide](https://docs.hatlabs.fi/sh-rpi/docs/add-ons/can_hat/)
- [MacArthur HAT NMEA 2000 documentation](https://macarthur-hat-documentation.readthedocs.io/en/latest/nmea2000.html)
- [NMEA 2000 overview](https://www.nmea.org/nmea-2000.html)
