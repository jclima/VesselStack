# Third-Party Software

VesselStack orchestrates and integrates third-party software but does not
relicense those projects. Each component remains subject to its own license.

Key runtime components include Home Assistant, SignalK, InfluxDB, Grafana,
Prometheus, Eclipse Mosquitto, Heimdall, Docker, Python, sqlite-vec, and, when
the opt-in displays bridge is enabled, paho-mqtt (Eclipse Public License 2.0 /
Eclipse Distribution License 1.0) and websockets (BSD-3-Clause). Image and Python
dependency versions are declared in `templates/compose.yml`,
`boat-chat/requirements.txt`, and `displays/requirements.txt`.

Before distributing a release, review the licenses and notices shipped by the
exact dependency versions and container images. Preserve all upstream license,
copyright, and attribution notices.

VesselStack's Apache-2.0 license applies only to original VesselStack material;
it does not override or replace any upstream license.
