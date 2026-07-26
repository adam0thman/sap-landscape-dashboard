-- ============================================================================
-- SAP Landscape Dashboard — PostgreSQL schema
-- ============================================================================
-- The dashboard (webapp) is a *read model*: it only SELECTs from these tables.
-- Any producer (the reference SAP-RFC collector, the SLD ingest, your own
-- script, or a plain INSERT) can populate them. Nothing in the webapp is
-- SAP-specific beyond these column semantics — feed it and it renders.
--
-- Conventions used everywhere:
--   status  integer   0 = OK/green · 1 = warning/amber · 2 = critical/red · 3 = stale/n-a/grey
--   env     text      'D' = Development · 'Q' = Quality/QA · 'P' = Production (drives the 3 sections)
--   sid     text      3-char SAP System ID (or any short system key), e.g. 'EAP'
--   ts      timestamptz  event time (UTC stored; the UI renders in the browser's zone)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- systems — the registry: one row per monitored system. The dashboard lists
-- every row here as a card. Upserted by the collector, or insert manually.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS systems (
    sid    text PRIMARY KEY,     -- system key, e.g. 'EAP'
    env    text,                 -- 'D' | 'Q' | 'P'
    stype  text,                 -- system type: ABAP | JAVA | HANA | BOBJ | WEBDISP | ...
    host   text,                 -- primary app-server host or IP (used by the live TCP probe)
    sysnr  text,                 -- SAP instance number (ABAP), e.g. '00' (probe port = 3200+sysnr)
    client text,                 -- default client (ABAP), e.g. '100'
    descr  text                  -- human label, e.g. 'S/4HANA Production'
);
COMMENT ON TABLE  systems IS 'Registry of monitored systems; one card per row.';
COMMENT ON COLUMN systems.stype IS 'ABAP systems get RFC checks; others get a TCP-port probe.';

-- ---------------------------------------------------------------------------
-- jco_results — APPEND-ONLY time series of check results. The collector inserts
-- one row per (sid, check_name) every run. The dashboard reads the *latest* row
-- per (sid, check_name) for current status, and the max(status) over a system's
-- checks decides the card colour.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jco_results (
    ts         timestamptz DEFAULT now(),
    sid        text,
    check_name text,            -- AVAIL | LOCKS | DUMPS | JOBS_ABORTED | UPD_RECORDS | SAL | STORAGE
    status     integer,         -- 0/1/2/3
    detail     text,            -- one-line summary shown as the check's headline (see DATA_MODEL.md)
    env        text
);
COMMENT ON TABLE jco_results IS 'Append-only check history; latest row per (sid,check_name) = current state.';

-- ---------------------------------------------------------------------------
-- jco_details — CURRENT-SNAPSHOT detail rows behind each check (the drill-down
-- tables). The reference collector TRUNCATES this table at the start of every
-- run and re-inserts, so it always reflects "now". Each `item` is a set of
-- columns joined by TWO-OR-MORE spaces; the UI splits on /\s{2,}/.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jco_details (
    ts         timestamptz DEFAULT now(),
    env        text,
    sid        text,
    check_name text,
    item       text             -- e.g. 'Z_JOB  BATCHUSER  07-26 03:47  03:47  0:00:00'
);
COMMENT ON TABLE jco_details IS 'Current-snapshot drill-down rows; wiped+rewritten each collector run. item = 2-space-delimited columns.';

-- ---------------------------------------------------------------------------
-- uptime — response-time / reachability time series. Feeds the 6h availability
-- chart and the RESP metric on the cards.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS uptime (
    ts          timestamptz DEFAULT now(),
    sid         text,
    reachable   integer,        -- 1 = reachable, 0 = down (red markers on the chart)
    logon_ok    integer,        -- 1 = full logon succeeded (ABAP), 0 = TCP-only/failed
    response_ms integer,        -- probe/logon latency in ms
    env         text
);
COMMENT ON TABLE uptime IS 'Reachability + latency time series; source of the 6h availability chart.';

-- ---------------------------------------------------------------------------
-- sld_systems — landscape INVENTORY (one row per sid). Populated from the SAP
-- SLD data supplier (RZ70 push) or by hand. Drives the hero line + the
-- Landscape/Inventory pane. Not time series — it's the current facts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sld_systems (
    sid            text NOT NULL,          -- SAP System ID
    system_home    text NOT NULL DEFAULT '', -- central/message-server host; disambiguates same-SID copies
    install_no     text,                   -- installation/license number (may be identical on a copy)
    source_ip      text,                   -- IP that pushed this payload (audit)
    sys_release    text,         -- e.g. '756'
    sys_number     text,
    db_schema      text,         -- e.g. 'SAPHANADB' / 'SAPSR3'
    tms_domain     text,
    license_exp    text,         -- 'YYYYMMDD' ('99991231' = never)
    product        text,         -- e.g. 'SAP S/4HANA 2022'
    sp_stack       text,
    db_name        text,
    db_type        text,         -- HDB | SYB | ORA | MSS | DB6 ...
    db_release     text,
    db_vendor      text,
    db_host        text,         -- SystemHome; for remote HANA this is the DB host to df
    fqdn           text,
    ip             text,
    os             text,         -- 'Linux'
    os_release     text,         -- kernel, e.g. '5.14.21-150400'
    ram_mb         integer,      -- physical RAM in MB
    app_servers    integer,
    clients        integer,
    components     text,         -- JSON array: [{"name":"SAP_BASIS","version":"756"}, ...]
    appserver_list text,         -- JSON array: [{"inst":"...","nr":"00","host":"..."}]
    updated        timestamptz DEFAULT now(),
    PRIMARY KEY (sid, system_home)   -- identity = SID + host, so a system and its copy/POC don't collide
);
COMMENT ON TABLE sld_systems IS 'Per-system landscape inventory (release, DB, RAM, components...). Keyed (sid, system_home). components/appserver_list are JSON text.';


-- Host inventory from the SAP Host Agent (sldreg, <sapdata type="ComputerSystem">).
-- Covers ANY host including bare-metal DB hosts, no RFC/SSH needed. Keyed by host name.
CREATE TABLE IF NOT EXISTS sld_hosts (
    host          text PRIMARY KEY,   -- short hostname
    fqdn          text,
    ip            text,
    cpu_type      text,               -- e.g. 'Intel(R) Xeon(R) Gold 6238R CPU @ 2.20GHz'
    cpu_count     int,
    cpu_rate      int,                -- MHz
    ram_mb        int,                -- PhysicalRAMInMB
    vram_mb       int,                -- VirtualRAMInMB
    os            text,               -- e.g. 'LINUX_X86_64'
    os_release    text,               -- e.g. 'SUSE Linux Enterprise Server 15 SP3'
    os_kernel     text,               -- uname, e.g. '5.3.18-57-default'
    os_bits       int,
    manufacturer  text,               -- e.g. 'VMware, Inc.'
    machine_type  text,               -- e.g. 'VMware7,1'
    virt_info     text,               -- e.g. 'VMware ESX 7.0.3 build-24585291'
    hardware_id   text,
    status        text,
    source_ip     text,
    updated       timestamptz DEFAULT now()
);
COMMENT ON TABLE sld_hosts IS 'Per-host hardware/OS inventory from the SAP Host Agent (sldreg ComputerSystem payload).';

-- ---------------------------------------------------------------------------
-- backups — optional backup-monitoring rows (latest per sid/backup_type).
-- Reserved for the backup pane; safe to leave empty.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backups (
    ts          timestamptz DEFAULT now(),
    sid         text,
    db_type     text,
    backup_type text,            -- FULL | INCR | LOG | ...
    status      integer,         -- 0/1/2/3
    size_bytes  bigint,
    oldest_kept timestamptz,     -- retention floor
    path        text
);
COMMENT ON TABLE backups IS 'Optional backup-run monitoring; latest row per (sid,backup_type).';

-- ---------------------------------------------------------------------------
-- Indexes — the hot query is "latest row per (sid,check_name)" and "last 6h of
-- uptime per sid". These make both cheap as history grows.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS jco_results_latest_idx ON jco_results (sid, check_name, ts DESC);
CREATE INDEX IF NOT EXISTS jco_results_ts_idx     ON jco_results (ts);
CREATE INDEX IF NOT EXISTS uptime_sid_ts_idx      ON uptime (sid, ts);
CREATE INDEX IF NOT EXISTS jco_details_lookup_idx ON jco_details (sid, check_name);
