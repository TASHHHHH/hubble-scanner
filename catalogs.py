"""Explicit catalog schemas; all output coordinates are ICRS degrees."""
import os
import csv
import re
from pathlib import Path
from urllib.parse import urlparse
from astropy.coordinates import SkyCoord, FK4, FK5
from astropy.time import Time
import astropy.units as u
from runtime import cached_call

def coordinates(row, ra_key, dec_key, frame, ra_unit='hourangle'):
    ra,dec=row[ra_key],row[dec_key]
    if ra is None or dec is None: raise ValueError('Missing coordinates')
    f = FK4(equinox=Time('B1900')) if frame == 'fk4_b1900' else FK5(equinox=Time('J2000')) if frame == 'fk5_j2000' else 'icrs'
    c=SkyCoord(str(ra).replace(' ',':'),str(dec).replace(' ',':'),unit=(u.deg if ra_unit=='deg' else u.hourangle,u.deg),frame=f).icrs
    return float(c.ra.deg),float(c.dec.deg)

def vizier_rows(catalog):
    from astroquery.vizier import Vizier
    from mast_support import table_rows
    def fetch():
        v=Vizier(columns=['**'],row_limit=-1,timeout=45)
        v.VIZIER_SERVER=os.getenv('HUBBLE_VIZIER_SERVER','vizier.cds.unistra.fr')
        tables=v.get_catalogs(catalog)
        if not len(tables) or not len(tables[0]):raise RuntimeError(f'No catalog rows: {catalog}')
        return table_rows(tables[0])
    return cached_call('vizier',{'catalog':catalog,'schema':2},fetch,ttl=2592000)

def convert_builtin(row, tag):
    from scanner import firstnum, safe_float, validate_object
    if tag == 'arp':
        number=firstnum(row.get('Arp',row.get('APG')))
        ra,dec=coordinates(row,'RAJ2000','DEJ2000','fk5_j2000')
        ident=f'ARP{number:03d}';titles=[f'Arp {number}'];size=safe_float(row.get('Size'))
        typ='peculiar galaxy field';basis='Arp original photographic field long dimension'
    else:
        number=firstnum(row.get('Sh2'))
        ra,dec=coordinates(row,'RA1900','DE1900','fk4_b1900')
        ident=f'SH2-{number:03d}';titles=[f'Sharpless {number}',f'Sharpless 2-{number}']
        size=safe_float(row.get('Diam'));typ='HII region';basis='catalog angular diameter'
    result={'id':ident,'ngc':'','ic':'','messier':'','ra_deg':ra,'dec_deg':dec,
        'major_arcmin':max(0,size),'wiki_titles':titles,'source':tag,'aliases':[],
        'object_type':typ,'extent_basis':basis,'coordinate_frame':'ICRS'}
    validate_object(result)
    return result

def builtin_catalogs():
    rows=[]
    for catalog,tag in [('VII/192/arpord','arp'),('VII/20/catalog','sh2')]:
        try:
            raw=vizier_rows(catalog)
        except Exception as exc:
            print(f'WARNING: skipped optional VizieR source {catalog}: {exc}', flush=True)
            continue
        converted=[]
        for i,row in enumerate(raw):
            try:converted.append(convert_builtin(row,tag))
            except Exception as ex:raise ValueError(f'{catalog} row {i}: {ex}; refusing a silently incomplete catalog') from ex
        print(f'Loaded {tag}: {len(converted)} rows')
        rows.extend(converted)
    return rows


def add_wide_catalogs(rows, snapshot=None):
    """Join by SIMBAD identity or an exact main NGC/IC designation, never proximity."""
    path = snapshot or Path(__file__).with_name('catalog_data') / 'simbad-identifiers.csv'
    by_designation = {r['id']:r for r in rows}
    by_oid = {}
    with Path(path).open(encoding='utf-8', newline='') as f:
        for record in csv.DictReader(f):
            family, number = record['family'], record['number']
            title = f'Sh2-{number}' if family == 'Sh2' else f'{family} {number}'
            oid = record['oid']
            row = by_oid.get(oid)
            if row is None:
                main = re.fullmatch(r'(NGC|IC)\s*(\d+)([A-Za-z]?)', record['main_id'], re.I)
                if main:
                    row = by_designation.get(f'{main[1].upper()}{int(main[2]):04d}{main[3].upper()}')
                if row is None:
                    row = dict(id=title, ra_deg=float(record['ra_deg']), dec_deg=float(record['dec_deg']),
                               source='simbad', object_type='', major_arcmin=0, wiki_titles=[], aliases=[],
                               ngc='', ic='', messier='')
                    rows.append(row)
                by_oid[oid] = row
            row['simbad_oid'] = oid
            for name in [title] + ([f'Herbig-Haro {number}', f'Herbig–Haro {number}'] if family=='HH' else []):
                if name not in row['wiki_titles']:
                    row['wiki_titles'].append(name)
            if title != row['id'] and title not in row['aliases']:
                row['aliases'].append(title)
    print(f'Loaded {len(by_oid)} distinct SIMBAD objects from the expanded catalog snapshot.', flush=True)


SPACECATALOG_FILES = {
    'objects': ('https://github.com/space-catalog/data-releases/releases/download/dr1/spacecatalog-dr1-objects.csv.gz',
                '630ba7ae703659f0c4fe67480d11dd1f2c7eafd89bb2443361d68d9e91321398'),
    'designations': ('https://github.com/space-catalog/data-releases/releases/download/dr1/spacecatalog-dr1-designations.csv.gz',
                     '1ea90d8d329d8b5b03f84fa15846a9ca2ecada5825dfd984fd0bc156b17114e6'),
    'sources': ('https://github.com/space-catalog/data-releases/releases/download/dr1/spacecatalog-dr1-sources.csv',
                '8f9c4d6441d1ed5c3ba38c35164ae275726167b3609df8e7eb2a1732dcb57ea4'),
}


def spacecatalog_rows(cache_dir):
    """Download pinned SpaceCatalog release, verify checksums, and import named sky objects."""
    import csv
    import gzip
    import hashlib
    import io
    import requests
    root=Path(cache_dir)/'spacecatalog-dr1'
    root.mkdir(parents=True,exist_ok=True)
    tables={}
    for key,(url,expected) in SPACECATALOG_FILES.items():
        filename=Path(urlparse(url).path).name
        path=root/filename
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            response=requests.get(url,timeout=(15,180),headers={'User-Agent':'HubbleScanner/1.0 research catalog import'})
            response.raise_for_status()
            raw=response.content
            if hashlib.sha256(raw).hexdigest()!=expected:
                raise ValueError(f'SpaceCatalog {key} checksum mismatch')
            temporary=path.with_suffix('.tmp')
            temporary.write_bytes(raw);temporary.replace(path)
        opener=gzip.open if path.name.endswith('.gz') else open
        with opener(path,'rt',encoding='utf-8',newline='') as stream:
            tables[key]=list(csv.DictReader(stream))
    aliases={}
    for row in tables['designations']:
        aliases.setdefault(row.get('slug'),[]).append(row.get('designation','').strip())
    provenance={row['id']:row for row in tables['sources']}
    results=[]
    for row in tables['objects']:
        try:
            ra=float(row['ra_deg']);dec=float(row['dec_deg'])
        except (TypeError,ValueError,KeyError): continue
        if not (0<=ra<360 and -90<=dec<=90): continue
        ident='SC-'+row['slug']
        names=list(dict.fromkeys([row['name']]+aliases.get(row['slug'],[])))
        names=[n for n in names if n]
        source=provenance.get(row.get('source',''),{})
        results.append({'id':ident,'ra_deg':ra,'dec_deg':dec,'major_arcmin':0,
            'object_type':row.get('object_type') or row.get('category',''),
            'wiki_titles':names or [ident],'aliases':names[1:],
            'source':'spacecatalog:'+row.get('source','unknown'),
            'source_name':source.get('name',''),'source_url':source.get('url',''),
            'source_license':source.get('license',''),'source_retrieved_at':source.get('retrieved_at',''),
            'spacecatalog_slug':row['slug'],'spacecatalog_category':row.get('category',''),
            'catalog_verified':True,'catalog_release':'SpaceCatalog DR1',
            'publication_status':'not_assessed'})
    return results
