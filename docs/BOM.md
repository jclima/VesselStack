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
| NMEA interface | Pi 5-compatible, galvanically isolated SocketCAN adapter | Prefer a marine-oriented isolated interface with a supported Linux overlay/driver. Verify Pi 5 support before purchase. |
| NMEA cabling | One compatible Micro-C/DeviceNet drop cable and T-piece | Connector families differ. Do not assume SeaTalkNG, SimNet, or proprietary connectors mate with Micro-C. |
| Enclosure | Ventilated, splash-resistant enclosure with cable glands | Mount dry, above likely bilge water, away from engine heat and ignition sources. Avoid condensation traps. |
| Network | Ethernet cable preferred; Wi-Fi optional | Use Ethernet for initial setup and stable telemetry where practical. |
| Backup | Separate USB SSD/flash drive | Must mount outside `VESSELSTACK_DATA`; remove or replicate off-boat periodically. |
| Optional AIS | Linux-compatible USB AIS receiver and antenna system | VesselStack supports AIS-catcher only when explicitly enabled. |

## NMEA interface examples

These are compatibility paths, not endorsements or claims of NMEA
certification:

- Hat Labs Sailor Hat v2 plus its supported isolated CAN/NMEA add-on. This can
  combine marine power management and CAN, but the exact stack, jumpers, and
  enclosure must follow Hat Labs' current instructions.
- A Pi 5-compatible isolated MCP2515/SocketCAN HAT such as the Waveshare
  two-channel isolated CAN HAT documented by Hat Labs. Use its isolated NMEA
  channel, select 3.3 V logic as instructed, and normally leave termination off.
- A reputable isolated USB-to-NMEA 2000 gateway exposing SocketCAN on Linux.
  This avoids GPIO/HAT stacking, but confirm driver and galvanic-isolation details.

The MacArthur HAT documentation states that its NMEA 2000 circuit is not
electrically isolated. Treat that as a materially different design choice; this
reference build recommends galvanic isolation between the Pi and vessel bus.

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
- [Hat Labs isolated CAN HAT guide](https://docs.hatlabs.fi/sh-rpi/docs/add-ons/can_hat/)
- [MacArthur HAT NMEA 2000 documentation](https://macarthur-hat-documentation.readthedocs.io/en/latest/nmea2000.html)
- [NMEA 2000 overview](https://www.nmea.org/nmea-2000.html)
