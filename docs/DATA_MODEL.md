# Data Model & Data Contract

This is the complete specification of the data the dashboard reads. The webapp
is a **pure read model** — it only runs `SELECT`s. If you populate these six
PostgreSQL tables with rows shaped as described here, the dashboard renders.
There is no hidden coupling to SAP beyond these column meanings; a Kubernetes
cluster, a database fleet, or anything else could feed the same shapes.

> TL;DR for an AI assistant: to add a system, insert one row into `systems`;
> then per health metric insert/append a row into `jco_results`
> (`status` 0/1/2/3 + a one-line `detail`), optionally push drill-down rows into
> `jco_details` (columns joined by **two spaces**), and append latency samples
> into `uptime`. `sld_systems` holds static inventory. See the [seed](../db/seed_demo.sql).

---

## Global conventions

| Field | Type | Meaning |
|---|---|---|
| `status` | `integer` | **0** = OK (green) · **1** = warning (amber) · **2** = critical (red) · **3** = stale / not-applicable (grey) |
| `env` | `text` | **`D`** Development · **`Q`** Quality/QA · **`P`** Production — drives the three dashboard sections and the env filter |
| `sid` | `text` | system key (SAP 3-char SID or any short id), e.g. `EAP` |
| `ts` | `timestamptz` | event time. Store UTC; the browser renders local time |

**A system's card colour = `max(status)` across its latest check per `check_name`.**
So one `status=2` row anywhere turns the card red.

---

## Tables

### 1. `systems` — registry (1 row per system)
The list of things to show. Every row becomes a card.

| column | type | notes |
|---|---|---|
| `sid` | text PK | `EAP` |
| `env` | text | `D`/`Q`/`P` |
| `stype` | text | `ABAP`, `JAVA`, `HANA`, `BOBJ`, `WEBDISP`, … (free text; shown on the card) |
| `host` | text | primary host/IP — used by the **live TCP probe** endpoint |
| `sysnr` | text | SAP instance nr (ABAP); the live probe hits `3200+sysnr` |
| `client` | text | default client (informational) |
| `descr` | text | human label, e.g. `S/4HANA Production` |

### 2. `jco_results` — check results (append-only time series)
One row per `(sid, check_name)` per collection cycle. The UI reads the **latest**
row per pair.

| column | type | notes |
|---|---|---|
| `ts` | timestamptz | default `now()` |
| `sid` | text | |
| `check_name` | text | one of the checks below |
| `status` | int | 0/1/2/3 |
| `detail` | text | one-line summary shown as the check headline; the card also parses the **first integer** out of it for the numeric metric (e.g. `7 aborted jobs` → `7`) |
| `env` | text | |

### 3. `jco_details` — drill-down snapshot (current state)
The rows behind each check (the detail tables). **Wiped and rewritten every
cycle** by the reference collector, so it is always "now" — it is *not* history.

| column | type | notes |
|---|---|---|
| `ts`, `env`, `sid`, `check_name` | | as above |
| `item` | text | **columns joined by two-or-more spaces.** The UI splits on `/\s{2,}/` and maps to the headers for that check (below). |

### 4. `uptime` — latency / reachability (time series)
Feeds the 6-hour availability chart and the `RESP` card metric.

| column | type | notes |
|---|---|---|
| `ts` | timestamptz | |
| `sid` | text | |
| `reachable` | int | 1 up / 0 down (0 draws a red marker at the baseline) |
| `logon_ok` | int | 1 = full logon, 0 = TCP-only or failed |
| `response_ms` | int | latency in ms (0 when down) |
| `env` | text | |

### 5. `sld_systems` — inventory (1 row per system instance, upsert)
Static facts about each system (from an SAP SLD push or entered by hand). Drives
the hero line and the **Landscape / Inventory** pane. See columns in
[`schema.sql`](../db/schema.sql). Two columns are **JSON stored as text**:
- `components` → `[{"name":"SAP_BASIS","version":"756"}, …]`
- `appserver_list` → `[{"inst":"…","nr":"00","host":"…"}]`

**Identity = `(sid, system_home)`, not `sid` alone.** A system and its copy/POC
share a SID but live on different hosts (`system_home`), so keying on SID alone
would make them overwrite each other. `install_no` (installation number) and
`source_ip` (who pushed) are also recorded — note an installation number can be
*identical* on a copy, which is why the host is the real disambiguator. The
capture-first ingest (`collector/sld_ingest.py`) archives every raw push, only
upserts known payload types, and supports an optional allowlist
(`SLD_ALLOW_FILE`, lines of `SID` or `SID@host`) so stray/garbage senders are
captured-only, never stored.

### 6. `backups` — optional backup monitoring
Latest row per `(sid, backup_type)`. Safe to leave empty.

---

## Checks (`check_name`) and their string formats

The dashboard is generic, but it knows how to render these seven check names. You
can invent your own — unknown checks still show their `detail` and (if present)
`jco_details` rows under a generic table; only the column headers below are
check-specific.

| `check_name` | `detail` example | first-int metric | `jco_details.item` columns (2-space delimited) |
|---|---|---|---|
| `AVAIL` | `logon ok 14ms` / `JAVA host:50000 closed` | – (UP/DOWN) | `AVAIL` probe note (free text) |
| `LOCKS` | `12 lock entries` | 12 | `User  Object  Client  Arg` |
| `DUMPS` | `2 dumps today` | 2 | `Time  User  Client  Host` |
| `JOBS_ABORTED` | `7 aborted jobs (24h)` | 7 | `Job  User  Start  End  Duration` |
| `UPD_RECORDS` | `0 pending update recs` | 0 | `Update` (free text) |
| `SAL` | `21 SAL events (0 crit)` | 21 | `Event` (free text) |
| `STORAGE` | `max 84% (/hana/data)` | 84 | `Mount  SizeBytes  UsedBytes  Pct` |

**STORAGE detail rows** are special: the UI renders them as utilization bars, so
the four columns must be `mount`, `size_bytes`, `used_bytes`, `pct` (integers for
the last three). Example item: `/hana/data  1099511627776  571230650368  52`.

**Card metrics** (the four/five small numbers on each card) are derived:
`AVAIL` → UP/DOWN + `RESP` (latest `uptime.response_ms`); `JOBS✗` → first-int of
the `JOBS_ABORTED` detail; `FS` → first-int of the `STORAGE` detail; `SAL` →
OK/`!` from the SAL status.

---

## How the webapp reads it (API surface)

| endpoint | query | used for |
|---|---|---|
| `GET /api/overview` | latest row per `(sid,check_name)` from `jco_results`, joined to `systems`; worst status per sid | the card wall + counts |
| `GET /api/system/<SID>` | latest per check, last 6h of `uptime`, all current `jco_details`, the `sld_systems` row | the detail view (panes, chart, inventory) |
| `GET /api/ping/<SID>` | **no DB** — a live TCP connect to the system's `host:port` (`3200+sysnr` for ABAP, else the port) | the live auto-scrolling latency chart |
| `GET /healthz` | – | container health |

The "latest per pair" query is literally:
```sql
SELECT DISTINCT ON (sid, check_name) sid, check_name, status, detail, ts
FROM jco_results ORDER BY sid, check_name, ts DESC;
```

---

## Minimal example: make one green card appear

```sql
INSERT INTO systems (sid, env, stype, host, sysnr, client, descr)
VALUES ('DEV','D','ABAP','app-dev.example.corp','00','100','My Dev System');

INSERT INTO jco_results (sid, check_name, status, detail, env) VALUES
 ('DEV','AVAIL',0,'logon ok 12ms','D'),
 ('DEV','JOBS_ABORTED',0,'0 aborted jobs (24h)','D');

INSERT INTO uptime (sid, reachable, logon_ok, response_ms, env)
VALUES ('DEV',1,1,12,'D');
```
Refresh the dashboard — a green `DEV` card appears under Development. Everything
else (drill-downs, inventory, storage bars) is additive: add the rows when you
have the data. See [FEEDING_DATA.md](FEEDING_DATA.md) for producer patterns.
