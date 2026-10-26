#!/usr/bin/env python3
"""Loopback-only first-run configuration wizard for VesselStack."""

import argparse, contextlib, hmac, ipaddress, json, os, platform, re, secrets, shlex, shutil, subprocess, sys, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIZARD = ROOT / "wizard"
OUTPUT = ROOT / "vesselstack.env"
ALLOWED = re.compile(r"^[A-Za-z0-9 _.,:/@+()-]*$")

DEFAULTS = {
 "BOAT_NAME":"My Boat","BOAT_TYPE":"Motor vessel","BOAT_MMSI":"","BOAT_CALLSIGN":"","BOAT_LOA_M":"","BOAT_BEAM_M":"","BOAT_TIMEZONE":"UTC","BOAT_UNITS":"metric",
 "VESSELSTACK_USER":"boat","VESSELSTACK_UID":"1000","VESSELSTACK_GID":"1000","VESSELSTACK_ROOT":"/opt/vesselstack","VESSELSTACK_DATA":"/opt/vesselstack-data","VESSELSTACK_BACKUP":"/media/vesselstack-backup/backups",
 "SIGNALK_MODE":"docker","SIGNALK_VERSION":"v2.27.0","SIGNALK_URL":"http://127.0.0.1:3000","SOCKETCAN_ENABLE":"false","VESSELSTACK_CAN_CREATE_VCAN":"false","SOCKETCAN_INTERFACE":"can0","SOCKETCAN_BITRATE":"250000",
 "AIS_ENABLE":"false","AIS_IMAGE":"ghcr.io/jvde-github/ais-catcher:v0.70","AIS_DEVICE":"/dev/bus/usb","AIS_CATCHER_ARGS":"-q -N 8100 -S 5011","AIS_WEB_PORT":"8100","AIS_TCP_PORT":"5011",
 "HOME_ASSISTANT_URL":"http://127.0.0.1:8123","HOME_ASSISTANT_TOKEN":"","INFLUXDB_URL":"http://127.0.0.1:8086","INFLUXDB_PORT":"8086","INFLUXDB_CONTAINER_NAME":"vesselstack-influxdb","INFLUXDB_ORG":"vesselstack","INFLUXDB_USERNAME":"boatadmin",
 "INFLUXDB_RAW_BUCKET":"signalk","INFLUXDB_HISTORY_BUCKET":"signalk_1m","INFLUXDB_HOME_ASSISTANT_BUCKET":"homeassistant","INFLUXDB_AIS_BUCKET":"ais","INFLUXDB_RAW_RETENTION":"","INFLUXDB_PASSWORD":"GENERATE","INFLUXDB_TOKEN":"GENERATE","GRAFANA_ADMIN_PASSWORD":"GENERATE",
 "MQTT_USERNAME":"homeassistant","MQTT_PASSWORD":"GENERATE","MQTT_PORT":"1883","BOAT_CHAT_PROVIDER":"local","BOAT_CHAT_HOST":"0.0.0.0","BOAT_CHAT_PORT":"8765","BOAT_CHAT_SETTINGS_TOKEN":"GENERATE","BOAT_CHAT_ACCESS_TOKEN":"GENERATE","VESSELSTACK_DATA_BIND_ADDRESS":"127.0.0.1","VESSELSTACK_WEB_BIND_ADDRESS":"0.0.0.0","TELEGRAM_ENABLE":"false","TELEMETRY_INDEXER_ENABLE":"true",
 "GRAFANA_PORT":"43000","HEIMDALL_PORT":"80","HEIMDALL_HTTPS_PORT":"443","PROMETHEUS_PORT":"9090","VESSELSTACK_UNTRUSTED_INTERFACE":"wlan0","VESSELSTACK_FIREWALL_ENABLE":"false","VESSELSTACK_FIREWALL_STRICT":"false","VESSELSTACK_FIREWALL_ALLOW_TCP":"22","VESSELSTACK_FIREWALL_ALLOW_UDP":"","CONTROL_PANEL_HOST":"127.0.0.1","CONTROL_PANEL_PORT":"8780"
}

def compose_available():
    if not shutil.which("docker"): return False
    try:
        return subprocess.run(["docker","compose","version"], timeout=4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired): return False

def system_report():
    # Exclude container/VPN/bridge names: they add noise and can expose local
    # topology in support screenshots. Only installation-relevant links belong
    # in the browser inventory.
    interfaces=sorted(p.name for p in Path("/sys/class/net").glob("*") if p.name.startswith(("eth","en","wlan","wl","can")))
    return {"architecture":platform.machine(),"os":platform.platform(),"docker":bool(shutil.which("docker")),"compose":compose_available(),"interfaces":interfaces,"can0":"can0" in interfaces,"memory_gib":round(os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")/2**30,1),"output":str(OUTPUT)}

def validate(values):
    unknown=set(values)-set(DEFAULTS)
    if unknown: raise ValueError("Unknown settings: "+", ".join(sorted(unknown)))
    result=DEFAULTS.copy()
    for key,value in values.items():
        if not isinstance(value,str) or "\n" in value or "\r" in value or not ALLOWED.fullmatch(value): raise ValueError(f"Invalid value for {key}")
        result[key]=value
    if result["SIGNALK_MODE"] not in {"existing","docker","native"}: raise ValueError("Invalid SignalK mode")
    for key in ("SOCKETCAN_ENABLE","VESSELSTACK_CAN_CREATE_VCAN","AIS_ENABLE","TELEGRAM_ENABLE","TELEMETRY_INDEXER_ENABLE","VESSELSTACK_FIREWALL_ENABLE","VESSELSTACK_FIREWALL_STRICT"):
        if result[key] not in {"true","false"}: raise ValueError(f"Invalid boolean for {key}")
    for key in ("VESSELSTACK_FIREWALL_ALLOW_TCP","VESSELSTACK_FIREWALL_ALLOW_UDP"):
        if result[key] and not re.fullmatch(r"[0-9]{1,5}(:[0-9]{1,5})?(,[0-9]{1,5}(:[0-9]{1,5})?)*",result[key]): raise ValueError(f"Invalid port list for {key}")
    if result["INFLUXDB_RAW_RETENTION"] and not re.fullmatch(r"0|[1-9][0-9]{0,4}[hdw]",result["INFLUXDB_RAW_RETENTION"]): raise ValueError("Invalid raw retention: use 0, or a duration such as 720h, 30d or 8w")
    return result

def load_env(path):
    """Parse a KEY=VALUE env file (shell quoting only; nothing is expanded or executed)."""
    values={}
    with open(path,encoding="utf-8") as handle:
        for number,raw in enumerate(handle,1):
            line=raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"): continue
            match=re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)",line)
            if not match: raise ValueError(f"line {number}: expected KEY=VALUE")
            key,raw_value=match.groups()
            try: tokens=shlex.split(raw_value,posix=True)
            except ValueError as exc: raise ValueError(f"line {number}: {exc}") from exc
            if len(tokens)>1: raise ValueError(f"line {number}: {key} must be a single value")
            values[key]=tokens[0] if tokens else ""
    return values

def render(values):
    return "# Generated by the VesselStack installation wizard.\n"+"".join(f"{key}={shlex.quote(value)}\n" for key,value in validate(values).items())

def save_env(path,text):
    """Write the env file 0600 from the start (never briefly world-readable), never through a symlink."""
    temp=path.with_name(path.name+".tmp")
    try: os.unlink(temp)
    except FileNotFoundError: pass
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"w") as handle: handle.write(text)
    os.replace(temp,path)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

class Handler(SimpleHTTPRequestHandler):
    # Per-run secret; set by main() (or tests). While unset, every API call is refused.
    token = None  # never embedded in served files: delivered in the URL fragment (see main)
    # When true (--exit-after-save) the server stops after the first successful save,
    # so a calling installer knows the wizard is finished.
    exit_after_save = False
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(WIZARD),**kwargs)
    def log_message(self,fmt,*args): pass
    def end_headers(self):
        self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'"); super().end_headers()
    def reply(self,status,payload):
        body=json.dumps(payload).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def allowed_origins(self):
        port=self.server.server_address[1]
        return {f"{host}:{port}" for host in LOCAL_HOSTS}
    def host_ok(self):
        # DNS-rebinding defence: only the loopback names on the bound port are served.
        if self.headers.get("Host","").lower() not in self.allowed_origins(): return False
        origin=self.headers.get("Origin")
        if origin is not None and origin.lower() not in {"http://"+h for h in self.allowed_origins()}: return False
        return True
    def token_ok(self):
        expected=type(self).token; given=self.headers.get("X-Wizard-Token","")
        return bool(expected) and hmac.compare_digest(expected.encode(),given.encode())
    def do_GET(self):
        if not self.host_ok(): return self.reply(403,{"error":"Forbidden host or origin"})
        route=self.path.split("?",1)[0]
        if route=="/api/system":
            if not self.token_ok(): return self.reply(403,{"error":"Missing or invalid wizard token"})
            return self.reply(200,system_report())
        return super().do_GET()
    def do_POST(self):
        if not self.host_ok(): return self.reply(403,{"error":"Forbidden host or origin"})
        if self.path!="/api/save": return self.reply(404,{"error":"Not found"})
        if not self.token_ok(): return self.reply(403,{"error":"Missing or invalid wizard token"})
        if self.headers.get_content_type()!="application/json": return self.reply(415,{"error":"Content-Type must be application/json"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if size>65536: raise ValueError("Request too large")
            values=json.loads(self.rfile.read(size)).get("values",{})
            save_env(OUTPUT,render(values))
            self.reply(200,{"saved":str(OUTPUT),"next":f"sudo ./install.sh --config {OUTPUT.name} --dry-run"})
            if type(self).exit_after_save: threading.Thread(target=self.server.shutdown,daemon=True).start()
        except (ValueError,json.JSONDecodeError,OSError,AttributeError) as exc: self.reply(400,{"error":str(exc)})

# ---------------------------------------------------------------------------
# Terminal wizard (--cli). No network surface: it only reads answers and writes
# the env file. It reuses DEFAULTS, validate(), render() and ALLOWED above, so the
# rules are the same as in the web wizard; FIELDS only adds prompts, help text and a
# few stricter checks that install.sh enforces anyway (so mistakes show up now, not later).
# ---------------------------------------------------------------------------

TIMEZONE_FILE = Path("/etc/timezone")
LOCALTIME_LINK = Path("/etc/localtime")
YES_NO = {"y":"true","yes":"true","true":"true","1":"true","n":"false","no":"false","false":"false","0":"false"}
BACK = object()

class Aborted(Exception):
    """The answers ended (EOF) before the configuration was saved."""

def _re(regex,message):
    compiled=re.compile(regex)
    return lambda value: None if compiled.fullmatch(value) else message

def _number(low,high,what):
    def check(value):
        if not re.fullmatch(r"[0-9]{1,7}",value) or not low<=int(value)<=high: return f"{what}: enter a whole number from {low} to {high}."
    return check

def _ipv4(value):
    try: ipaddress.IPv4Address(value)
    except ValueError: return "Enter an IPv4 address such as 127.0.0.1 (this device only) or 0.0.0.0 (all networks)."

def _any_ip(value):
    try: ipaddress.ip_address(value)
    except ValueError: return "Enter an IP address such as 127.0.0.1."

def _timezone(value):
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}",value): return "Use a name such as Europe/Oslo or UTC."
    try:
        import zoneinfo
        known=zoneinfo.available_timezones()
    except (ImportError,OSError): known=set()
    if known and value not in known and value!="UTC": return "Unknown timezone. Use a name such as Europe/Oslo, America/New_York or UTC."

def _install_path(value):
    if not value.startswith("/") or ".." in value.split("/"): return "Enter an absolute path such as /opt/vesselstack."
    if " " in value: return "The path cannot contain spaces."
    if value.startswith(("/home/","/root/")) or value in ("/home","/root"): return "Not under /home or /root (the services run with ProtectHome)."

def _backup_path(value):
    if not value.startswith("/") or ".." in value.split("/"): return "Enter an absolute path such as /media/vesselstack-backup/backups."

def _account(value):
    if value=="root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}",value): return "Use a plain lower-case account name (not root), for example boat."

PORT=_number(1,65535,"Port")
NAME=_re(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}","Letters, digits, dot, underscore and hyphen only (no spaces).")
URL=_re(r"https?://[A-Za-z0-9_.-]+(:[0-9]{1,5})?(/[A-Za-z0-9_./-]*)?","Enter an address such as http://127.0.0.1:3000.")
SECRET=_re(r"\S{12,128}","At least 12 characters, no spaces. (Press Enter to have one generated instead.)")
TOKEN=_re(r"\S{1,2048}","No spaces.")
BOATCHAT_PROVIDERS=[("local","built into this Pi, no cloud account needed (recommended)"),("openai","OpenAI (API key added later in Boat Chat Settings)"),("vercel","Vercel AI Gateway"),("bedrock","Amazon Bedrock"),("google","Google Gemini"),("ollama","an Ollama server you run"),("openai_compatible","any OpenAI-compatible endpoint")]

def F(key,label,help,kind="text",**extra):
    spec={"key":key,"label":label,"help":help,"kind":kind,"advanced":False,"optional":False,"required":False}
    spec.update(extra); return spec

def _can_hint(s):
    if s.facts.get("can0"): return "Found a can0 interface on this computer. Even so, enable it only after the bench checks in docs/INSTALLATION.md."
    return "No can0 interface found yet (normal before the CAN HAT overlay is installed). You can enable it later."

def _vcan_after(s):
    v=s.values
    if v["VESSELSTACK_CAN_CREATE_VCAN"]=="true" and v["SOCKETCAN_INTERFACE"]=="can0": v["SOCKETCAN_INTERFACE"]="vcan0"
    if v["VESSELSTACK_CAN_CREATE_VCAN"]=="false" and v["SOCKETCAN_INTERFACE"]=="vcan0": v["SOCKETCAN_INTERFACE"]="can0"

def _fw_hint(s):
    names=[n for n in s.facts.get("interfaces",[]) if n.startswith(("wl",))]
    return ("Wi-Fi interface found: "+", ".join(names)+".") if names else "No Wi-Fi interface found; the default name is wlan0."

# "Who can reach the dashboards?" is a pseudo-setting: it expands at once into the real bind
# settings and is never written to the env file (so it cannot drift from them). The control
# panel (CONTROL_PANEL_HOST) is deliberately in NO preset: it is a root-level configuration editor.
LAN_PRESETS={
 "local":{"VESSELSTACK_WEB_BIND_ADDRESS":"127.0.0.1","VESSELSTACK_DATA_BIND_ADDRESS":"127.0.0.1","BOAT_CHAT_HOST":"127.0.0.1"},
 "dashboards":{"VESSELSTACK_WEB_BIND_ADDRESS":"0.0.0.0","VESSELSTACK_DATA_BIND_ADDRESS":"127.0.0.1","BOAT_CHAT_HOST":"0.0.0.0"},
 "all":{"VESSELSTACK_WEB_BIND_ADDRESS":"0.0.0.0","VESSELSTACK_DATA_BIND_ADDRESS":"0.0.0.0","BOAT_CHAT_HOST":"0.0.0.0"},
}
LAN_DEFAULT="dashboards"

def lan_preset_of(values):
    for name,keys in LAN_PRESETS.items():
        if all(values.get(key)==value for key,value in keys.items()): return name
    return "custom"

def apply_lan_preset(values,name): values.update(LAN_PRESETS[name])

def firewall_note(values):
    """Plain statement of what an enabled firewall does to LAN access (empty when it is off)."""
    if values.get("VESSELSTACK_FIREWALL_ENABLE")!="true": return ""
    iface=values.get("VESSELSTACK_UNTRUSTED_INTERFACE","wlan0")
    if values.get("VESSELSTACK_FIREWALL_STRICT")=="true":
        what="everything except the allow-listed ports ("+(values.get("VESSELSTACK_FIREWALL_ALLOW_TCP") or "none")+")"
    else:
        what="Grafana, InfluxDB, Prometheus and MQTT"
    return (f"The firewall is ON and treats {iface} as untrusted: devices that reach this Pi over {iface} (usually Wi-Fi) are blocked from {what}. "
            f"If this Pi is on your network over {iface}, those services will NOT be reachable from other devices: pick another untrusted interface, or turn the firewall off.")

def lan_notes(values):
    """Warnings that go with the chosen access level (never secrets)."""
    preset=lan_preset_of(values); notes=[]
    if preset!="local" and firewall_note(values): notes.append(firewall_note(values))
    if preset in ("all","custom") and values.get("VESSELSTACK_DATA_BIND_ADDRESS")!="127.0.0.1":
        notes.append("InfluxDB (API token), MQTT (password) and Prometheus (NO login at all) are open to every device on the network. Turn on the firewall (and keep Ethernet or a VPN as your way in), or use the 'dashboards' option.")
    if preset=="local":
        notes.append("SignalK (3000) and Home Assistant (8123) run in host-network containers and still listen on every interface; to limit them use the firewall's strict mode or Home Assistant's own http server_host setting. Check with: sudo vesselstackctl urls")
    return notes

def _lan_hint(s):
    lines=["Grafana, Heimdall, Boat Chat (needs its access token), Home Assistant and SignalK; the control panel always stays on this Pi only (SSH tunnel)."]
    note=firewall_note(s.values)
    if note: lines.append("WARNING: "+note)
    return "\n    ".join(lines)

def _secret(key,label,what):
    return F(key,label,f"{what} Press Enter to have a strong random value generated during installation (recommended). It is never shown on screen or stored anywhere but the root-only configuration.",
        "secret",advanced=True,check=SECRET,generate=True)

SECTIONS=[
 {"title":"Your boat","intro":"Names shown in dashboards and Boat Chat. Everything stays on this Raspberry Pi.","fields":[
  F("BOAT_NAME","Boat name","Shown in dashboards and Boat Chat. Letters, digits, spaces, dot, underscore, hyphen.",required=True,check=_re(r"[A-Za-z0-9_. -]{1,60}","Letters, digits, spaces, dot, underscore and hyphen only (up to 60).")),
  F("BOAT_TYPE","Boat type","For example Motor vessel, Sailing yacht, Catamaran. Free text.",required=True),
  F("BOAT_UNITS","Units","How speeds, depths and distances are shown.","choice",choices=[("metric","metres, km/h, degrees C"),("us_customary","feet, mph, degrees F"),("nautical","knots, nautical miles, metres/fathoms")]),
  F("BOAT_TIMEZONE","Timezone","Used for timestamps, daily summaries and charts. Detected from this computer when possible.",required=True,check=_timezone,hint=lambda s:"Detected from this computer: "+s.facts["timezone"] if s.facts.get("timezone") else None),
  F("@details","Add optional vessel details (MMSI, call sign, length, beam)?","All of these are optional and private: they are stored only in the root-only configuration on this Pi, never sent anywhere, and only used to label your own data (for example to recognise your own vessel in AIS). Skip them if unsure.","gate",
    default=lambda s:any(s.values[k] for k in ("BOAT_MMSI","BOAT_CALLSIGN","BOAT_LOA_M","BOAT_BEAM_M"))),
  F("BOAT_MMSI","MMSI (9 digits)","Your Maritime Mobile Service Identity. Optional and private. Type - to clear.",optional=True,private=True,check=_re(r"[0-9]{9}","An MMSI has exactly 9 digits."),when=lambda s:s.flags.get("@details")),
  F("BOAT_CALLSIGN","Call sign","Your radio call sign. Optional and private. Type - to clear.",optional=True,private=True,check=_re(r"[A-Za-z0-9]{3,10}","Letters and digits only, 3 to 10 characters."),when=lambda s:s.flags.get("@details")),
  F("BOAT_LOA_M","Length overall (metres)","Optional, for example 12.5. Type - to clear.",optional=True,check=_re(r"[0-9]{1,3}(\.[0-9]{1,2})?","A number such as 12 or 12.5."),when=lambda s:s.flags.get("@details")),
  F("BOAT_BEAM_M","Beam (metres)","Optional, for example 4.2. Type - to clear.",optional=True,check=_re(r"[0-9]{1,2}(\.[0-9]{1,2})?","A number such as 4 or 4.2."),when=lambda s:s.flags.get("@details")),
  F("VESSELSTACK_USER","Service account","The Linux account the services run as. It is created if missing (no login shell).",advanced=True,required=True,check=_account),
  F("VESSELSTACK_UID","Service account user id","Numeric user id for container volumes.",advanced=True,required=True,check=_number(1,65534,"User id")),
  F("VESSELSTACK_GID","Service account group id","Numeric group id for container volumes.",advanced=True,required=True,check=_number(1,65534,"Group id")),
  F("VESSELSTACK_ROOT","Application folder","Where the application is installed. Not under /home or /root, no spaces.",advanced=True,required=True,check=_install_path),
  F("VESSELSTACK_DATA","Data folder","Where databases and logs live. Put it on the SSD. Not under /home or /root, no spaces.",advanced=True,required=True,check=_install_path),
  F("VESSELSTACK_BACKUP","Backup folder","Where backups are written; removable media is best.",advanced=True,required=True,check=_backup_path)]},
 {"title":"Signal K (the boat data hub)","intro":"Signal K collects data from your instruments and serves it to everything else.","fields":[
  F("SIGNALK_MODE","How should Signal K run?","docker: VesselStack installs and manages Signal K in a container (recommended). existing: you already run Signal K elsewhere. native: installed directly on the Pi (not in a container).","choice",
    choices=[("docker","VesselStack runs Signal K for you (recommended)"),("existing","use a Signal K server you already have"),("native","install Signal K directly on this Pi")]),
  F("SIGNALK_URL","Address of your Signal K server","For example http://127.0.0.1:3000 (or another computer on the boat network).",required=True,check=URL,when=lambda s:s.values["SIGNALK_MODE"]=="existing"),
  F("SIGNALK_VERSION","Signal K version","Release tag of the Signal K container image.",advanced=True,required=True,check=_re(r"v?[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?","A version such as v2.27.0."))]},
 {"title":"NMEA 2000 (CAN bus)","intro":"Reads your boat's NMEA 2000 network through an isolated CAN interface. Skip this until the hardware is installed and bench-tested; you can enable it later by running the installer again.","fields":[
  F("SOCKETCAN_ENABLE","Enable SocketCAN / NMEA 2000?","Needs an isolated CAN interface, a powered and terminated backbone, and the can0 device present. Leave it off for a first dry run or a desk install.","bool",hint=_can_hint),
  F("VESSELSTACK_CAN_CREATE_VCAN","Home-lab mode: create a virtual CAN interface (vcan0, no real bus)?","For replaying recorded data at a desk. It creates a virtual vcan interface at boot. Answer no on a boat.","bool",when=lambda s:s.values["SOCKETCAN_ENABLE"]=="true",after=_vcan_after),
  F("SOCKETCAN_INTERFACE","CAN interface name","Usually can0 (vcan0 in home-lab mode).",required=True,check=_re(r"[A-Za-z][A-Za-z0-9_.-]{0,14}","Letters, digits, dot, underscore, hyphen; up to 15 characters."),when=lambda s:s.values["SOCKETCAN_ENABLE"]=="true"),
  F("SOCKETCAN_BITRATE","CAN bit rate","NMEA 2000 always uses 250000. Change it only for a non-marine CAN network.",required=True,check=_number(10000,1000000,"Bit rate"),when=lambda s:s.values["SOCKETCAN_ENABLE"]=="true")]},
 {"title":"AIS receiver","intro":"Optional: a USB AIS receiver shows nearby ships.","fields":[
  F("AIS_ENABLE","Do you have a USB AIS receiver to enable?","Needs a connected receiver under /dev/bus/usb. Say no if unsure.","bool"),
  F("AIS_DEVICE","AIS device","USB bus or receiver device path passed to AIS-catcher. The default works for most single receivers.",required=True,check=_re(r"/dev/[A-Za-z0-9_./-]+","A device path such as /dev/bus/usb."),when=lambda s:s.values["AIS_ENABLE"]=="true"),
  F("AIS_IMAGE","AIS-catcher container image","Container image for the AIS decoder.",advanced=True,required=True,check=_re(r"[A-Za-z0-9_./:-]+","A container image reference.")),
  F("AIS_CATCHER_ARGS","AIS-catcher arguments","Command-line options for the decoder; must match the two ports below.",advanced=True,required=True),
  F("AIS_WEB_PORT","AIS web port","Port of the decoder's status page.",advanced=True,required=True,check=PORT),
  F("AIS_TCP_PORT","AIS TCP port","Port Signal K reads decoded AIS from.",advanced=True,required=True,check=PORT)]},
 {"title":"Home Assistant, Boat Chat and Telegram","intro":"Optional connections. None of them is needed to get started.","fields":[
  F("HOME_ASSISTANT_URL","Home Assistant address","Where your Home Assistant runs; the default is the one VesselStack installs on this Pi.",check=URL,optional=True),
  F("HOME_ASSISTANT_TOKEN","Home Assistant access token (optional)","A long-lived access token from your Home Assistant profile, so Boat Chat can read it. Optional: press Enter to skip, you can add it later in Boat Chat Settings. Input is hidden and never printed.","secret",optional=True,check=TOKEN,generate=False),
  F("BOAT_CHAT_PROVIDER","Boat Chat language model","Boat Chat answers questions about your boat's data. local needs no account and sends nothing out. Other providers need an API key, which you add later in Boat Chat Settings.","choice",choices=BOATCHAT_PROVIDERS),
  F("TELEGRAM_ENABLE","Run the Telegram bot worker?","Lets you ask Boat Chat questions from Telegram. It also needs a bot token and allowed chat ids in Boat Chat Settings. Say no if unsure.","bool"),
  F("TELEMETRY_INDEXER_ENABLE","Refresh Boat Chat's data memory every five minutes?","Recommended. Turn it off only to save a little CPU.","bool",advanced=True),
  F("BOAT_CHAT_HOST","Boat Chat listens on","IP address Boat Chat binds to. 0.0.0.0 = every network (needs its access token); 127.0.0.1 = this device only.",advanced=True,required=True,check=_any_ip),
  F("BOAT_CHAT_PORT","Boat Chat port","TCP port of Boat Chat.",advanced=True,required=True,check=PORT),
  _secret("BOAT_CHAT_ACCESS_TOKEN","Boat Chat access token","Needed to open Boat Chat from another device."),
  _secret("BOAT_CHAT_SETTINGS_TOKEN","Boat Chat settings token","Protects the Boat Chat settings page.")]},
 {"title":"Network protection (firewall)","intro":"Optionally block administration ports on an untrusted network such as marina Wi-Fi.","fields":[
  F("VESSELSTACK_FIREWALL_ENABLE","Enable the VesselStack firewall rules?","Blocks administration ports on the untrusted interface. Turn it on only after confirming the interface name and that you have another way in (for example Ethernet or a VPN).","bool",hint=_fw_hint),
  F("VESSELSTACK_UNTRUSTED_INTERFACE","Untrusted network interface","The interface to protect, usually the Wi-Fi adapter.",required=True,check=_re(r"[A-Za-z][A-Za-z0-9_.-]{0,14}","Letters, digits, dot, underscore, hyphen; up to 15 characters."),when=lambda s:s.values["VESSELSTACK_FIREWALL_ENABLE"]=="true"),
  F("VESSELSTACK_FIREWALL_STRICT","Strict mode (drop everything except the allow list)?","Strict mode only lets the ports below through on the untrusted interface. Say no unless you know you need it.","bool",when=lambda s:s.values["VESSELSTACK_FIREWALL_ENABLE"]=="true"),
  F("VESSELSTACK_FIREWALL_ALLOW_TCP","TCP ports to keep open","Comma-separated ports or ranges, for example 22 or 22,5000:5100. Keep 22 so you can still log in over SSH. Type - for none.",optional=True,when=lambda s:s.values["VESSELSTACK_FIREWALL_ENABLE"]=="true"),
  F("VESSELSTACK_FIREWALL_ALLOW_UDP","UDP ports to keep open","Comma-separated ports or ranges. Usually none. Type - for none.",optional=True,when=lambda s:s.values["VESSELSTACK_FIREWALL_ENABLE"]=="true")]},
 {"title":"Data retention","intro":"How long InfluxDB keeps the full-detail (raw) readings. One-minute averages are kept much longer.","fields":[
  F("INFLUXDB_RAW_RETENTION","Keep raw data for","Enter a duration such as 30d, 8w or 720h, 0 to keep it forever, or press Enter for the installer default: 30d on a new install and no change to an existing database. Raw data is large: 30 days suits a small SSD.",optional=True)]},
 {"title":"Who can reach what","intro":"Which devices on your network can open the dashboards. The default shares the dashboards and keeps the databases private to this Pi. You can change this later with: sudo vesselstackctl lan","fields":[
  F("LAN_ACCESS","Who should be able to reach the dashboards?","local: only this Pi (reach everything through an SSH tunnel). dashboards (recommended): Grafana, Heimdall, Boat Chat, Home Assistant and SignalK on your boat network, while InfluxDB, Prometheus and MQTT stay on this Pi. all: also InfluxDB, Prometheus (it has no login) and MQTT on the network; turn the firewall on if you choose this. The control panel stays on this Pi in every case.","choice",
    choices=[("local","only this Pi (SSH tunnel for everything)"),("dashboards","dashboards on the boat network (recommended)"),("all","dashboards plus MQTT, InfluxDB, Prometheus on the network")],hint=_lan_hint),
  F("VESSELSTACK_WEB_BIND_ADDRESS","Dashboards (Grafana, Heimdall) listen on","0.0.0.0 shares them with every device on the boat network (default). 127.0.0.1 keeps them on this Pi only. (Set by the question above.)",advanced=True,required=True,check=_ipv4),
  F("VESSELSTACK_DATA_BIND_ADDRESS","Databases (InfluxDB, Prometheus, MQTT) listen on","127.0.0.1 keeps them private to this Pi (default and recommended). Change it only to a trusted LAN or VPN address. (Set by the question above.)",advanced=True,required=True,check=_ipv4),
  F("CONTROL_PANEL_HOST","Control panel listens on","Keep 127.0.0.1 and use an SSH tunnel to reach it (recommended).",advanced=True,required=True,check=_any_ip),
  F("CONTROL_PANEL_PORT","Control panel port","TCP port of the control panel.",advanced=True,required=True,check=PORT),
  F("GRAFANA_PORT","Grafana port","TCP port of Grafana.",advanced=True,required=True,check=PORT),
  F("HEIMDALL_PORT","Heimdall web port","TCP port of the Heimdall start page (HTTP).",advanced=True,required=True,check=PORT),
  F("HEIMDALL_HTTPS_PORT","Heimdall HTTPS port","TCP port of the Heimdall start page (HTTPS).",advanced=True,required=True,check=PORT),
  F("PROMETHEUS_PORT","Prometheus port","TCP port of Prometheus.",advanced=True,required=True,check=PORT),
  F("MQTT_PORT","MQTT port","TCP port of the MQTT broker.",advanced=True,required=True,check=PORT),
  F("MQTT_USERNAME","MQTT user name","User name Home Assistant uses on the MQTT broker.",advanced=True,required=True,check=NAME),
  _secret("MQTT_PASSWORD","MQTT password","The MQTT broker password.")]},
 {"title":"Time-series database","intro":"InfluxDB stores the history. The defaults are fine for nearly everyone.","fields":[
  F("INFLUXDB_URL","InfluxDB address","How other services reach InfluxDB.",advanced=True,required=True,check=URL),
  F("INFLUXDB_PORT","InfluxDB port","TCP port of InfluxDB.",advanced=True,required=True,check=PORT),
  F("INFLUXDB_CONTAINER_NAME","InfluxDB container name","Name of the InfluxDB container.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_ORG","InfluxDB organisation","InfluxDB organisation name.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_USERNAME","InfluxDB administrator","InfluxDB administrator user name.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_RAW_BUCKET","Raw data bucket","Bucket for full-detail readings.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_HISTORY_BUCKET","One-minute history bucket","Bucket for one-minute averages.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_HOME_ASSISTANT_BUCKET","Home Assistant bucket","Bucket for Home Assistant data.",advanced=True,required=True,check=NAME),
  F("INFLUXDB_AIS_BUCKET","AIS bucket","Bucket for AIS data.",advanced=True,required=True,check=NAME),
  _secret("INFLUXDB_PASSWORD","InfluxDB administrator password","The InfluxDB administrator password."),
  _secret("INFLUXDB_TOKEN","InfluxDB API token","The token services use to read and write InfluxDB."),
  _secret("GRAFANA_ADMIN_PASSWORD","Grafana administrator password","The password of the Grafana admin account.")]},
]
FIELDS=[f for section in SECTIONS for f in section["fields"] if not f["key"].startswith("@")]
FIELD_INDEX={f["key"]:f for f in FIELDS}
INTERACTIVE_ONLY_KEYS=set()  # every DEFAULTS key has a prompt; tests assert this

class Session:
    """Everything a prompt needs: the settings so far, the yes/no gates, the console and system facts."""
    def __init__(self,console,values,facts):
        self.console=console; self.values=values; self.facts=facts; self.flags={}

class Console:
    def __init__(self,read_line,read_secret,write,quiet=None):
        self.read_line=read_line; self.read_secret=read_secret; self.write=write; self.quiet=quiet
    def say(self,text=""): self.write(text+"\n")
    def ask(self,prompt,secret=False):
        if secret and self.quiet:
            # Echo goes off BEFORE the prompt is shown, so even typed-ahead or pasted text is never displayed.
            with self.quiet():
                self.write(prompt); line=self.read_secret()
        else:
            self.write(prompt); line=(self.read_secret if secret else self.read_line)()
        if line is None: raise Aborted()
        return line.strip()

class FdInput:
    """Reads answers one byte at a time from a file descriptor.

    Unbuffered on purpose: the bootstrap hands over the SAME descriptor (/dev/tty or the
    scripted-answers file) that it keeps reading its own questions from afterwards, so a
    buffered reader would swallow the answers meant for the next question."""
    def __init__(self,fd,write): self.fd=fd; self.write=write; self.tty=os.isatty(fd)
    def line(self):
        data=bytearray()
        while True:
            try: byte=os.read(self.fd,1)
            except InterruptedError: continue
            if not byte:
                if not data: return None
                break
            if byte==b"\n": break
            data+=byte
        if not self.tty: self.write("\n")  # keep scripted transcripts readable; nothing typed is repeated
        return data.decode("utf-8","replace").rstrip("\r")
    def secret(self): return self.line()
    @contextlib.contextmanager
    def quiet(self):
        """Terminal echo off for the duration (restored even on Ctrl-C); a no-op when the input is not a terminal."""
        if not self.tty:
            yield; return
        import termios
        saved=termios.tcgetattr(self.fd); hidden=termios.tcgetattr(self.fd); hidden[3]&=~termios.ECHO
        try:
            termios.tcsetattr(self.fd,termios.TCSADRAIN,hidden)
            yield
        finally:
            termios.tcsetattr(self.fd,termios.TCSADRAIN,saved); self.write("\n")

def detect_timezone():
    pattern=re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}")
    candidates=[]
    try: candidates.append(TIMEZONE_FILE.read_text().strip())
    except OSError: pass
    try:
        target=os.path.realpath(LOCALTIME_LINK)
        if "/zoneinfo/" in target: candidates.append(target.split("/zoneinfo/",1)[1])
    except OSError: pass
    for name in candidates:
        if name and pattern.fullmatch(name): return name
    return "UTC"

def detect_facts():
    """System facts that inform defaults and hints (never a reason to enable hardware)."""
    facts=system_report(); facts["timezone"]=detect_timezone()
    facts["wifi"]=next((n for n in facts["interfaces"] if n.startswith(("wl",))),"")
    return facts

def initial_values(facts):
    values=DEFAULTS.copy(); values["BOAT_TIMEZONE"]=facts.get("timezone") or "UTC"
    if facts.get("wifi") and "wlan0" not in facts.get("interfaces",[]): values["VESSELSTACK_UNTRUSTED_INTERFACE"]=facts["wifi"]
    return values

def normalize(field,raw,values):
    """Turn typed text into a value for this field. Returns (value, None) or (None, message). Never echoes the input."""
    key=field["key"]; kind=field["kind"]; raw=raw.strip()
    if kind=="bool":
        value=YES_NO.get(raw.lower())
        if value is None: return None,"Please answer y or n."
    elif kind=="choice":
        names=[name for name,_ in field["choices"]]
        if raw.isdigit() and 1<=int(raw)<=len(names): value=names[int(raw)-1]
        elif raw.lower() in names: value=raw.lower()
        else: return None,"Choose a number from 1 to "+str(len(names))+" or one of: "+", ".join(names)+"."
    else:
        value=raw
        if kind=="secret" and field.get("generate") and raw.lower()=="generate": value="GENERATE"
        elif key=="INFLUXDB_RAW_RETENTION" and raw.lower()=="default": value=""
    if kind in ("bool","choice") or value=="GENERATE" and field.get("generate"): pass
    elif value=="":
        if field["required"]: return None,"This setting cannot be empty."
    else:
        if not ALLOWED.fullmatch(value): return None,"Use only letters, digits, spaces and . , : / @ + ( ) - _"
        if key.startswith("VESSELSTACK_FIREWALL_ALLOW_") and not re.fullmatch(r"[0-9]{1,5}(:[0-9]{1,5})?(,[0-9]{1,5}(:[0-9]{1,5})?)*",value): return None,"Ports separated by commas, ranges as LOW:HIGH, for example 22,5000:5100."
        if key=="INFLUXDB_RAW_RETENTION" and not re.fullmatch(r"0|[1-9][0-9]{0,4}[hdw]",value): return None,"Use 0 (forever) or a duration such as 720h, 30d or 8w."
        check=field.get("check")
        if check:
            message=check(value)
            if message: return None,message
    if key=="LAN_ACCESS": return value,None
    try: validate({**values,key:value})
    except ValueError as exc: return None,str(exc)
    return value,None

def apply_settings(values,pairs):
    """Apply KEY=VALUE pairs (the --set option) with the same rules as the prompts."""
    for pair in pairs:
        key,separator,raw=pair.partition("=")
        if not separator: raise ValueError(f"--set needs KEY=VALUE (got {key!r} without a value)")
        field=FIELD_INDEX.get(key)
        if field is None: raise ValueError(f"Unknown setting {key}: see vesselstack.example.env for the settings VesselStack knows")
        value,message=normalize(field,raw,values)
        if message: raise ValueError(f"{key}: {message}")
        if key=="LAN_ACCESS": apply_lan_preset(values,value); continue
        values[key]=value
        if field.get("after"): field["after"](Session(None,values,{}))
    return values

def describe(field,value):
    """How a value is shown on screen: secrets never are."""
    if field["kind"]=="secret":
        if value=="GENERATE": return "generated during installation"
        return "set (hidden)" if value else "not set"
    if field.get("private"): return "set (hidden)" if value else "none"
    if field["kind"]=="choice": return value
    if field["kind"]=="bool": return "yes" if value=="true" else "no"
    if field["key"]=="INFLUXDB_RAW_RETENTION" and not value: return "installer default (30d on a new install)"
    return value if value else "none"

def prompt_text(field,value):
    shown=describe(field,value)
    if field["kind"]=="bool": return "  "+field["label"]+(" [Y/n]: " if value=="true" else " [y/N]: ")
    if field["kind"]=="choice": return f"  {field['label']} - choose 1-{len(field['choices'])} [{value}]: "
    return f"  {field['label']} [{shown}]: "

def show_help(console,field):
    console.say("    "+field["help"])
    if field["kind"]=="choice":
        for number,(name,text) in enumerate(field["choices"],1): console.say(f"      {number}) {name:<18} {text}")
    elif field["kind"]=="bool": console.say("    Answer y or n; Enter keeps the answer in the brackets.")
    elif field["optional"] or field["kind"]=="secret" and not field.get("generate"): console.say("    Enter keeps the value in the brackets; type - to clear it.")
    else: console.say("    Enter keeps the value in the brackets.")
    console.say("    b = previous question")

def ask_gate(session,field):
    console=session.console; current="true" if field["default"](session) else "false"
    gate={"key":field["key"],"label":field["label"],"kind":"bool","help":field["help"],"optional":False,"required":False}
    while True:
        raw=console.ask(prompt_text(gate,current))
        if raw=="?": show_help(console,gate); continue
        if raw.lower()=="b": return BACK
        value=current if raw=="" else YES_NO.get(raw.lower())
        if value is None: console.say("    Please answer y or n."); continue
        session.flags[field["key"]]=value=="true"; return None

def ask_field(session,field):
    """Prompt until the answer is valid. Returns None when the value was set, BACK to go back."""
    console=session.console
    if field["kind"]=="gate": return ask_gate(session,field)
    hint=field.get("hint")
    if hint:
        text=hint(session)
        if text: console.say("    "+text)
    if field["kind"]=="choice":
        for number,(name,text) in enumerate(field["choices"],1): console.say(f"    {number}) {name:<18} {text}")
    current=lan_preset_of(session.values) if field["key"]=="LAN_ACCESS" else session.values[field["key"]]
    while True:
        raw=console.ask(prompt_text(field,current),secret=field["kind"]=="secret")
        if raw=="?": show_help(console,field); continue
        if raw.lower()=="b": return BACK
        if raw=="": return None  # Enter keeps the value shown in the brackets (already valid)
        if raw=="-":
            if field["optional"]: session.values[field["key"]]=""; return None
            console.say("    This setting cannot be cleared. Press Enter to keep it, or type ? for help."); continue
        value,message=normalize(field,raw,session.values)
        if message: console.say("    "+message+" (type ? for help)"); continue
        if field["key"]=="LAN_ACCESS":
            apply_lan_preset(session.values,value)
            for note in lan_notes(session.values): console.say("    NOTE: "+note)
            return None
        session.values[field["key"]]=value
        if field.get("after"): field["after"](session)
        return None

def walk(session,fields):
    """Ask the visible fields in order; b goes back to the previous question."""
    history=[]; index=0
    while index<len(fields):
        field=fields[index]; when=field.get("when")
        if when and not when(session): index+=1; continue
        if ask_field(session,field) is BACK:
            if history: index=history.pop()
            else: session.console.say("    That is the first question here.")
            continue
        history.append(index); index+=1

def section_fields(section,advanced):
    return [f for f in section["fields"] if f["advanced"]==advanced or f["kind"]=="gate" and not advanced]

def run_section(session,number,advanced=False):
    section=SECTIONS[number]
    session.console.say(); session.console.say(f"[{number+1}/{len(SECTIONS)}] {section['title']}" + (" - advanced" if advanced else ""))
    if not advanced: session.console.say("  "+section["intro"])
    walk(session,section_fields(section,advanced))

def review(session):
    c=session.console
    c.say(); c.say("Review (secrets are never shown):")
    for number,section in enumerate(SECTIONS):
        lines=[]
        for f in section["fields"]:
            if f["kind"]=="gate": continue
            when=f.get("when")
            lan=f["key"]=="LAN_ACCESS"
            value=lan_preset_of(session.values) if lan else session.values[f["key"]]
            default=LAN_DEFAULT if lan else DEFAULTS[f["key"]]
            if when and not when(session) and value==default: continue
            if f["advanced"] and value==default: continue
            lines.append(f"      {f['label']:<52} {describe(f,value)}")
        c.say(f"  {number+1}. {section['title']}")
        for line in lines or ["      (all defaults)"]: c.say(line)
    c.say("  (Advanced settings you did not change are not listed; they keep their defaults.)")
    for note in lan_notes(session.values): c.say("  NOTE: "+note)

def run_cli(console,values,facts):
    """Walk through every section, then review. Returns the final values, or None if the user quits."""
    session=Session(console,values,facts)
    console.say("VesselStack setup. Press Enter to accept the answer in [brackets]. ? explains a question, b goes back one question.")
    console.say("Everything stays on this computer; no secrets are shown or printed.")
    console.say(f"This computer: {facts.get('architecture','?')}, {facts.get('memory_gib','?')} GiB RAM, Docker {'ready' if facts.get('docker') and facts.get('compose') else 'not ready yet'}; "
                f"CAN: {'can0 found' if facts.get('can0') else 'no can0 yet'}; network: {', '.join(facts.get('interfaces',[])) or 'none found'}.")
    for number in range(len(SECTIONS)): 
        if any(not f["advanced"] for f in SECTIONS[number]["fields"]): run_section(session,number)
    console.say()
    if console.ask("Advanced settings (service account, folders, ports, container and bucket names, passwords)? [y/N]: ").lower() in ("y","yes"):
        for number,section in enumerate(SECTIONS):
            if any(f["advanced"] for f in section["fields"]): run_section(session,number,advanced=True)
    while True:
        review(session)
        console.say()
        answer=console.ask("  [s] save and continue   [e] edit a section   [q] quit without saving - choose [s]: ").lower() or "s"
        if answer in ("s","save"):
            try: render(values)
            except ValueError as exc: console.say("  Cannot save: "+str(exc)); continue
            return values
        if answer in ("q","quit"): return None
        if answer in ("e","edit"):
            picked=console.ask(f"  Section number (1-{len(SECTIONS)}): ")
            if picked.isdigit() and 1<=int(picked)<=len(SECTIONS):
                number=int(picked)-1; run_section(session,number)
                if any(f["advanced"] for f in SECTIONS[number]["fields"]) and console.ask("  Also review this section's advanced settings? [y/N]: ").lower() in ("y","yes"): run_section(session,number,advanced=True)
            else: console.say("  Please enter a section number from the list.")
            continue
        console.say("  Please answer s, e or q.")

def cli_main(args):
    facts=detect_facts(); values=initial_values(facts)
    try:
        if args.base: values=validate({**values,**load_env(args.base)})
        apply_settings(values,args.set or [])
    except (OSError,ValueError) as exc: raise SystemExit(f"error: {exc}")
    output=Path(args.output).resolve() if args.output else OUTPUT
    def write(text): sys.stdout.write(text); sys.stdout.flush()
    if args.set and any(pair.partition("=")[0]=="LAN_ACCESS" for pair in args.set):
        for note in lan_notes(values): write("NOTE: "+note+"\n")
    if args.non_interactive:
        write("VesselStack setup: no questions asked"+(f" ({len(args.set)} setting(s) given with --set)" if args.set else "")+".\n")
    else:
        reader=FdInput(args.input_fd,write)
        try: result=run_cli(Console(reader.line,reader.secret,write,reader.quiet),values,facts)
        except Aborted: write("\nThe answers ended before the configuration was saved. Nothing was written.\n"); return 1
        except KeyboardInterrupt: write("\nCancelled. Nothing was written.\n"); return 130
        if result is None: write("Quit without saving. Nothing was written.\n"); return 1
        values=result
    try: save_env(output,render(values))
    except (OSError,ValueError) as exc: raise SystemExit(f"error: {exc}")
    write("Configuration saved (secrets are generated during installation and are not shown).\n")
    return 0

def main():
    global OUTPUT
    parser=argparse.ArgumentParser(description="VesselStack first-run wizard"); parser.add_argument("--host",default="127.0.0.1",choices=["127.0.0.1","::1"]); parser.add_argument("--port",type=int,default=8088,help="TCP port; 0 picks a random free port")
    parser.add_argument("--output",help="where to write the generated env file (default: vesselstack.env next to this script)")
    parser.add_argument("--validate",metavar="FILE",help="check an existing env file with the wizard's own rules (known keys, allowed characters, enums); exit 0 if valid, 1 otherwise; starts no server")
    parser.add_argument("--exit-after-save",action="store_true",help="stop the wizard after the configuration has been saved once")
    parser.add_argument("--cli",action="store_true",help="terminal wizard: ask the questions in this terminal (no web server, no token)")
    parser.add_argument("--non-interactive","--defaults",dest="non_interactive",action="store_true",help="with --cli: ask nothing; use the defaults plus any --set and --base values (implied by --set)")
    parser.add_argument("--set",action="append",metavar="KEY=VALUE",help="with --cli: set one setting (repeatable), validated with the wizard's rules")
    parser.add_argument("--base",metavar="FILE",help="with --cli: start from this existing env file instead of the defaults")
    parser.add_argument("--input-fd",type=int,default=0,metavar="N",help="with --cli: read answers from this file descriptor (default 0, standard input)")
    args=parser.parse_args()
    if args.set: args.non_interactive=True  # --set means "no questions"; --base alone prefills the questions
    if args.non_interactive or args.base: args.cli=True
    if args.validate:
        try: validate(load_env(args.validate))
        except (OSError,ValueError) as exc: raise SystemExit(f"{args.validate}: {exc}")
        print(f"{args.validate}: valid"); return
    if args.output: OUTPUT=Path(args.output).resolve()
    if args.cli: sys.exit(cli_main(args))
    Handler.exit_after_save=args.exit_after_save
    Handler.token=secrets.token_urlsafe(32)
    server=ThreadingHTTPServer((args.host,args.port),Handler); shown=f"[{args.host}]" if ":" in args.host else args.host
    port=server.server_address[1]; print(f"VesselStack wizard: http://{shown}:{port}",flush=True)
    # The token travels in the URL fragment: browsers never send it to the server, so it is
    # not in any request line, log or served file. app.js reads it and removes it from the address bar.
    print(f"Open this address (valid for this run only): http://{shown}:{port}/#token={Handler.token}",flush=True); print(f"Configuration will be written to: {OUTPUT}",flush=True); print("The wizard stops by itself after you save." if args.exit_after_save else "Press Ctrl-C to stop.",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=="__main__": main()
