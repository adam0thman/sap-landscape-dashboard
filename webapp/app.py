#!/usr/bin/env python3
"""BFI SAP Landscape Operations — custom operator dashboard (containerised).

Read-only SPA served behind nginx, backed by the same Postgres the native
pyrfc collector writes to.

  /                -> the SPA (self-contained HTML/CSS/JS)
  /api/overview    -> all systems, latest status + parsed card metrics
  /api/system/<S>  -> one system's detail panes (6h trend, findings, lists)
  /api/ping/<S>    -> single fast TCP-connect latency probe (feeds live chart)

Config via env: PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD SYSTEMS_JSON.
SELECT-only on the DB; the only outbound action is a TCP connect for latency.
"""
import json
import os
import re
import socket
import time
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import psycopg2
import psycopg2.extras

PORT = int(os.environ.get("APP_PORT", "8080"))
SYSTEMS_JSON = os.environ.get("SYSTEMS_JSON", "/etc/sapmon/systems.json")
DSN = dict(
    host=os.environ.get("PGHOST", "postgres"),
    port=int(os.environ.get("PGPORT", "5432")),
    dbname=os.environ.get("PGDATABASE", "sapmon"),
    user=os.environ.get("PGUSER", "grafana"),
    password=os.environ.get("PGPASSWORD", ""),
)


def db():
    return psycopg2.connect(**DSN)


def load_targets():
    """Map sid -> (host, port) for the live latency probe. ABAP -> sapdp 3200+nr."""
    try:
        cfg = json.load(open(SYSTEMS_JSON))
    except Exception:
        return {}
    t = {}
    for s in cfg.get("systems", []):
        if s.get("type") == "ABAP" and s.get("sysnr") is not None:
            t[s["sid"]] = (s["host"], 3200 + int(s["sysnr"]))
        elif s.get("port"):
            t[s["sid"]] = (s["host"], int(s["port"]))
    return t


def first_int(s, default=0):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else default


def badge(worst):
    return {0: ("HEALTHY", "green"), 1: ("WARNING", "amber"),
            2: ("CRITICAL", "red"), 3: ("STALE", "grey")}.get(worst, ("STALE", "grey"))


def overview():
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT sid, env, stype, host, descr FROM systems ORDER BY env, sid")
        systems = cur.fetchall()
        cur.execute("""SELECT DISTINCT ON (sid, check_name) sid, check_name, status, detail, ts
                       FROM jco_results ORDER BY sid, check_name, ts DESC""")
        latest, newest = {}, {}
        for r in cur.fetchall():
            latest.setdefault(r["sid"], {})[r["check_name"]] = r
            if r["sid"] not in newest or r["ts"] > newest[r["sid"]]:
                newest[r["sid"]] = r["ts"]
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for s in systems:
        sid = s["sid"]
        chk = latest.get(sid, {})
        worst = max((c["status"] for c in chk.values()), default=3)
        b_txt, b_col = badge(worst)
        av = chk.get("AVAIL")
        avail_text, resp = "—", None
        if av:
            if av["status"] == 0:
                m = re.search(r"(\d+)\s*ms", av["detail"] or "")
                resp = int(m.group(1)) if m else None
                avail_text = "UP"
            else:
                avail_text = "DOWN"
        age = int((now - newest[sid]).total_seconds()) if sid in newest else None
        out.append({
            "sid": sid, "env": s["env"], "stype": s["stype"], "host": s["host"],
            "descr": s["descr"], "worst": worst, "badge": b_txt, "color": b_col, "age": age,
            "avail": avail_text, "resp": resp,
            "jobs": first_int(chk["JOBS_ABORTED"]["detail"]) if "JOBS_ABORTED" in chk else None,
            "dumps": first_int(chk["DUMPS"]["detail"]) if "DUMPS" in chk else None,
            "locks": first_int(chk["LOCKS"]["detail"]) if "LOCKS" in chk else None,
            "updates": first_int(chk["UPD_RECORDS"]["detail"]) if "UPD_RECORDS" in chk else None,
            "sal_status": chk.get("SAL", {}).get("status"),
            "fs": (first_int(chk["STORAGE"]["detail"]) if chk["STORAGE"]["status"] != 3 else None) if "STORAGE" in chk else None,
        })
    counts = {"red": sum(1 for o in out if o["worst"] == 2),
              "amber": sum(1 for o in out if o["worst"] == 1),
              "green": sum(1 for o in out if o["worst"] == 0),
              "total": len(out)}
    return {"systems": out, "counts": counts}


def rows_to_table(items):
    return [re.split(r"\s{2,}", it.strip()) for it in items]


def system_detail(sid):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT sid, env, stype, host, descr FROM systems WHERE sid=%s", (sid,))
        s = cur.fetchone()
        if not s:
            return None
        cur.execute("""SELECT DISTINCT ON (check_name) check_name, status, detail, ts
                       FROM jco_results WHERE sid=%s ORDER BY check_name, ts DESC""", (sid,))
        chk = {r["check_name"]: r for r in cur.fetchall()}
        cur.execute("""SELECT extract(epoch from ts)*1000 AS t, reachable, response_ms
                       FROM uptime WHERE sid=%s AND ts > now() - interval '6 hours' ORDER BY ts""", (sid,))
        trend = [{"t": int(r["t"]), "up": r["reachable"], "ms": r["response_ms"]} for r in cur.fetchall()]
        cur.execute("SELECT check_name, item FROM jco_details WHERE sid=%s ORDER BY check_name, item", (sid,))
        lists = {}
        for r in cur.fetchall():
            lists.setdefault(r["check_name"], []).append(r["item"])
        sld = None
        try:
            # sld_systems is keyed (sid, system_home): a SID may have >1 row
            # (e.g. prod + its copy/POC). Prefer the one whose host matches this
            # monitored system, else the most recently updated.
            cur.execute("""SELECT sl.* FROM sld_systems sl
                           WHERE sl.sid=%s
                           ORDER BY (sl.system_home = (SELECT host FROM systems WHERE sid=%s)) DESC NULLS LAST,
                                    sl.updated DESC
                           LIMIT 1""", (sid, sid))
            sld = cur.fetchone()
        except Exception:
            c.rollback()
            sld = None
    worst = max((v["status"] for v in chk.values()), default=3)
    b_txt, b_col = badge(worst)
    now = datetime.datetime.now(datetime.timezone.utc)
    age = int((now - max(v["ts"] for v in chk.values())).total_seconds()) if chk else None
    resp = None
    if chk.get("AVAIL") and chk["AVAIL"]["status"] == 0:
        m = re.search(r"(\d+)\s*ms", chk["AVAIL"]["detail"] or "")
        resp = int(m.group(1)) if m else None
    sld_out = None
    if sld:
        sld_out = dict(sld)
        sld_out["components"] = json.loads(sld_out.get("components") or "[]")
        sld_out["appserver_list"] = json.loads(sld_out.get("appserver_list") or "[]")
        u = sld_out.get("updated")
        sld_out["updated"] = u.isoformat() if u else None
    return {
        "sid": sid, "env": s["env"], "stype": s["stype"], "host": s["host"], "descr": s["descr"],
        "badge": b_txt, "color": b_col, "age": age, "resp": resp,
        "avail_detail": chk.get("AVAIL", {}).get("detail", "—"),
        "findings": {
            "jobs": first_int(chk["JOBS_ABORTED"]["detail"]) if "JOBS_ABORTED" in chk else 0,
            "dumps": first_int(chk["DUMPS"]["detail"]) if "DUMPS" in chk else 0,
            "updates": first_int(chk["UPD_RECORDS"]["detail"]) if "UPD_RECORDS" in chk else 0,
            "locks": first_int(chk["LOCKS"]["detail"]) if "LOCKS" in chk else 0,
            "sal_status": chk.get("SAL", {}).get("status", 3),
            "sal_text": chk.get("SAL", {}).get("detail", "—"),
        },
        "collected": int(max(v["ts"] for v in chk.values()).timestamp() * 1000) if chk else None,
        "times": {k: int(v["ts"].timestamp() * 1000) for k, v in chk.items()},
        "trend": trend,
        "lists": {k: rows_to_table(lists.get(k, []))
                  for k in ("JOBS_ABORTED", "DUMPS", "UPD_RECORDS", "LOCKS", "SAL", "STORAGE")},
        "sld": sld_out,
    }


TARGETS = load_targets()


def ping(sid):
    tgt = TARGETS.get(sid)
    if not tgt:
        return {"up": 0, "ms": None, "note": "no probe target"}
    host, port = tgt
    t0 = time.time()
    try:
        socket.create_connection((host, port), timeout=2).close()
        return {"up": 1, "ms": int((time.time() - t0) * 1000), "port": port}
    except Exception:
        return {"up": 0, "ms": None, "port": port}


HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BFI · SAP Landscape Operations</title>
<style>
:root{--bg:#0b0d10;--panel:#14171c;--panel2:#1a1e24;--line:#262b33;--txt:#e6e9ef;
 --dim:#8b93a1;--faint:#5b636f;--red:#f2495c;--amber:#ff9830;--green:#56b877;--grey:#6b7280;--blue:#3b82f6;
 --mono:"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:14px}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;
 background:linear-gradient(90deg,#12161c,#0f1319 60%,#0b0d10);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.bolt{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;background:linear-gradient(135deg,#f2b705,#ff7a00);color:#111;font-size:17px;font-weight:800}
.brand b{font-weight:700;font-size:15px}.brand span{color:var(--dim);font-size:12.5px;margin-left:10px}
.spacer{flex:1}
.pill{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;color:var(--dim);font-size:12.5px;display:flex;align-items:center;gap:7px}
select.pill{color:var(--txt);cursor:pointer}
.dotpulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(86,184,119,.5)}70%{box-shadow:0 0 0 7px rgba(86,184,119,0)}100%{box-shadow:0 0 0 0 rgba(86,184,119,0)}}
.strip-h{display:flex;align-items:baseline;gap:12px;padding:16px 22px 8px}
.strip-h .t{font-size:12px;letter-spacing:1.5px;color:var(--dim);font-weight:600}
.strip-h .sub{color:var(--faint);font-size:12px}
.legend{margin-left:auto;display:flex;gap:16px;font-size:12px;color:var(--dim)}
.legend i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:6px;vertical-align:middle}
.strip{display:flex;gap:12px;overflow-x:auto;padding:4px 22px 18px;scroll-snap-type:x proximity}
.strip::-webkit-scrollbar{height:8px}.strip::-webkit-scrollbar-thumb{background:#2a2f38;border-radius:4px}
.card{min-width:250px;max-width:250px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--grey);
 border-radius:11px;padding:14px;cursor:pointer;scroll-snap-align:start;transition:transform .12s,background .12s}
.card:hover{transform:translateY(-2px);background:var(--panel2)}
.card.sel{background:var(--panel2);box-shadow:0 0 0 1px var(--blue) inset}
.card.red{border-left-color:var(--red)}.card.amber{border-left-color:var(--amber)}
.card.green{border-left-color:var(--green)}.card.grey{border-left-color:var(--grey)}
.card .top{display:flex;align-items:center;gap:9px}
.card .sid{font-family:var(--mono);font-weight:700;font-size:20px;letter-spacing:.5px}
.card .dot{width:10px;height:10px;border-radius:50%;margin-left:auto}
.tag{font-size:9.5px;font-weight:700;letter-spacing:.6px;padding:3px 7px;border-radius:5px}
.tag.red{background:rgba(242,73,92,.16);color:#ff8595}.tag.amber{background:rgba(255,152,48,.16);color:#ffb266}
.tag.green{background:rgba(86,184,119,.16);color:#7fd39b}.tag.grey{background:rgba(107,114,128,.18);color:#9aa2af}
.d-red{background:var(--red)}.d-amber{background:var(--amber)}.d-green{background:var(--green)}.d-grey{background:var(--grey)}
.card .desc{color:var(--dim);font-size:12px;margin:9px 0 12px;min-height:16px}.card .desc b{color:var(--faint);font-weight:500}
.card .mrow{display:flex;justify-content:space-between;text-align:center;border-top:1px solid var(--line);padding-top:11px;gap:6px}
.card .m{flex:1}.card .m .v{font-family:var(--mono);font-size:15px;font-weight:600}
.card .m .k{color:var(--faint);font-size:9.5px;letter-spacing:.5px;margin-top:3px}
.v.red{color:var(--red)}.v.amber{color:var(--amber)}.v.green{color:var(--green)}.v.dim{color:var(--dim)}
.detail{padding:2px 22px 40px}
.hero{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 20px;display:flex;align-items:center;gap:16px;margin-bottom:14px;border-left:4px solid var(--grey)}
.hero.red{border-left-color:var(--red)}.hero.amber{border-left-color:var(--amber)}.hero.green{border-left-color:var(--green)}.hero.grey{border-left-color:var(--grey)}
.hero .sid{font-family:var(--mono);font-size:30px;font-weight:700}
.hero .meta b{font-size:14px}.hero .meta{color:var(--dim);font-size:12.5px}
.hero .meta .l2{margin-top:4px;color:var(--faint);font-family:var(--mono);font-size:12px}
.hero .act{margin-left:auto;display:flex;gap:9px}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:8px 14px;border-radius:8px;font-size:12.5px;cursor:pointer;text-decoration:none}
.btn:hover{border-color:#3a4150}
.live{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px 8px;margin-bottom:14px;position:relative;overflow:hidden}
.live h3{margin:0 0 4px;font-size:12px;letter-spacing:.8px;color:var(--dim);font-weight:600;display:flex;align-items:center;gap:8px}
.live .now{margin-left:auto;font-family:var(--mono);font-size:22px;font-weight:700}
.live .unit{color:var(--faint);font-size:11px;font-weight:400;margin-left:3px}
.livec{width:100%;height:150px;display:block}
.livelbl{position:absolute;left:16px;bottom:12px;color:var(--faint);font-size:10.5px;font-family:var(--mono)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.pane{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px;min-width:0}
.pane h3{margin:0 0 12px;font-size:12px;letter-spacing:.8px;color:var(--dim);font-weight:600;display:flex;align-items:center}
.pane h3 .src{margin-left:auto;color:var(--faint);font-weight:400;letter-spacing:0}
.col12{grid-column:span 12}.col7{grid-column:span 7}.col6{grid-column:span 6}.col5{grid-column:span 5}.col4{grid-column:span 4}
@media(max-width:1100px){.col7,.col6,.col5,.col4{grid-column:span 12}}
.fsrow{display:flex;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid #1e232a}
.fsrow:last-child{border-bottom:none}
.fsmount{font-family:var(--mono);font-size:12.5px;width:120px;flex-shrink:0;color:var(--txt)}
.fsbar{flex:1;height:9px;background:#0b0d10;border:1px solid var(--line);border-radius:5px;overflow:hidden}
.fsfill{height:100%;border-radius:5px;transition:width .5s ease}
.fspct{font-family:var(--mono);font-size:12.5px;width:42px;text-align:right;font-weight:700}
.fssz{font-family:var(--mono);font-size:11px;color:var(--faint);width:150px;text-align:right;flex-shrink:0}
@media(max-width:640px){.fssz{display:none}.fsmount{width:90px}}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:11px 20px;margin-bottom:14px}
.kvk{color:var(--faint);font-size:10px;letter-spacing:.5px;text-transform:uppercase}
.kvv{font-family:var(--mono);font-size:12.5px;margin-top:2px;color:var(--txt);word-break:break-word}
.complist{display:flex;flex-wrap:wrap;gap:6px;border-top:1px solid var(--line);padding-top:12px}
.chip{font-family:var(--mono);font-size:11px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:3px 8px;color:var(--dim)}
.chip b{color:var(--txt);margin-left:6px;font-weight:600}
.l3{margin-top:4px;color:var(--dim);font-size:12px}
.findings{display:grid;grid-template-columns:1fr 1fr;gap:14px 20px}
.finding .n{font-family:var(--mono);font-size:30px;font-weight:700;line-height:1}
.finding .lb{color:var(--dim);font-size:11.5px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--faint);font-weight:500;font-size:10.5px;letter-spacing:.5px;padding:0 10px 8px 0;border-bottom:1px solid var(--line)}
td{padding:8px 10px 8px 0;border-bottom:1px solid #1e232a;font-family:var(--mono);color:var(--dim)}
tr:last-child td{border-bottom:none}td.k{color:var(--txt)}
.empty{color:var(--faint);padding:14px 0;font-size:12.5px}.sal-note{color:var(--amber);font-size:12px;margin-top:10px}
.trend-foot{display:flex;justify-content:space-between;color:var(--faint);font-size:11px;margin-top:6px}
.trendc{width:100%;height:150px;display:block;cursor:crosshair}
.chart-tip{position:absolute;background:#0b0d10;border:1px solid var(--line);border-radius:7px;padding:6px 9px;
 font-size:11.5px;font-family:var(--mono);color:var(--txt);pointer-events:none;z-index:5;white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.5)}
.chart-tip b{color:var(--dim);font-weight:600}
.win{color:var(--faint);font-size:10.5px;font-family:var(--mono);margin:-4px 0 10px;letter-spacing:.2px}
</style></head>
<body>
<header>
  <div class="bolt">⚡</div>
  <div class="brand"><b>SAP Landscape Operations</b><span>Basis Monitoring · BFI</span></div>
  <div class="spacer"></div>
  <select id="envf" class="pill"><option value="">All environments</option>
    <option value="D">Development</option><option value="Q">Quality/QA</option><option value="P">Production</option></select>
  <div class="pill"><span class="dotpulse"></span><span id="clock">live · 30s</span></div>
</header>
<div class="strip-h"><span class="t">SYSTEM LANDSCAPE</span><span class="sub" id="stripcount"></span>
  <div class="legend">
    <span><i class="d-red"></i>Critical <b id="lc-red">0</b></span>
    <span><i class="d-amber"></i>Warning <b id="lc-amber">0</b></span>
    <span><i class="d-green"></i>Healthy <b id="lc-green">0</b></span></div></div>
<div class="strip" id="strip"></div>
<div class="detail" id="detail"></div>
<script>
const $=(s,e=document)=>e.querySelector(s);
const H=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ENVL=e=>({D:'Development',Q:'Quality/QA',P:'Production'}[e]||e);
let SEL=null,OV=null,LIVE=null;
function ago(s){if(s==null)return'—';if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago';}
function latColor(ms,up){if(!up||ms==null)return'var(--red)';if(ms<300)return'var(--green)';if(ms<800)return'var(--amber)';return'var(--red)';}

function card(o){
  const mv=(v,c)=>`<div class="m"><div class="v ${c||'dim'}">${v}</div>`;
  const availC=o.avail==='UP'?'green':(o.avail==='DOWN'?'red':'dim');
  const jobsC=o.jobs==null?'dim':(o.jobs>50?'red':o.jobs>0?'amber':'green');
  const salC=o.sal_status===0?'green':o.sal_status==null?'dim':'amber';
  const fsC=o.fs==null?'dim':(o.fs>=90?'red':o.fs>=80?'amber':'green');
  const resp=o.resp==null?'n/a':o.resp+'ms';
  return `<div class="card ${o.color} ${SEL===o.sid?'sel':''}" data-sid="${o.sid}">
    <div class="top"><span class="sid">${H(o.sid)}</span><span class="tag ${o.color}">${o.badge}</span><span class="dot d-${o.color}"></span></div>
    <div class="desc">${H(o.descr)} <b>· ${H(o.stype)}</b></div>
    <div class="mrow">
      ${mv(o.avail,availC)}<div class="k">AVAIL</div></div>
      ${mv(resp,'dim')}<div class="k">RESP</div></div>
      ${mv(o.jobs==null?'—':o.jobs,jobsC)}<div class="k">JOBS✗</div></div>
      ${mv(o.fs==null?'—':o.fs+'%',fsC)}<div class="k">FS</div></div>
      ${mv(o.sal_status==null?'—':(o.sal_status===0?'OK':'!'),salC)}<div class="k">SAL</div></div>
    </div></div>`;
}
function renderStrip(){
  const env=$('#envf').value, sys=OV.systems.filter(o=>!env||o.env===env);
  $('#strip').innerHTML=sys.map(card).join('')||'<div class="empty" style="padding:20px">No systems in this environment.</div>';
  $('#stripcount').textContent=sys.length+' SIDs'+(sys.length>3?' · scroll for more →':'');
  $('#lc-red').textContent=OV.counts.red;$('#lc-amber').textContent=OV.counts.amber;$('#lc-green').textContent=OV.counts.green;
  document.querySelectorAll('.card').forEach(c=>c.onclick=()=>select(c.dataset.sid));
  if(sys.length&&(!SEL||!sys.find(o=>o.sid===SEL)))select(sys[0].sid);
}
function pad2(n){return String(n).padStart(2,'0');}
function fmt(ms,withDate){const d=new Date(ms),t=pad2(d.getHours())+':'+pad2(d.getMinutes());
  return withDate?d.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' '+t:t;}
function fmtS(ms){const d=new Date(ms);return pad2(d.getHours())+':'+pad2(d.getMinutes())+':'+pad2(d.getSeconds());}
function winLabel(check,ts){
  if(ts==null)return'';const DAY=864e5;
  if(check==='JOBS_ABORTED')return'⏱ 24h window · '+fmt(ts-DAY,true)+' → '+fmt(ts,true);
  if(check==='DUMPS'||check==='SAL'){const d=new Date(ts);d.setHours(0,0,0,0);return'⏱ today · '+fmt(d.getTime(),false)+' → '+fmt(ts,false);}
  return'⏱ snapshot @ '+fmtS(ts);  // LOCKS / UPD_RECORDS = point-in-time
}
/* interactive 6h availability chart: time axis + hover crosshair + tooltip */
function drawTrend(trend){
  const cv=$('#trendc');if(!cv||!trend.length)return;
  const tip=$('#trendtip'),padL=42,padR=12,padT=12,padB=24;
  const t0=trend[0].t,t1=trend[trend.length-1].t||t0+1;
  const max=Math.max(30,...trend.filter(p=>p.up).map(p=>p.ms||0));
  const geom=()=>{const w=cv.clientWidth,h=cv.clientHeight;return{w,h,
    X:t=>padL+((t-t0)/(t1-t0||1))*(w-padL-padR),Y:v=>h-padB-(v/(max*1.1))*(h-padT-padB)};};
  function render(hi){
    const dpr=window.devicePixelRatio||1,{w,h,X,Y}=geom();
    if(cv.width!==w*dpr){cv.width=w*dpr;cv.height=h*dpr;}
    const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    ctx.font='10px ui-monospace,monospace';
    // y grid + ms labels
    ctx.textAlign='right';
    [0,Math.round(max/2),Math.round(max)].forEach(v=>{const y=Y(v);
      ctx.strokeStyle='rgba(255,255,255,.05)';ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(w-padR,y);ctx.stroke();
      ctx.fillStyle='#5b636f';ctx.fillText(v,padL-6,y+3);});
    ctx.fillStyle='#5b636f';ctx.textAlign='left';ctx.fillText('ms',4,Y(max)+3);
    // x time ticks
    ctx.textAlign='center';const N=6;
    for(let i=0;i<=N;i++){const t=t0+(t1-t0)*i/N,x=X(t);
      ctx.strokeStyle='rgba(255,255,255,.03)';ctx.beginPath();ctx.moveTo(x,padT);ctx.lineTo(x,h-padB);ctx.stroke();
      ctx.fillStyle='#5b636f';ctx.fillText(fmt(t,false),x,h-7);}
    // area + line
    ctx.beginPath();ctx.moveTo(X(trend[0].t),h-padB);
    trend.forEach(p=>ctx.lineTo(X(p.t),p.up?Y(p.ms||0):h-padB));
    ctx.lineTo(X(t1),h-padB);ctx.closePath();
    const g=ctx.createLinearGradient(0,padT,0,h-padB);g.addColorStop(0,'rgba(86,184,119,.35)');g.addColorStop(1,'rgba(86,184,119,0)');
    ctx.fillStyle=g;ctx.fill();
    ctx.beginPath();let st=false;trend.forEach(p=>{if(!p.up){st=false;return;}const x=X(p.t),y=Y(p.ms||0);if(!st){ctx.moveTo(x,y);st=true;}else ctx.lineTo(x,y);});
    ctx.strokeStyle='#56b877';ctx.lineWidth=2;ctx.lineJoin='round';ctx.stroke();
    trend.forEach(p=>{if(!p.up){ctx.fillStyle='#f2495c';ctx.beginPath();ctx.arc(X(p.t),h-padB,2.5,0,6.283);ctx.fill();}});
    if(hi!=null){const p=trend[hi],x=X(p.t),y=p.up?Y(p.ms||0):h-padB;
      ctx.strokeStyle='rgba(255,255,255,.28)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,padT);ctx.lineTo(x,h-padB);ctx.stroke();
      ctx.fillStyle=p.up?'#56b877':'#f2495c';ctx.beginPath();ctx.arc(x,y,4,0,6.283);ctx.fill();
      ctx.strokeStyle='#0b0d10';ctx.lineWidth=2;ctx.stroke();}
  }
  cv.onmousemove=e=>{const r=cv.getBoundingClientRect(),mx=e.clientX-r.left,{w}=geom();
    const t=t0+((mx-padL)/(w-padL-padR))*(t1-t0);
    let bi=0,bd=1e18;trend.forEach((p,i)=>{const dd=Math.abs(p.t-t);if(dd<bd){bd=dd;bi=i;}});
    render(bi);const p=trend[bi];
    tip.style.display='block';
    tip.innerHTML='<b>'+fmtS(p.t)+'</b><br>'+(p.up?((p.ms||0)+' ms'):'<span style="color:#f2495c">unreachable</span>');
    let tx=mx+14;if(tx>r.width-96)tx=mx-100;tip.style.left=Math.max(4,tx)+'px';tip.style.top='30px';};
  cv.onmouseleave=()=>{tip.style.display='none';render(null);};
  render(null);
  window.addEventListener('resize',()=>render(null),{once:true});
  const tr=$('#trendrange');if(tr)tr.textContent=trend.length+' probes · '+fmt(t0,true)+' → '+fmt(t1,true);
}
function tbl(head,rows,emptymsg){
  if(!rows.length)return`<div class="empty">${emptymsg}</div>`;
  const th=head.map(h=>`<th>${h}</th>`).join('');
  const body=rows.slice(0,8).map(r=>'<tr>'+head.map((_,i)=>`<td class="${i===0?'k':''}">${H(r[i]||'')}</td>`).join('')+'</tr>').join('');
  const more=rows.length>8?`<div class="empty">+${rows.length-8} more</div>`:'';
  return`<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>${more}`;
}
function fmtB(n){n=+n;if(n>=1e12)return(n/1e12).toFixed(1)+' TB';if(n>=1e9)return(n/1e9).toFixed(0)+' GB';if(n>=1e6)return(n/1e6).toFixed(0)+' MB';return n+' B';}
function storageBars(rows){
  if(!rows||!rows.length)return'<div class="empty">No filesystem data — host not reachable via SSH.</div>';
  return rows.map(r=>{const m=r[0],sz=+r[1],us=+r[2],pc=+r[3];
    const c=pc>=90?'var(--red)':pc>=80?'var(--amber)':'var(--green)';
    return `<div class="fsrow"><div class="fsmount">${H(m)}</div>
      <div class="fsbar"><div class="fsfill" style="width:${Math.min(pc,100)}%;background:${c}"></div></div>
      <div class="fspct" style="color:${c}">${pc}%</div>
      <div class="fssz">${fmtB(us)} / ${fmtB(sz)}</div></div>`;}).join('');
}
function kv(k,v){return v?`<div><div class="kvk">${H(k)}</div><div class="kvv">${H(v)}</div></div>`:'';}
function fmtLic(d){return d&&d.length===8?(d==='99991231'?'never':d.slice(0,4)+'-'+d.slice(4,6)+'-'+d.slice(6,8)):(d||'');}
function landscape(s){
  if(!s)return'';
  const gb=s.ram_mb?Math.round(s.ram_mb/1024)+' GB':'';
  return `<div class="pane col12"><h3>LANDSCAPE / INVENTORY<span class="src">SLD · RZ70${s.updated?' · '+fmtS(new Date(s.updated).getTime()):''}</span></h3>
    <div class="kv">
      ${kv('Product',s.product)}${kv('Release',s.sys_release)}${kv('System No.',s.sys_number)}${kv('SP stack',s.sp_stack)}
      ${kv('Database',[s.db_type,s.db_release,s.db_vendor].filter(Boolean).join(' · '))}${kv('DB host',s.db_host)}
      ${kv('Schema',s.db_schema)}${kv('TMS domain',s.tms_domain)}${kv('OS / kernel',[s.os,s.os_release].filter(Boolean).join(' '))}
      ${kv('RAM',gb)}${kv('App servers',s.app_servers)}${kv('Clients',s.clients)}${kv('License expiry',fmtLic(s.license_exp))}${kv('FQDN',s.fqdn)}
    </div>
    ${(s.components&&s.components.length)?`<div class="complist">${s.components.map(c=>`<span class="chip">${H(c.name)}<b>${H(c.version)}</b></span>`).join('')}</div>`:''}
  </div>`;
}

/* ---- live auto-scrolling latency chart (canvas, rAF) ---- */
function startLive(sid){
  if(LIVE){cancelAnimationFrame(LIVE.raf);clearInterval(LIVE.poll);}
  const cv=$('#livec');if(!cv)return;
  const WIN=90000; // 90s visible window
  const buf=[]; // {t, ms, up}
  const L={raf:0,poll:0};LIVE=L;
  async function probe(){
    try{const r=await(await fetch('/api/ping/'+sid)).json();
      buf.push({t:Date.now(),ms:r.ms,up:r.up});
      while(buf.length&&buf[0].t<Date.now()-WIN-4000)buf.shift();
      const cur=$('#livenow');if(cur){cur.textContent=r.up?(r.ms):'DOWN';cur.style.color=latColor(r.ms,r.up);}
    }catch(e){}
  }
  probe();L.poll=setInterval(probe,2000);
  const ctx=cv.getContext('2d');
  const tip=$('#livetip'),box=cv.closest('.live');let hoverX=null;
  cv.onmousemove=e=>{const r=cv.getBoundingClientRect();hoverX=e.clientX-r.left;
    const br=box.getBoundingClientRect();let tx=e.clientX-br.left+14;if(tx>br.width-100)tx=e.clientX-br.left-104;
    tip.style.left=Math.max(4,tx)+'px';tip.style.top=(e.clientY-br.top+12)+'px';};
  cv.onmouseleave=()=>{hoverX=null;tip.style.display='none';};
  function draw(){
    const dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
    if(cv.width!==w*dpr){cv.width=w*dpr;cv.height=h*dpr;}
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const now=Date.now(),pad=6,max=Math.max(60,...buf.filter(p=>p.up).map(p=>p.ms));
    const X=t=>w-((now-t)/WIN)*(w-pad);
    const Y=v=>h-pad-(v/(max*1.15))*(h-2*pad);
    // grid: vertical time lines every 15s scrolling
    ctx.strokeStyle='rgba(255,255,255,.04)';ctx.lineWidth=1;
    for(let g=0;g<=WIN;g+=15000){const gx=X(now-g);ctx.beginPath();ctx.moveTo(gx,0);ctx.lineTo(gx,h);ctx.stroke();}
    if(buf.length>1){
      const pts=buf.filter(p=>p.t>=now-WIN-2000);
      // area
      ctx.beginPath();ctx.moveTo(X(pts[0].t),h-pad);
      pts.forEach(p=>ctx.lineTo(X(p.t),p.up?Y(p.ms):h-pad));
      ctx.lineTo(X(pts[pts.length-1].t),h-pad);ctx.closePath();
      const grad=ctx.createLinearGradient(0,0,0,h);grad.addColorStop(0,'rgba(59,130,246,.30)');grad.addColorStop(1,'rgba(59,130,246,0)');
      ctx.fillStyle=grad;ctx.fill();
      // line
      ctx.beginPath();let started=false;
      pts.forEach(p=>{if(!p.up){started=false;return;}const px=X(p.t),py=Y(p.ms);if(!started){ctx.moveTo(px,py);started=true;}else ctx.lineTo(px,py);});
      ctx.strokeStyle='#4f9dff';ctx.lineWidth=2;ctx.lineJoin='round';ctx.stroke();
      // down markers
      pts.forEach(p=>{if(!p.up){ctx.fillStyle='var(--red)';ctx.fillStyle='#f2495c';ctx.fillRect(X(p.t)-1,h-pad-3,2,3);}});
      // leading dot
      const last=pts[pts.length-1];
      if(last.up){const lx=X(last.t),ly=Y(last.ms);
        ctx.fillStyle='#4f9dff';ctx.beginPath();ctx.arc(lx,ly,3.2,0,6.283);ctx.fill();
        ctx.globalAlpha=.25;ctx.beginPath();ctx.arc(lx,ly,7,0,6.283);ctx.fill();ctx.globalAlpha=1;}
    }
    if(hoverX!=null&&buf.length){
      const t=now-WIN*(w-hoverX)/(w-pad);let bi=0,bd=1e18;
      buf.forEach((p,i)=>{const dd=Math.abs(p.t-t);if(dd<bd){bd=dd;bi=i;}});
      const p=buf[bi],hx=X(p.t),hy=p.up?Y(p.ms):h-pad;
      if(hx>=0&&hx<=w){
        ctx.strokeStyle='rgba(255,255,255,.28)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(hx,0);ctx.lineTo(hx,h);ctx.stroke();
        ctx.fillStyle=p.up?'#4f9dff':'#f2495c';ctx.beginPath();ctx.arc(hx,hy,4,0,6.283);ctx.fill();
        ctx.strokeStyle='#0b0d10';ctx.lineWidth=2;ctx.stroke();
        tip.style.display='block';
        tip.innerHTML='<b>'+fmtS(p.t)+'</b><br>'+(p.up?(p.ms+' ms'):'<span style="color:#f2495c">unreachable</span>');
      } else tip.style.display='none';
    }
    L.raf=requestAnimationFrame(draw);
  }
  draw();
}

async function select(sid){
  SEL=sid;document.querySelectorAll('.card').forEach(c=>c.classList.toggle('sel',c.dataset.sid===sid));
  const d=await(await fetch('/api/system/'+sid)).json(),f=d.findings,salOK=f.sal_status===0;
  const fN=(v,warn,crit)=>`<div class="finding"><div class="n" style="color:${v>=crit?'var(--red)':v>=warn?'var(--amber)':'var(--green)'}">${v}</div>`;
  $('#detail').innerHTML=`
    <div class="hero ${d.color}"><span class="sid">${H(d.sid)}</span>
      <div class="meta"><b>${H(d.descr)}</b> · <span class="tag ${d.color}">${d.badge}</span>
        <div class="l2">${H(d.host)} · ${H(d.stype)} · ${ENVL(d.env)} · last check ${ago(d.age)}</div>
        ${d.sld?`<div class="l3">${[d.sld.product,'rel '+(d.sld.sys_release||'?'),[d.sld.db_type,d.sld.db_release].filter(Boolean).join(' '),d.sld.ram_mb?Math.round(d.sld.ram_mb/1024)+' GB RAM':'',d.sld.os_release].filter(Boolean).map(H).join(' · ')}</div>`:''}</div>
      <div class="act"><button class="btn" onclick="select('${d.sid}')">↻ Re-check</button></div></div>
    <div class="live"><h3>LIVE LATENCY · TCP PROBE <span style="color:var(--faint);font-weight:400">2s cadence · 90s window</span>
        <span class="now"><span id="livenow">…</span><span class="unit">ms</span></span></h3>
      <canvas id="livec" class="livec"></canvas><div id="livetip" class="chart-tip" style="display:none"></div><div class="livelbl">now ←</div></div>
    <div class="grid">
      <div class="pane col7" style="position:relative"><h3>AVAILABILITY &amp; RESPONSE (6h)<span class="src">${H(d.avail_detail)}</span></h3>
        ${d.trend.length?'<canvas id="trendc" class="trendc"></canvas><div id="trendtip" class="chart-tip" style="display:none"></div>':'<div class="empty">No probe history in window.</div>'}
        <div class="trend-foot"><span id="trendrange">${d.trend.length} probes</span><span>collector: ${d.resp==null?'n/a':d.resp+' ms'}</span></div></div>
      <div class="pane col5"><h3>OPEN FINDINGS</h3>
        <div class="win">⏱ collected ${d.collected?fmtS(d.collected):'—'}</div>
        <div class="findings">
        ${fN(f.jobs,1,50)}<div class="lb">Aborted jobs (24h)</div></div>
        ${fN(f.dumps,1,5)}<div class="lb">Short dumps (today)</div></div>
        ${fN(f.updates,1,1)}<div class="lb">Stuck update records</div></div>
        ${fN(f.locks,999,9999)}<div class="lb">Lock entries</div></div></div>
        <div class="sal-note">SAL · ${salOK?'active':H(f.sal_text)}</div></div>
      <div class="pane col12"><h3>STORAGE / FILESYSTEMS<span class="src">${H(d.host)} · df</span></h3>
        <div class="win">${winLabel('STORAGE',d.times.STORAGE)}</div>
        ${storageBars(d.lists.STORAGE)}</div>
      ${landscape(d.sld)}
      <div class="pane col6"><h3>FAILED / ABORTED JOBS<span class="src">SM37</span></h3>
        <div class="win">${winLabel('JOBS_ABORTED',d.times.JOBS_ABORTED)}</div>
        ${tbl(['Job','User','Start','End','Dur'],d.lists.JOBS_ABORTED,'No aborted jobs in the last 24h.')}</div>
      <div class="pane col6"><h3>LOCK ENTRIES<span class="src">SM12</span></h3>
        <div class="win">${winLabel('LOCKS',d.times.LOCKS)}</div>
        ${tbl(['User','Object','Client','Arg'],d.lists.LOCKS,'No lock entries held.')}</div>
      <div class="pane col4"><h3>SHORT DUMPS<span class="src">ST22</span></h3>
        <div class="win">${winLabel('DUMPS',d.times.DUMPS)}</div>
        ${tbl(['Time','User','Client','Host'],d.lists.DUMPS,'No dumps today.')}</div>
      <div class="pane col4"><h3>STUCK UPDATES<span class="src">SM13</span></h3>
        <div class="win">${winLabel('UPD_RECORDS',d.times.UPD_RECORDS)}</div>
        ${tbl(['Update'],d.lists.UPD_RECORDS,'No pending update records.')}</div>
      <div class="pane col4"><h3>SECURITY AUDIT LOG<span class="src">SM20 / SAL</span></h3>
        <div class="win">${winLabel('SAL',d.times.SAL)}</div>
        ${tbl(['Event'],d.lists.SAL,salOK?'No flagged audit events.':H(f.sal_text))}</div>
    </div>`;
  drawTrend(d.trend);
  startLive(sid);
}
async function load(){OV=await(await fetch('/api/overview')).json();renderStrip();}
$('#envf').onchange=renderStrip;
load();setInterval(load,30000);
let t=30;setInterval(()=>{t=t<=1?30:t-1;$('#clock').textContent='live · '+t+'s';},1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            p = self.path.split("?", 1)[0]
            if p == "/" or p.startswith("/index"):
                return self._send(200, HTML.encode(), "text/html; charset=utf-8")
            if p == "/healthz":
                return self._send(200, b'{"ok":true}', "application/json")
            if p == "/api/overview":
                return self._send(200, json.dumps(overview()).encode(), "application/json")
            if p.startswith("/api/system/"):
                d = system_detail(p.rsplit("/", 1)[-1][:8].upper())
                if d is None:
                    return self._send(404, b'{"error":"unknown sid"}', "application/json")
                return self._send(200, json.dumps(d).encode(), "application/json")
            if p.startswith("/api/ping/"):
                return self._send(200, json.dumps(ping(p.rsplit("/", 1)[-1][:8].upper())).encode(), "application/json")
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
