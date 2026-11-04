"""All-science-HDU, distortion-aware sampled-boundary WCS geometry checks.

Geometric verification alone does not establish valid pixels or RGB quality.
"""
import argparse
import json
from itertools import combinations
from pathlib import Path
import numpy as np
from shapely.geometry import Polygon
from footprint_groups import filter_names, angular_wavelength_nm, project_gnomonic

def inspect_file(path, edge_samples=32):
    from astropy.io import fits
    from astropy.wcs import WCS
    from astropy.wcs.utils import pixel_to_skycoord
    records, errors = [], []
    path = Path(path)
    try:
        with fits.open(path, memmap=True) as hdul:
            primary = hdul[0].header
            primary_filters = filter_names(' '.join(str(primary.get(k,'')) for k in ('FILTER','FILTER1','FILTER2')))
            for i, hdu in enumerate(hdul):
                hdr = hdu.header
                if i > 0 and str(hdr.get('EXTNAME','')).upper() not in ('SCI','SCIENCE','PRIMARY',''):
                    continue
                if hdr.get('NAXIS',0) != 2: continue
                nx, ny = hdr.get('NAXIS1',0), hdr.get('NAXIS2',0)
                if min(nx,ny) < 2: continue
                try:
                    w = WCS(hdr, hdul).celestial
                    if not w.has_celestial: raise ValueError('No celestial WCS')
                    x = np.linspace(-0.5,nx-0.5,edge_samples)
                    y = np.linspace(-0.5,ny-0.5,edge_samples)
                    boundary = np.array([(v,-0.5) for v in x] + [(nx-0.5,v) for v in y[1:]] +
                                        [(v,ny-0.5) for v in x[-2::-1]] + [(-0.5,v) for v in y[-2:0:-1]])
                    coords = pixel_to_skycoord(boundary[:,0],boundary[:,1],w,origin=0,mode='all').icrs
                    sky = np.column_stack((coords.ra.deg,coords.dec.deg))
                    if not np.isfinite(sky).all(): raise ValueError('Nonfinite WCS boundary')
                    filters = filter_names(' '.join(str(hdr.get(k,'')) for k in ('FILTER','FILTER1','FILTER2'))) or primary_filters
                    if len(filters) != 1: raise ValueError('Ambiguous or missing science filter')
                    records.append({'path':str(path.resolve()),'hdu':i,'filters':filters,
                        'footprint':sky.tolist(),'shape':[ny,nx],'instrument':str(primary.get('INSTRUME','')),
                        'exptime':float(hdr.get('EXPTIME',primary.get('EXPTIME',0)) or 0)})
                except Exception as ex:
                    errors.append({'hdu':i,'error':str(ex)})
    except Exception as ex:
        errors.append({'error':str(ex)})
    return {'path':str(path),'extensions':records,'errors':errors}

def verify(paths, min_fraction=0.35):
    if not 0 < min_fraction <= 1: raise ValueError('min_fraction must be in (0,1]')
    files = [inspect_file(p) for p in dict.fromkeys(paths)]
    good = [r for f in files for r in f['extensions']]
    groups = []
    for triple in combinations(good,3):
        if len({r['path'] for r in triple}) != 3: continue
        fl = [r['filters'][0] for r in triple]
        wave = [angular_wavelength_nm(f) for f in fl]
        if None in wave or len(set(wave)) != 3: continue
        ra0, dec0 = triple[0]['footprint'][0]
        polys = []
        for record in triple:
            points = [project_gnomonic(ra,dec,ra0,dec0) for ra,dec in record['footprint']]
            if any(p is None for p in points): break
            poly = Polygon(points)
            if not poly.is_valid or poly.is_empty or poly.area <= 0: break
            polys.append(poly)
        if len(polys) != 3: continue
        shared = polys[0].intersection(polys[1]).intersection(polys[2])
        ratio = shared.area/min(p.area for p in polys)
        if ratio < min_fraction: continue
        order = sorted(range(3),key=lambda i:wave[i],reverse=True)
        groups.append({'filters_rgb':[fl[i] for i in order],
            'paths_rgb':[triple[i]['path'] for i in order],'hdu_rgb':[triple[i]['hdu'] for i in order],
            'common_area_arcmin2':round(shared.area,6),'common_fraction_of_smallest':round(ratio,6),
            'wcs_verified':True,'pixel_validity_verified':False,
            'geometry_basis':'FITS celestial WCS sampled detector boundary (32 samples/edge)',
            'publication_status':'not_assessed'})
    groups.sort(key=lambda g:g['common_area_arcmin2'],reverse=True)
    return {'files':files,'groups':groups}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('files',nargs='+');p.add_argument('--min-overlap',type=float,default=0.35)
    p.add_argument('--out',default='out/fits_overlap.json');a=p.parse_args()
    result=verify(a.files,a.min_overlap)
    dest=Path(a.out);dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(f'{len(result["groups"])} WCS geometric groups -> {dest}; pixel quality remains unverified')

if __name__ == '__main__': main()
