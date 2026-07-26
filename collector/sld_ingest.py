#!/usr/bin/env python3.9
"""SLD data-supplier ingest for sapmon.

Listens on :50000 for RZ70 / ABAP+Java SLD Data Supplier pushes to /sld/ds,
parses the CIM-ish <sapdata> document, and upserts a landscape-inventory row
per SID into sld_systems. Parse-and-discard (no raw retained). Responds 200 so
the sender is happy. Run:  systemd-run --unit=sld-ingest --collect python3.9 sld_ingest.py
Replay a captured file:     python3.9 sld_ingest.py --replay <capturefile>
"""
import os
import sys
import json
import datetime
import psycopg2
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 50000
PGPW = open("/monitoring/secrets/pg_grafana.pw").read().strip()

DDL = """CREATE TABLE IF NOT EXISTS sld_systems(
 sid text PRIMARY KEY, sys_release text, sys_number text, db_schema text, tms_domain text, license_exp text,
 product text, sp_stack text, db_name text, db_type text, db_release text, db_vendor text, db_host text,
 fqdn text, ip text, os text, os_release text, ram_mb int, app_servers int, clients int,
 components text, appserver_list text, updated timestamptz DEFAULT now())"""


def db():
    return psycopg2.connect(host="127.0.0.1", dbname="sapmon", user="grafana", password=PGPW)


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
        msg = "ignored"
        try:
            sid = parse_and_store(body)
            msg = "stored " + sid if sid else "no BCSystem doc"
        except Exception as x:
            msg = "ERR " + str(x)[:150]
        print(datetime.datetime.now().isoformat(), "POST", self.path, len(body), "bytes ->", msg, flush=True)
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
