"""Explicit catalog schemas; all output coordinates are ICRS degrees."""
import os
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
        raw=vizier_rows(catalog)
        converted=[]
        for i,row in enumerate(raw):
            try:converted.append(convert_builtin(row,tag))
            except Exception as ex:raise ValueError(f'{catalog} row {i}: {ex}; refusing a silently incomplete catalog') from ex
        print(f'Loaded {tag}: {len(converted)} rows')
        rows.extend(converted)
    return rows
