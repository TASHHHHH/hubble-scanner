"""Evidence-bearing name resolution, independent of the distributed job queue.

Only catalog name lookups establish equivalence. Coordinates veto a proposed
match; they never establish one. Input aliases and MAST pointing names are not
automatically trusted. Nested CSV fields use JSON, not delimiter concatenation.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone

from runtime import atomic_json

SCHEMA = 1


def now():
    return datetime.now(timezone.utc).isoformat()


def normalized(name):
    """Formatting only: do not equate Gl/GJ, parent/component, or host/planet."""
    value = ' '.join(str(name).split())
    match = re.fullmatch(r'(NGC|IC|PGC|HIP|HD)\s*0*(\d+)(.*)', value, re.I)
    if match:
        return match[1].upper() + ' ' + str(int(match[2])) + match[3]
    return value


def unique(names):
    found = {}
    for name in names:
        if name and str(name).strip():
            found.setdefault(normalized(name), str(name).strip())
    return list(found.values())


def query_spellings(name):
    """Spacing/zero padding variants only, with component suffix preserved."""
    spaced = re.sub(r'^((?:GJ|Gl|HD|HIP)\s*[\d.]+)\s*([AB])$', r'\1 \2', name)
    spaced = re.sub(r'^MWSC\s*(\d+)$', r'MWSC \1', spaced)
    return list(dict.fromkeys([name, normalized(name), spaced]))


def component(name):
    value = str(name).strip()
    m = re.search(r'\s*NED\s*(\d+)$', value, re.I)
    if m:
        return {'kind': 'galaxy_component', 'label': 'NED' + m[1].zfill(2),
                'parent_designation': value[:m.start()].strip(), 'relationship_status': 'designation_only'}
    m = re.search(r'\s([b-z])$', value)
    if m:
        return {'kind': 'planet', 'label': m[1], 'parent_designation': value[:m.start()],
                'relationship_status': 'designation_only'}
    m = re.fullmatch(r'((?:GJ|Gl|HD|HIP)\s*[\d.]+)\s*([AB])', value)
    if m:
        return {'kind': 'stellar_component', 'label': m[2], 'parent_designation': m[1],
                'relationship_status': 'designation_only'}
    return {'kind': 'object', 'label': None, 'parent_designation': None,
            'relationship_status': 'not_established'}


def separation(a, b):
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    return float(SkyCoord(a[0]*u.deg, a[1]*u.deg).separation(
        SkyCoord(b[0]*u.deg, b[1]*u.deg)).arcsec)


def coords(record):
    try:
        ra, dec = float(record['ra_deg']), float(record['dec_deg'])
        if math.isfinite(ra) and math.isfinite(dec) and 0 <= ra < 360 and -90 <= dec <= 90:
            return ra, dec
    except (TypeError, ValueError, KeyError):
        pass
    return None


def table_rows(table):
    import numpy as np
    result = []
    for row in table:
        item = {}
        for key in table.colnames:
            value = row[key]
            if np.ma.is_masked(value): value = None
            elif hasattr(value, 'item'): value = value.item()
            if isinstance(value, bytes): value = value.decode('utf-8')
            if isinstance(value, float) and not math.isfinite(value): value = None
            item[key] = value
        result.append(item)
    return result


class QueryCache:
    """Atomic positive/empty cache; failed attempts retained but retried on resume."""
    def __init__(self, path, retries=3, delay=1.0, ttl=30*86400):
        self.path = Path(path)
        self.retries, self.delay, self.ttl = retries, delay, ttl
        self.last_call = 0
        self.blocked_sources = {}

    def call(self, source, query, producer):
        key = {'schema': SCHEMA, 'source': source, 'query': query}
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        path = self.path / (digest + '.json')
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                if cached['status'] == 'ok' and time.time() - path.stat().st_mtime < self.ttl:
                    return dict(cached, cache_hit=True)
            except (ValueError, KeyError, OSError):
                pass
        if source in self.blocked_sources:
            return dict(key, status='service_error', queried_at=now(), data=[], attempts=[],
                        error='Request not sent: service rate limit encountered earlier in this run',
                        rate_limit=self.blocked_sources[source])
        attempts = []
        for attempt in range(self.retries):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_call)))
            stamp = now()
            try:
                data = producer()
                self.last_call = time.monotonic()
                attempts.append({'timestamp': stamp, 'status': 'ok'})
                result = dict(key, status='ok', queried_at=stamp, data=data, attempts=attempts)
                atomic_json(path, result)
                return result
            except Exception as exc:
                self.last_call = time.monotonic()
                attempts.append({'timestamp': stamp, 'status': 'service_error',
                                 'error': type(exc).__name__ + ': ' + str(exc)[:2000]})
                response = getattr(exc, 'response', None)
                if getattr(response, 'status_code', None) == 429:
                    self.blocked_sources[source] = dict(getattr(response, 'headers', {}))
                    break
                if attempt + 1 < self.retries:
                    response = getattr(exc, 'response', None)
                    retry_after = getattr(response, 'headers', {}).get('Retry-After', '0')
                    try: wait = max(2**attempt, float(retry_after))
                    except ValueError: wait = 2**attempt
                    # Long rate limits leave the row retryable; never retry earlier.
                    if wait > 60: break
                    time.sleep(wait)
        result = dict(key, status='service_error', queried_at=stamp, data=[], attempts=attempts)
        atomic_json(path, result)
        return result


class CatalogServices:
    def __init__(self, cache, simbad_server='simbad.cds.unistra.fr', timeout=30):
        self.cache, self.server, self.timeout = cache, simbad_server, timeout

    def simbad(self, names):
        query = {'method': 'astroquery.simbad.Simbad.query_objects', 'names': names,
                 'fields': ['ids', 'otype'], 'server': self.server, 'timeout': self.timeout}
        def fetch():
            from astroquery.simbad import Simbad
            s = Simbad()
            s.server = self.server
            s.timeout = self.timeout
            s.add_votable_fields('ids', 'otype')
            payload = s.query_objects(names, get_query_payload=True)
            table = s.query_objects(names)
            if table is None or not {'main_id','ra','dec','ids','otype','user_specified_id'} <= set(table.colnames):
                raise ValueError('Unexpected SIMBAD response schema; cannot infer absent objects')
            records = table_rows(table)
            returned = {str(r['user_specified_id']).strip() for r in records}
            if not {n.strip() for n in names} <= returned:
                raise ValueError('SIMBAD omitted input rows; possible incomplete response')
            return {'payload': payload, 'records': records}
        return self.cache.call('SIMBAD', query, fetch)

    def ned(self, name):
        query = {'method': 'astroquery.ipac.ned.Ned.query_object', 'name': name,
                 'endpoint': 'https://ned.ipac.caltech.edu/cgi-bin/objsearch', 'timeout': self.timeout}
        def fetch():
            from astroquery.ipac.ned import Ned
            from astroquery.exceptions import RemoteServiceError
            n = type(Ned)()
            n.TIMEOUT = self.timeout
            payload = n.query_object(name, get_query_payload=True)
            try:
                table = n.query_object(name)
            except RemoteServiceError as exc:
                # Only NED's explicit absence message is a negative lookup.
                if 'not recognized' in str(exc).lower():
                    return {'payload': payload, 'records': [], 'absence_message': str(exc)}
                raise
            if table is None or not {'Object Name','RA','DEC'} <= set(table.colnames):
                raise ValueError('Unexpected NED response schema')
            return {'payload': payload, 'records': table_rows(table)}
        return self.cache.call('NED', query, fetch)

    def exoplanet(self, name):
        import requests
        planet = component(name)['kind'] == 'planet'
        field = 'pl_name' if planet else 'hostname'
        sql = ("SELECT DISTINCT pl_name,hostname,ra,dec FROM ps WHERE " + field + "='" +
               name.replace("'", "''") + "'")
        params = {'query': sql, 'format': 'json'}
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync'
        def fetch():
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            records = response.json()
            if not isinstance(records, list): raise ValueError('Unexpected Exoplanet Archive response')
            return {'records': records}
        return self.cache.call('NASA Exoplanet Archive', {'url': url, 'params': params}, fetch)


def simbad_candidates(evidence, name):
    if evidence['status'] != 'ok': return []
    records = evidence['data']['records']
    candidates = []
    for r in records:
        if str(r.get('user_specified_id', '')).strip() != name.strip() or not r.get('main_id'): continue
        candidates.append({'canonical_designation': r['main_id'],
                           'aliases': unique([r['main_id']] + str(r.get('ids') or '').split('|')),
                           'object_type': r.get('otype'), 'ra_deg': r.get('ra'), 'dec_deg': r.get('dec'),
                           'source': 'SIMBAD', 'query_name': name,
                           'provenance': {'source':evidence.get('source','SIMBAD'), 'query':evidence.get('query'),
                                          'queried_at':evidence.get('queried_at')}})
    return candidates


def ned_candidates(evidence, name):
    if evidence['status'] != 'ok': return []
    from astropy.coordinates import SkyCoord, FK5
    import astropy.units as u
    result = []
    for r in evidence['data']['records']:
        if not r.get('Object Name'): continue
        position = SkyCoord(r['RA']*u.deg, r['DEC']*u.deg, frame=FK5(equinox='J2000')).icrs
        result.append({'canonical_designation': r['Object Name'].strip(),
             'aliases': unique([name, r['Object Name']]), 'object_type': r.get('Type'),
             'ra_deg': float(position.ra.deg), 'dec_deg': float(position.dec.deg),
             'source': 'NED', 'query_name': name, 'coordinate_transform': 'FK5 J2000 to ICRS',
             'provenance': {'source':'NED','query':evidence.get('query'),'queried_at':evidence.get('queried_at')}})
    return result


def resolve(row, evidence, candidates, max_sep=3.0):
    name = row['object_name']
    result = {'schema_version': SCHEMA, 'input_row': row['input_row'],
              'object_id': row.get('object_id', ''), 'original_designation': name,
              'canonical_designation': None, 'confirmed_aliases': [], 'alias_sources': [],
              'object_type': None, 'ra_deg': None, 'dec_deg': None, 'coordinate_frame': 'ICRS',
              'component_relationship': component(name), 'coordinate_checks': [],
              'resolution_status': 'not_found', 'evidence_notes': [],
              'queried_at': now(), 'evidence': evidence, 'candidate_records': candidates,
              'input_record': row, 'alias_coverage': 'incomplete'}
    # Multiple records from a source are not resolved by selecting the nearest.
    identities = {}
    for c in candidates:
        identities.setdefault(c['source'], set()).add(normalized(c['canonical_designation']))
    ambiguous = any(len(v) > 1 for v in identities.values())
    anchor = coords(row)
    first = candidates[0] if candidates else None
    for c in candidates:
        position = coords(c)
        if position is None:
            ambiguous = True
            result['evidence_notes'].append(c['source'] + ': missing/invalid ICRS coordinates')
        for label, reference in [('input_catalog', anchor), ('preferred_catalog', coords(first) if first else None)]:
            if position and reference:
                sep = separation(reference, position)
                consistent = sep <= max_sep
                result['coordinate_checks'].append({'source': c['source'], 'reference': label,
                    'separation_arcsec': sep, 'threshold_arcsec': max_sep, 'consistent': consistent,
                    'note': 'Input epoch not assumed; see candidate coordinate transforms. Discrepancies require review.'})
                if not consistent: ambiguous = True
        # An explicit component query must return a component-bearing identifier.
        wanted = component(name)
        if wanted['kind'] != 'object' and c['source'] != 'NASA Exoplanet Archive':
            if not any(component(a)['kind'] == wanted['kind'] and component(a)['label'] == wanted['label']
                       for a in ([c['canonical_designation']] if c['source'] == 'NED' else c['aliases'])):
                # SIMBAD identifier membership explicitly confirms the exact component.
                if c['source'] != 'SIMBAD' or normalized(name) not in {normalized(a) for a in c['aliases']}:
                    ambiguous = True
                    result['evidence_notes'].append('Component identity not independently established by ' + c['source'])
    if ambiguous:
        result['resolution_status'] = 'ambiguous'
        result['evidence_notes'].append('Candidate identities conflict or require coordinate/component review; no aliases promoted.')
    elif first:
        result.update({k: first[k] for k in ('canonical_designation', 'object_type', 'ra_deg', 'dec_deg')})
        result['resolution_status'] = 'confirmed'
        for c in candidates:
            for alias in c['aliases']:
                provenance = c.get('alias_provenance',{}).get(alias,c.get('provenance'))
                if provenance is None:
                    provenance = [{'source':e.get('source'),'query':e.get('query'),'queried_at':e.get('queried_at')}
                                  for e in evidence if e.get('source') and
                                  (c['source']==e['source'] or c['source'].startswith(e['source']+' '))]
                result['alias_sources'].append({'alias': alias, 'source': c['source'],
                    'evidence': 'Catalog identifier list or exact name lookup', 'query_name': c['query_name'],
                    'provenance': provenance})
        result['confirmed_aliases'] = unique(a['alias'] for a in result['alias_sources'])
        result['evidence_notes'].append('Exact name lookup confirmed; aliases are limited to returned catalog evidence.')
    elif any(e['status'] == 'service_error' for e in evidence):
        result['resolution_status'] = 'service_error'
    result['evidence_notes'].append('Supplementary cross-identifications and parent relationships may still require review.')
    return result


def load_input(path, catalog=None, expected_count=None):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(f'Expected {expected_count} input rows; found {len(rows)}. Refusing substitution.')
    if not rows or any(not r.get('object_name', '').strip() for r in rows):
        raise ValueError('Every input row must contain a nonempty object_name')
    positions = {}
    if catalog:
        from runtime import read_rows
        wanted = {r.get('object_id') for r in rows}
        for record in read_rows(catalog):
            if record.get('id') in wanted:
                if record['id'] in positions: raise ValueError('Duplicate catalog object_id: ' + record['id'])
                positions[record['id']] = record
        missing = wanted - positions.keys()
        if missing: raise ValueError(f'{len(missing)} input IDs absent from coordinate catalog; refusing a silent incomplete join')
    for i, row in enumerate(rows, 1):
        row['input_row'] = i
        if row.get('object_id') in positions:
            position = positions[row['object_id']]
            for key in ('ra_deg', 'dec_deg'):
                if not row.get(key): row[key] = position.get(key)
            row['coordinate_provenance'] = {'catalog': str(Path(catalog).resolve()), 'id': row['object_id']}
    return rows


def export(out, results):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'aliases.jsonl'
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for result in results: f.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fields = ['input_row', 'object_id', 'original_designation', 'canonical_designation', 'confirmed_aliases',
              'alias_sources', 'object_type', 'ra_deg', 'dec_deg', 'coordinate_frame', 'coordinate_checks',
              'component_relationship', 'resolution_status', 'alias_coverage', 'evidence_notes', 'queried_at', 'evidence']
    for filename, selected in [('aliases.csv', results), ('unresolved_aliases.csv',
            [r for r in results if r['resolution_status'] != 'confirmed'])]:
        path = out / filename
        tmp = path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in selected:
                writer.writerow({k: json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict))
                                 else row[k] for k in fields})
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)


def run(args, services=None):
    rows = load_input(args.input, args.catalog, args.expected_count)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {'schema': SCHEMA, 'input_sha256': hashlib.sha256(Path(args.input).read_bytes()).hexdigest(),
                'rows_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                'row_count': len(rows), 'max_separation_arcsec': args.max_separation_arcsec,
                'simbad_server': args.simbad_server, 'supplement': getattr(args, 'supplement', False)}
    lock = out / 'resolve.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        manifest_path = out / 'manifest.json'
        if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
            raise ValueError('Input/settings changed; use a new output directory.')
        atomic_json(manifest_path, manifest)
        (out/'completion.json').unlink(missing_ok=True)
        services = services or CatalogServices(QueryCache(out/'cache', args.retries, args.delay), args.simbad_server, args.timeout)
        results = []
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start+args.batch_size]
            sim = services.simbad(list(dict.fromkeys(n for r in batch for n in query_spellings(r['object_name']))))
            for row in batch:
                name = row['object_name']
                evidence = [sim]
                candidates = [c for spelling in query_spellings(name) for c in simbad_candidates(sim, spelling)]
                if re.match(r'^(ESO|PGC|NGC|IC)\s*\d', name, re.I):
                    ned = services.ned(name)
                    evidence.append(ned)
                    candidates.extend(ned_candidates(ned, name))
                if name.startswith('KMT-'):
                    exo = services.exoplanet(name)
                    evidence.append(exo)
                    if exo['status'] == 'ok':
                        field = 'pl_name' if component(name)['kind'] == 'planet' else 'hostname'
                        for r in exo['data']['records']:
                            if r.get(field) == name:
                                candidates.append({'canonical_designation': r[field], 'aliases': [r[field]],
                                    'object_type': 'Planet' if field == 'pl_name' else 'Microlensing host',
                                    'ra_deg': r.get('ra'), 'dec_deg': r.get('dec'),
                                    'source': 'NASA Exoplanet Archive', 'query_name': name,
                                    'relationship': {'kind': 'host_planet', 'host': r['hostname'],
                                                     'planet': r['pl_name'], 'status': 'catalog_confirmed'}})
                result = resolve(row, evidence, candidates, args.max_separation_arcsec)
                results.append(result)
                atomic_json(out/'checkpoints'/f'{row["input_row"]:05d}.json', result)
                print(f'{len(results)}/{len(rows)} {name}: {result["resolution_status"]}', flush=True)
            export(out, results)
        if getattr(args, 'supplement', False):
            from alias_supplements import supplement
            results = supplement(results, services, args.max_separation_arcsec, out)
            export(out, results)
        atomic_json(out/'completion.json', {'row_count':len(results), 'completed_at':now(),
            'input_sha256':manifest['input_sha256'], 'all_identities_confirmed':all(r['resolution_status']=='confirmed' for r in results)})
        return results
    finally:
        lock.unlink(missing_ok=True)


def add_parser(sub):
    parser = sub.add_parser('resolve-aliases', help='Resolve exact catalog identities with auditable provenance')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True, help='Output directory')
    parser.add_argument('--catalog', help='Optional original catalog JSONL; join coordinates by object_id only')
    parser.add_argument('--expected-count', type=int)
    parser.add_argument('--batch-size', type=int, default=25)
    parser.add_argument('--simbad-server', default='simbad.cds.unistra.fr')
    parser.add_argument('--max-separation-arcsec', type=float, default=3.0)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--delay', type=float, default=1.0)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--supplement', action='store_true', help='Also check NED cross-IDs, VizieR, OpenNGC and Gaia')
    parser.set_defaults(func=cli)


def cli(args):
    if min(args.batch_size, args.retries, args.timeout, args.max_separation_arcsec) <= 0 or args.delay < 0:
        raise ValueError('Batch size, retries, timeout and separation must be positive; delay nonnegative')
    run(args)
