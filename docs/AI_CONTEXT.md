# AI Context Brief

Paste this whole file into an LLM to give it everything it needs to help you run,
feed, or extend this dashboard. It is self-contained.

## What this is

A self-hosted single-page dashboard for an SAP system landscape. Backend is one
Python file (`webapp/app.py`, stdlib `http.server` + `psycopg2`) that only reads
from PostgreSQL and serves HTML + JSON. Data comes from *producers* (a SAP RFC
collector, an SLD HTTP ingest, or any script) that write six tables. The webapp
does no writes and holds no state.

## The six tables (the entire contract)

- **`systems`**(`sid` PK, `env`, `stype`, `host`, `sysnr`, `client`, `descr`) —
  registry; one card per row.
- **`jco_results`**(`ts`, `sid`, `check_name`, `status`, `detail`, `env`) —
  append-only check history; UI uses the latest row per `(sid,check_name)`.
- **`jco_details`**(`ts`, `env`, `sid`, `check_name`, `item`) — current-snapshot
  drill-down rows; `item` = columns joined by **two-or-more spaces**.
- **`uptime`**(`ts`, `sid`, `reachable`, `logon_ok`, `response_ms`, `env`) —
  latency/reachability series; feeds the 6h chart.
- **`sld_systems`**(`sid` PK, … release/DB/RAM/kernel/components …) — static
  inventory; `components` & `appserver_list` are JSON stored as text.
- **`backups`**(`ts`, `sid`, `db_type`, `backup_type`, `status`, `size_bytes`,
  `oldest_kept`, `path`) — optional.

Full DDL with comments: `db/schema.sql`. Working example rows: `db/seed_demo.sql`.

## Conventions (memorize these)

- `status` integer: **0** green/OK · **1** amber/warning · **2** red/critical ·
  **3** grey/stale-or-n/a. A card's colour = `max(status)` over its latest checks.
- `env`: **`D`** / **`Q`** / **`P`** → the three dashboard sections.
- `ts` stored UTC; UI renders in the browser timezone.

## Check names and formats

`check_name` is one of `AVAIL`, `LOCKS`, `DUMPS`, `JOBS_ABORTED`, `UPD_RECORDS`,
`SAL`, `STORAGE` (you may add your own; unknown ones render generically).

`jco_results.detail` is a one-line headline; the card also parses its **first
integer** as the numeric metric. `jco_details.item` columns per check:

| check | detail example | item columns (2-space delimited) |
|---|---|---|
| AVAIL | `logon ok 14ms` | free text |
| LOCKS | `12 lock entries` | `User  Object  Client  Arg` |
| DUMPS | `2 dumps today` | `Time  User  Client  Host` |
| JOBS_ABORTED | `7 aborted jobs (24h)` | `Job  User  Start  End  Duration` |
| UPD_RECORDS | `0 pending update recs` | `Update` |
| SAL | `21 SAL events (0 crit)` | `Event` |
| STORAGE | `max 84% (/hana/data)` | `Mount  SizeBytes  UsedBytes  Pct` (last 3 are ints) |

## API (what the browser calls)

- `GET /api/overview` → all systems + latest status + counts (card wall).
- `GET /api/system/<SID>` → latest checks, last 6h `uptime`, current `jco_details`,
  the `sld_systems` row.
- `GET /api/ping/<SID>` → **live** TCP connect to the system's `host:port`
  (`3200+sysnr` for ABAP, else the port from `systems.json`); not from the DB.
- `GET /healthz`.

## To add / update a system (typical AI task)

```sql
-- 1. register it
INSERT INTO systems(sid,env,stype,host,sysnr,client,descr)
VALUES ('DEV','D','ABAP','app-dev.example.corp','00','100','My Dev');
-- 2. publish checks (append; UI reads latest)
INSERT INTO jco_results(sid,check_name,status,detail,env) VALUES
 ('DEV','AVAIL',0,'logon ok 12ms','D'),
 ('DEV','JOBS_ABORTED',1,'3 aborted jobs (24h)','D'),
 ('DEV','STORAGE',1,'max 88% (/usr/sap)','D');
-- 3. drill-down snapshot (clear+insert per sid/check)
DELETE FROM jco_details WHERE sid='DEV' AND check_name='STORAGE';
INSERT INTO jco_details(sid,check_name,item,env) VALUES
 ('DEV','STORAGE','/usr/sap  53687091200  47244640256  88','D');
-- 4. latency sample
INSERT INTO uptime(sid,reachable,logon_ok,response_ms,env) VALUES ('DEV',1,1,12,'D');
```

## Gotchas

- Only `status` colours the UI; `detail` is text.
- `jco_details.item` splits on `\s{2,}` — single space inside a value, two between columns.
- STORAGE rows must be `mount  size_bytes  used_bytes  pct`.
- The live chart reads `systems.json` (mounted into the webapp) for `host:port`,
  not the DB — keep them consistent.
- `jco_details` is a *snapshot* (wiped/rewritten), not history. `jco_results` and
  `uptime` are history (retention job trims them).
- Deploy: all-in-one via `docker compose up`, or run the RFC collector natively
  (pyrfc + SAP NW RFC SDK) writing to the same PostgreSQL. Webapp is stateless.
