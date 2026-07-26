#!/usr/bin/env python3.9
"""SLD data-supplier ingest (capture-first).

Listens on :50000 for SLD Data Supplier pushes to /sld/ds from ANY source —
RZ70 (ABAP), the JAVA supplier, HANA/hdblcm, the SAP Host Agent (sldreg), web
dispatcher, etc. For every push it:
  1. ARCHIVES the raw payload to CAP_DIR (last CAP_KEEP kept) and logs its
     sapdata `type` + CIM-class inventory — so new/unknown system types can be
     reverse-engineered from real samples before a parser exists for them;
  2. parses the ABAP `type="BCSystem"` document into sld_systems (the one type
     we understand today). Others are "captured-only" until we add their parser.
Always responds 200. Run: systemd-run --unit=sld-ingest --collect python3.9 sld_ingest.py
Replay a captured file:   python3.9 sld_ingest.py --replay <capturefile>
Inspect captures:         ls -t $SLD_CAPTURE_DIR   (default /monitoring/sld_capture)
"""
import os
import re
import sys
import glob
import json
import datetime
import collections
import psycopg2
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 50000
PGPW = open("/monitoring/secrets/pg_grafana.pw").read().strip()

# Capture-first: archive the raw payload + a class inventory for EVERY push, so
# new system types (JAVA / dual-stack / HANA / DB / Host Agent / web dispatcher)
# can be reverse-engineered from ground truth before we write their parsers.
CAP_DIR = os.environ.get("SLD_CAPTURE_DIR", "/monitoring/sld_capture")
CAP_KEEP = int(os.environ.get("SLD_CAPTURE_KEEP", "80"))   # bound disk: keep last N raw files
os.makedirs(CAP_DIR, exist_ok=True)

DDL = """CREATE TABLE IF NOT EXISTS sld_systems(
 sid text PRIMARY KEY, sys_release text, sys_number text, db_schema text, tms_domain text, license_exp text,
 product text, sp_stack text, db_name text, db_type text, db_release text, db_vendor text, db_host text,
 fqdn text, ip text, os text, os_release text, ram_mb int, app_servers int, clients int,
 components text, appserver_list text, updated timestamptz DEFAULT now())"""


def db():
    return psycopg2.connect(host="127.0.0.1", dbname="sapmon", user="grafana", password=PGPW)


def classify(body):
    """Best-effort inventory of a payload for reverse-engineering.
    Returns (sapdata_type, {classname: count}, guessed_sid)."""
    try:
        root = ET.fromstring(body)
    except Exception:
        return ("unparseable", {}, None)
    if root.tag != "sapdata":
        return (root.tag, {}, None)
    cc = collections.Counter(i.get("classname") for i in root.findall("instance"))
    sid = None
    for i in root.findall("instance"):
        cn = i.get("classname") or ""
        if cn in ("SAP_BCSystem", "SAP_J2EEEngineCluster", "SAP_XIDomain") or cn.endswith("System"):
            for p in i.findall("property"):
                if p.get("name") in ("SAPSystemName", "Name", "SystemName") and p.findtext("value"):
                    sid = p.findtext("value")
                    if p.get("name") == "SAPSystemName":
                        break
            if sid:
                break
    return (root.get("type") or "?", dict(cc), sid)


def capture_raw(body, sapdata_type, sid):
    """Archive one raw payload; prune to the last CAP_KEEP files."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s_%s" % (sapdata_type or "unknown", sid or "na"))[:60]
    fn = os.path.join(CAP_DIR, "req_%s_%s.xml" % (ts, safe))
    with open(fn, "wb") as f:
        f.write(body)
    files = sorted(glob.glob(os.path.join(CAP_DIR, "req_*")), key=os.path.getmtime)
    for old in files[:-CAP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return fn


def props(i):
    d = {}
    for p in i.findall("property"):
        vs = [v.text for v in p.findall("value") if v.text is not None]
        d[p.get("name")] = vs[0] if len(vs) == 1 else vs
    return d


def parse_and_store(body):
    root = ET.fromstring(body)
    if root.tag != "sapdata":
        return None
    byc = {}
    for i in root.findall("instance"):
        byc.setdefault(i.get("classname"), []).append(i)
    if not byc.get("SAP_BCSystem"):
        return None  # not a system doc (e.g. generic/associations)
    b = props(byc["SAP_BCSystem"][0])
    sid = b.get("SAPSystemName")
    if not sid:
        return None
    dbi = props(byc["SAP_DatabaseSystem"][0]) if byc.get("SAP_DatabaseSystem") else {}
    cs = props(byc["SAP_ComputerSystem"][0]) if byc.get("SAP_ComputerSystem") else {}
    prod = props(byc["SAP_InstalledProduct"][0]) if byc.get("SAP_InstalledProduct") else {}
    sps = props(byc["SAP_InstalledSupportPackageStack"][0]) if byc.get("SAP_InstalledSupportPackageStack") else {}
    comps = sorted(({"name": props(c).get("Name"), "version": props(c).get("Version")}
                    for c in byc.get("SAP_InstalledSoftwareComponent", []) if props(c).get("Name")),
                   key=lambda c: c["name"])
    apps = [{"inst": props(a).get("InstanceName"), "nr": props(a).get("Number"), "host": props(a).get("HostName")}
            for a in byc.get("SAP_BCApplicationServer", [])]
    ram = cs.get("PhysicalRAMInMB")
    row = dict(
        sid=sid, sys_release=b.get("Release"), sys_number=b.get("SystemNumber"),
        db_schema=b.get("SystemDBSchema"), tms_domain=b.get("TMSDomain"), license_exp=b.get("LicenseExpiration"),
        product=((prod.get("ProductName", "") + " " + prod.get("ProductVersion", "")).strip() or None),
        sp_stack=(sps.get("Version") or None),
        db_name=dbi.get("DBName"), db_type=dbi.get("DBTypeForSAP"), db_release=dbi.get("Release"),
        db_vendor=dbi.get("Manufacturer"), db_host=dbi.get("SystemHome"),
        fqdn=cs.get("FQDName"), ip=cs.get("IPAddress"), os=cs.get("OpSys"), os_release=cs.get("OpSysRelease"),
        ram_mb=int(ram) if isinstance(ram, str) and ram.isdigit() else None,
        app_servers=len(apps) or None, clients=len(byc.get("SAP_BCClient", [])) or None,
        components=json.dumps(comps), appserver_list=json.dumps(apps))
    cols = list(row.keys())
    sets = ", ".join("%s=EXCLUDED.%s" % (k, k) for k in cols if k != "sid")
    sql = ("INSERT INTO sld_systems(%s, updated) VALUES(%s, now()) "
           "ON CONFLICT(sid) DO UPDATE SET %s, updated=now()"
           % (", ".join(cols), ", ".join(["%s"] * len(cols)), sets))
    with db() as c, c.cursor() as cur:
        cur.execute(DDL)
        cur.execute(sql, [row[k] for k in cols])
        c.commit()
    return sid


class H(BaseHTTPRequestHandler):
    def _read(self):
        te = (self.headers.get("Transfer-Encoding", "") or "").lower()
        if "chunked" in te:
            data = b""
            while True:
                ln = self.rfile.readline()
                if not ln:
                    break
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    n = int(ln.split(b";")[0], 16)
                except ValueError:
                    break
                if n == 0:
                    self.rfile.readline()
                    break
                data += self.rfile.read(n)
                self.rfile.read(2)
            return data
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        body = self._read()
        sapdata_type, classes, sid = classify(body)
        raw = capture_raw(body, sapdata_type, sid)              # always archive for reverse-engineering
        try:
            stored = parse_and_store(body)
            action = "stored " + stored if stored else "captured-only (no parser yet)"
        except Exception as x:
            action = "ERR " + str(x)[:120]
        top = ", ".join("%s×%d" % (k, v) for k, v in sorted(classes.items(), key=lambda kv: -kv[1])[:6])
        print("%s POST %s %dB type=%s sid=%s -> %s | classes[%s] raw=%s"
              % (datetime.datetime.now().isoformat(), self.path, len(body),
                 sapdata_type, sid, action, top, os.path.basename(raw)), flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        raw = open(sys.argv[2], "rb").read()
        body = raw.split(b"--- body (", 1)[1].split(b") ---\n", 1)[1] if b"--- body (" in raw else raw
        print("replay ->", parse_and_store(body))
        sys.exit(0)
    with db() as c, c.cursor() as cur:
        cur.execute(DDL)
        c.commit()
    print("SLD ingest on :%d" % PORT, flush=True)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
