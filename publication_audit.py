"""Alias-aware discovery with conservative, separately reviewed A--E evidence.

Search hits are leads. A/B require explicit reviewed evidence; failures and
incomplete searches cannot produce a negative classification.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from alias_resolution import QueryCache, now, unique
from literature_search import make_query, ADS_URL, FIELDS
from runtime import atomic_json, read_rows


def classify(identity, searches, reviews):
    if identity != 'confirmed': return 'E'
    for review in reviews:
        if not review.get('identity_verified') or not review.get('excerpt') or not review.get('url'): continue
        host = urlparse(review['url']).hostname or ''
        official = host in ('esahubble.org','hubblesite.org','science.nasa.gov','www.nasa.gov','esa.int','www.esa.int','images.nasa.gov')
        if review.get('kind') == 'official_hst_image' and official and review.get('hst_image_verified'):
            return 'A'
    sources = {s.get('source') for s in searches}
    complete = {'NASA ADS','NASA official image search','ESA official image search'} <= sources and all(
        s['status'] == 'ok' and s.get('complete', False) for s in searches)
    hst_figure = any(r.get('kind') == 'paper_hst_image' and r.get('identity_verified') and
                    r.get('hst_image_verified') and r.get('figure') and r.get('url') and r.get('excerpt') for r in reviews)
    if hst_figure:
        no_release = all(s.get('match_count') == 0 for s in searches if s.get('source') != 'NASA ADS')
        return 'B' if complete and no_release else 'E'
    if any(r.get('kind') == 'paper' and r.get('identity_verified') and
           r.get('url') and r.get('excerpt') for r in reviews): return 'C'
    if reviews: return 'E'
    if complete and all(s.get('match_count') == 0 for s in searches): return 'D'
    return 'E'


def ads_query(cache, names, token):
    query = make_query(names)
    params = {'q': query, 'fl': FIELDS, 'rows': 100, 'start': 0, 'sort': 'score desc'}
    def fetch():
        if not token: raise RuntimeError('ADS token unavailable')
        response = requests.get(ADS_URL, params=params,
            headers={'Authorization': 'Bearer '+token, 'User-Agent':'HubbleScanner alias audit'}, timeout=30)
        response.raise_for_status()
        data = response.json()['response']
        if not isinstance(data.get('docs'), list): raise ValueError('ADS returned invalid document list')
        return data
    result = cache.call('NASA ADS', {'url': ADS_URL, 'params': params, 'checked_aliases': names}, fetch)
    result['complete'] = result['status']=='ok' and len(result['data']['docs']) >= result['data']['numFound']
    result['match_count'] = result['data']['numFound'] if result['status']=='ok' else None
    return result


def official_query(cache, name, source):
    if source == 'NASA':
        url, params = 'https://images-api.nasa.gov/search', {'q':name, 'media_type':'image','page_size':100}
    else:
        url, params = 'https://esahubble.org/images/archive/search/', {'search':name}
    def fetch():
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        if source == 'NASA':
            data = response.json()['collection']
            return {'items':data['items'], 'total_hits':data.get('metadata',{}).get('total_hits'),
                    'url':response.url}
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        links = sorted({urljoin(url,a['href']) for a in soup.select('a[href]')
                        if re.fullmatch(r'/images/[^/]+/', a['href']) and '/archive/' not in a['href']
                        and '/search/' not in a['href']})
        return {'candidate_urls':links, 'url':response.url, 'html':response.text,
                'text':soup.get_text(' ',strip=True)}
    result = cache.call(source + ' official image search', {'url':url,'params':params,'checked_aliases':[name]}, fetch)
    # Search interfaces do not provide exhaustive coverage of all releases.
    result['complete'] = False
    result['match_count'] = (result['data'].get('total_hits') if source=='NASA' else
                            len(result['data']['candidate_urls'])) if result['status']=='ok' else None
    return result


def run(args):
    rows = list(read_rows(args.aliases))
    manifest_path = Path(args.aliases).parent/'manifest.json'
    if manifest_path.exists():
        alias_manifest = json.loads(manifest_path.read_text())
        if alias_manifest['row_count'] != len(rows): raise ValueError('Alias resolution is still incomplete')
    if args.expected_count and len(rows)!=args.expected_count: raise ValueError('Alias row count mismatch')
    if [r['input_row'] for r in rows] != list(range(1,len(rows)+1)): raise ValueError('Alias rows incomplete/out of order')
    if any('resolution_status' not in r for r in rows): raise ValueError('Run alias resolution first')
    out = Path(args.out);out.mkdir(parents=True, exist_ok=True)
    manifest = {'alias_sha256':hashlib.sha256(Path(args.aliases).read_bytes()).hexdigest(), 'rows':len(rows)}
    path = out/'manifest.json'
    if path.exists() and json.loads(path.read_text()) != manifest: raise ValueError('Alias input changed; use a new output directory')
    atomic_json(path,manifest)
    caches = {source:QueryCache(out/'cache', retries=2, delay=1) for source in ('ADS','NASA','ESA')}
    token = os.getenv('HUBBLE_ADS_TOKEN')
    if not token:
        from secret_store import load_ads_token
        token = load_ads_token()
    reviews = list(read_rows(args.reviews)) if args.reviews else []
    results = []
    for row in rows:
        names = unique([row['original_designation']]+row['confirmed_aliases'])
        # Prioritize human-facing identifiers on official galleries. Log remaining
        # aliases explicitly; partial gallery coverage can never establish B/D.
        release_names = unique([row['original_designation'],row.get('canonical_designation')]+[
            n for n in names if re.fullmatch(r'(NGC|IC|UGC|M|Arp)\s*\d+[A-Za-z]?', n)])
        # Independent services have separate one-request-at-a-time rate limiters.
        # A catalog is never queried concurrently with itself.
        def ads_searches():
            return [ads_query(caches['ADS'],names[start:start+30],token) for start in range(0,len(names),30)]
        def release_searches(source):
            return [official_query(caches[source],name,source) for name in release_names]
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(ads_searches),pool.submit(release_searches,'NASA'),pool.submit(release_searches,'ESA')]
            searches = [item for job in jobs for item in job.result()]
        checked_reviews = [r for r in reviews if r.get('input_row')==row['input_row']]
        result = {'input_row':row['input_row'],'object_id':row['object_id'],
            'original_designation':row['original_designation'],'canonical_designation':row['canonical_designation'],
            'identity_status':row['resolution_status'],'publication_status':classify(row['resolution_status'],searches,checked_reviews),
            'ads_checked_aliases':names,'release_checked_aliases':release_names,
            'release_unchecked_aliases':[n for n in names if n not in release_names],
            'searches':searches,'reviewed_evidence':checked_reviews,'queried_at':now(),
            'notes':'Search hits require identity/image review. E includes unreviewed evidence and incomplete coverage. No novelty inference.'}
        atomic_json(out/'checkpoints'/f'{row["input_row"]:05d}.json',result)
        results.append(result)
        print(f'Publication {len(results)}/{len(rows)} {row["original_designation"]}: {result["publication_status"]}',flush=True)
        export(out,results)
    return results


def export(out, results):
    out=Path(out)
    path=out/'publication_checks.jsonl'
    temp=path.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in results),encoding='utf-8')
    os.replace(temp,path)
    fields=['input_row','object_id','original_designation','canonical_designation','identity_status',
            'publication_status','ads_checked_aliases','release_checked_aliases','release_unchecked_aliases','reviewed_evidence','queried_at','notes']
    path=out/'publication_checks.csv';temp=path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for r in results:writer.writerow({k:json.dumps(r[k],ensure_ascii=False) if isinstance(r[k],list) else r[k] for k in fields})
    os.replace(temp,path)


def add_parser(sub):
    p=sub.add_parser('audit-publications',help='Search ADS and official galleries using resolved aliases')
    p.add_argument('--aliases',required=True);p.add_argument('--out',required=True)
    p.add_argument('--expected-count',type=int);p.add_argument('--reviews',help='Reviewed evidence JSONL')
    p.set_defaults(func=run)
