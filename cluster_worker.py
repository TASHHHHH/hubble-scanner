"""Windows/LAN worker: claim one job, heartbeat, run in child, commit fenced result."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
import requests
from runtime import atomic_json

def request(url,secret,endpoint,payload,timeout=10):
    from urllib.parse import urlparse
    parsed=urlparse(url)
    if parsed.scheme not in ('http','https') or (parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1')):
        raise ValueError('Remote coordinator connections require HTTPS')
    with requests.Session() as session:
        session.trust_env=False  # LAN credentials must not be sent through an ambient proxy.
        response=session.post(url.rstrip('/')+endpoint,json=payload,
                              headers={'Authorization':'Bearer '+secret},timeout=timeout,
                              verify=os.getenv('HUBBLE_CA_BUNDLE') or True)
        response.raise_for_status();return response.json()

def stop(child):
    if child.poll() is None:
        child.terminate()
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=10)

def work(url,worker,folder,stages,max_jobs=0):
    secret=os.environ.get('HUBBLE_QUEUE_TOKEN','')
    if len(secret)<24:raise ValueError('Set HUBBLE_QUEUE_TOKEN to the coordinator token')
    root=Path(folder).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ.setdefault('HUBBLE_CACHE_DIR',str(root/'cache'))
    count=0
    while not max_jobs or count<max_jobs:
        job=request(url,secret,'/claim',{'worker':worker,'stages':stages})
        if job is None:
            status=request(url,secret,'/status',{})
            if not any(r['state'] in ('ready','running') for r in status['counts']):
                print('No pending jobs. Held/failed jobs, if any, require coordinator review.',flush=True);return
            time.sleep(2);continue
        attempt=root/(job['key'].replace(':','_')+'_'+job['token'][:12]);attempt.mkdir()
        atomic_json(attempt/'job.json',job)
        interval=min(10,job['lease_seconds']/4)
        print(f'{worker}: {job["stage"]} {job["key"]}',flush=True)
        with (attempt/'task.log').open('w',encoding='utf-8') as log:
            child_env=os.environ.copy()
            for name in ('HUBBLE_TELEGRAM_TOKEN','HUBBLE_TELEGRAM_CHAT_ID','HUBBLE_QUEUE_TOKEN'):child_env.pop(name,None)
            child=subprocess.Popen([sys.executable,'-u','-B',str(Path(__file__).with_name('cluster_task.py')),str(attempt/'job.json')],
                                   stdout=log,stderr=subprocess.STDOUT,env=child_env)
            try:
                next_beat=time.monotonic()+interval
                while child.poll() is None:
                    time.sleep(min(0.5,interval))
                    if time.monotonic()>=next_beat:
                        # Any heartbeat failure stops this worker's task before more work is claimed.
                        request(url,secret,'/heartbeat',{'key':job['key'],'token':job['token']},timeout=min(5,interval))
                        next_beat=time.monotonic()+interval
                if child.returncode:
                    completion={'key':job['key'],'token':job['token'],'error':f'Task exited {child.returncode}; inspect {attempt.name}/task.log on {worker}'}
                else:
                    completion={'key':job['key'],'token':job['token'],'result':json.loads((attempt/'completion.json').read_text(encoding='utf-8'))}
                request(url,secret,'/finish',completion)
                count+=1
                if job['stage'] in ('mast','archive_review'):
                    time.sleep(5)  # selected PC processes one candidate fully, then throttles the next query
            finally:stop(child)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--server',required=True)
    p.add_argument('--worker',default=socket.gethostname()+'-'+uuid.uuid4().hex[:8])
    p.add_argument('--work-dir',default='work/cluster-worker');p.add_argument('--stages',nargs='+',choices=['wiki','mast','archive_review','quality'],default=['wiki','mast','archive_review','quality'])
    p.add_argument('--max-jobs',type=int,default=0);a=p.parse_args()
    if a.max_jobs<0:p.error('max-jobs must be nonnegative')
    try:work(a.server,a.worker,a.work_dir,a.stages,a.max_jobs)
    except KeyboardInterrupt:raise SystemExit(130)

if __name__=='__main__':main()
