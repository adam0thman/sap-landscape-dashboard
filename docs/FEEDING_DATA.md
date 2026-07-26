# Feeding Data

The dashboard reads six tables (see [DATA_MODEL.md](DATA_MODEL.md)). Here are the
common ways to fill them. Pick any — they are not mutually exclusive.

## 1. Plain SQL / cron

The simplest producer is a script that `INSERT`s. A "current state" refresh looks
like: append a `jco_results` row per check, replace the `jco_details` snapshot,
append an `uptime` sample.

```sql
-- one refresh cycle for system 'EAP'
INSERT INTO jco_results (sid, check_name, status, detail, env) VALUES
 ('EAP','AVAIL',0,'logon ok 31ms','P'),
 ('EAP','JOBS_ABORTED',1,'7 aborted jobs (24h)','P');

-- drill-down is a current snapshot: clear this system's rows, re-insert
DELETE FROM jco_details WHERE sid='EAP' AND check_name='JOBS_ABORTED';
INSERT INTO jco_details (sid, check_name, item, env) VALUES
 ('EAP','JOBS_ABORTED','Z_BILLING_RUN  BATCHUSER  07-26 03:47:12  03:52:40  0:05:28','P');

INSERT INTO uptime (sid, reachable, logon_ok, response_ms, env)
VALUES ('EAP',1,1,31,'P');
```

> The reference collector clears the **whole** `jco_details` table each run
> (`DELETE FROM jco_details;`) because it refreshes every system at once. If you
> update systems independently, delete per `(sid, check_name)` instead, as above.

## 2. Python

```python
import psycopg2
db = psycopg2.connect(host="127.0.0.1", dbname="sapmon", user="dashboard", password="…")
db.autocommit = True
cur = db.cursor()

def result(sid, check, status, detail, env):
    cur.execute("INSERT INTO jco_results(sid,check_name,status,detail,env) VALUES(%s,%s,%s,%s,%s)",
                (sid, check, status, detail, env))

def detail(sid, check, item, env):                       # columns joined by TWO spaces
    cur.execute("INSERT INTO jco_details(sid,check_name,item,env) VALUES(%s,%s,%s,%s)",
                (sid, check, item[:250], env))

result('EAP', 'DUMPS', 1, '2 dumps today', 'P')
detail('EAP', 'DUMPS', '03:47:15  BATCHUSER  100  app-eap-01', 'P')
```

## 3. Reference SAP RFC collector (`collector/collector.py`)

Config-driven by `systems.json`. For each ABAP system it opens an RFC connection
(`pyrfc`, needs the **SAP NW RFC SDK**) and runs:

| check | source |
|---|---|
| AVAIL | RFC logon + timing |
| LOCKS | `ENQUEUE_READ` |
| DUMPS | `RFC_READ_TABLE` on `SNAP` (today) |
| JOBS_ABORTED | `RFC_READ_TABLE` on `TBTCO` (status A, last 24h) |
| UPD_RECORDS | `RFC_READ_TABLE` on `VBHDR` |
| SAL | `RSAU_READ_LOG` (Security Audit Log; needs Basis ≥ 7.50) |
| STORAGE | `ssh <host> df -PB1` (non-ABAP get a TCP-port probe for AVAIL) |

Run it on a schedule (cron every 5 min). It reads DB + RFC passwords from files
(`secrets/…`) — never hard-code credentials. Adapt the checks to your systems.

```
*/5 * * * * root LD_LIBRARY_PATH=/opt/nwrfcsdk/lib python3 /opt/collector.py >/var/log/collect.log 2>&1
```

## 4. SLD inventory (`collector/sld_ingest.py`)

An HTTP listener on `:50000` that catches SAP **SLD Data Supplier** pushes (from
transaction `RZ70`, HTTP target `http://<host>:50000/sld/ds`). It parses the
`<sapdata>` CIM document and upserts one `sld_systems` row per system — release,
DB type/version/host, RAM, kernel, support-package stack, installed components.
No host access or credentials required; the ABAP system pushes to you. Replay a
captured payload with `python3 sld_ingest.py --replay <file>`.

You can equally fill `sld_systems` by hand — it is just inventory.

## 5. Retention (keep the DB bounded)

`jco_results` and `uptime` are append-only. `collector/retention.sh` purges rows
older than N days (default 90) and trims the `jco_details` snapshot. Run daily:

```
30 2 * * * root /opt/retention.sh 90 >/var/log/retention.log 2>&1
```

## Notes & gotchas

- **`status` is the only thing that colours the UI.** Get 0/1/2/3 right and the
  cards behave; `detail` is just the headline text (the card also parses its first
  integer for the numeric metric).
- **`jco_details.item` splits on two-or-more spaces** — use a single space inside a
  value (e.g. a job name) and two between columns.
- **STORAGE detail rows must be** `mount  size_bytes  used_bytes  pct` so the UI can
  draw utilization bars.
- **Live latency chart** reads `systems.json` (mounted into the webapp), *not* the
  DB, to know each system's `host:port`. Keep it in sync with the `systems` table.
