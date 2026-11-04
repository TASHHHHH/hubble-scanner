"""Reproducible offline prototype demonstration. Every target/product is synthetic."""
import argparse
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from scanner import write_jsonl,merged
from footprint_groups import run as group_run
from fits_verify import verify
from mast_support import product_rows,FIELDS
from test_regressions import item,make_fits

def run(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    target=item()
    target.update({'source':'synthetic_fixture','wiki_titles':['SYNTHETIC'],'publication_status':'not_assessed'})
    write_jsonl(out/'catalog.jsonl',[{k:v for k,v in target.items() if k not in ('observations','status','wiki_status')}])
    write_jsonl(out/'wiki_0.jsonl',[{'id':target['id'],'wiki_status':'not_detected','fixture':True}])
    write_jsonl(out/'mast_0.jsonl',[{'id':target['id'],'status':'ok','observation_count':3,'filter_count':3,
        'filters':['F435W','F606W','F814W'],'observations':target['observations'],'fixture':True}])
    merged(SimpleNamespace(catalog=out/'catalog.jsonl',wiki_glob=str(out/'wiki_*.jsonl'),mast_glob=str(out/'mast_*.jsonl'),out=str(out/'candidates.jsonl')))
    group_run(SimpleNamespace(results=out/'candidates.jsonl',out=str(out/'filter_groups.jsonl'),min_overlap=0.35,top_n=3))
    paths=[]
    for filt in ('F435W','F606W','F814W'):
        path=out/f'SYNTHETIC_{filt}.fits'
        if not path.exists():make_fits(path,filt)
        paths.append(str(path))
    result=verify(paths)
    (out/'fits_overlap.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    from footprint_groups import grouped_candidates
    def provider(obsid):
        return [{'obsID':obsid,'productType':'SCIENCE','productSubGroupDescription':'DRC',
                 'productFilename':f'SYNTHETIC_{obsid}_drc.fits','dataURI':f'mast:HST/product/SYNTHETIC_{obsid}_drc.fits'}]
    rows=product_rows(target,grouped_candidates(target)[0][0],0,provider=provider)
    with (out/'SYNTHETIC_tsdp_schema_example.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    assert len(result['groups']) == 1
    print('Offline demo passed. SYNTHETIC CSV demonstrates schema only; do not queue its fake URIs.')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',default='out/demo');a=p.parse_args();run(a.out)
