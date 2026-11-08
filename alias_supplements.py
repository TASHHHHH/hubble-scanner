"""Supplement exact-name identities. Never promote a positional neighbor."""
import io
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

from alias_resolution import (component, coords, normalized, resolve, simbad_candidates,
                              table_rows, unique, export)
from runtime import atomic_json


def ned_crossids(services, name):
    url = 'https://ned.ipac.caltech.edu/NED::API/CrossidsOfObject'
    params = {'TARGET': name}
    def fetch():
        from astropy.io.votable import parse
        response = requests.get(url, params=params, timeout=services.timeout)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        statuses = [e.attrib.get('value', e.text or '') for e in root.iter()
                    if e.attrib.get('name') == 'QUERY_STATUS']
        if not statuses or any(s != 'OK' for s in statuses):
            raise ValueError('NED query status: ' + repr(statuses))
        table = parse(io.BytesIO(response.content)).get_first_table().to_table(use_names_over_ids=True)
        return {'records': table_rows(table), 'raw_votable': response.text}
    return services.cache.call('NED CrossidsOfObject', {'url': url, 'params': params}, fetch)


def vizier(services, catalog, filters):
    query = {'catalog': catalog, 'filters': filters, 'server': 'vizier.cfa.harvard.edu', 'columns': ['**']}
    def fetch():
        from astroquery.vizier import Vizier
        v = Vizier(columns=['**'], row_limit=-1, timeout=services.timeout)
        v.VIZIER_SERVER = query['server']
        tables = v.query_constraints(catalog=catalog, **filters)
        return {'records': [r for table in tables for r in table_rows(table)]}
    return services.cache.call('VizieR', query, fetch)


def vizier_candidates(evidence, name):
    if evidence['status'] != 'ok': return []
    from astropy.coordinates import SkyCoord, FK4, FK5
    import astropy.units as u
    result = []
    catalog = evidence['query']['catalog']
    for r in evidence['data']['records']:
        aliases, typ = [name], None
        if catalog == 'V/70A/catalog':
            wanted = component(name)
            # Name+Comp is the identifier. Never pick a sibling or combined AB row.
            if str(r.get('Comp') or '').strip() != (wanted['label'] or ''): continue
            canonical = str(r['Name']).strip() + (' ' + wanted['label'] if wanted['label'] else '')
            position = SkyCoord(r['RAB1950'], r['DEB1950'], unit=(u.hourangle, u.deg),
                                frame=FK4(equinox='B1950')).icrs
            # Old CNS3 component coordinates can be shared/rounded; do not borrow
            # HD/DM system IDs for individual components.
            typ = 'Star (CNS3)'
        elif catalog == 'J/A+A/558/A53/catalog':
            canonical = str(r['Name']).replace('_', ' ')
            position = SkyCoord(float(r['RAJ2000'])*u.deg, float(r['DEJ2000'])*u.deg,
                                frame=FK5(equinox='J2000')).icrs
            typ = str(r.get('Type') or 'Stellar cluster')
        elif catalog == 'VII/237/pgc':
            canonical = 'PGC ' + str(int(r['PGC']))
            position = SkyCoord(r['RAJ2000'], r['DEJ2000'], unit=(u.hourangle, u.deg),
                                frame=FK5(equinox='J2000')).icrs
            typ = 'Galaxy'
        else: continue
        result.append(dict(canonical_designation=canonical, aliases=unique(aliases+[canonical]),
                           ra_deg=float(position.ra.deg), dec_deg=float(position.dec.deg), object_type=typ,
                           source='VizieR ' + catalog, query_name=name,
                           coordinate_note='Catalog frame transformed to ICRS; no epoch/proper-motion inference'))
    return result


def openngc(services):
    import csv
    import hashlib
    url = 'https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv'
    def fetch():
        response = requests.get(url, timeout=services.timeout)
        response.raise_for_status()
        return {'sha256': hashlib.sha256(response.content).hexdigest(),
                'records': list(csv.DictReader(io.StringIO(response.text), delimiter=';'))}
    return services.cache.call('OpenNGC', {'url': url}, fetch)


def gaia(services, identifiers):
    import csv
    url = 'https://gea.esac.esa.int/tap-server/tap/sync'
    sql = ('SELECT source_id,ra,dec,ref_epoch,pmra,pmdec FROM gaiadr3.gaia_source WHERE source_id IN (' +
           ','.join(identifiers) + ')')
    params = {'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'csv', 'QUERY': sql}
    def fetch():
        response = requests.get(url, params=params, timeout=services.timeout)
        response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(response.text)))
        if rows and 'source_id' not in rows[0]: raise ValueError('Unexpected Gaia result schema')
        return {'records': rows}
    return services.cache.call('Gaia DR3', {'url': url, 'params': params}, fetch)


def supplement(results, services, max_sep, out):
    from astropy.coordinates import SkyCoord, FK5
    from astropy.time import Time
    import astropy.units as u
    for result in results:
        name = result['original_designation']
        evidence, candidates = result['evidence'], result['candidate_records']
        ned = [c for c in candidates if c['source'] == 'NED']
        if len(ned) == 1:
            cross = ned_crossids(services, name)
            evidence.append(cross)
            if cross['status'] == 'ok':
                names = [r['Object Name'] for r in cross['data']['records']]
                ned[0]['aliases'] = unique(ned[0]['aliases'] + names)
                ned[0]['alias_provenance'] = {a:{'source':cross['source'],'query':cross['query'],
                    'queried_at':cross['queried_at']} for a in names}
        v = None
        if re.match(r'^(Gl|GJ)\s*', name):
            m = re.fullmatch(r'(?:Gl|GJ)\s*([\d.]+)\s*[AB]?', name)
            if m: v = vizier(services, 'V/70A/catalog', {'Name': name.split()[0] + ' ' + m[1]})
        elif name.startswith('MWSC'):
            v = vizier(services, 'J/A+A/558/A53/catalog', {'MWSC': re.sub(r'\D', '', name)})
        elif name.startswith('PGC') and not candidates:
            v = vizier(services, 'VII/237/pgc', {'PGC': re.sub(r'\D', '', name)})
        if v:
            evidence.append(v)
            try: candidates.extend(vizier_candidates(v, name))
            except (KeyError, ValueError, TypeError) as exc:
                evidence.append({'source': 'VizieR conversion', 'status': 'service_error', 'error': str(exc),
                                 'query': v['query']})
        revised = resolve(result['input_record'], evidence, candidates, max_sep)
        result.update(revised)
        atomic_json(Path(out)/'checkpoints'/f'{result["input_row"]:05d}.json', result)
        print('Supplement', result['input_row'], name, result['resolution_status'], flush=True)
    # Catalog-returned canonical identifiers can be looked up in SIMBAD, with the
    # explicit cross-ID chain retained. No unverified CSV alias is used here.
    targets = {}
    for r in results:
        for c in r['candidate_records']:
            if c['source'] != 'SIMBAD': targets.setdefault(c['canonical_designation'], []).append(r)
    names = list(targets)
    for start in range(0, len(names), 25):
        batch = names[start:start+25]
        evidence = services.simbad(batch)
        for name in batch:
            candidates = simbad_candidates(evidence, name)
            for result in targets[name]:
                result['evidence'].append(evidence)
                for c in candidates:
                    c = dict(c, identity_chain={'from': result['original_designation'], 'via_catalog_name': name})
                    result['candidate_records'].append(c)
    ngc = openngc(services)
    for result in results:
        existing = {normalized(a) for c in result['candidate_records'] for a in c['aliases']}
        # Components are deliberately excluded from parent-galaxy OpenNGC joins.
        if component(result['original_designation'])['kind'] != 'object': continue
        if ngc['status'] == 'ok':
            matched = [r for r in ngc['data']['records'] if normalized(r.get('Name', '')) in existing]
            if matched:
                slim = dict(ngc, data={'sha256': ngc['data']['sha256'], 'records': matched})
                result['evidence'].append(slim)
                for r in matched:
                    p = SkyCoord(r['RA'], r['Dec'], unit=(u.hourangle,u.deg), frame=FK5(equinox='J2000')).icrs
                    aliases = [r['Name']]
                    for field, prefix in [('NGC','NGC '), ('IC','IC '), ('M','M '), ('PGC','PGC ')]:
                        aliases += [prefix+n.strip() for n in r.get(field,'').split(',') if n.strip()]
                    result['candidate_records'].append(dict(canonical_designation=r['Name'], aliases=unique(aliases),
                        object_type=r.get('Type'), ra_deg=float(p.ra.deg), dec_deg=float(p.dec.deg),
                        source='OpenNGC', query_name=r['Name']))
    stars = [r for r in results if re.match(r'^(Gl|GJ|HD|HIP)\s*', r['original_designation'])]
    identifiers = sorted({m[1] for r in stars for c in r['candidate_records'] for a in c['aliases']
                          if (m := re.fullmatch(r'Gaia DR3 (\d+)', a))})
    if identifiers:
        g = gaia(services, identifiers)
        for r in stars:
            aliases = {a for c in r['candidate_records'] for a in c['aliases']}
            relevant = [x for x in g.get('data', {}).get('records', []) if 'Gaia DR3 '+x['source_id'] in aliases] if g['status']=='ok' else []
            r['evidence'].append(dict(g, data={'records': relevant}) if g['status']=='ok' else g)
            for x in relevant:
                if not x.get('pmra') or not x.get('pmdec'): continue
                p = SkyCoord(float(x['ra'])*u.deg,float(x['dec'])*u.deg,
                    pm_ra_cosdec=float(x['pmra'])*u.mas/u.yr,pm_dec=float(x['pmdec'])*u.mas/u.yr,
                    obstime=Time(float(x['ref_epoch']),format='jyear')).apply_space_motion(new_obstime=Time('J2000'))
                name = 'Gaia DR3 '+x['source_id']
                r['candidate_records'].append(dict(canonical_designation=name,aliases=[name],source='Gaia DR3',
                    query_name=name,ra_deg=float(p.ra.deg),dec_deg=float(p.dec.deg),object_type='Star',
                    coordinate_transform='ICRS epoch 2016 propagated to J2000 with catalog proper motion'))
    final = []
    for result in results:
        revised = resolve(result['input_record'], result['evidence'], result['candidate_records'], max_sep)
        final.append(revised)
        atomic_json(Path(out)/'checkpoints'/f'{result["input_row"]:05d}.json', revised)
    export(out, final)
    return final
