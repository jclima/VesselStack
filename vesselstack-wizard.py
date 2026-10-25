#!/usr/bin/env python3
"""Loopback-only first-run configuration wizard for VesselStack."""

import argparse, hmac, json, os, platform, re, secrets, shlex, shutil, subprocess, threading
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
 "GRAFANA_PORT":"43000","HEIMDALL_PORT":"80","HEIMDALL_HTTPS_PORT":"443","PROMETHEUS_PORT":"9090","VESSELSTACK_UNTRUSTED_INTERFACE":"wlan0","VESSELSTACK_FIREWALL_ENABLE":"false","VESSELSTACK_FIREWALL_STRICT":"false","VESSELSTACK_FIREWALL_ALLOW_TCP":"22","VESSELSTACK_FIREWALL_ALLOW_UDP":"","CONTROL_PANEL_HOST":"127.0.0.1","CONTROL_PANEL_PORT":"8780",
 "DISPLAYS_ENABLE":"false","DISPLAYS_BRIDGE_MQTT_USERNAME":"vesselstack-bridge","DISPLAYS_SIGNALK_TOKEN":"",
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
    for key in ("SOCKETCAN_ENABLE","VESSELSTACK_CAN_CREATE_VCAN","AIS_ENABLE","TELEGRAM_ENABLE","TELEMETRY_INDEXER_ENABLE","DISPLAYS_ENABLE","VESSELSTACK_FIREWALL_ENABLE","VESSELSTACK_FIREWALL_STRICT"):
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
            text=render(values); temp=OUTPUT.with_name(OUTPUT.name+".tmp")
            # Created 0600 from the start (never briefly world-readable) and never through a symlink.
            try: os.unlink(temp)
            except FileNotFoundError: pass
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,"w") as handle: handle.write(text)
            os.replace(temp,OUTPUT)
            self.reply(200,{"saved":str(OUTPUT),"next":f"sudo ./install.sh --config {OUTPUT.name} --dry-run"})
            if type(self).exit_after_save: threading.Thread(target=self.server.shutdown,daemon=True).start()
        except (ValueError,json.JSONDecodeError,OSError,AttributeError) as exc: self.reply(400,{"error":str(exc)})

def main():
    global OUTPUT
    parser=argparse.ArgumentParser(description="VesselStack first-run wizard"); parser.add_argument("--host",default="127.0.0.1",choices=["127.0.0.1","::1"]); parser.add_argument("--port",type=int,default=8088,help="TCP port; 0 picks a random free port")
    parser.add_argument("--output",help="where to write the generated env file (default: vesselstack.env next to this script)")
    parser.add_argument("--validate",metavar="FILE",help="check an existing env file with the wizard's own rules (known keys, allowed characters, enums); exit 0 if valid, 1 otherwise; starts no server")
    parser.add_argument("--exit-after-save",action="store_true",help="stop the wizard after the configuration has been saved once")
    args=parser.parse_args()
    if args.validate:
        try: validate(load_env(args.validate))
        except (OSError,ValueError) as exc: raise SystemExit(f"{args.validate}: {exc}")
        print(f"{args.validate}: valid"); return
    if args.output: OUTPUT=Path(args.output).resolve()
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
