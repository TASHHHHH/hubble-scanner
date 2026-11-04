"""Public MAST metadata cache and exact-product TSDP export."""
import argparse
import csv
import json
import time
from pathlib import Path
from urllib.parse import quote
from runtime import cached_call

def table_rows(table):
    import numpy as np
    rows = []
    for row in table:
        result = {}
        for key in table.colnames:
            v = row[key]
            if np.ma.is_masked(v): v = None
            elif isinstance(v, np.generic): v = v.item()
            if isinstance(v, bytes): v = v.decode('utf-8')
            if isinstance(v, float) and not np.isfinite(v): v = None
            result[key] = v
        rows.append(result)
    return rows

def query_observations(pos, radius):
    from astropy.table import Table
    from astroquery.mast import Observations
    import astropy.units as u
    key = {'ra':float(pos.ra.deg),'dec':float(pos.dec.deg),'radius_arcmin':radius,'schema':1}
    rows = cached_call('mast_observations', key,
        lambda: table_rows(Observations.query_region(pos, radius=radius*u.arcmin)))
    return Table(rows=rows) if rows else Table(names=['obs_collection','dataproduct_type'])

def products(obsid):
    from astroquery.mast import Observations
    return cached_call('mast_products', str(obsid), lambda:table_rows(Observations.get_product_list(str(obsid))))

FIELDS = ['target_name','obsid','obs_id','obs_collection','instrument_name','filters',
          't_exptime','s_ra','s_dec','productFilename','dataURI','dataURL',
          'productType','productSubGroupDescription','calib_level','size',
          'scanner_group','rgb_channel','geometry_basis','publication_status']

def product_rows(item, group, group_index, allowed=('DRC','DRZ','FLC','FLT','CAL','C0M','C0F'), provider=products):
    result = []
    obsmap = {str(o['obsid']):o for o in item.get('observations', [])}
    for channel, filt, obsid in zip('RGB', group['filters_rgb'], group['obsids_rgb']):
        obs = obsmap[str(obsid)]
        available = provider(obsid)
        selected = []
        # Prefer one calibration family per observation to avoid duplicate raw/drizzled inputs.
        for subgroup in allowed:
            selected = [p for p in available if str(p.get('productType','')).upper() == 'SCIENCE'
                        and str(p.get('productSubGroupDescription','')).upper() == subgroup
                        and str(p.get('productFilename','')).lower().endswith(('.fits','.fits.gz','.fits.fz'))
                        and p.get('dataURI')
                        and str(p.get('dataURI')).startswith('mast:HST/product/')
                        and (not p.get('filters') or str(p['filters']).upper() == filt.upper())
                        and str(p.get('dataRights','PUBLIC')).upper() == 'PUBLIC'
                        and str(p.get('obsID',p.get('obsid',obsid))) == str(obsid)]
            if selected: break
        if not selected:
            raise ValueError(f'No eligible science FITS product for {item["id"]} {filt} obsid={obsid}')
        for p in selected:
            uri = p['dataURI']
            result.append({'target_name':item['id'],'obsid':obsid,'obs_id':obs.get('obs_id',''),
                'obs_collection':'HST','instrument_name':obs.get('instrument',''),'filters':filt,
                't_exptime':obs.get('exptime',0),'s_ra':item['ra_deg'],'s_dec':item['dec_deg'],
                **{k:p.get(k,'') for k in ('productFilename','dataURI','productType','productSubGroupDescription','calib_level','size')},
                'dataURL':'https://mast.stsci.edu/api/v0.1/Download/file?uri='+quote(uri,safe=''),
                'scanner_group':f'{item["id"]}:{group_index}','rgb_channel':channel,
                'geometry_basis':group['geometry_basis'],'publication_status':'not_assessed'})
    return result

def export(results, groups, out, limit=10):
    from scanner import read_jsonl
    items = {x['id']:x for x in read_jsonl(results)}
    rows, errors, seen = [], [], set()
    for index, group in enumerate(read_jsonl(groups)):
        if limit and index >= limit: break
        try:
            item = items[group['id']]
            if item.get('wiki_status') != 'not_detected': continue
            for row in product_rows(item, group, index):
                key = (row['scanner_group'],row['rgb_channel'],row['dataURI'])
                if key not in seen: rows.append(row); seen.add(key)
        except Exception as ex:
            errors.append({'id':group['id'],'error':str(ex)})
    path=Path(out);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    path.with_suffix('.errors.json').write_text(json.dumps(errors,indent=2),encoding='utf-8')
    print(f'TSDP: {len(rows)} exact product rows, {len(errors)} failed groups -> {path}')
    print(f'Listed size: {sum(int(r.get("size") or 0) for r in rows)/1e9:.3f} GB (may include products reused across groups)')
    if errors: raise SystemExit(1)

if __name__ == '__main__':
    p=argparse.ArgumentParser(description='Export exact RGB science products to TSDP Import MAST CSV')
    p.add_argument('--results',default='out/candidates.jsonl')
    p.add_argument('--groups',default='out/filter_groups.jsonl')
    p.add_argument('--out',default='out/tsdp_products.csv')
    p.add_argument('--limit',type=int,default=10)
    a=p.parse_args()
    if a.limit < 0:p.error('limit must be nonnegative')
    export(a.results,a.groups,a.out,a.limit)
