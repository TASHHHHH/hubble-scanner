"""One leased task in a separate, terminable process. Invoked by cluster_worker."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from runtime import atomic_json,write_rows,read_rows

def execute(job,folder):
    payload=job['payload'];stage=job['stage'];folder=Path(folder)
    if stage=='archive_band':
        from archive_sweep import fetch_archive_band
        root=Path(os.getenv('HUBBLE_CACHE_DIR',str(folder.parent/'cache')))
        rows=fetch_archive_band(root,payload['ra_min'],payload['ra_max'])
        return {'status':'ok','ra_min':payload['ra_min'],'ra_max':payload['ra_max'],'observations':rows}
    if stage=='quality':
        from fits_quality import assess,file_digest
        before={p:(Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in payload['files']}
        for p,expected in payload.get('fingerprints',{}).items():
            if file_digest(p)!=expected:raise ValueError(f'Quality input changed since enqueue: {p}')
        result=assess(payload['files'],**payload.get('options',{}))
        if any(before[p]!=(Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in payload['files']):
            raise ValueError('Quality input changed while it was being read')
        result['input_sha256']=payload.get('fingerprints',{})
        return result
    from scanner import wiki,mast
    obj=payload['object'];catalog=folder/'catalog.jsonl';out=folder/'result.jsonl'
    write_rows(catalog,[obj])
    args=SimpleNamespace(catalog=str(catalog),out=str(out),worker=0,workers=1,limit=0,delay=1.2)
    if stage=='wiki':
        args.batch_size=1;wiki(args)
    elif stage in ('mast','archive_review'):
        # Server creates this task only after a completed negative-attribution scan.
        wikifile=folder/'wiki_input.jsonl'
        write_rows(wikifile,[{'id':obj['id'],'wiki_status':'not_detected'}])
        args.wiki_glob=str(wikifile);args.max_radius_arcmin=payload['max_radius'];args.max_obs_saved=payload['max_obs']
        mast(args)
    else:raise ValueError(f'Unsupported stage {stage}')
    result=list(read_rows(out))
    if len(result)!=1:raise RuntimeError('Task did not produce exactly one result')
    if stage in ('mast','archive_review') and (payload.get('investigate') or os.getenv('HUBBLE_INVESTIGATE_DIR')):
        from investigator import investigate
        result[0]['investigation']=investigate({**obj,**result[0]},os.getenv('HUBBLE_INVESTIGATE_DIR',str(folder.parent/'investigations')))
        if stage=='archive_review':
            result[0]['archive_review_status']='reviewed'
            result[0]['publication_status']=obj.get('publication_status','not_assessed')
    return result[0]

if __name__=='__main__':
    path=Path(sys.argv[1]);job=json.loads(path.read_text(encoding='utf-8'))
    result=execute(job,path.parent)
    atomic_json(path.parent/'completion.json',result)
