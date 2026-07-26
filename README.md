# SAP Landscape Dashboard

A lightweight, self-hosted operator dashboard for an SAP (or any) system
landscape. A single-page app renders a **card wall** of every system grouped by
Development / QA / Production, with per-system drill-down: live latency,
availability history, open findings (dumps, aborted jobs, locks, stuck updates,
security-audit gaps), filesystem utilization, and an SLD-style inventory pane.

It is deliberately small: **one Python file** (stdlib HTTP server + `psycopg2`),
PostgreSQL, and nginx — no framework, no build step, no JS bundler. The webapp is
a **pure read model**: it only `SELECT`s from six tables. Anything that can write
those rows can drive it.

```
┌────────── producers (pluggable) ──────────┐        ┌──── read model ────┐
│  SAP RFC collector   │  SLD ingest (RZ70)  │        │  webapp (app.py)   │
│  your own script     │  plain INSERTs      │──────▶ │  stdlib HTTP + PG  │◀── nginx ◀── browser
└──────────────────────┴─────────────────────┘  PG    └────────────────────┘
                          PostgreSQL (6 tables)
```

## Quick start (demo data, ~1 min)

Requires Docker + Docker Compose.

```bash
git clone https://github.com/adam0thman/sap-landscape-dashboard
cd sap-landscape-dashboard
docker compose up -d --build      # postgres auto-loads schema + anonymized demo data
```

Open **http://localhost:8080**. You get a fictional 6-system landscape (Dev/QA/Prod,
mixed health) so every panel has something to show. The demo data lives in
[`db/seed_demo.sql`](db/seed_demo.sql) — 100% fake (`*.example.corp`, `10.20.x.x`).

To wipe the demo and start clean: `docker compose down -v` then bring it back up
and feed your own rows.

## What it shows

- **Card wall** grouped D/Q/P, each card coloured by worst check (green/amber/red),
  with AVAIL · RESP · JOBS✗ · FS · SAL metrics and a status badge.
- **Live latency chart** — a real TCP probe every 2s to the selected system,
  auto-scrolling, with a hover crosshair + tooltip.
- **6h availability chart** — time axis, hover tooltip (exact ms per probe).
- **Open findings** — aborted jobs, short dumps, stuck updates, lock entries, SAL.
- **Drill-down tables** with the snapshot window stated per pane (SM37/ST22/SM12/SM13/SM20).
- **Storage / filesystems** — per-mount utilization bars.
- **Landscape / Inventory** — product, release, DB type/version, RAM, kernel,
  support-package stack, clients, and installed software components.

## How to feed your own data

The dashboard reads six PostgreSQL tables. **Start here:**

- **[docs/DATA_MODEL.md](docs/DATA_MODEL.md)** — the complete data contract: every
  table, column semantics, the per-check string formats, and how each maps to the
  UI. *This is the file to hand to an AI assistant.*
- **[docs/FEEDING_DATA.md](docs/FEEDING_DATA.md)** — producer recipes (SQL, Python,
  the reference SAP collector, the SLD ingest).
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit + deploy notes.
- **[docs/AI_CONTEXT.md](docs/AI_CONTEXT.md)** — a single self-contained brief you can
  paste into an LLM so it understands the whole system.

Minimal "make a green card appear":

```sql
INSERT INTO systems (sid, env, stype, host, sysnr, client, descr)
VALUES ('DEV','D','ABAP','app-dev.example.corp','00','100','My Dev System');
INSERT INTO jco_results (sid, check_name, status, detail, env)
VALUES ('DEV','AVAIL',0,'logon ok 12ms','D');
INSERT INTO uptime (sid, reachable, logon_ok, response_ms, env)
VALUES ('DEV',1,1,12,'D');
```

## Repository layout

```
webapp/           the SPA — app.py (stdlib HTTP + psycopg2), Dockerfile, requirements
nginx/            reverse-proxy config
db/               schema.sql (commented DDL) + seed_demo.sql (anonymized demo data)
collector/        reference producers:
                    collector.py      SAP RFC collector (pyrfc) — checks + storage via SSH df
                    sld_ingest.py     HTTP listener for SAP SLD data-supplier (RZ70) pushes
                    systems.example.json, retention.sh
docs/             DATA_MODEL, FEEDING_DATA, ARCHITECTURE, AI_CONTEXT
scripts/          seed.sh
```

## Producers included (optional, SAP-specific)

- `collector/collector.py` — connects to ABAP systems over **RFC** (needs the SAP
  NW RFC SDK + `pyrfc`) and writes AVAIL/LOCKS/DUMPS/JOBS_ABORTED/UPD_RECORDS/SAL,
  plus filesystem utilization via SSH `df`. It is config-driven by `systems.json`;
  no hosts or credentials are baked in. Treat it as a reference to adapt.
- `collector/sld_ingest.py` — a tiny HTTP endpoint that catches SAP SLD data-supplier
  pushes (`/sld/ds`) and upserts `sld_systems`.

You do **not** need these — any script that writes the six tables works. They ship
so SAP shops have a working starting point.

## Status

Early and evolving — improved incrementally. Issues and PRs welcome.
Licensed [MIT](LICENSE).
