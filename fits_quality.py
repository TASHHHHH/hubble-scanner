"""Bounded-memory pixel QA and sampled common-sky validity for local HST FITS.

Read-only screening: never repairs pixels or claims calibrated RGB suitability.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import skycoord_to_pixel
from astropy.coordinates import SkyCoord
from shapely.geometry import Polygon
from shapely import contains_xy
from fits_verify import inspect_file, verify
from footprint_groups import project_gnomonic
from runtime import atomic_json

def file_digest(path):
    digest=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def flags(primary, saturation_bits=None, cosmic_bits=None):
    instrument=str(primary.get('INSTRUME','')).upper()
    detector=str(primary.get('DETECTOR','')).upper()
    defaults=(None,None)
    if instrument=='ACS' or (instrument=='WFC3' and detector=='UVIS'):
        defaults=(256|2048,4096|8192)
    elif instrument=='WFC3' and detector=='IR':
        defaults=(256,1024|4096)
    return (saturation_bits if saturation_bits is not None else defaults[0],
            cosmic_bits if cosmic_bits is not None else defaults[1])

def companions(hdul, science):
    """Pair masks only within an EXTVER, never use another chip's mask."""
    version=hdul[science].header.get('EXTVER',1)
    shape=(hdul[science].header['NAXIS2'],hdul[science].header['NAXIS1'])
    result={}; issues=[]
    for name in ('DQ','ERR','WHT'):
        matches=[h for h in hdul if h.name.upper()==name and h.header.get('EXTVER',1)==version]
        if len(matches)>1:
            issues.append(f'ambiguous_{name}');continue
        if not matches:continue
        h=matches[0]
        if name=='ERR':
            sciunit=str(hdul[science].header.get('BUNIT','')).strip().casefold()
            errunit=str(h.header.get('BUNIT','')).strip().casefold()
            if sciunit and errunit and sciunit!=errunit:
                issues.append('ERR_unit_mismatch');continue
        if h.header.get('NAXIS',0)==0 and 'PIXVALUE' in h.header:
            result[name]=float(h.header['PIXVALUE'])
        elif h.header.get('NAXIS',0)==2 and (h.header['NAXIS2'],h.header['NAXIS1'])==shape:
            result[name]=h
        else:issues.append(f'incompatible_{name}_shape')
    return result,issues

def section(h, sl, shape):
    return np.full(shape,h) if isinstance(h,(int,float)) else np.asarray(h.section[sl])

def validity(data, masks, bad_bits):
    good=np.isfinite(data)
    counts={'nonfinite':int((~good).sum())}
    if 'DQ' in masks:
        dq=np.asarray(masks['DQ'])
        finite=np.isfinite(dq)
        integer=np.zeros(dq.shape,dtype=np.uint64)
        finite &= (dq>=0) & (dq==np.floor(dq))
        integer[finite]=dq[finite].astype(np.uint64)
        bad=~finite | ((integer!=0) if bad_bits==-1 else (integer & bad_bits)!=0)
        counts['dq_rejected']=int(bad.sum());good &= ~bad
    for name in ('ERR','WHT'):
        if name in masks:
            bad=~np.isfinite(masks[name]) | (masks[name]<=0)
            counts[name.lower()+'_rejected']=int(bad.sum());good &= ~bad
    return good,counts

def plane_quality(path, record, bad_bits=-1, saturation_bits=None, cosmic_bits=None):
    ny,nx=record['shape'];total=ny*nx
    count={'pixels':total,'valid_pixels':0,'nonfinite':0}
    values=[];differences=[];snr=[]
    stride=max(1,math.ceil(total/200000))
    with fits.open(path,memmap=False) as hdul:
        science=hdul[record['hdu']]
        related,issues=companions(hdul,record['hdu'])
        sat,cr=flags(hdul[0].header,saturation_bits,cosmic_bits)
        if sat is not None and 'DQ' in related:count['saturation_flagged']=0
        if cr is not None and 'DQ' in related:count['cosmic_ray_flagged']=0
        # Bound each tile, including extremely wide images.
        for y in range(0,ny,256):
            for x in range(0,nx,1024):
                sl=(slice(y,min(y+256,ny)),slice(x,min(x+1024,nx)))
                data=np.asarray(science.section[sl],dtype=np.float64)
                masks={k:section(h,sl,data.shape) for k,h in related.items()}
                good,c=validity(data,masks,bad_bits)
                count['valid_pixels']+=int(good.sum())
                for key,v in c.items():count[key]=count.get(key,0)+v
                dq=np.nan_to_num(masks.get('DQ',np.zeros(data.shape)),nan=0).astype(np.uint64)
                for name,bits in [('saturation_flagged',sat),('cosmic_ray_flagged',cr)]:
                    if name in count:count[name]+=int(((dq & bits)!=0).sum())
                # Global regular subsampling bounds metric storage independently of file size.
                yy,xx=np.indices(data.shape)
                sample=((yy+y)*nx+(xx+x))%stride==0
                v=data[sample];g=good[sample];values.extend(v[g].tolist())
                delta=data[:,1:]-data[:,:-1];dg=good[:,1:] & good[:,:-1]
                differences.extend(delta[sample[:,1:] & dg].tolist())
                if 'ERR' in masks:
                    err=masks['ERR'][sample]
                    snr.extend((v[g]/err[g]).tolist())
        count['valid_fraction']=count['valid_pixels']/total
        count['saturation_flagged']=count.get('saturation_flagged')
        count['cosmic_ray_flagged']=count.get('cosmic_ray_flagged')
        metrics={}
        if values:
            v=np.asarray(values);median=float(np.median(v))
            metrics={'sample_count':len(v),'median':median,'p05':float(np.percentile(v,5)),
                     'p95':float(np.percentile(v,95))}
            noise=float(np.median(np.abs(np.asarray(differences)-np.median(differences)))/0.9538725524) if differences else 0
            metrics['neighbor_difference_noise_proxy']=noise
            metrics['p95_contrast_over_noise_proxy']=(metrics['p95']-median)/noise if noise>0 else None
        if snr:
            metrics['median_sci_over_err']=float(np.median(snr))
            metrics['p95_sci_over_err']=float(np.percentile(snr,95))
        warnings=[]
        contrast=metrics.get('p95_contrast_over_noise_proxy')
        if contrast is None:warnings.append('noise_proxy_unavailable_or_flat_image')
        elif contrast<3:warnings.append('low_contrast_proxy_review_source_signal')
        if count.get('saturation_flagged'):warnings.append('saturation_flags_present')
        if count.get('cosmic_ray_flagged'):warnings.append('cosmic_ray_flags_present')
        return {**record,**count,'metrics':metrics,'available_masks':sorted(related),
                'warnings':warnings,
                'mask_issues':issues,'dq_bad_bits':bad_bits,'saturation_bits':sat,'cosmic_ray_bits':cr,
                'metrics_note':'SCI/ERR is per-pixel pipeline ratio, not background-subtracted source SNR. Noise proxy assumes independent neighbors; inspect correlated/drizzled data.',
                'status':'rejected_no_valid_pixels' if not count['valid_pixels'] else 'review_required'}

def sample_valid(path, hdu, coords, bad_bits):
    result=np.zeros(len(coords),dtype=bool)
    with fits.open(path,memmap=False) as hdul:
        science=hdul[hdu];w=WCS(science.header,hdul).celestial
        px,py=skycoord_to_pixel(coords,w,origin=0,mode='all')
        ny,nx=science.header['NAXIS2'],science.header['NAXIS1']
        inside=np.isfinite(px)&np.isfinite(py)&(px>=-0.5)&(px<nx-0.5)&(py>=-0.5)&(py<ny-0.5)
        selected=np.flatnonzero(inside)
        ix=np.floor(px[selected]+0.5).astype(int);iy=np.floor(py[selected]+0.5).astype(int)
        related,issues=companions(hdul,hdu)
        # Group samples into small reads instead of loading the full science image.
        keys=(iy//256)*math.ceil(nx/256)+(ix//256)
        for key in np.unique(keys):
            take=np.flatnonzero(keys==key);x0=(ix[take[0]]//256)*256;y0=(iy[take[0]]//256)*256
            sl=(slice(y0,min(y0+256,ny)),slice(x0,min(x0+256,nx)))
            data=np.asarray(science.section[sl])
            masks={k:section(h,sl,data.shape) for k,h in related.items()}
            good,_=validity(data,masks,bad_bits)
            result[selected[take]]=good[iy[take]-y0,ix[take]-x0]
    return result

def common_quality(group, records, grid=128, bad_bits=-1, min_valid=0.5):
    planes=[records[(p,h)] for p,h in zip(group['paths_rgb'],group['hdu_rgb'])]
    ra0,dec0=planes[0]['footprint'][0]
    polys=[Polygon([project_gnomonic(ra,dec,ra0,dec0) for ra,dec in p['footprint']]) for p in planes]
    shared=polys[0].intersection(polys[1]).intersection(polys[2])
    x0,y0,x1,y1=shared.bounds
    xx,yy=np.meshgrid(x0+(np.arange(grid)+0.5)*(x1-x0)/grid,y0+(np.arange(grid)+0.5)*(y1-y0)/grid)
    inside=contains_xy(shared,xx,yy);x=xx[inside]*np.pi/10800;y=yy[inside]*np.pi/10800
    d0=np.deg2rad(dec0);r0=np.deg2rad(ra0)
    ra=r0+np.arctan2(x,np.cos(d0)-y*np.sin(d0))
    dec=np.arctan2(np.sin(d0)+y*np.cos(d0),np.sqrt((np.cos(d0)-y*np.sin(d0))**2+x*x))
    coords=SkyCoord(ra=np.rad2deg(ra)%360,dec=np.rad2deg(dec),unit='deg')
    masks=[sample_valid(p,h,coords,bad_bits) for p,h in zip(group['paths_rgb'],group['hdu_rgb'])]
    valid=np.logical_and.reduce(masks);n=len(valid);fraction=float(valid.mean()) if n else 0
    issues=[issue for plane in planes for issue in plane['mask_issues']]
    missing=any(not p['available_masks'] for p in planes)
    status='rejected_low_common_coverage' if fraction<min_valid else 'review_required' if issues or missing or n<64 else 'passes_coverage_screen'
    return {**group,'pixel_validity_verified':False,'quality_status':status,
        'common_grid_samples':n,'common_valid_samples':int(valid.sum()),'sampled_common_valid_fraction':fraction,
        'band_valid_fractions_rgb':[float(m.mean()) if n else 0 for m in masks],
        'sampled_usable_area_arcmin2':group['common_area_arcmin2']*fraction,
        'missing_quality_masks':missing,'mask_issues':issues,'minimum_common_valid_fraction':min_valid,
        'band_warnings_rgb':[p['warnings'] for p in planes],
        'quality_method':f'{grid}x{grid} regular tangent-plane grid, nearest science pixel, DQ/ERR/WHT masks; finite zeros retained',
        'quality_note':'Sampled coverage screen only. Sub-grid defects, unflagged cosmic rays, saturation without DQ, source SNR and calibrated RGB suitability remain unverified.'}

def assess(paths, min_overlap=0.35, grid=128, min_valid=0.5, bad_bits=-1, saturation_bits=None, cosmic_bits=None):
    if not 16<=grid<=512 or not 0<min_valid<=1 or bad_bits < -1 or any(v is not None and v<0 for v in (saturation_bits,cosmic_bits)):raise ValueError('Invalid quality settings')
    geometry=verify(paths,min_overlap);planes=[];errors=[]
    for f in geometry['files']:
        errors.extend({'path':f['path'],**e} for e in f['errors'])
        if not f['extensions'] and not f['errors']:
            errors.append({'path':f['path'],'error':'No supported 2D science extension with an unambiguous filter and celestial WCS'})
        for r in f['extensions']:
            try:planes.append(plane_quality(f['path'],r,bad_bits,saturation_bits,cosmic_bits))
            except Exception as ex:errors.append({'path':f['path'],'hdu':r['hdu'],'error':str(ex)})
    index={(p['path'],p['hdu']):p for p in planes};groups=[]
    for g in geometry['groups']:
        try:groups.append(common_quality(g,index,grid,bad_bits,min_valid))
        except Exception as ex:errors.append({'paths':g['paths_rgb'],'error':str(ex)})
    return {'schema':'fits-quality-1','planes':planes,'groups':groups,'errors':errors,
            'publication_status':'not_assessed','read_only':True}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('files',nargs='+')
    p.add_argument('--out',default='out/fits_quality.json');p.add_argument('--grid',type=int,default=128)
    p.add_argument('--min-valid',type=float,default=0.5);p.add_argument('--min-overlap',type=float,default=0.35)
    p.add_argument('--bad-bits',type=int,default=-1,help='-1 rejects every nonzero DQ flag; otherwise explicit bit mask')
    p.add_argument('--saturation-bits',type=int);p.add_argument('--cosmic-bits',type=int);a=p.parse_args()
    report=assess(a.files,a.min_overlap,a.grid,a.min_valid,a.bad_bits,a.saturation_bits,a.cosmic_bits)
    atomic_json(a.out,report)
    print(f'{len(report["planes"])} science planes, {len(report["groups"])} RGB coverage checks, {len(report["errors"])} errors -> {a.out}')
    if report['errors']:raise SystemExit(1)

if __name__=='__main__':main()
