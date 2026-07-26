# Architecture

## Components

| Component | What | Tech |
|---|---|---|
| **webapp** | the SPA + JSON API | one Python file (`app.py`) using the stdlib `http.server` + `psycopg2`. No framework/build. Serves the HTML at `/` and JSON under `/api/*`. |
| **postgres** | the store (read model) | PostgreSQL 16. Six tables (see [DATA_MODEL.md](DATA_MODEL.md)). |
| **nginx** | reverse proxy | proxies `/` → `webapp:8080`. TLS terminates here if you add it. |
| **producers** | fill the tables | the reference SAP collector, the SLD ingest, or anything you write. |

```
browser ──▶ nginx :80/443 ──▶ webapp :8080 ──▶ postgres :5432
                                   ▲                 ▲
                                   │ /api/ping        │ INSERT/UPSERT
                              live TCP probe      producers (collector, sld_ingest, your script)
                              to SAP host:port
```

## Data flow

1. A **producer** writes rows: append `jco_results`/`uptime`, refresh the
   `jco_details` snapshot, upsert `systems`/`sld_systems`.
2. The **webapp** answers `/api/overview` (latest per `sid,check_name` → card wall)
   and `/api/system/<SID>` (detail panes). It also serves `/api/ping/<SID>`, a
   *live* TCP connect to the system's `host:port` — this bypasses the DB so the
   scrolling latency chart is real-time.
3. The browser polls `/api/overview` every 30s and `/api/ping` every 2s.

The webapp holds **no state** and does **no writes** — restart it anytime.

## Deployment topologies

**All-in-one (this repo's `docker-compose.yml`)** — postgres + webapp + nginx as
containers on one host. Postgres auto-loads `schema.sql` + `seed_demo.sql` on first
boot. Good for evaluation and small landscapes.

**Split producer (recommended for SAP RFC)** — run postgres + webapp + nginx as
containers, but run `collector.py` **natively** (cron), because `pyrfc` needs the
SAP NW RFC SDK shared libraries which are awkward to containerize. The native
collector connects to `127.0.0.1:5432` (publish the postgres port to localhost).
The webapp connects to postgres over the compose network. Nothing else changes.

## Ports

| Port | Who | Notes |
|---|---|---|
| 8080 (host) → 80 | nginx | the dashboard URL; change the host side freely |
| 5432 | postgres | published to `127.0.0.1` only, so a native collector can reach it |
| 50000 | `sld_ingest.py` | only if you use SLD push (RZ70 target) |
| 8080 (container) | webapp | internal; nginx proxies to it |

## Retention & log lifecycle

`jco_results` and `uptime` grow ~O(systems × checks) per cycle. `retention.sh`
(cron, daily) purges rows past N days and trims the `jco_details` snapshot. The
live-ping samples are **never stored** (browser-side only), so high-frequency
latency data never touches disk. Cap container logs (`json-file`, `max-size`) so
they can't fill the disk.

## Build note (webapp base image)

`webapp/Dockerfile` pins `python:3.11-slim-bullseye`. That's a compatibility choice
for **very old Docker (≤ 19.03)** whose default seccomp profile blocks the
`clone3()` syscall used by newer glibc — which manifests as `RuntimeError: can't
start new thread`. On modern Docker any `python:3.x-slim` works; keep bullseye if
you might run on legacy hosts.

## Scaling / limits

- The webapp is single-process, threaded per request — fine for a few operators
  and dozens–hundreds of systems. It is not built for thousands of concurrent
  users; put it behind auth and it's an internal tool.
- The heavy query ("latest per `sid,check_name`") is indexed
  (`jco_results_latest_idx`). At very large scale, add a materialized "latest"
  view refreshed by the producer.
- No auth is built in. Front it with nginx basic-auth / SSO / mTLS as appropriate;
  it is designed to sit on an internal/management network.
