#!/usr/bin/env python3
"""Wikipedia -> HST footprint shortlist. Research prototype, not a novelty detector.

All source queries happen at RUN TIME on the user's machine; this bundle contains
no pre-fetched Wikipedia or Hubble results.
"""
import argparse
import csv
import glob
import hashlib
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from runtime import cached_call, read_rows, write_rows, job_guard
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NGC_URL = 'https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv'
ADDENDUM_URL = 'https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv'
WIKI_URL = 'https://en.wikipedia.org/w/api.php'
COMMONS_URL = 'https://commons.wikimedia.org/w/api.php'
HST_RE = re.compile(r'\b(hubble|hst|esa/hubble)\b', re.I)
INSTITUTION_RE = re.compile(r'\b(stsci|space telescope science institute)\b', re.I)
IMAGE_RE = re.compile(r'\.(jpg|jpeg|png|webp|tif|tiff)$', re.I)
USER_AGENT = os.getenv('HUBBLE_SCANNER_USER_AGENT', 'HubbleCatalogResearch/0.1 (educational astronomy research; add a contact email)')


def session():
    from requests_cache import CachedSession
    s = CachedSession(str(Path(os.getenv('HUBBLE_CACHE_DIR', '.cache')) / 'http'), expire_after=86400, allowable_methods=('GET',), filter_fn=lambda r: r.status_code == 200 and '"error"' not in r.text)
    s.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    retry = Retry(total=4, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), respect_retry_after_header=True)
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


def get_json(s, url, params, pause=0.0):
    if pause: time.sleep(pause)
    r = s.get(url, params=params, timeout=65)
    r.raise_for_status()
    data = r.json()
    if 'warnings' in data:
        raise RuntimeError(f'API warning: {data["warnings"]}')
    if 'error' in data:
        raise RuntimeError(f'API error: {data["error"]}')
    return data


def download_text(s, url):
    r = s.get(url, timeout=60)
    r.raise_for_status()
    return r.content.decode('utf-8-sig')


def firstnum(v):
    m = re.search(r'\d+', str(v or ''))
    return int(m.group()) if m else None


def coord_degrees(ra, dec):
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    try:
        c = SkyCoord(str(ra).replace(' ', ':'), str(dec).replace(' ', ':'), unit=(u.hourangle, u.deg), frame='icrs')
        return round(float(c.ra.deg), 8), round(float(c.dec.deg), 8)
    except Exception:
        return None, None


def safe_float(value, fallback=0.0):
    try:
        result=float(value)
        return result if math.isfinite(result) else fallback
    except (ValueError,TypeError):
        return fallback

def read_ngc_csv(content):
    # Support both semicolon-delimited OpenNGC and comma-delimited exports.
    first_line = content.splitlines()[0]
    delimiter = ',' if 'Name,' in first_line else ';'
    return list(csv.DictReader(io.StringIO(content), delimiter=delimiter))


def extra_vizier_catalogs():
    from catalogs import builtin_catalogs
    return builtin_catalogs()


def validate_object(row):
    ra, dec, size = (float(row[k]) for k in ('ra_deg','dec_deg','major_arcmin'))
    if not all(math.isfinite(v) for v in (ra,dec,size)) or not 0 <= ra < 360 or not -90 <= dec <= 90 or size < 0:
        raise ValueError(f'Invalid ICRS coordinates/size for {row["id"]}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. +()\-]*', row['id']):
        raise ValueError('Catalog id must be a safe filename component')

def load_extra_csv(path):
    """Allow any additional catalog with preconverted ICRS coordinates."""
    results=[]
    with open(path,encoding='utf-8-sig',newline='') as fh:
        for r in csv.DictReader(fh):
            name=r['id'].strip()
            if not name: continue
            results.append({'id':name,'ngc':'','ic':'','messier':'',
                 'object_type':r.get('object_type',''),
                 'ra_deg':float(r['ra_deg']),'dec_deg':float(r['dec_deg']),
                 'major_arcmin':float(r.get('major_arcmin') or 0),
                 'wiki_titles':[x.strip() for x in r.get('wiki_titles',name).split('|') if x.strip()],
                 'source':r.get('source','extra'),'aliases':[]})
    return results


def catalog(args):
    s=session()
    data=Path(args.ngc_csv).read_text(encoding='utf-8-sig') if args.ngc_csv else download_text(s,NGC_URL)
    add=Path(args.addendum_csv).read_text(encoding='utf-8-sig') if args.addendum_csv else download_text(s,ADDENDUM_URL)
    rows=[]
    messiers=set()
    for src, entries in [('openngc',read_ngc_csv(data)),('addendum',read_ngc_csv(add))]:
        for r in entries:
            name=(r.get('Name') or '').strip()
            ngc=firstnum(name) if re.match(r'^NGC\s*\d+',name,re.I) else None
            ic=firstnum(name) if re.match(r'^IC\s*\d+',name,re.I) else None
            m=firstnum(r.get('M')) or (firstnum(name) if re.match(r'^M\s*\d+',name,re.I) else None)
            if not ngc and not ic and not m and src != 'addendum': continue
            if str(r.get('Type','')).strip() in ('NonEx','Dup'): continue
            ra,dec=coord_degrees(r.get('RA'),r.get('Dec'))
            if ra is None: continue
            if m: messiers.add(m)
            ident=re.sub(r'\s+', '', name.upper()) if ngc or ic or not m else f'M{m:03d}'
            try: maj=float(r.get('MajAx') or 0)
            except ValueError: maj=0.0
            titles=(([f'Messier {m}'] if m else [])+
                    ([f'NGC {ngc}'] if ngc else [])+
                    ([f'IC {ic}'] if ic else []))
            titles += [v.strip() for v in str(r.get('Common names') or '').split(',') if v.strip()]
            if not titles: titles=[name]
            aliases=[]
            for prefix in ('NGC','IC'):
                aliases.extend(f'{prefix} {v.strip()}' for v in str(r.get(prefix) or '').split(',') if v.strip())
            rows.append({'id':ident,'ngc':ngc or '', 'ic':ic or '', 'messier':m or '',
                         'object_type':r.get('Type') or '', 'ra_deg':ra,'dec_deg':dec,
                         'major_arcmin':maj,'wiki_titles':titles,'source':src,'aliases':aliases})
    if args.with_vizier:
        rows.extend(extra_vizier_catalogs())
    if getattr(args, 'wide', False):
        from catalogs import add_wide_catalogs
        add_wide_catalogs(rows)
    if getattr(args, 'spacecatalog', False):
        from catalogs import spacecatalog_rows
        rows.extend(spacecatalog_rows(Path(args.out).parent / '.cache'))
    for path in args.extra_csv or []:
        rows.extend(load_extra_csv(path))
    # Catalog designations remain separate where scientifically ambiguous; aliases preserved.
    uniq={}
    for r in rows:
        validate_object(r)
        if r['id'] in uniq and r != uniq[r['id']]:
            raise ValueError(f'Conflicting catalog identifier: {r["id"]}')
        uniq[r['id']]=r
    ordered=sorted(uniq.values(),key=lambda r:r['id'])
    write_jsonl(args.out,ordered)
    from collections import Counter
    print(f'Catalog: {len(ordered)} entries, Messier coverage {len(messiers)}/110; by source {dict(Counter(r["source"] for r in ordered))}')
    print(f'Missing Messier designations in source: {sorted(set(range(1,111))-messiers)}; no identities inferred.')
    print(f'Saved {args.out}. Coordinate coincidences across catalogs may need reconciliation.')

def write_jsonl(path, rows, append=False):
    write_rows(path, rows, append)

def read_jsonl(path):
    yield from read_rows(path)


def shard(objs, worker, workers, limit=0):
    if workers < 1 or not 0 <= worker < workers or limit < 0:
        raise ValueError('Invalid worker partition or limit')
    selected = [r for r in objs if int(hashlib.sha256(r['id'].encode()).hexdigest(), 16) % workers == worker]
    return selected[:limit] if limit else selected


def chunks(items, n):
    for i in range(0, len(items), n): yield items[i:i+n]


SKY_TEMPLATES = frozenset('Template:' + name for name in (
    'Infobox galaxy', 'Infobox galaxy cluster', 'Infobox galaxy group',
    'Infobox nebula', 'Infobox planetary nebula', 'Infobox supernova remnant',
    'Infobox open cluster', 'Infobox globular cluster', 'Infobox star cluster',
    'Infobox astronomical object', 'Starbox begin'))


def wiki_images(s, titles, delay):
    # Returns images from the full article, not just the lead image.
    if len(titles) > 50:
        return {k:v for batch in chunks(titles, 50) for k,v in wiki_images(s, batch, delay).items()}
    data_images = {t: None for t in titles}
    ambiguous = set()
    sky_titles = set()
    cont = {}
    while True:
        params = {'action': 'query', 'format': 'json', 'formatversion': 2, 'redirects': 1,
                  'prop': 'images|pageprops|templates', 'ppprop': 'disambiguation',
                  'tltemplates': '|'.join(sorted(SKY_TEMPLATES)), 'tllimit': 'max',
                  'imlimit': 'max', 'titles': '|'.join(titles), **cont}
        d = get_json(s, WIKI_URL, params, delay)
        redirects = {x['from']: x['to'] for x in d.get('query', {}).get('normalized', [])}
        redirects.update({x['from']: x['to'] for x in d.get('query', {}).get('redirects', [])})
        pages = {p.get('title'): p for p in d.get('query', {}).get('pages', [])}
        for t in titles:
            actual = redirects.get(t, t)
            actual = redirects.get(actual, actual)
            p = pages.get(actual)
            if p is None: continue
            if 'disambiguation' in p.get('pageprops', {}):
                ambiguous.add(t)
            if any(item['title'] in SKY_TEMPLATES for item in p.get('templates', [])):
                sky_titles.add(t)
            if t in ambiguous or 'missing' in p or 'invalid' in p:
                data_images[t] = None
                continue
            if data_images[t] is None: data_images[t] = []
            data_images[t].extend(i['title'] for i in p.get('images', []))
        cont = d.get('continue') or {}
        if not cont: break
    return {t: images if t in sky_titles and t not in ambiguous else None
            for t, images in data_images.items()}


def commons_metadata(s, titles, delay):
    # Wikipedia imageinfo resolves both local files and shared Commons files.
    found = {}
    for group in chunks(sorted(set(titles)), 5):
        params = {'action':'query', 'format':'json', 'formatversion':2,
                  'redirects':1, 'prop':'imageinfo', 'iiprop':'extmetadata',
                  'titles':'|'.join(group)}
        d = get_json(s, WIKI_URL, params, delay)
        q = d.get('query', {})
        redirects = {x['from']:x['to'] for x in q.get('normalized', []) + q.get('redirects', [])}
        pages = {p.get('title'):p for p in q.get('pages', [])}
        for title in group:
            actual = title
            seen = set()
            while actual in redirects and actual not in seen:
                seen.add(actual); actual = redirects[actual]
            info = (pages.get(actual, {}).get('imageinfo') or [{}])[0]
            fields = info.get('extmetadata') or {}
            # Empty metadata is unknown, never negative attribution evidence.
            if not fields:
                continue
            txt = ' '.join(str(v.get('value','')) for v in fields.values())
            if INSTITUTION_RE.search(txt) and not HST_RE.search(txt):
                continue  # STScI also operates JWST; institution alone is ambiguous.
            found[title] = bool(HST_RE.search(txt))
    return found


def wiki(args):
    s = session()
    all_objects = shard(list(read_jsonl(args.catalog)), args.worker, args.workers, args.limit)
    existing = {o['id'] for o in read_jsonl(args.out) if o.get('wiki_status') in ('not_detected','hubble_detected','no_article')} if Path(args.out).exists() else set()
    pending = [o for o in all_objects if o['id'] not in existing]
    print(f'Wikipedia worker {args.worker}/{args.workers}: {len(pending)} objects pending')
    failures=0
    for grp in chunks(pending, args.batch_size):
        titles = list(dict.fromkeys(t for o in grp for t in o['wiki_titles']))
        try:
            d = wiki_images(s, titles, args.delay)
            need_meta = []
            obj_files = {}
            for obj in grp:
                names = sorted({fname for t in obj['wiki_titles'] for fname in (d.get(t) or [])
                                if IMAGE_RE.search(fname)})
                obj_files[obj['id']] = names
                # Check source metadata for all embedded raster images, not only matching filenames.
                need_meta.extend(n for n in names if not HST_RE.search(n))
            meta = commons_metadata(s, need_meta, args.delay) if need_meta else {}
            results = []
            for obj in grp:
                imgs = obj_files[obj['id']]
                evidence = [n for n in imgs if HST_RE.search(n) or meta.get(n, False)]
                any_article = any(d.get(t) is not None for t in obj['wiki_titles'])
                status = ('hubble_detected' if evidence else
                          'no_article' if not any_article else
                          'incomplete' if any(n not in meta and not HST_RE.search(n) for n in imgs) else
                          'not_detected')
                results.append({'id': obj['id'], 'wiki_status': status,
                                'catalog_verified': bool(obj.get('simbad_oid')),
                                'hubble_evidence': evidence[:12], 'wiki_images': len(imgs),
                                'wiki_titles_checked': obj['wiki_titles'], 'checked_at_unix':time.time(),
                                'publication_status':'not_assessed'})
            write_jsonl(args.out, results, append=True)
            print(f'Processed {len(results)} wiki objects; saved to {args.out}', flush=True)
        except Exception as ex:
            failures+=1
            print(f'Wikipedia batch failed: {ex}. Retry with same command.', file=sys.stderr)
            time.sleep(6)
            # No results written for failed batch: resume retries it.
    if failures: raise SystemExit(1)


def merge_glob(pattern):
    result = {}
    for path in sorted(glob.glob(pattern)):
        latest={row['id']:row for row in read_jsonl(path)}
        for ident,row in latest.items():
            if ident in result and result[ident] != row:
                raise ValueError(f'Conflicting worker files for {ident}; collect one consistent run')
            result[ident] = row
    return result


def mast(args):
    from astroquery.mast import Observations
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    wikimap = merge_glob(args.wiki_glob)
    objs = [o for o in read_jsonl(args.catalog)
            if o['id'] in wikimap and (wikimap[o['id']]['wiki_status'] == 'not_detected'
                or (wikimap[o['id']]['wiki_status']=='no_article' and o.get('simbad_oid')))]
    objs = shard(objs, args.worker, args.workers, args.limit)
    previous = {x['id'] for x in read_jsonl(args.out) if x.get('status') == 'ok'} if Path(args.out).exists() else set()
    print(f'MAST worker {args.worker}/{args.workers}: {len(objs)} assigned')
    failures=0
    for obj in objs:
        if obj['id'] in previous: continue
        radius = max(0.5, min(args.max_radius_arcmin, float(obj['major_arcmin'] or 2) / 2))
        try:
            pos = SkyCoord(float(obj['ra_deg'])*u.deg, float(obj['dec_deg'])*u.deg, frame='icrs')
            from mast_support import query_observations
            observations = query_observations(pos, radius)
            matched = []
            filters = set()
            for r in observations:
                if str(r['obs_collection']).upper() != 'HST': continue
                if str(r['dataproduct_type']).lower() != 'image': continue
                if 'dataRights' in observations.colnames and str(r['dataRights']).upper() != 'PUBLIC': continue
                filt = str(r['filters']) if 'filters' in observations.colnames else ''
                if filt not in ('', '--', 'None', 'nan'):
                    filters.update(x.strip() for x in re.split(r'[;,]', filt) if x.strip())
                matched.append({'obsid': str(r['obsid']),
                                'obs_id': str(r['obs_id']),
                                'instrument': str(r['instrument_name']) if 'instrument_name' in observations.colnames else '',
                                'filter': filt,
                                'exptime': (safe_float(r['t_exptime']) if 't_exptime' in observations.colnames else 0.0),
                                's_region': str(r['s_region']) if 's_region' in observations.colnames else ''})
            results = {'id': obj['id'], 'status': 'ok', 'radius_arcmin': round(radius, 2),
                       'observation_count': len(matched), 'filter_count': len(filters),
                       'filters': sorted(filters), 'observations': matched if args.max_obs_saved == 0 else matched[:args.max_obs_saved],
                       'observation_list_truncated': args.max_obs_saved > 0 and len(matched) > args.max_obs_saved,
                       'search_radius_capped':float(obj['major_arcmin'] or 2)/2 > args.max_radius_arcmin,
                       'wcs_method':'MAST spatial query; local footprint and FITS verification required',
                       'checked_at_unix':time.time()}
        except Exception as ex:
            failures+=1
            results = {'id': obj['id'], 'status': 'ERROR', 'message': str(ex)}
        write_jsonl(args.out, [results], append=True)
        print(f'{obj["id"]}: {results.get("observation_count", "error")} observations; {results.get("filter_count", "?")} filters', flush=True)
        time.sleep(args.delay)
    if failures: raise SystemExit(1)


def merged(args):
    catalogdata = {x['id']: x for x in read_jsonl(args.catalog)}
    wikimap = merge_glob(args.wiki_glob)
    mastmap = merge_glob(args.mast_glob)
    combined = []
    for ident, o in catalogdata.items():
        w = wikimap.get(ident) or {}
        m = mastmap.get(ident) or {}
        nfilters = int(m.get('filter_count') or 0)
        nobs = int(m.get('observation_count') or 0)
        status = w.get('wiki_status', 'not_checked')
        rank = nfilters * 8 + math.log1p(nobs) * 3 + (4 if status == 'not_detected' else 0)
        out = {**o, **w, **m, 'priority_score': round(rank, 3), 'publication_status':'not_assessed'}
        from footprint_groups import grouped_candidates
        groups, errors = grouped_candidates(out) if status == 'not_detected' and m.get('status') == 'ok' else ([],{})
        out['rgb_group_count'] = len(groups)
        out['rgb_best_score'] = groups[0]['score'] if groups else 0
        out['rgb_best_obsids'] = groups[0]['obsids_rgb'] if groups else []
        out['priority_score'] = round(out['rgb_best_score'] + math.log1p(nobs),3) if groups else 0
        out['geometry_skipped'] = errors
        out['mast_observation_ids'] = [x['obsid'] for x in m.get('observations', [])]
        combined.append(out)
    combined.sort(key=lambda x: (x['priority_score'], x.get('filter_count', 0)), reverse=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out, combined)
    csvpath = str(Path(args.out).with_suffix('.csv'))
    fields = ['id', 'ngc', 'ic', 'messier', 'source', 'object_type', 'ra_deg', 'dec_deg', 'major_arcmin',
              'wiki_status', 'wiki_images', 'status', 'observation_count', 'filter_count',
              'filters', 'priority_score', 'rgb_group_count', 'rgb_best_score', 'mast_observation_ids', 'publication_status', 'search_radius_capped']
    with open(csvpath, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for row in combined:
            wr.writerow({k: json.dumps(row.get(k), ensure_ascii=False) if isinstance(row.get(k), list) else row.get(k, '') for k in fields})
    print(f'Saved {args.out} and {csvpath}. Scores are candidate-selection heuristics, not novelty evidence.')


def check_wcs(args):
    from fits_verify import inspect_file
    from footprint_groups import project_gnomonic
    from shapely.geometry import Polygon, Point
    for path in args.files:
        report=inspect_file(path)
        for extension in report['extensions']:
            pts=[project_gnomonic(ra,dec,args.ra,args.dec) for ra,dec in extension['footprint']]
            inside=all(p is not None for p in pts) and Polygon(pts).covers(Point(0,0))
            print(f"{path} HDU {extension['hdu']}: target inside sampled WCS footprint={inside}")
        for error in report['errors']: print(path,error)


def previews(args):
    """Download available JPEG preview products for selected high-ranking observations."""
    from astroquery.mast import Observations
    from PIL import Image
    items = [x for x in read_jsonl(args.results)
             if x.get('status') == 'ok' and x.get('observation_count', 0) > 0 and
             x.get('wiki_status') in ('not_detected','no_article') and x.get('rgb_group_count',0) > 0][:args.limit]
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
    for item in items:
        target = outdir / (item['id'] + '.jpg')
        if target.exists(): continue
        try:
            ids = item.get('rgb_best_obsids') or []
            if not ids: continue
            prods = Observations.get_product_list(ids[1])
            choices = [i for i, r in enumerate(prods)
                       if 'PREVIEW' in str(r['productType']).upper() and
                       str(r['productFilename']).lower().endswith(('.jpg','.jpeg','.png'))]
            if not choices:
                print(f'{item["id"]}: no JPEG preview product; use manual FITS preview')
                continue
            manifest = Observations.download_products(prods[choices[:1]], download_dir=str(outdir / '_download'))
            localcol = next((c for c in manifest.colnames if c.lower().replace(' ', '') == 'localpath'), None)
            if not localcol: continue
            path = Path(str(manifest[localcol][0]))
            if path.exists():
                im = Image.open(path).convert('RGB')
                im.thumbnail((512, 512))
                im.save(target, quality=88)
                from runtime import atomic_json
                atomic_json(target.with_suffix('.provenance.json'),{'id':item['id'],'obsid':ids[1],
                    'productFilename':str(prods[choices[0]]['productFilename']),
                    'dataURI':str(prods[choices[0]]['dataURI']),
                    'note':'single-band archive preview; not a constructed RGB composite'})
                print('Saved', target)
        except Exception as ex:
            print(f'{item["id"]}: preview failed ({ex})', file=sys.stderr)


def ai_rank(args):
    from ranking import run
    run(args)


def main():
    p = argparse.ArgumentParser(description='Hubble Wikipedia first-pass catalog / footprint scanner')
    sub = p.add_subparsers(dest='command', required=True)
    from alias_resolution import add_parser as add_alias_parser
    add_alias_parser(sub)
    from publication_audit import add_parser as add_publication_parser
    add_publication_parser(sub)
    a = sub.add_parser('catalog'); a.add_argument('--ngc-csv'); a.add_argument('--addendum-csv')
    extension = a.add_mutually_exclusive_group()
    extension.add_argument('--wide', action='store_true', help='Add the bundled SIMBAD designations, including Herbig-Haro objects')
    extension.add_argument('--spacecatalog', action='store_true', help='Download and checksum SpaceCatalog DR1 objects and designations')
    extension.add_argument('--with-vizier', action='store_true', help='Add Arp and Sharpless 2 catalogs via VizieR')
    a.add_argument('--extra-csv', action='append', help='Additional catalog with ICRS ra_deg/dec_deg'); a.add_argument('--out', default='out/catalog.jsonl'); a.set_defaults(func=catalog)
    a = sub.add_parser('wiki'); a.add_argument('--catalog', default='out/catalog.jsonl'); a.add_argument('--out', required=True)
    a.add_argument('--worker', type=int, default=0); a.add_argument('--workers', type=int, default=1)
    a.add_argument('--limit', type=int, default=0); a.add_argument('--batch-size', type=int, default=8)
    a.add_argument('--delay', type=float, default=1.0); a.set_defaults(func=wiki)
    a = sub.add_parser('mast'); a.add_argument('--catalog', default='out/catalog.jsonl'); a.add_argument('--wiki-glob', default='out/wiki_*.jsonl'); a.add_argument('--out', required=True)
    a.add_argument('--worker', type=int, default=0); a.add_argument('--workers', type=int, default=1)
    a.add_argument('--limit', type=int, default=0); a.add_argument('--max-radius-arcmin', type=float, default=12.0)
    a.add_argument('--max-obs-saved', type=int, default=0, help='0 saves all observations'); a.add_argument('--delay', type=float, default=1.2); a.set_defaults(func=mast)
    a = sub.add_parser('merge'); a.add_argument('--catalog', default='out/catalog.jsonl'); a.add_argument('--wiki-glob', default='out/wiki_*.jsonl'); a.add_argument('--mast-glob', default='out/mast_*.jsonl'); a.add_argument('--out', default='out/candidates.jsonl'); a.set_defaults(func=merged)
    a = sub.add_parser('wcs-check'); a.add_argument('--ra', required=True, type=float); a.add_argument('--dec', required=True, type=float); a.add_argument('files', nargs='+'); a.set_defaults(func=check_wcs)
    a = sub.add_parser('previews'); a.add_argument('--results', default='out/candidates.jsonl'); a.add_argument('--out', default='out/previews'); a.add_argument('--limit', type=int, default=30); a.set_defaults(func=previews)
    a = sub.add_parser('ai-rank'); a.add_argument('--previews', default='out/previews'); a.add_argument('--out', default='out/ai_rank.csv'); a.add_argument('--results', default='out/candidates.jsonl'); a.add_argument('--model', default='facebook/dinov2-small'); a.add_argument('--revision', default='main'); a.set_defaults(func=ai_rank)
    args = p.parse_args()
    if hasattr(args, 'worker') and not (0 <= args.worker < args.workers): p.error('worker must be between 0 and workers-1')
    if getattr(args, 'limit', 0) < 0 or getattr(args, 'batch_size', 1) < 1: p.error('limit must be nonnegative and batch-size positive')
    if getattr(args, 'max_radius_arcmin', 1) <= 0 or getattr(args, 'max_obs_saved', 0) < 0: p.error('invalid MAST bounds')
    if args.command in ('wiki','mast'):
        with job_guard(args): args.func(args)
    else:
        args.func(args)

if __name__ == '__main__':
    main()
