"""Crossmatch named catalog positions against the public HST image archive."""
import argparse
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlencode
import time
import requests
from astropy.coordinates import SkyCoord
import astropy.units as u
from scipy.spatial import cKDTree
import numpy as np

TAP='https://mast.stsci.edu/vo-tap/api/v0.1/caom/async'
FIELDS=('target_name','obsid','obs_id','proposal_id','s_ra','s_dec','filters','s_region','instrument_name','t_exptime')
PAPERTRACK='https://archive.stsci.edu/papertrack/api/v0.1/public-search'
CACHE_TTL_SECONDS=14*24*60*60


def cache_is_fresh(path):
    return path.exists() and time.time()-path.stat().st_mtime<CACHE_TTL_SECONDS


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


def fetch_archive_band(cache_dir, lower, upper):
    # Version the cache because its schema includes proposal IDs used by Papertrack.
    cache=Path(cache_dir)/'mast-hst-public-images-v2'
    cache.mkdir(parents=True,exist_ok=True)
    session=requests.Session()
    session.headers['User-Agent']='HubbleScanner/1.0 (public HST archive crossmatch)'
    if lower<0 or upper>360 or lower>=upper:raise ValueError('Invalid RA band')
    path=cache/f'ra-{lower:03d}-{upper:03d}.csv'
    if cache_is_fresh(path): raw=path.read_text(encoding='utf-8')
    else:
        print(f'Querying MAST image archive, RA {lower}-{upper} degrees',flush=True)
        raw=run_tap_band(session,lower,upper)
        path.write_text(raw,encoding='utf-8')
    rows=list(csv.DictReader(io.StringIO(raw)))
    if len(rows)>=100000:
        raise RuntimeError(f'MAST RA band {lower}-{upper} hit the 100000-row cap, refusing incomplete results')
    return rows


def fetch_archive(cache_dir):
    records=[]
    for band in range(36):
        records.extend(fetch_archive_band(cache_dir,band*10,(band+1)*10))
    return records


def _normalize_name(value):
    value=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+',' ',value).strip()


def fetch_papers(program_ids,cache_dir,session=None):
    """Fetch published STScI HST science papers linked to MAST proposal IDs."""
    cache=Path(cache_dir)/'papertrack-hst'
    cache.mkdir(parents=True,exist_ok=True)
    session=session or requests.Session()
    session.headers['User-Agent']='HubbleScanner/1.0 (MAST Papertrack publication crosscheck)'
    programs=sorted({str(p).strip().upper() for p in program_ids if str(p or '').strip()
                     and str(p).strip().upper() not in {'N/A','NA','NONE','NULL','-'}})
    papers=[]
    for start in range(0,len(programs),50):
        batch=programs[start:start+50]
        batch_key=hashlib.sha256(','.join(batch).encode()).hexdigest()[:20]
        page=0
        while True:
            path=cache/f'{batch_key}-{page:04d}.json'
            if cache_is_fresh(path):
                data=json.loads(path.read_text(encoding='utf-8'))
            else:
                body={'mission':'HST','page':page,'pagesize':1000,'hst_programs':','.join(batch),
                      'papertypes':['SCIENCE'],'paper_state':'published','format':'json'}
                response=session.post(PAPERTRACK,json=body,timeout=(20,120))
                response.raise_for_status()
                data=response.json()
                if not isinstance(data,list):
                    raise RuntimeError(f'MAST Papertrack returned an unexpected response for page {page}')
                path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            if not isinstance(data,list):
                raise RuntimeError(f'Invalid cached MAST Papertrack page: {path}')
            papers.extend(data)
            if len(data)<1000: break
            page+=1
            if page>=100: raise RuntimeError('MAST Papertrack pagination exceeded 100 pages for one batch')
    return papers

def scan(objects,out,cache_dir,observations_file=None):
    from footprint_groups import parse_sregion
    objects=[o for o in objects if o.get('ra_deg') is not None and o.get('dec_deg') is not None]
    if observations_file:
        observations=[r for r in __import__('runtime').read_rows(observations_file)]
    else: observations=fetch_archive(cache_dir)
    observations=[r for r in observations if r.get('s_region') and
                  r.get('s_ra') not in (None,'') and r.get('s_dec') not in (None,'')]
    ra=np.array([float(r['s_ra']) for r in observations]);dec=np.array([float(r['s_dec']) for r in observations])
    coords=SkyCoord(ra*u.deg,dec*u.deg)
    xyz=np.column_stack((np.cos(coords.dec.rad)*np.cos(coords.ra.rad),
                         np.cos(coords.dec.rad)*np.sin(coords.ra.rad),np.sin(coords.dec.rad)))
    tree=cKDTree(xyz)
    results=[]
    program_by_object=[]
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
        programs=sorted({str(o.get('proposal_id','')).strip().upper() for o in hits
                         if str(o.get('proposal_id','')).strip()
                         and str(o.get('proposal_id','')).strip().upper() not in {'N/A','NA','NONE','NULL','-'}})
        results.append({**obj,'mast_observation_count':len(hits),'mast_filters':filters,
             'mast_filter_count':len(filters),'mast_obsids':[o['obsid'] for o in hits],
             '_matched_observations':hits,
             'mast_program_ids':programs,
             'archive_status':'observed' if hits else 'no_public_image_at_center',
             'mast_search_url':'https://archive.stsci.edu/hst/search.php?'+urlencode({
                 'RA':ora,'DEC':odec,'radius':radius,'action':'Search'}),
             'mast_archive':'MAST public HST science image footprints','publication_status':'not_assessed'})
        program_by_object.append(programs)
        if index%10000==0: print(f'Matched {index}/{len(objects)} catalog targets',flush=True)
    all_programs={p for group in program_by_object for p in group}
    try:
        papers=fetch_papers(all_programs,cache_dir)
    except Exception as exc:
        print(f'MAST Papertrack lookup failed; archive matches remain usable: {exc}',flush=True)
        papers=[]
        paper_lookup_failed=True
    else:
        paper_lookup_failed=False
    papers_by_program={}
    for paper in papers:
        paper_types=paper.get('papertypes') or [paper.get('paper_type',paper.get('papertype','SCIENCE'))]
        if 'SCIENCE' not in {str(value).upper() for value in paper_types}: continue
        if paper.get('refereed') is False: continue
        if str(paper.get('paper_state','published')).lower()=='preprint': continue
        short={k:paper.get(k) for k in ('bibcode','title','publication_date','journal','refereed')}
        if short.get('bibcode'):
            short['ads_url']='https://ui.adsabs.harvard.edu/abs/'+str(short['bibcode'])+'/abstract'
        associated_programs=paper.get('programs') or paper.get('program_ids') or []
        if isinstance(associated_programs,str): associated_programs=associated_programs.split(',')
        for program in associated_programs:
            program=str(program).strip().upper()
            key=short.get('bibcode') or short.get('title')
            paper_row=papers_by_program.setdefault(program,{}).setdefault(
                key,{**short,'matched_program_ids':[]})
            if program not in paper_row['matched_program_ids']:
                paper_row['matched_program_ids'].append(program)
    for row,programs in zip(results,program_by_object):
        associated_map={}
        for program in programs:
            for key,paper in papers_by_program.get(program,{}).items():
                match=associated_map.setdefault(key,{**paper,'matched_program_ids':[]})
                if program not in match['matched_program_ids']:
                    match['matched_program_ids'].append(program)
        associated=[associated_map[key] for key in sorted(associated_map)]
        row['program_paper_count']=len(associated)
        row['program_papers']=associated
        aliases=row.get('wiki_titles') or row.get('aliases') or [row.get('id','')]
        normalized_aliases={_normalize_name(alias) for alias in aliases
                             if len(_normalize_name(alias))>=4 or
                             (len(_normalize_name(alias))==3 and any(c.isdigit() for c in _normalize_name(alias)))}
        title_matches=[]
        for paper in associated:
            title=_normalize_name(paper.get('title',''))
            if any(re.search(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',title)
                   for alias in normalized_aliases):
                title_matches.append(paper)
        row['object_name_in_paper_title']=bool(title_matches)
        row['object_title_papers']=title_matches
        row['program_bibcodes']=sorted({p['bibcode'] for p in associated if p.get('bibcode')})
        row['object_title_bibcodes']=sorted({p['bibcode'] for p in title_matches if p.get('bibcode')})
        if not row['mast_observation_count']:
            row['publication_status']='no_public_hst_image_at_center'
        elif paper_lookup_failed:
            row['publication_status']='lookup_failed'
        elif not programs:
            row['publication_status']='program_id_missing'
        elif associated:
            row['publication_status']='program_has_published_science_paper'
        elif row['mast_observation_count']:
            row['publication_status']='no_published_science_paper_found_for_programs'
    results.sort(key=lambda r:(r['mast_filter_count'],r['mast_observation_count']),reverse=True)
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',encoding='utf-8') as f:
        for r in results:
            saved={k:v for k,v in r.items() if k not in {'program_papers','object_title_papers','_matched_observations'}}
            f.write(json.dumps(saved,ensure_ascii=False)+'\n')
    cols=['id','object_type','source','source_name','source_url','source_license','ra_deg','dec_deg',
          'mast_observation_count','mast_filter_count','mast_filters','mast_program_ids',
          'mast_search_url','archive_status','publication_status','program_paper_count',
          'object_name_in_paper_title']
    with out.with_suffix('.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader()
        for r in results:
            writer.writerow({k:json.dumps(r.get(k),ensure_ascii=False) if isinstance(r.get(k),list) else r.get(k,'') for k in cols})
    paper_cols=['program_id','bibcode','title','publication_date','journal','ads_url']
    with out.with_name(out.stem+'.papers.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=paper_cols);writer.writeheader()
        for program in sorted(papers_by_program):
            for paper in sorted(papers_by_program[program].values(),key=lambda p:p.get('bibcode') or p.get('title') or ''):
                writer.writerow({'program_id':program,
                    'bibcode':paper.get('bibcode',''),'title':paper.get('title',''),
                    'publication_date':paper.get('publication_date',''),'journal':paper.get('journal',''),
                    'ads_url':paper.get('ads_url','')})
    target_paper_cols=['object_id','object_name','ra_deg','dec_deg','program_ids','bibcode','title',
                       'publication_date','journal','ads_url']
    with out.with_name(out.stem+'.target_papers.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=target_paper_cols);writer.writeheader()
        seen_target_papers=set()
        for row in results:
            for paper in row['object_title_papers']:
                key=(row.get('id'),paper.get('bibcode'))
                if key in seen_target_papers: continue
                seen_target_papers.add(key)
                writer.writerow({'object_id':row.get('id',''),
                    'object_name':(row.get('wiki_titles') or row.get('aliases') or [''])[0],
                    'ra_deg':row.get('ra_deg',''),'dec_deg':row.get('dec_deg',''),
                    'program_ids':','.join(paper.get('matched_program_ids',[])),
                    'bibcode':paper.get('bibcode',''),'title':paper.get('title',''),
                    'publication_date':paper.get('publication_date',''),'journal':paper.get('journal',''),
                    'ads_url':paper.get('ads_url','')})
    review_cols=['id','object_name','object_type','source','source_name','source_url','source_license',
                 'ra_deg','dec_deg','mast_observation_count','mast_filters','mast_program_ids',
                 'publication_status','program_paper_count','object_name_in_paper_title','mast_search_url']
    with out.with_name(out.stem+'.review.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=review_cols);writer.writeheader()
        for row in results:
            if row['mast_observation_count']:
                values={**row,'object_name':(row.get('wiki_titles') or row.get('aliases') or [''])[0]}
                writer.writerow({k:json.dumps(values.get(k),ensure_ascii=False) if isinstance(values.get(k),list)
                                 else values.get(k,'') for k in review_cols})
    observation_cols=['object_id','object_name','ra_deg','dec_deg','obsid','obs_id','program_id',
                      'filters','instrument_name','t_exptime','mast_search_url']
    with out.with_name(out.stem+'.observations.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=observation_cols);writer.writeheader()
        for row in results:
            object_name=(row.get('wiki_titles') or row.get('aliases') or [''])[0]
            for observation in row['_matched_observations']:
                writer.writerow({'object_id':row.get('id',''),'object_name':object_name,
                    'ra_deg':row.get('ra_deg',''),'dec_deg':row.get('dec_deg',''),
                    'obsid':observation.get('obsid',''),'obs_id':observation.get('obs_id',''),
                    'program_id':observation.get('proposal_id',''),'filters':observation.get('filters',''),
                    'instrument_name':observation.get('instrument_name',''),
                    't_exptime':observation.get('t_exptime',''),'mast_search_url':row['mast_search_url']})
            row.pop('_matched_observations',None)
    hits=sum(bool(r['mast_observation_count']) for r in results)
    named_papers=sum(r['object_name_in_paper_title'] for r in results)
    program_papers=sum(r['program_paper_count']>0 for r in results)
    print(f'Public HST image center matches: {hits}/{len(results)}; '
          f'{program_papers} matched targets have a published science paper linked to their HST program; '
          f'{named_papers} have a catalog name in a linked paper title. '
          'Program links do not prove the paper studied that target.',flush=True)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',default='out/spacecatalog-hst.jsonl')
    p.add_argument('--cache',default='.cache/spacecatalog-hst')
    p.add_argument('--observations-file')
    a=p.parse_args()
    from catalogs import spacecatalog_rows
    objects=spacecatalog_rows(Path(a.cache).parent)
    scan(objects,a.out,a.cache,a.observations_file)
