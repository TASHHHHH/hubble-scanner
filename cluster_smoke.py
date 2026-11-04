"""Real local HTTP coordinator + ten independent workers, offline FITS tasks.

This exercises process/claim/heartbeat/result plumbing, not ten physical PCs.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
import numpy as np
from coordinator import Queue,make_server
from test_cluster_quality import image
from runtime import atomic_json

def run(folder):
    root=Path(folder).resolve();root.mkdir(parents=True,exist_ok=True)
    if (root/'queue.sqlite').exists():raise ValueError('Use a new smoke output directory')
    q=Queue(root/'queue.sqlite',lease_seconds=30)
    for i in range(12):
        p=root/f'fixture_{i}.fits';image(p,science=np.full((32,32),i,dtype=np.float32),dq=np.zeros((32,32),dtype=np.uint16))
        q.seed_quality(str(i),[str(p)]);q.seed_quality(str(i),[str(p)])
    token=secrets.token_urlsafe(32);server=make_server(q,'127.0.0.1',0,token)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    procs=[];logs=[];start=time.monotonic()
    try:
        env={**os.environ,'HUBBLE_QUEUE_TOKEN':token}
        for i in range(10):
            log=(root/f'worker_{i}.log').open('w',encoding='utf-8');logs.append(log)
            procs.append(subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('cluster_worker.py')),
                '--server',f'http://127.0.0.1:{server.server_port}','--worker',f'PC-{i}',
                '--work-dir',str(root/f'pc_{i}'),'--stages','quality'],env=env,stdout=log,stderr=subprocess.STDOUT))
        for p in procs:
            if p.wait(timeout=90)!=0:raise AssertionError('Worker failed; inspect logs')
        status=q.status()
        with q.db() as c:
            jobs=[dict(r) for r in c.execute('SELECT key,state,attempts,owner FROM jobs')]
        assert len(jobs)==12 and all(j['state']=='done' and j['attempts']==1 for j in jobs),jobs
        assert not status['attention']
        q.export(root/'export')
        report={'status':'passed','worker_processes':10,'submitted_jobs_including_duplicates':24,
                'unique_jobs':12,'completed':12,'executions':sum(j['attempts'] for j in jobs),
                'workers_used':len({j['owner'] for j in jobs}),'elapsed_seconds':round(time.monotonic()-start,2),
                'scope':'real HTTP and child processes, synthetic FITS on one Windows machine, no external APIs'}
        atomic_json(root/'report.json',report);print(json.dumps(report,indent=2))
        return report
    finally:
        for p in procs:
            if p.poll() is None:p.terminate();p.wait(timeout=10)
        for log in logs:log.close()
        server.shutdown();server.server_close();t.join()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True);a=p.parse_args();run(a.out)
