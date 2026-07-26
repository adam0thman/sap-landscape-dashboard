-- ============================================================================
-- Demo / anonymized seed data for the SAP Landscape Dashboard
-- ============================================================================
-- 100% fictional: fake SIDs, hosts (*.example.corp), IPs (10.20.x.x). Loads a
-- believable 6-system landscape with varied health so the UI has something to
-- render. Safe to load into an empty schema:  psql < schema.sql && psql < seed_demo.sql
-- Re-runnable: it clears the demo rows first.
-- ============================================================================
BEGIN;

DELETE FROM jco_details; DELETE FROM jco_results; DELETE FROM uptime;
DELETE FROM sld_systems; DELETE FROM backups; DELETE FROM systems;

-- ---- registry (6 systems across Dev/QA/Prod) -------------------------------
INSERT INTO systems (sid, env, stype, host, sysnr, client, descr) VALUES
 ('EAD','D','ABAP','app-ead-01.example.corp','00','100','S/4HANA Dev'),
 ('EAQ','Q','ABAP','app-eaq-01.example.corp','01','100','S/4HANA QA'),
 ('EAP','P','ABAP','app-eap-01.example.corp','02','100','S/4HANA Production'),
 ('BID','D','ABAP','app-bid-01.example.corp','20','001','BW/4HANA Dev'),
 ('PIP','P','JAVA','app-pip-01.example.corp',NULL,NULL,'Process Integration (Prod, Java)'),
 ('SMP','P','ABAP','app-smp-01.example.corp','50','001','Solution Manager (Prod)');

-- ---- current check results (latest row per sid/check drives the cards) ------
-- status: 0 green, 1 amber, 2 red, 3 grey
INSERT INTO jco_results (sid, check_name, status, detail, env) VALUES
 -- EAD: all healthy
 ('EAD','AVAIL',0,'logon ok 14ms','D'),
 ('EAD','LOCKS',0,'3 lock entries','D'),
 ('EAD','DUMPS',0,'0 dumps today','D'),
 ('EAD','JOBS_ABORTED',0,'0 aborted jobs (24h)','D'),
 ('EAD','UPD_RECORDS',0,'0 pending update recs','D'),
 ('EAD','SAL',0,'4 SAL events (0 crit)','D'),
 ('EAD','STORAGE',0,'max 61% (/usr/sap)','D'),
 -- EAQ: storage amber
 ('EAQ','AVAIL',0,'logon ok 22ms','Q'),
 ('EAQ','LOCKS',0,'1 lock entries','Q'),
 ('EAQ','DUMPS',0,'0 dumps today','Q'),
 ('EAQ','JOBS_ABORTED',0,'0 aborted jobs (24h)','Q'),
 ('EAQ','UPD_RECORDS',0,'0 pending update recs','Q'),
 ('EAQ','SAL',1,'0 events today - verify SAL active (SM19)','Q'),
 ('EAQ','STORAGE',1,'max 84% (/hana/data)','Q'),
 -- EAP: aborted jobs amber, 2 dumps
 ('EAP','AVAIL',0,'logon ok 31ms','P'),
 ('EAP','LOCKS',0,'12 lock entries','P'),
 ('EAP','DUMPS',1,'2 dumps today','P'),
 ('EAP','JOBS_ABORTED',1,'7 aborted jobs (24h)','P'),
 ('EAP','UPD_RECORDS',0,'0 pending update recs','P'),
 ('EAP','SAL',0,'21 SAL events (0 crit)','P'),
 ('EAP','STORAGE',0,'max 72% (/)','P'),
 -- BID: SAL gap amber
 ('BID','AVAIL',0,'logon ok 18ms','D'),
 ('BID','LOCKS',0,'2 lock entries','D'),
 ('BID','DUMPS',0,'0 dumps today','D'),
 ('BID','JOBS_ABORTED',1,'4 aborted jobs (24h)','D'),
 ('BID','UPD_RECORDS',0,'0 pending update recs','D'),
 ('BID','SAL',1,'0 events today - verify SAL active (SM19)','D'),
 ('BID','STORAGE',0,'max 55% (/backup)','D'),
 -- PIP: DOWN (critical)
 ('PIP','AVAIL',2,'JAVA app-pip-01.example.corp:50000 closed','P'),
 ('PIP','STORAGE',0,'max 68% (/usr/sap)','P'),
 -- SMP: healthy (Sybase)
 ('SMP','AVAIL',0,'logon ok 41ms','P'),
 ('SMP','LOCKS',0,'5 lock entries','P'),
 ('SMP','DUMPS',0,'0 dumps today','P'),
 ('SMP','JOBS_ABORTED',0,'0 aborted jobs (24h)','P'),
 ('SMP','UPD_RECORDS',0,'0 pending update recs','P'),
 ('SMP','SAL',1,'SAL read FM n/a (rel<7.50) - audit via SM20','P'),
 ('SMP','STORAGE',1,'max 81% (/sybase)','P');

-- ---- drill-down snapshot rows (item = 2-space-delimited columns) ------------
INSERT INTO jco_details (sid, check_name, item, env) VALUES
 ('EAP','JOBS_ABORTED','Z_BILLING_RUN  BATCHUSER  07-26 03:47:12  03:52:40  0:05:28','P'),
 ('EAP','JOBS_ABORTED','SAP_REORG_JOBS  DDIC  07-26 01:00:03  01:00:07  0:00:04','P'),
 ('EAP','JOBS_ABORTED','Z_IDOC_DISPATCH  WF-BATCH  07-26 06:15:00  06:15:00  0:00:00','P'),
 ('EAP','DUMPS','03:47:15  BATCHUSER  100  app-eap-01','P'),
 ('EAP','DUMPS','11:02:41  MSMITH  100  app-eap-02','P'),
 ('EAP','LOCKS','BGRFC_SUPER  BGRFC_I_SERVER_REGISTRATION  100  app-eap-01','P'),
 ('EAP','LOCKS','WF-BATCH  SWWWIHEAD  100  000123456','P'),
 ('EAP','STORAGE','/  68719476736  49478023987  72','P'),
 ('EAP','STORAGE','/usr/sap  53687091200  30064771072  56','P'),
 ('EAP','STORAGE','/hana/data  1099511627776  571230650368  52','P'),
 ('EAQ','STORAGE','/hana/data  549755813888  461708984320  84','Q'),
 ('EAQ','STORAGE','/  68719476736  34359738368  50','Q'),
 ('BID','JOBS_ABORTED','RSBTCDEL2  DDIC  07-26 02:11:09  02:11:20  0:00:11','D'),
 ('BID','JOBS_ABORTED','BI_PROCESS_LOADING  ALEREMOTE  07-26 04:30:00  04:41:33  0:11:33','D'),
 ('SMP','STORAGE','/sybase  322122547200  260919263232  81','P'),
 ('SMP','STORAGE','/  42949672960  21474836480  50','P'),
 ('PIP','AVAIL','JAVA probe app-pip-01.example.corp:50000 = CLOSED','P');

-- ---- landscape inventory (SLD-style) ---------------------------------------
INSERT INTO sld_systems
 (sid,sys_release,sys_number,db_schema,tms_domain,license_exp,product,sp_stack,
  db_name,db_type,db_release,db_vendor,db_host,fqdn,ip,os,os_release,ram_mb,app_servers,clients,
  components,appserver_list) VALUES
 ('EAP','756','0021000123','SAPHANADB','DOMAIN_EAP','99991231','SAP S/4HANA 2022','06 (2023)',
  'EAP','HDB','2.00.071','SAP SE','db-eap-01.example.corp','app-eap-01.example.corp','10.20.30.11',
  'Linux','5.14.21-150400',262144,4,3,
  '[{"name":"SAP_BASIS","version":"756"},{"name":"S4CORE","version":"106"},{"name":"SAP_UI","version":"756"},{"name":"SAP_GWFND","version":"756"}]',
  '[{"inst":"app-eap-01_EAP_02","nr":"02","host":"app-eap-01.example.corp"}]'),
 ('EAD','756','0021000121','SAPHANADB','DOMAIN_EAP','99991231','SAP S/4HANA 2022','06 (2023)',
  'EAD','HDB','2.00.071','SAP SE','db-ead-01.example.corp','app-ead-01.example.corp','10.20.10.11',
  'Linux','5.14.21-150400',131072,1,5,
  '[{"name":"SAP_BASIS","version":"756"},{"name":"S4CORE","version":"106"},{"name":"SAP_UI","version":"756"}]',
  '[{"inst":"app-ead-01_EAD_00","nr":"00","host":"app-ead-01.example.corp"}]'),
 ('SMP','740','0021000199','SAPSR3','DOMAIN_SMP','99991231','SAP SOLUTION MANAGER 7.2','14 (2022)',
  'SMP','SYB','16.0.03.11','Sybase','app-smp-01.example.corp','app-smp-01.example.corp','10.20.30.50',
  'Linux','5.3.18-150300',65536,1,4,
  '[{"name":"SAP_BASIS","version":"740"},{"name":"ST","version":"720"},{"name":"ST-PI","version":"740"}]',
  '[{"inst":"app-smp-01_SMP_50","nr":"50","host":"app-smp-01.example.corp"}]');

-- ---- backups ---------------------------------------------------------------
INSERT INTO backups (sid, db_type, backup_type, status, size_bytes, oldest_kept, path) VALUES
 ('EAP','HDB','FULL',0, 486539264000, now() - interval '14 days','/hana/backup/EAP/full'),
 ('EAP','HDB','LOG', 0,  12884901888, now() - interval '3 days', '/hana/backup/EAP/log'),
 ('SMP','SYB','FULL',1, 128849018880, now() - interval '30 days','/sybase/backup/SMP');

-- ---- uptime: 6h of 5-min points per reachable system (deterministic jitter) -
-- response_ms = base + small oscillation + an occasional spike; PIP is down.
INSERT INTO uptime (ts, sid, reachable, logon_ok, response_ms, env)
SELECT now() - (g || ' minutes')::interval, s.sid, 1, 1,
       s.base + (g % 11) + CASE WHEN g % 55 = 0 THEN 120 ELSE 0 END, s.env
FROM generate_series(0, 355, 5) AS g
CROSS JOIN (VALUES
   ('EAD',14,'D'), ('EAQ',22,'Q'), ('EAP',31,'P'), ('BID',18,'D'), ('SMP',41,'P')
 ) AS s(sid, base, env);

-- PIP: down for the whole window (red markers), plus a brief blip up at t-120m
INSERT INTO uptime (ts, sid, reachable, logon_ok, response_ms, env)
SELECT now() - (g || ' minutes')::interval, 'PIP',
       CASE WHEN g = 120 THEN 1 ELSE 0 END, 0,
       CASE WHEN g = 120 THEN 95 ELSE 0 END, 'P'
FROM generate_series(0, 355, 5) AS g;

COMMIT;

-- quick check
SELECT 'systems' t, count(*) FROM systems
UNION ALL SELECT 'jco_results', count(*) FROM jco_results
UNION ALL SELECT 'jco_details', count(*) FROM jco_details
UNION ALL SELECT 'uptime', count(*) FROM uptime
UNION ALL SELECT 'sld_systems', count(*) FROM sld_systems
UNION ALL SELECT 'backups', count(*) FROM backups;
