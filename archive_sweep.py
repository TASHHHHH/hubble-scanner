"""Crossmatch named catalog positions against the public HST image archive."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from urllib.parse import urlencode
import time
import requests
from astropy.coordinates import SkyCoord
import astropy.units as u
from scipy.spatial import cKDTree
import numpy as np

TAP='https://mast.stsci.edu/vo-tap/api/v0.1/caom/async'
FIELDS=('target_name','obsid','obs_id','s_ra','s_dec','filters','s_region','instrument_name','t_exptime')


def run_tap_band(session, lower, upper):
    query=("SELECT TOP 100000 "+','.join(FIELDS)+" FROM dbo.obspointing "
           "WHERE obs_collection='HST' AND dataproduct_type='image' "
           "AND intenttype='science' AND datarights='PUBLIC' "
           f"AND s_ra>={lower} AND s_ra<{upper}")
    response=session.post(TAP,data={'REQUEST':'doQuery','LANG':'ADQL','FORMAT':'csv','QUERY':query},
                          allow_redirects=False,timeout=(20,120))
    if response.status_code!=303:
        response.raise_for_status()
        raise RuntimeError(f'MAST TAP expected an async job, got {response.status_code}')
    job=response.headers['Location'].rstrip('/')
    started=session.post(job+'/phase',data={'PHASE':'RUN'},timeout=30)
    started.raise_for_status()
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        phase=session.get(job+'/phase',timeout=30).text.strip()
        if phase=='COMPLETED':break
        if phase in ('ERROR','ABORTED'):
            detail=session.get(job+'/error',timeout=30).text[:2000]
            raise RuntimeError(f'MAST TAP {phase}: {detail}')
        time.sleep(3)
    else:
        raise TimeoutError(f'MAST TAP query for RA {lower}-{upper} exceeded 30 minutes')
    result=session.get(job+'/results/result',timeout=(20,300))
    result.raise_for_status()
    if result.text.lstrip().startswith('<?xml'):
        raise RuntimeError(f'MAST TAP result for RA {lower}-{upper} was an error VOTable')
    return result.text


def fetch_archive(cache_dir):
    cache=Path(cache_dir)/'mast-hst-public-images'
    cache.mkdir(parents=True,exist_ok=True)
    session=requests.Session()
    session.headers['User-Agent']='HubbleScanner/1.0 (public HST archive crossmatch)'
    records=[]
    for band in range(36):
        lower,upper=band*10,(band+1)*10
        path=cache/f'ra-{lower:03d}-{upper:03d}.csv'
        if path.exists():
            raw=path.read_text(encoding='utf-8')
        else:
            print(f'Querying MAST image archive, RA {lower}-{upper} degrees',flush=True)
            raw=run_tap_band(session,lower,upper)
            path.write_text(raw,encoding='utf-8')
        rows=list(csv.DictReader(io.StringIO(raw)))
        if len(rows)>=100000:
            raise RuntimeError(f'MAST RA band {lower}-{upper} hit the 100000-row cap, refusing incomplete results')
        records.extend(rows)
    return records

def scan(objects,out,cache_dir):
    from footprint_groups import parse_sregion
    objects=[o for o in objects if o.get('ra_deg') is not None and o.get('dec_deg') is not None]
    observations=fetch_archive(cache_dir)
    observations=[r for r in observations if r.get('s_region') and r.get('s_ra') and r.get('s_dec')]
    ra=np.array([float(r['s_ra']) for r in observations]);dec=np.array([float(r['s_dec']) for r in observations])
    coords=SkyCoord(ra*u.deg,dec*u.deg)
    xyz=np.column_stack((np.cos(coords.dec.rad)*np.cos(coords.ra.rad),
                         np.cos(coords.dec.rad)*np.sin(coords.ra.rad),np.sin(coords.dec.rad)))
    tree=cKDTree(xyz)
    results=[]
    for index,obj in enumerate(objects,1):
        ora=float(obj['ra_deg']);odec=float(obj['dec_deg'])
        radius=max(.5,min(12,float(obj.get('major_arcmin') or 2)/2))
        angular=(radius+6)/60
        center=SkyCoord(ora*u.deg,odec*u.deg)
        vector=(math.cos(center.dec.rad)*math.cos(center.ra.rad),
                math.cos(center.dec.rad)*math.sin(center.ra.rad),math.sin(center.dec.rad))
        chord=2*math.sin(math.radians(angular)/2)
        hits=[]
        for i in tree.query_ball_point(vector,chord):
            region,_=parse_sregion(observations[i]['s_region'],ora,odec)
            if region is not None and region.covers(__import__('shapely.geometry',fromlist=['Point']).Point(0,0)):
                hits.append(observations[i])
        filters=sorted({f.strip() for o in hits for f in o.get('filters','').replace(',', ';').split(';') if f.strip()})
        results.append({**obj,'mast_observation_count':len(hits),'mast_filters':filters,
             'mast_filter_count':len(filters),'mast_obsids':[o['obsid'] for o in hits],
             'archive_status':'observed' if hits else 'no_public_image_at_center',
             'mast_search_url':'https://archive.stsci.edu/hst/search.php?'+urlencode({
                 'RA':ora,'DEC':odec,'radius':radius,'action':'Search'}),
             'mast_archive':'MAST public HST science image footprints','publication_status':'not_assessed'})
        if index%10000==0: print(f'Matched {index}/{len(objects)} catalog targets',flush=True)
    results.sort(key=lambda r:(r['mast_filter_count'],r['mast_observation_count']),reverse=True)
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',encoding='utf-8') as f:
        for r in results:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    cols=['id','object_type','source','source_name','source_url','source_license','ra_deg','dec_deg',
          'mast_observation_count','mast_filter_count','mast_filters','mast_obsids','mast_search_url',
          'archive_status','publication_status']
    with out.with_suffix('.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader()
        for r in results:
            writer.writerow({k:json.dumps(r.get(k),ensure_ascii=False) if isinstance(r.get(k),list) else r.get(k,'') for k in cols})
    hits=sum(bool(r['mast_observation_count']) for r in results)
    print(f'Public HST image center matches: {hits}/{len(results)}. Publication status is not assessed.',flush=True)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',default='out/spacecatalog-hst.jsonl')
    p.add_argument('--cache',default='.cache/spacecatalog-hst')
    a=p.parse_args()
    from catalogs import spacecatalog_rows
    objects=spacecatalog_rows(Path(a.cache).parent)
    scan(objects,a.out,a.cache)
