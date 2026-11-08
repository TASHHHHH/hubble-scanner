"""Search NASA ADS for literature mentioning HST archive-matched targets.

This is a name/object-tag discovery aid. Hits and misses both require review;
the tool does not decide whether a paper actually studies the HST observations.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import time

import requests
from runtime import read_rows, write_rows

ADS_URL = 'https://api.adsabs.harvard.edu/v1/search/query'
FIELDS = ('bibcode,title,author,pubdate,pub,doi,abstract,keyword,property')


def names_for(row):
    names=[]
    for value in [*(row.get('wiki_titles') or []), *(row.get('aliases') or []),
                  row.get('source_name'), row.get('id')]:
        if not value: continue
        value=' '.join(str(value).split()).strip()
        if value and value.casefold() not in {n.casefold() for n in names}:
            names.append(value)
    # Giant alias lists make overly broad ADS queries. Prefer the catalog's
    # displayed names, plus a few alternate designations.
    return names[:6]


def make_query(names):
    terms=[]
    for name in names:
        phrase=name.replace('\\',' ').replace('"',' ').strip()
        if not phrase: continue
        safe='"'+phrase+'"'
        terms.extend((f'object:{safe}',f'full:{safe}'))
    if not terms: raise ValueError('Target has no searchable names')
    return 'database:astronomy AND ('+' OR '.join(terms)+')'


def _cache_path(cache_dir, query):
    digest=hashlib.sha256(query.encode('utf-8')).hexdigest()
    return Path(cache_dir)/(digest+'.json')


def search_ads(query, token, cache_dir, session=None, rows=25, retries=3):
    """Return cached ADS docs, or query its official API with a bearer token."""
    path=_cache_path(cache_dir,query)
    if path.exists():
        try:
            cached=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(cached,dict) and isinstance(cached.get('docs'),list): return cached
        except (OSError,ValueError): pass
    path.parent.mkdir(parents=True,exist_ok=True)
    session=session or requests.Session()
    headers={'Authorization':'Bearer '+token,'Accept':'application/json',
             'User-Agent':'HubbleScanner/1.0 object literature crosscheck'}
    params={'q':query,'fl':FIELDS,'rows':rows,'start':0,'sort':'score desc'}
    last_error='ADS lookup failed'
    for attempt in range(retries):
        try:
            response=session.get(ADS_URL,headers=headers,params=params,timeout=45)
            if response.status_code in (401,403):
                raise RuntimeError('ADS rejected the API token (HTTP '+str(response.status_code)+'). Check the token and retry.')
            if response.status_code==429:
                raise RuntimeError('ADS rate limit reached (HTTP 429); wait before resuming.')
            if response.status_code>=400:
                detail=' '.join(str(getattr(response,'text','')).split())[:240]
                last_error=f'ADS HTTP {response.status_code}'+(f': {detail}' if detail else '')
                if response.status_code<500 and response.status_code!=408:
                    raise RuntimeError('ADS rejected the search request. '+last_error)
                if attempt+1<retries:
                    time.sleep(min(30,2**attempt));continue
                raise RuntimeError(last_error)
            data=response.json()
            docs=data.get('response',{}).get('docs')
            if not isinstance(docs,list): raise RuntimeError('ADS returned an unexpected response.')
            result={'docs':docs,'num_found':data.get('response',{}).get('numFound',len(docs)),
                    'query':query,'searched_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
            path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            return result
        except RuntimeError: raise
        except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
            last_error=type(exc).__name__
            if attempt+1<retries: time.sleep(min(30,2**attempt))
    raise RuntimeError(last_error)


def _paper(doc):
    def text_value(value):
        if isinstance(value,(list,tuple)): return ' '.join(str(part) for part in value if part)
        return str(value or '')
    bibcode=doc.get('bibcode','')
    author=doc.get('author') or []
    if isinstance(author,str): author=[author]
    doi=doc.get('doi') or []
    if isinstance(doi,str): doi=[doi]
    return {'bibcode':bibcode,'title':text_value(doc.get('title')),
            'first_author':author[0] if author else '',
            'year':str(doc.get('pubdate',''))[:4], 'journal':text_value(doc.get('pub')),
            'doi':doi[0] if doi else '',
            'ads_url':f'https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract' if bibcode else '',
            'abstract':text_value(doc.get('abstract'))[:4000],
            'refereed':'refereed' in (doc.get('property') or [])}


def telegram_notify(message):
    """Send to the locally configured Telegram chat; never fail the scan."""
    try:
        from secret_store import load
        from investigator import notify
        credentials=load()
        return notify(message,credentials) if credentials else 'not_configured'
    except Exception:
        return 'failed'


def _progress_text(checked,total,counts,leads=()):
    lines=[f'ADS literature search: {checked}/{total} objects checked',
           f"Since last update — paper matches: {counts['papers_found']} | no name match: {counts['no_name_match']} | errors: {counts['lookup_failed']}"]
    if leads:
        lines.append('Potential manual-review leads (no Papertrack program paper + no ADS name match): '+', '.join(leads[:10]))
        lines.append('These are leads to review, not confirmed unpublished objects or discoveries.')
    return '\n'.join(lines)


def run(input_path, out_path, token, cache_dir, delay=0.25, limit=0, session=None, notifier=None):
    if not token: raise ValueError('Set HUBBLE_ADS_TOKEN to your NASA ADS API token.')
    input_rows=list(read_rows(input_path))
    rows=[r for r in input_rows if int(r.get('mast_observation_count') or 0)>0]
    if not rows:
        raise ValueError(f'No archive-matched objects in {input_path}: found {len(input_rows)} catalog rows, '
                         'but none have mast_observation_count > 0. Restore or collect valid HST archive results first.')
    out_path=Path(out_path);out_path.parent.mkdir(parents=True,exist_ok=True)
    prior={}
    if out_path.exists():
        for row in read_rows(out_path):
            if row.get('id') and row.get('literature_status') in ('papers_found','no_name_match'):
                prior[row['id']]=row
    processed=0;batch_counts={'papers_found':0,'no_name_match':0,'lookup_failed':0};batch_leads=[]
    notifier=notifier or telegram_notify
    def report(text):
        try:notifier(text)
        except Exception:pass
    searchable_total=sum(1 for r in rows if r.get('id') not in prior)
    for index,row in enumerate(rows):
        if row.get('id') in prior: continue
        if limit and processed>=limit: break
        names=names_for(row)
        result={k:row.get(k) for k in ('id','source_name','wiki_titles','aliases','ra_deg','dec_deg',
                                         'mast_observation_count','mast_program_ids','mast_obsids',
                                         'publication_status','program_paper_count','object_name_in_paper_title')}
        result['search_names']=names
        try:
            query=make_query(names)
            found=search_ads(query,token,cache_dir,session=session)
            papers=[_paper(doc) for doc in found['docs']]
            result.update({'literature_status':'papers_found' if papers else 'no_name_match',
                           'ads_num_found':found['num_found'],'ads_query':query,'papers':papers})
        except RuntimeError as exc:
            result.update({'literature_status':'lookup_failed','literature_error':str(exc)})
            prior[row['id']]=result
            batch_counts['lookup_failed']+=1
            print(f"ADS lookup stopped at {row.get('id')}: {exc}",flush=True)
            report(_progress_text(processed+1,searchable_total,batch_counts))
            # A lookup error is usually shared by the API, credentials or query
            # builder. Checkpoint it and stop instead of issuing thousands more.
            break
        prior[row['id']]=result
        processed+=1
        batch_counts[result['literature_status']]+=1
        if (result['literature_status']=='no_name_match'
                and row.get('publication_status')=='no_published_science_paper_found_for_programs'):
            batch_leads.append(str(row.get('id','unknown')))
        # Checkpoint after every object; existing ADS responses are cached too.
        write_rows(out_path, sorted(prior.values(),key=lambda r:str(r.get('id',''))))
        if delay and index+1<len(rows):time.sleep(delay)
        if (processed%100)==0:print(f'Literature searched: {len(prior)} / {len(rows)}',flush=True)
        if processed%10==0:
            report(_progress_text(processed,searchable_total,batch_counts,batch_leads))
            batch_counts={'papers_found':0,'no_name_match':0,'lookup_failed':0};batch_leads=[]
    write_rows(out_path, sorted(prior.values(),key=lambda r:str(r.get('id',''))))
    papers_path=out_path.with_name(out_path.stem+'.papers.csv')
    columns=('object_id','object_names','literature_status','bibcode','title','first_author',
             'year','journal','doi','ads_url','refereed','abstract')
    with papers_path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        for item in prior.values():
            for paper in item.get('papers',[]):
                writer.writerow({'object_id':item.get('id',''),
                    'object_names':' | '.join(item.get('search_names') or []),
                    'literature_status':item.get('literature_status'),**paper})
    counts={status:sum(1 for r in prior.values() if r.get('literature_status')==status)
            for status in ('papers_found','no_name_match','lookup_failed')}
    print(f'Complete so far: {len(prior)} / {len(rows)}; '+', '.join(f'{k}={v}' for k,v in counts.items()),flush=True)
    print(f'Results: {out_path}\nPaper details: {papers_path}',flush=True)
    if processed%10 or not rows:
        report(_progress_text(processed,searchable_total,batch_counts,batch_leads))
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,help='spacecatalog_hst.jsonl from collection')
    parser.add_argument('--out',required=True,help='literature JSONL output; reruns resume safely')
    parser.add_argument('--cache',required=True,help='persistent ADS response cache directory')
    parser.add_argument('--delay',type=float,default=.25)
    parser.add_argument('--limit',type=int,default=0,help='limit fresh target lookups; 0 means all')
    args=parser.parse_args()
    if args.delay<0 or args.limit<0: parser.error('--delay and --limit must be nonnegative')
    try:run(args.input,args.out,os.getenv('HUBBLE_ADS_TOKEN',''),args.cache,args.delay,args.limit)
    except ValueError as exc:parser.error(str(exc))


if __name__=='__main__':main()
