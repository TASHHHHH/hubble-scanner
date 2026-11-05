"""Single-host durable job queue served over a trusted LAN. SQLite stays local.

Leases are fenced by random tokens. Expired claims are HELD, not automatically
reassigned: no duplicate search just because a worker temporarily lost network.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from runtime import atomic_json, read_rows, write_rows

SCHEMA=1
def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))
def key(stage,value):return stage+':'+hashlib.sha256(encoded(value).encode()).hexdigest()
def designation(value):
    value=re.sub(r'\s+','',str(value)).upper()
    m=re.fullmatch(r'(NGC|IC|M|ARP|SH2-?)0*(\d+)([A-Z]*)',value)
    if not m:return value
    prefix='SH2-' if m[1].startswith('SH2') else m[1]
    return f'{prefix}{int(m[2])}{m[3]}'
def radius(obj,cap):return max(0.5,min(cap,float(obj.get('major_arcmin') or 2)/2))

class Queue:
    def __init__(self,path,lease_seconds=90,max_attempts=3,wiki_slots=1,mast_slots=4):
        if str(path).startswith(('\\\\','//')):raise ValueError('Queue database must be on the coordinator local disk, never SMB')
        if lease_seconds<3 or max_attempts<1 or min(wiki_slots,mast_slots)<1:raise ValueError('Invalid coordinator limits')
        self.path=str(Path(path).resolve());Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        self.lease_seconds=lease_seconds;self.max_attempts=max_attempts
        self.slots={'wiki':wiki_slots,'mast':mast_slots,'archive_review':mast_slots,'archive_band':mast_slots,'quality':10}
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, stage TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready', owner TEXT, token TEXT,
                lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
                result TEXT, error TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS targets(id TEXT PRIMARY KEY, object TEXT NOT NULL, wiki_key TEXT NOT NULL, mast_key TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_permissions(name TEXT PRIMARY KEY,role TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications(job_key TEXT PRIMARY KEY,message TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(state,stage,available);''')
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
        try:
            yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    @staticmethod
    def add(c,job_key,stage,payload):
        c.execute('INSERT OR IGNORE INTO jobs(key,stage,payload,updated) VALUES(?,?,?,?)',(job_key,stage,encoded(payload),time.time()))
    @staticmethod
    def expire(c):
        c.execute("UPDATE jobs SET state='interrupted',error='Heartbeat expired; confirm old worker stopped before requeue',updated=? WHERE state='running' AND lease_until<=?",(time.time(),time.time()))
    def seed(self,objects,max_radius=12,max_obs=0):
        from scanner import validate_object
        if max_radius<=0 or max_obs<0:raise ValueError('Invalid MAST settings')
        groups={}
        for o in objects:
            validate_object(o)
            if not o.get('wiki_titles'):raise ValueError(f'Missing wiki_titles: {o["id"]}')
            canonical=designation(o.get('canonical_id',o['id']))
            groups.setdefault(canonical,[]).append(o)
        config=encoded({'schema':SCHEMA,'max_radius':max_radius,'max_obs':max_obs})
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute("SELECT value FROM settings WHERE key='scan'").fetchone()
            if previous and previous['value']!=config:raise ValueError('Changed scan settings: use a new database')
            c.execute("INSERT OR IGNORE INTO settings VALUES('scan',?)",(config,))
            for members in groups.values():
                titles=sorted({str(t).strip().replace('_',' ') for o in members for t in o['wiki_titles']})
                wiki_key=key('wiki',{'titles':titles,'schema':SCHEMA})
                self.add(c,wiki_key,'wiki',{'object':{**members[0],'wiki_titles':titles},'max_radius':max_radius,'max_obs':max_obs})
                for o in members:
                    old=c.execute('SELECT * FROM targets WHERE id=?',(o['id'],)).fetchone()
                    if old and (old['object']!=encoded(o) or old['wiki_key']!=wiki_key):
                        raise ValueError(f'Conflicting reseed for {o["id"]}; use a new run database')
                    c.execute('INSERT OR IGNORE INTO targets(id,object,wiki_key) VALUES(?,?,?)',(o['id'],encoded(o),wiki_key))
                done=c.execute('SELECT result FROM jobs WHERE key=? AND state=\'done\'',(wiki_key,)).fetchone()
                if done:self.enqueue_mast(c,wiki_key,json.loads(done['result']))
    def enqueue_mast(self,c,wiki_key,result):
        if result.get('wiki_status')!='not_detected' and not (
            result.get('wiki_status')=='no_article' and result.get('catalog_verified')
        ):return
        config=json.loads(c.execute("SELECT value FROM settings WHERE key='scan'").fetchone()['value'])
        for target in c.execute('SELECT id,object FROM targets WHERE wiki_key=?',(wiki_key,)).fetchall():
            o=json.loads(target['object']);rad=radius(o,config['max_radius'])
            mast_key=key('mast',{'ra':float(o['ra_deg']),'dec':float(o['dec_deg']),'radius':rad,'max_obs':config['max_obs'],'schema':SCHEMA})
            self.add(c,mast_key,'mast',{'object':o,'max_radius':config['max_radius'],'max_obs':config['max_obs']})
            c.execute('UPDATE targets SET mast_key=? WHERE id=?',(mast_key,target['id']))
    def seed_quality(self,job_id,files,options=None):
        # Paths must be accessible on workers (same UNC share or replicated identical paths).
        from fits_quality import file_digest
        files=sorted(set(str(Path(p).resolve()) for p in files));options=options or {}
        if not files:raise ValueError('No quality files')
        fingerprints={p:file_digest(p) for p in files}
        payload={'files':files,'options':options,'fingerprints':fingerprints}
        with self.db() as c:
            self.add(c,key('quality',{'hashes':sorted(fingerprints.values()),'options':options,'schema':SCHEMA}),'quality',payload)

    def seed_archive_review(self,rows,max_radius=12,max_obs=0):
        queued=0
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in rows:
                if not row.get('mast_observation_count') or row.get('publication_status') not in (
                    'no_published_science_paper_found_for_programs','program_id_missing'):
                    continue
                keep=('id','ra_deg','dec_deg','major_arcmin','wiki_titles','aliases','object_type',
                      'source','source_name','source_url','source_license','publication_status',
                      'mast_program_ids','mast_filters','mast_search_url')
                obj={k:row[k] for k in keep if k in row}
                if not obj.get('id') or not obj.get('wiki_titles'):continue
                payload={'object':obj,'max_radius':max_radius,'max_obs':max_obs,'investigate':True}
                self.add(c,key('archive_review',{'object':obj,'schema':SCHEMA}),'archive_review',payload)
                queued+=1
        return queued
    def seed_archive_bands(self,width=5):
        if width not in (5,10):raise ValueError('RA band width must be 5 or 10 degrees')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for lower in range(0,360,width):
                upper=lower+width;payload={'ra_min':lower,'ra_max':upper,'schema':SCHEMA}
                self.add(c,key('archive_band',payload),'archive_band',payload)
        return 360//width
    def claim(self,worker,stages=None):
        stages=stages or ['wiki','mast','quality']
        if not worker or len(worker)>200 or any(s not in self.slots for s in stages):raise ValueError('Invalid worker/stages')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.expire(c)
            if c.execute("SELECT 1 FROM jobs WHERE owner=? AND state='running'",(worker,)).fetchone():return None
            for stage in stages:
                if stage in ('mast','archive_review','archive_band'):
                    active=c.execute("SELECT count(*) FROM jobs WHERE stage IN ('mast','archive_review','archive_band') AND state='running'").fetchone()[0]
                else:
                    active=c.execute("SELECT count(*) FROM jobs WHERE stage=? AND state='running'",(stage,)).fetchone()[0]
                capacity=self.level(c)['investigators'] if stage in ('mast','archive_review','archive_band') else self.slots[stage]
                if active>=capacity:continue
                job=c.execute("SELECT * FROM jobs WHERE stage=? AND state='ready' AND available<=? ORDER BY updated,key LIMIT 1",(stage,time.time())).fetchone()
                if not job:continue
                token=secrets.token_urlsafe(32);deadline=time.time()+self.lease_seconds
                c.execute("UPDATE jobs SET state='running',owner=?,token=?,lease_until=?,attempts=attempts+1,updated=? WHERE key=?",(worker,token,deadline,time.time(),job['key']))
                payload=json.loads(job['payload'])
                if stage in ('mast','archive_review') and self.level(c)['enabled']:payload['investigate']=True
                return {'key':job['key'],'stage':stage,'payload':payload,'token':token,'lease_seconds':self.lease_seconds}
        return None
    def heartbeat(self,job_key,token):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.expire(c)
            n=c.execute("UPDATE jobs SET lease_until=?,updated=? WHERE key=? AND token=? AND state='running'",(time.time()+self.lease_seconds,time.time(),job_key,token)).rowcount
            if n!=1:raise ValueError('Lease lost')
        return {'ok':True}
    def finish(self,job_key,token,result=None,error=None):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.expire(c)
            row=c.execute('SELECT * FROM jobs WHERE key=?',(job_key,)).fetchone()
            if row and row['state']=='done' and row['token']==token:
                return {'ok':True,'already_completed':True}  # lost HTTP response, no repeated work
            if not row or row['state']!='running' or row['token']!=token:raise ValueError('Lease lost; stale completion rejected')
            if error is None:
                if not isinstance(result,dict):raise ValueError('Result must be an object')
                if row['stage']=='wiki' and result.get('wiki_status') not in ('not_detected','hubble_detected','no_article'):
                    error='Incomplete Wikipedia scan; retry required'
                if row['stage'] in ('mast','archive_review') and result.get('status')!='ok':error='MAST search failed'
                if row['stage']=='archive_band':
                    payload=json.loads(row['payload']);lo=payload['ra_min'];hi=payload['ra_max']
                    if result.get('status')!='ok' or result.get('ra_min')!=lo or result.get('ra_max')!=hi or not isinstance(result.get('observations'),list):error='Invalid archive band result'
                    elif any(not isinstance(o,dict) or not o.get('s_ra') or not (lo<=float(o['s_ra'])<hi) for o in result['observations']):error='Observation outside assigned RA band'
                if row['stage']=='quality' and result.get('errors'):error='FITS quality errors; inspect worker output'
            if error is not None:
                state='failed' if row['attempts']>=self.max_attempts else 'ready'
                c.execute('UPDATE jobs SET state=?,error=?,available=?,lease_until=NULL,token=NULL,updated=? WHERE key=?',
                          (state,str(error)[:8000],time.time()+min(60,5*row['attempts']),time.time(),job_key))
            else:
                c.execute("UPDATE jobs SET state='done',result=?,lease_until=NULL,error=NULL,updated=? WHERE key=?",(encoded(result),time.time(),job_key))
                if row['stage']=='wiki':self.enqueue_mast(c,job_key,result)
                if row['stage']=='mast' and result.get('investigation',{}).get('match'):
                    from investigator import match_pair
                    obj=json.loads(row['payload'])['object']
                    pair,_=match_pair({**obj,'observations':result.get('observations',[])})
                    if pair:
                        inv=result['investigation']
                        message=f"Hubble candidate: {obj['id']}\nBands: {', '.join(pair['filters'])}\nFootprint overlap: {pair['overlap_fraction']:.0%}\nProcessing: {str(inv.get('status','unknown'))[:80]}\nWikipedia status: {result.get('wiki_status')}. Publication status: not assessed."
                        if inv.get('ranking'):message+='\nDINOv2 worker-local diversity: '+str(inv['ranking'].get('visual_diversity','unknown'))[:30]+' (not novelty)'
                        c.execute('INSERT OR IGNORE INTO notifications(job_key,message) VALUES(?,?)',(job_key,message))
        return {'ok':True}

    def configure_levels(self,step=10,maximum=5):
        if not 1<=step<=10000 or not 1<=maximum<=10:raise ValueError('Invalid level limits')
        with self.db() as c:c.execute("INSERT OR REPLACE INTO settings VALUES('levels',?)",(encoded({'step':step,'maximum':maximum}),))

    def level(self,c):
        config=c.execute("SELECT value FROM settings WHERE key='levels'").fetchone()
        backlog=c.execute("SELECT count(*) FROM jobs WHERE stage IN ('mast','archive_review','archive_band') AND state IN ('ready','running')").fetchone()[0]
        if not config:return {'enabled':False,'backlog':backlog,'investigators':self.slots['mast']}
        config=json.loads(config['value'])
        return {'enabled':True,'backlog':backlog,'investigators':min(config['maximum'],1+backlog//config['step']),**config}
    def requeue(self,job_key,confirmed=False):
        if not confirmed:raise ValueError('Confirm the old worker process has stopped before requeue')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.expire(c)
            n=c.execute("UPDATE jobs SET state='ready',owner=NULL,token=NULL,lease_until=NULL,attempts=0,available=0,error=NULL,updated=? WHERE key=? AND state IN ('failed','interrupted')",(time.time(),job_key)).rowcount
            if not n:raise ValueError('Only failed/interrupted jobs can be requeued')
    def import_results(self,objects,wiki_rows,mast_rows):
        """Migrate matching completed v3 checkpoints before starting workers."""
        imported={'wiki':0,'mast':0}
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]:
                raise ValueError('Stop workers before importing checkpoints')
            for o in objects:
                t=c.execute('SELECT object FROM targets WHERE id=?',(o['id'],)).fetchone()
                if not t or t['object']!=encoded(o):raise ValueError(f'Catalog mismatch for {o["id"]}')
            allowed={o['id'] for o in objects}
            config=json.loads(c.execute("SELECT value FROM settings WHERE key='scan'").fetchone()['value'])
            for stage,rows in [('wiki',wiki_rows),('mast',mast_rows)]:
                for r in rows:
                    if r['id'] not in allowed:raise ValueError('Checkpoint target outside supplied catalog')
                    if stage=='wiki' and r.get('wiki_status') not in ('not_detected','hubble_detected','no_article'):continue
                    if stage=='mast' and r.get('status')!='ok':continue
                    if not isinstance(r.get('checked_at_unix'),(int,float)):raise ValueError('Checkpoint has no retrieval timestamp')
                    t=c.execute('SELECT * FROM targets WHERE id=?',(r['id'],)).fetchone()
                    k=t[stage+'_key']
                    if not k:continue
                    j=c.execute('SELECT * FROM jobs WHERE key=?',(k,)).fetchone()
                    if j['state']=='done':continue
                    payload=json.loads(j['payload'])
                    if stage=='wiki':
                        if set(r.get('wiki_titles_checked',[]))!=set(payload['object']['wiki_titles']):
                            raise ValueError('Wikipedia title set changed; rescan rather than importing')
                    else:
                        if r.get('observation_list_truncated') or r.get('radius_arcmin')!=round(radius(json.loads(t['object']),config['max_radius']),2):
                            raise ValueError('MAST checkpoint is truncated or uses a different query radius')
                    c.execute("UPDATE jobs SET state='done',result=?,error=NULL,token=NULL,lease_until=NULL,updated=? WHERE key=?",(encoded(r),time.time(),k))
                    imported[stage]+=1
                    if stage=='wiki':self.enqueue_mast(c,k,r)
        return imported
    def status(self):
        with self.db() as c:
            self.expire(c)
            return {'level':self.level(c),'counts':[dict(r) for r in c.execute('SELECT stage,state,count(*) AS count FROM jobs GROUP BY stage,state')],
                    'attention':[dict(r) for r in c.execute("SELECT key,stage,state,owner,attempts,error FROM jobs WHERE state IN ('failed','interrupted')")],
                    'targets':c.execute('SELECT count(*) FROM targets').fetchone()[0]}
    def candidates(self):
        with self.db() as c:
            rows=c.execute("SELECT t.id,t.object,m.state,m.result,m.error,(SELECT state FROM notifications n WHERE n.job_key=m.key) AS delivery,w.result AS wiki_result FROM targets t JOIN jobs w ON w.key=t.wiki_key LEFT JOIN jobs m ON m.key=t.mast_key WHERE w.state='done' AND (json_extract(w.result,'$.wiki_status')='not_detected' OR (json_extract(w.result,'$.wiki_status')='no_article' AND json_extract(w.result,'$.catalog_verified')=1)) ORDER BY w.updated DESC LIMIT 2000").fetchall()
            return [{**json.loads(r['object']),**json.loads(r['wiki_result']),'mast_state':r['state'],
                     'investigation':{**json.loads(r['result']).get('investigation',{}),'notification':r['delivery'] or 'not_queued'} if r['result'] else {},'error':r['error']} for r in rows]

    def worker_token(self,name,role='wiki'):
        if role not in ('wiki','adaptive'):raise ValueError('Invalid worker role')
        token=secrets.token_urlsafe(32)
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS credentials(digest TEXT PRIMARY KEY,name TEXT UNIQUE)')
            c.execute('INSERT OR REPLACE INTO credentials VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),name))
            c.execute('INSERT OR REPLACE INTO worker_permissions VALUES(?,?)',(name,role))
        return token

    def worker_identity(self,token):
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS credentials(digest TEXT PRIMARY KEY,name TEXT UNIQUE)')
            row=c.execute('SELECT name FROM credentials WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return row['name'] if row else None

    def revoke_worker(self,name):
        with self.db() as c:
            c.execute('DELETE FROM credentials WHERE name=?',(name,))

    def worker_stages(self,name):
        with self.db() as c:
            row=c.execute('SELECT role FROM worker_permissions WHERE name=?',(name,)).fetchone()
            return ['archive_band','archive_review','mast','wiki'] if row and row['role']=='adaptive' else ['wiki']
    def export(self,out):
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.execute('BEGIN')
            targets=c.execute('SELECT * FROM targets ORDER BY id').fetchall()
            done={r['key']:json.loads(r['result']) for r in c.execute("SELECT key,result FROM jobs WHERE state='done'")}
            configrow=c.execute("SELECT value FROM settings WHERE key='scan'").fetchone()
            config=json.loads(configrow['value']) if configrow else {}
            catalog=[];wiki=[];mast=[]
            for t in targets:
                o=json.loads(t['object']);catalog.append(o)
                if t['wiki_key'] in done:wiki.append({**done[t['wiki_key']],'id':o['id'],'coordinator_key':t['wiki_key']})
                if t['mast_key'] in done:mast.append({**done[t['mast_key']],'id':o['id'],'coordinator_key':t['mast_key'],
                    'search_radius_capped':float(o.get('major_arcmin') or 2)/2>config['max_radius']})
            quality=[{'key':k,'result':v} for k,v in done.items() if k.startswith('quality:')]
            archive_reviews=[];archive_observations=[]
            for job in c.execute("SELECT result FROM jobs WHERE stage='archive_band' AND state='done'"):
                archive_observations.extend(json.loads(job['result'])['observations'])
            for job in c.execute("SELECT payload,result FROM jobs WHERE stage='archive_review' AND state='done'"):
                payload=json.loads(job['payload']);result=json.loads(job['result'])
                archive_reviews.append({**payload['object'],**result,
                    'publication_status':payload['object'].get('publication_status')})
        write_rows(out/'catalog.jsonl',catalog);write_rows(out/'wiki_coordinator.jsonl',wiki);write_rows(out/'mast_coordinator.jsonl',mast)
        write_rows(out/'quality_results.jsonl',quality);write_rows(out/'archive_review_results.jsonl',archive_reviews)
        write_rows(out/'archive_observations.jsonl',archive_observations)
        atomic_json(out/'queue_status.json',self.status())
        return {'targets':len(catalog),'wiki_completed':len(wiki),'mast_completed':len(mast),
                'quality_completed':len(quality),'archive_review_completed':len(archive_reviews),
                'archive_observations':len(archive_observations)}

def make_server(queue,host,port,secret,restricted=False):
    if len(secret)<24:raise ValueError('Set HUBBLE_QUEUE_TOKEN to a random token of at least 24 characters')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass  # never log auth headers/payloads
        def do_POST(self):
            self.connection.settimeout(15)
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:self.send_error(400);return
            if not 0<length<=32*1024*1024:self.send_error(413);return
            raw=self.rfile.read(length)
            supplied=self.headers.get('Authorization','').removeprefix('Bearer ')
            admin=hmac.compare_digest(supplied,secret)
            identity=queue.worker_identity(supplied) if restricted and not admin else None
            if not admin and not identity:
                self.send_error(401);return
            try:
                a=json.loads(raw)
                if identity:
                    allowed=queue.worker_stages(identity)
                    if self.path=='/claim':
                        stages=[s for s in allowed if s in a.get('stages',['wiki'])]
                        a={'worker':'remote:'+identity,'stages':stages or ['wiki']}
                    elif self.path in ('/heartbeat','/finish'):
                        with queue.db() as c:
                            owned=c.execute('SELECT owner,stage FROM jobs WHERE key=?',(a.get('key'),)).fetchone()
                        if not owned or owned['owner']!='remote:'+identity or owned['stage'] not in allowed:raise ValueError('Worker cannot access this job')
                    elif self.path!='/status':raise ValueError('Worker endpoint forbidden')
                if self.path=='/claim':result=queue.claim(a['worker'],a.get('stages'))
                elif self.path=='/heartbeat':result=queue.heartbeat(a['key'],a['token'])
                elif self.path=='/finish':result=queue.finish(a['key'],a['token'],a.get('result'),a.get('error'))
                elif self.path=='/status':
                    result=queue.status()
                    if identity:result={'level':result['level'],'counts':[r for r in result['counts'] if r['stage'] in allowed]}
                elif self.path=='/candidates':result=queue.candidates()
                else:self.send_error(404);return
                body=encoded(result).encode();self.send_response(200)
            except Exception as ex:
                body=encoded({'error':str(ex)}).encode();self.send_response(409)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    return server

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',default='out/queue.sqlite')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('seed');a.add_argument('--catalog',required=True);a.add_argument('--max-radius',type=float,default=12);a.add_argument('--max-obs',type=int,default=0)
    a=sub.add_parser('archive-review-seed');a.add_argument('--results',required=True)
    sub.add_parser('archive-seed')
    a=sub.add_parser('quality');a.add_argument('files',nargs='+')
    a=sub.add_parser('import-results');a.add_argument('--catalog',required=True);a.add_argument('--wiki-glob',required=True);a.add_argument('--mast-glob',required=True)
    a=sub.add_parser('serve');a.add_argument('--host',default='127.0.0.1');a.add_argument('--port',type=int,default=8765)
    a.add_argument('--lease-seconds',type=int,default=90);a.add_argument('--mast-slots',type=int,default=4)
    sub.add_parser('status')
    a=sub.add_parser('export');a.add_argument('--out',default='out/cluster')
    a=sub.add_parser('requeue');a.add_argument('--key',required=True);a.add_argument('--confirm-worker-stopped',action='store_true')
    args=p.parse_args();q=Queue(args.db,getattr(args,'lease_seconds',90),mast_slots=getattr(args,'mast_slots',4))
    if args.command=='seed':q.seed(list(read_rows(args.catalog)),args.max_radius,args.max_obs);print(encoded(q.status()))
    elif args.command=='archive-review-seed':print(q.seed_archive_review(list(read_rows(args.results))))
    elif args.command=='quality':q.seed_quality('',args.files);print(encoded(q.status()))
    elif args.command=='archive-seed':print(q.seed_archive_bands())
    elif args.command=='import-results':
        from scanner import merge_glob
        print(encoded(q.import_results(list(read_rows(args.catalog)),list(merge_glob(args.wiki_glob).values()),list(merge_glob(args.mast_glob).values()))))
    elif args.command=='status':print(json.dumps(q.status(),indent=2))
    elif args.command=='requeue':q.requeue(args.key,args.confirm_worker_stopped)
    elif args.command=='export':print(encoded(q.export(args.out)))
    else:
        server=make_server(q,args.host,args.port,os.getenv('HUBBLE_QUEUE_TOKEN',''))
        print(f'Coordinator listening on {args.host}:{server.server_port}; database stays on this host',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()

if __name__=='__main__':main()
