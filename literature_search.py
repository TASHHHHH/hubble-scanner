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
    catalog_names=[]
    # source_name identifies the upstream catalog (e.g. "HYG Database v4.1"),
    # and synthetic SC-* ids are internal keys; neither is a sky-object alias.
    for value in [*(row.get('wiki_titles') or []), *(row.get('aliases') or [])]:
        if not value: continue
        value=' '.join(str(value).split()).strip()
        if value and value.casefold() not in {n.casefold() for n in catalog_names}:
            catalog_names.append(value)
    target_names=[]
    ignored={'ANY','UNKNOWN','N/A','NA','NONE','NULL','-'}
    for value in row.get('mast_target_names') or []:
        value=' '.join(str(value).split()).strip()
        if (value and value.upper() not in ignored and 'DITHER' not in value.upper()
                and value.casefold() not in {n.casefold() for n in target_names}):
            target_names.append(value)
    # Catalog identities are the main query terms. Include up to two actual
    # MAST target labels as aliases because archive labels often name a SN,
    # companion, or field target instead of the catalog host object.
    names=catalog_names[:4] if catalog_names else target_names[:6]
    for value in (target_names[:2] if catalog_names else []):
        if value.casefold() not in {name.casefold() for name in names}:
            names.append(value)
    if not names and row.get('id'):
        names.append(' '.join(str(row['id']).split()).strip())
    return names[:6]


def make_query(names):
    phrases=[]
    for name in names:
        phrase=name.replace('\\',' ').replace('"',' ').strip()
        if not phrase: continue
        phrases.append('"'+phrase+'"')
    if not phrases: raise ValueError('Target has no searchable names')
    alternatives=' OR '.join(phrases)
    # `object:` works in the ADS web UI but is not an indexed query field in
    # the ADS API's Solr schema (HTTP 400: undefined field object). Search the
    # API's supported `full:` index, which covers title, abstract, body,
    # acknowledgements and keywords.
    return f'database:astronomy AND full:({alternatives})'


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
    failures=0;max_failures=max(1,retries)
    while failures<max_failures:
        try:
            response=session.get(ADS_URL,headers=headers,params=params,timeout=45)
            if response.status_code in (401,403):
                raise RuntimeError('ADS rejected the API token (HTTP '+str(response.status_code)+'). Check the token and retry.')
            if response.status_code==429:
                response_headers=getattr(response,'headers',{}) or {}
                reset=response_headers.get('X-RateLimit-Reset')
                retry_after=response_headers.get('Retry-After')
                try:
                    wait=max(1.0,float(reset)-time.time()+2.0) if reset else max(1.0,float(retry_after))
                except (TypeError,ValueError):
                    raise RuntimeError('ADS rate limit reached (HTTP 429) without a usable reset time; retry later.')
                reset_text=(time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(float(reset)))
                            if reset else 'after the server retry interval')
                print(f'ADS daily limit reached; pausing until {reset_text}.',flush=True)
                # ADS reports its daily reset in X-RateLimit-Reset (UTC epoch).
                # Do not consume a failure retry: keep the single target pending
                # and resume it after quota resets.
                time.sleep(min(wait,24*60*60))
                continue
            if response.status_code>=400:
                try:
                    body=response.json()
                    detail=''
                    if isinstance(body,dict):
                        nested=body.get('response')
                        detail=body.get('error') or body.get('message')
                        if not detail and isinstance(nested,dict): detail=nested.get('error')
                    if isinstance(detail,(dict,list)): detail=json.dumps(detail,ensure_ascii=False)
                except (ValueError,AttributeError): detail=''
                if not detail: detail=' '.join(str(getattr(response,'text','')).split())
                detail=str(detail).replace(token,'[redacted]')[:1000]
                last_error=f'ADS HTTP {response.status_code}'+(f': {detail}' if detail else '')
                if response.status_code<500 and response.status_code!=408:
                    raise RuntimeError('ADS rejected the search request. '+last_error)
                failures+=1
                if failures<max_failures:
                    time.sleep(min(30,2**(failures-1)));continue
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
            failures+=1
            if failures<max_failures: time.sleep(min(30,2**(failures-1)))
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


def _is_transient_error(message):
    message=str(message or '')
    return (message in {'ReadTimeout','ConnectTimeout','ConnectionError','Timeout'}
            or message.startswith('ADS HTTP 408')
            or message.startswith('ADS HTTP 425')
            or message.startswith('ADS HTTP 5'))


def telegram_notify(message):
    """Send to the locally configured Telegram chat; never fail the scan."""
    try:
        from secret_store import load
        from investigator import notify
        credentials=load()
        return notify(message,credentials) if credentials else 'not_configured'
    except Exception:
        return 'failed'


def _progress_text(checked,total,counts,leads=(),unmatched=()):
    lines=[f'ADS literature search: {checked}/{total} objects checked',
           f"Since last update — paper matches: {counts['papers_found']} | no name match: {counts['no_name_match']} | errors: {counts['lookup_failed']}"]
    if unmatched:
        lines.append('Objects with no ADS name match in these 10 searches: '+', '.join(unmatched[:10]))
        lines.append('Search used catalog-provided names; missing aliases can cause false no-name results.')
    if leads:
        lines.append('Manual-review leads (no Papertrack-linked program paper; ADS status shown): '+', '.join(leads[:10]))
        lines.append('A missing program-paper link is not proof that HST observations were unpublished.')
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
    processed=0;batch_counts={'papers_found':0,'no_name_match':0,'lookup_failed':0};batch_leads=[];batch_unmatched=[]
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
                                         'mast_target_names',
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
        if result['literature_status']=='no_name_match':
            batch_unmatched.append(str((names or [row.get('id','unknown')])[0]))
        if row.get('publication_status')=='no_published_science_paper_found_for_programs':
            display_name=(names or [row.get('id','unknown')])[0]
            batch_leads.append(f"{display_name} [{result['literature_status']}]")
        # Append each completed lookup immediately. Rewriting and sorting the
        # full result set here makes a long scan quadratic in both I/O and JSON
        # serialization time. Results are sorted once when the run exits.
        write_rows(out_path, [result], append=True)
        if delay and index+1<len(rows):time.sleep(delay)
        if (processed%100)==0:print(f'Literature searched: {len(prior)} / {len(rows)}',flush=True)
        if processed%10==0:
            report(_progress_text(processed,searchable_total,batch_counts,batch_leads,batch_unmatched))
            batch_counts={'papers_found':0,'no_name_match':0,'lookup_failed':0};batch_leads=[];batch_unmatched=[]
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
        report(_progress_text(processed,searchable_total,batch_counts,batch_leads,batch_unmatched))
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
    token=os.getenv('HUBBLE_ADS_TOKEN','')
    try:
        retry_counts={}
        out_path=Path(args.out)
        if out_path.exists():
            for row in read_rows(out_path):
                if row.get('literature_status')=='lookup_failed' and _is_transient_error(row.get('literature_error')):
                    retry_counts[row.get('id','')]=1
        while True:
            counts=run(args.input,args.out,token,args.cache,args.delay,args.limit)
            if not counts.get('lookup_failed'):break
            failed=[row for row in read_rows(args.out) if row.get('literature_status')=='lookup_failed']
            transient=[row for row in failed if _is_transient_error(row.get('literature_error'))]
            if not failed or len(transient)!=len(failed):break
            exhausted=[row for row in transient if retry_counts.get(row.get('id',''),0)>=5]
            if exhausted:
                print('Transient ADS request still failing after five resume attempts; saved progress is safe to resume later.',flush=True)
                break
            waits=[]
            for row in transient:
                ident=row.get('id','');retry_counts[ident]=retry_counts.get(ident,0)+1
                waits.append(min(300,30*2**(retry_counts[ident]-2)))
            wait=max(waits or [30])
            print(f'{len(transient)} transient ADS lookup(s) failed; keeping successful checkpoints and retrying in {wait}s.',flush=True)
            time.sleep(wait)
    except ValueError as exc:parser.error(str(exc))


if __name__=='__main__':main()
