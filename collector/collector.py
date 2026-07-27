#!/usr/bin/env python3.9
import os,json,socket,time,datetime,psycopg2,subprocess,urllib.request,urllib.error
from pyrfc import Connection
os.environ.setdefault('LD_LIBRARY_PATH','/monitoring/nwrfcsdk/lib')
class _NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k): return None   # don't chase redirects; a 3xx already means the stack answered
HTTP_OPENER=urllib.request.build_opener(_NoRedir)
CFG=json.load(open('/monitoring/etc/systems.json'))
RFCPW=open('/monitoring/secrets/rfc.pw').read().strip()
PGPW=open('/monitoring/secrets/pg_grafana.pw').read().strip()
db=psycopg2.connect(host='127.0.0.1',dbname='sapmon',user='grafana',password=PGPW);db.autocommit=True;cur=db.cursor()
cur.execute("DELETE FROM jco_details")   # current-state snapshot
def rec(e,s,c,st,d): cur.execute("INSERT INTO jco_results(env,sid,check_name,status,detail) VALUES(%s,%s,%s,%s,%s)",(e,s,c,st,d))
def det(e,s,c,it): cur.execute("INSERT INTO jco_details(env,sid,check_name,item) VALUES(%s,%s,%s,%s)",(e,s,c,it[:250]))
def up(e,s,r,l,ms): cur.execute("INSERT INTO uptime(env,sid,reachable,logon_ok,response_ms) VALUES(%s,%s,%s,%s,%s)",(e,s,r,l,ms))
def rt(c,tab,opt,fields,rc=0):
    return c.call('RFC_READ_TABLE',QUERY_TABLE=tab,DELIMITER='|',ROWCOUNT=rc,OPTIONS=[{'TEXT':opt}] if opt else [],FIELDS=[{'FIELDNAME':f} for f in fields])['DATA']
def fld(row): return [x.strip() for x in row['WA'].split('|')]
today=datetime.date.today().strftime('%Y%m%d'); yd=(datetime.date.today()-datetime.timedelta(days=1)).strftime('%Y%m%d')
def _hms(t): return t[:2]+':'+t[2:4]+':'+t[4:6] if len(t)>=6 else t
def _fmt_dt(d,t): return '%s-%s %s'%(d[4:6],d[6:8],_hms(t)) if len(d)==8 else (d+' '+t)
def _dur(sd,st,ed,et):
    try:
        a=datetime.datetime.strptime(sd+st,'%Y%m%d%H%M%S'); b=datetime.datetime.strptime(ed+et,'%Y%m%d%H%M%S')
        s=int((b-a).total_seconds())
        return '%d:%02d:%02d'%(s//3600,(s%3600)//60,s%60) if s>=0 else 'n/a'
    except Exception: return 'n/a'
for s in CFG['systems']:
    cur.execute("INSERT INTO systems(sid,env,stype,host,sysnr,client,descr) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(sid) DO UPDATE SET env=EXCLUDED.env,stype=EXCLUDED.stype,host=EXCLUDED.host,descr=EXCLUDED.descr",(s['sid'],s['env'],s['type'],s['host'],s.get('sysnr'),s.get('client'),s.get('descr')))
    e,sid,typ=s['env'],s['sid'],s['type']
    if typ=='ABAP':
        t0=time.time()
        try:
            c=Connection(ashost=s['host'],sysnr=s['sysnr'],client=s['client'],user=CFG['rfc_user'],passwd=RFCPW,lang='EN')
            ms=int((time.time()-t0)*1000); up(e,sid,1,1,ms); rec(e,sid,'AVAIL',0,'logon ok %dms'%ms)
            try:
                enq=c.call('ENQUEUE_READ',GCLIENT='',GNAME='',GARG='',GUNAME='').get('ENQ',[]); rec(e,sid,'LOCKS',1 if len(enq)>50 else 0,'%d lock entries'%len(enq))
                for l in enq[:200]: det(e,sid,'LOCKS','user=%s  object=%s  client=%s  arg=%s'%(l.get('GUNAME','').strip(),l.get('GNAME','').strip(),l.get('GCLIENT','').strip(),l.get('GARG','').strip()[:60]))
            except Exception as x: rec(e,sid,'LOCKS',2,str(x)[:60])
            try:
                d=rt(c,'SNAP',"DATUM = '%s' AND SEQNO = '000'"%today,['UZEIT','UNAME','MANDT','AHOST'],rc=500); rec(e,sid,'DUMPS',1 if len(d)>0 else 0,'%d dumps today'%len(d))
                for r in d[:200]:
                    f=fld(r)+['']*4; det(e,sid,'DUMPS','%s  %s  %s  %s'%(_hms(f[0]),f[1],f[2],f[3]))
            except Exception as x: rec(e,sid,'DUMPS',2,str(x)[:60])
            try:
                j=rt(c,'TBTCO',"STATUS = 'A' AND STRTDATE >= '"+yd+"'",['JOBNAME','SDLUNAME','STRTDATE','STRTTIME','ENDDATE','ENDTIME'],rc=2000); rec(e,sid,'JOBS_ABORTED',1 if len(j)>0 else 0,'%d aborted jobs (24h)'%len(j))
                for r in j[:200]:
                    f=fld(r)+['']*6; det(e,sid,'JOBS_ABORTED','%s  %s  %s  %s  %s'%(f[0],f[1],_fmt_dt(f[2],f[3]),_hms(f[5]),_dur(f[2],f[3],f[4],f[5])))
            except Exception as x: rec(e,sid,'JOBS_ABORTED',2,str(x)[:60])
            try:
                n=len(rt(c,'VBHDR','',['MANDT'],rc=500)); rec(e,sid,'UPD_RECORDS',1 if n>0 else 0,'%d pending update recs'%n)
            except Exception as x:
                rec(e,sid,'UPD_RECORDS',0,'0 pending update recs') if 'TABLE_WITHOUT_DATA' in str(x) else rec(e,sid,'UPD_RECORDS',2,str(x)[:60])
            try:
                _r=c.call("RSAU_READ_LOG", IS_INTV={"DAT_FROM":today,"DAT_TO":today,"TIM_FROM":"000000","TIM_TO":"235959"})
                evs=_r.get("ET_DATA",[])
                def _sev(ev):
                    for k in ("SEVERITY","SEVE","MSGSEV","CLASS"):
                        if k in ev and str(ev[k]).strip(): return str(ev[k]).strip()
                    return ""
                crit=[x2 for x2 in evs if _sev(x2) in ("2","3","C")]
                if not evs: rec(e,sid,"SAL",1,"0 events today - verify SAL active (SM19)")
                else:
                    rec(e,sid,"SAL",2 if crit else 1,"%d SAL events (%d crit)"%(len(evs),len(crit)))
                    for ev in evs[:200]: det(e,sid,"SAL"," ".join("%s=%s"%(k,str(v).strip()) for k,v in ev.items() if str(v).strip())[:250])
            except Exception as x:
                rec(e,sid,"SAL",1,"SAL read FM n/a (rel<7.50) - audit via SM20") if "FU_NOT_FOUND" in str(x) else rec(e,sid,"SAL",2,str(x)[:60])
            c.close()
        except Exception as x:
            up(e,sid,0,0,0); rec(e,sid,'AVAIL',2,'connect fail: '+str(x)[:80]); det(e,sid,'AVAIL',str(x)[:200])
    else:
        host=s['host']; port=s.get('port',0); path=s.get('http_path'); t0=time.time(); up_now=0
        if path is not None:   # HTTP health probe (NW Java etc.): ANY HTTP answer = web stack serving
            try:
                HTTP_OPENER.open('http://%s:%d%s'%(host,port,path),timeout=6); up_now=1; d='HTTP %s:%d%s ok'%(host,port,path)
            except urllib.error.HTTPError as he: up_now=1; d='HTTP %s:%d%s -> %d'%(host,port,path,he.code)
            except Exception as x: up_now=0; d='%s:%d%s unreachable: %s'%(host,port,path,str(x)[:45])
        else:                  # plain TCP for non-HTTP endpoints (BOBJ, WebDisp...)
            try: socket.create_connection((host,port),timeout=5).close(); up_now=1; d='%s %s:%d open'%(typ,host,port)
            except Exception: up_now=0; d='%s %s:%d closed'%(typ,host,port)
        ms=int((time.time()-t0)*1000)
        st=0
        if not up_now:         # debounce: single miss = YELLOW (transient); RED only if the prior probe was also down
            cur.execute("SELECT status FROM jco_results WHERE sid=%s AND check_name='AVAIL' ORDER BY ts DESC LIMIT 1",(sid,))
            r=cur.fetchone(); st=2 if (r and r[0] in (1,2)) else 1
        up(e,sid,up_now,0,ms if up_now else 0)
        rec(e,sid,'AVAIL',st,d+('' if up_now else (' [transient]' if st==1 else ' [down]')))
        det(e,sid,'AVAIL',d)
    # --- STORAGE: filesystem utilisation via bfisapadmin SSH (df) ---
    try:
        st=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=6','bfisapadmin@'+s['host'],'df -PB1'],
                          capture_output=True,text=True,timeout=15)
        best={}
        for ln in st.stdout.splitlines()[1:]:
            p=ln.split()
            if len(p)<6 or not p[1].isdigit() or int(p[1])<1000000000: continue
            dev,mount=p[0],p[5]
            if 'tmpfs' in dev or 'loop' in dev or mount.startswith('/boot') or mount.startswith('/dev'): continue
            pv=int(p[4].rstrip('%'))
            if dev not in best or len(mount)<len(best[dev][0]): best[dev]=(mount,int(p[1]),int(p[2]),pv)  # canonical mount per device
        rows=sorted(best.values(),key=lambda r:-r[3])
        if rows:
            mx=rows[0][3]; rec(e,sid,'STORAGE',2 if mx>=90 else 1 if mx>=80 else 0,'max %d%% (%s)'%(mx,rows[0][0]))
            for m,sz,us,pv in rows: det(e,sid,'STORAGE','%s  %d  %d  %d'%(m,sz,us,pv))
        else: rec(e,sid,'STORAGE',3,'no df rows')
    except Exception as x: rec(e,sid,'STORAGE',3,('ssh df n/a: '+str(x))[:60])
print('done')
