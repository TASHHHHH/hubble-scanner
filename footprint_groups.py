"""Fast, conservative HST multi-filter footprint grouping.

Uses MAST `s_region` geometry and a local gnomonic projection (arcminutes).
No FITS pixels downloaded. Confirms actual WCS / data quality only later.

CLI expects the existing scanner's merged `candidates.jsonl`.
"""
import argparse
import csv
import itertools
import json
import math
import re
from pathlib import Path

from shapely.geometry import Polygon, Point

BAD_FILTER = re.compile(r'(CLEAR|GRISM|PRISM|G\d{3}|BLOCK|POL|UNKNOWN|NONE|N/A)', re.I)
FILTER_CODE = re.compile(r'^FQ?\d{3,4}[A-Z0-9]*$', re.I)
WAVELENGTH_CODE = re.compile(r'^FQ?(\d{3,4})', re.I)


def angular_wavelength_nm(filt):
    """Approximate HST filter center solely for suggested ordering (not calibration)."""
    m = WAVELENGTH_CODE.match(filt.upper().strip())
    if not m:
        return None
    n = int(m.group(1))
    infrared = {'F098M','F105W','F110W','F110M','F125W','F126N','F127M',
                'F128N','F130N','F132N','F139M','F140W','F145M','F153M',
                'F160W','F164N','F165M','F167N','F170M','F180M','F187N','F207M',
                'F187W','F190N','F196N','F200N','F205W','F212N','F215N',
                'F216N','F222M','F237M'}
    if filt.upper().strip() in infrared:
        return n * 10.0
    return float(n)


def filter_names(raw):
    if not raw:
        return []
    tokens = re.split(r'[;,\s]+', str(raw).upper())
    return sorted({t for t in tokens if FILTER_CODE.match(t) and not BAD_FILTER.search(t)})


def project_gnomonic(ra_deg, dec_deg, origin_ra, origin_dec):
    """Project an ICRS direction to tangent-plane arcminutes around catalog center."""
    r, d = math.radians(ra_deg), math.radians(dec_deg)
    r0, d0 = math.radians(origin_ra), math.radians(origin_dec)
    dr = (r - r0 + math.pi) % (2 * math.pi) - math.pi
    den = math.sin(d0)*math.sin(d) + math.cos(d0)*math.cos(d)*math.cos(dr)
    if den <= 0:
        return None
    x = math.cos(d)*math.sin(dr)/den
    y = (math.cos(d0)*math.sin(d)-math.sin(d0)*math.cos(d)*math.cos(dr))/den
    return x*180/math.pi*60, y*180/math.pi*60


def parse_sregion(raw, origin_ra, origin_dec):
    """Supported STC-S geometries: POLYGON or CIRCLE in ICRS/J2000.

    Returns (shapely_geometry, method) or (None, reason). Unsupported and
    multipolygon cases are explicitly left unverified, never considered overlap.
    """
    if not raw or str(raw).lower() in ('none', 'nan', '--'):
        return None, 'missing'
    parts = str(raw).replace(',', ' ').strip().split()
    if not parts:
        return None, 'empty'
    shape = parts[0].upper()
    if shape not in ('POLYGON', 'CIRCLE'):
        return None, 'unsupported_stcs'
    # MAST also emits concatenated POLYGON components for separated chips.
    if shape == 'POLYGON' and sum(p.upper() == 'POLYGON' for p in parts) > 1:
        from shapely.ops import unary_union
        starts=[i for i,p in enumerate(parts) if p.upper()=='POLYGON']+[len(parts)]
        components=[]
        for a,b in zip(starts,starts[1:]):
            geom,reason=parse_sregion(' '.join(parts[a:b]),origin_ra,origin_dec)
            if geom is None:return None, reason
            components.append(geom)
        return unary_union(components), 'MAST_multipolygon'
    i = 1
    if i < len(parts) and parts[i].upper() in ('ICRS', 'J2000'):
        i += 1
    elif i < len(parts) and re.match(r'^[A-Za-z]', parts[i]):
        return None, 'unsupported_reference_frame'
    try:
        nums = list(map(float, parts[i:]))
        if not all(math.isfinite(x) for x in nums):
            return None, 'nonfinite_coordinates'
        if shape == 'POLYGON':
            if len(nums) < 6 or len(nums)%2:
                return None, 'bad_polygon'
            if any(not 0 <= nums[j] <= 360 or not -90 <= nums[j+1] <= 90 for j in range(0,len(nums),2)):
                return None, 'invalid_coordinates'
            pts = [project_gnomonic(nums[j], nums[j+1], origin_ra, origin_dec)
                   for j in range(0, len(nums), 2)]
            if any(p is None for p in pts):
                return None, 'out_of_projection'
            geom = Polygon(pts)
            if not geom.is_valid:
                return None, 'invalid_polygon'
            if geom.is_empty or geom.area <= 0:
                return None, 'invalid_polygon'
            return geom, 'MAST_polygon'
        if len(nums) != 3:
            return None, 'bad_circle'
        if not 0 <= nums[0] <= 360 or not -90 <= nums[1] <= 90:
            return None, 'invalid_coordinates'
        p = project_gnomonic(nums[0],nums[1],origin_ra,origin_dec)
        if p is None or nums[2] <= 0 or nums[2] > 1:
            return None, 'invalid_circle'
        return Point(p).buffer(nums[2]*60, resolution=64), 'MAST_circle_approximation'
    except (ValueError, TypeError):
        return None, 'bad_coordinates'


def observation_geometries(item):
    ra, dec = float(item['ra_deg']), float(item['dec_deg'])
    valid, errors = [], {}
    for o in item.get('observations', []):
        filters = filter_names(o.get('filter'))
        geom, method = parse_sregion(o.get('s_region'), ra, dec)
        if len(filters) != 1 or geom is None:
            key = 'ambiguous_or_missing_filter' if len(filters) != 1 else method
            errors[key] = errors.get(key, 0) + 1
            continue
        radius = max(0.5, float(item.get('major_arcmin') or 1)/2)
        if not geom.intersects(Point(0,0).buffer(radius)):
            errors['outside_target_region'] = errors.get('outside_target_region',0)+1
            continue
        for f in filters:
            valid.append({'filter':f, 'obsid':o.get('obsid'), 'instrument':o.get('instrument',''),
                          'exptime':float(o.get('exptime') or 0), 'geom':geom, 'method':method})
    return valid, errors


def grouped_candidates(item, minimum_fraction=0.35, top_n=3, max_filters=10):
    if not 0 < minimum_fraction <= 1 or top_n < 1:
        raise ValueError('Invalid overlap fraction or top_n')
    obs, errors = observation_geometries(item)
    if len({o['filter'] for o in obs}) < 3:
        return [], errors
    found = {}
    for seed in obs:
        seedgeom = seed['geom']
        if seedgeom.area <= 0:
            continue
        # Find best observation per filter with footprint covering the seed field.
        by_filter = {}
        for other in obs:
            shared = seedgeom.intersection(other['geom'])
            ratio = shared.area / seedgeom.area
            if ratio < minimum_fraction:
                continue
            quality = (ratio, other['exptime'])
            f = other['filter']
            if f not in by_filter or quality > by_filter[f][0]:
                by_filter[f] = (quality, other)
        if len(by_filter) < 3:
            continue
        # Bound combinations when a field has many narrow-band observations.
        choices = sorted(by_filter.values(), key=lambda z:z[0], reverse=True)[:max_filters]
        items = sorted([v[1] for v in choices], key=lambda z:(angular_wavelength_nm(z['filter']) or 1e7, z['filter']))
        for triple in itertools.combinations(items, 3):
            if len({o['obsid'] for o in triple}) != 3:
                continue
            # Need genuinely different wavelengths, not three copies of one band.
            wl = [angular_wavelength_nm(o['filter']) for o in triple]
            if any(w is None for w in wl) or len(set(wl)) < 3:
                continue
            intersection = triple[0]['geom'].intersection(triple[1]['geom']).intersection(triple[2]['geom'])
            if intersection.is_empty:
                continue
            target = Point(0,0).buffer(max(0.5,float(item.get('major_arcmin') or 1)/2))
            if intersection.intersection(target).area <= 0:
                continue
            # Fraction of the smallest contributing exposure's footprint.
            ref = min(o['geom'].area for o in triple)
            fraction = intersection.area / ref if ref else 0
            if fraction < minimum_fraction:
                continue
            triple = tuple(reversed(triple))  # Actual R/G/B order: long to short wavelength.
            wl = list(reversed(wl))
            names = [o['filter'] for o in triple]
            obsids = [str(o['obsid']) for o in triple]
            identity = tuple(sorted(zip(names, obsids)))
            wavelength_spread = (max(wl)-min(wl))/max(wl)
            score = 100*fraction + 12*wavelength_spread + 0.1*math.log1p(sum(o['exptime'] for o in triple))
            data = {'id':item['id'], 'filters_rgb':names, 'obsids_rgb':obsids,
                    'instruments_rgb':[o['instrument'] for o in triple],
                    'approx_wavelengths_nm':wl, 'common_area_arcmin2':round(intersection.area,4),
                    'common_fraction_of_smallest':round(fraction,4),
                    'score':round(score,3), 'geometry_basis':'MAST s_region (TAN projection)',
                    'requires_fits_wcs_check':True,
                    'search_complete':not item.get('observation_list_truncated',False) and not item.get('search_radius_capped',False),
                    'publication_status':'not_assessed',
                    'grouping_method':'bounded best-per-filter heuristic; not exhaustive',
                    'color_note':'R/G/B = longest/middle/shortest approximate wavelength; pseudo-color, not calibrated natural color'}
            if identity not in found or data['score'] > found[identity]['score']:
                found[identity] = data
    ranked = sorted(found.values(), key=lambda x:x['score'], reverse=True)
    return ranked[:top_n], errors


def run(args):
    from scanner import read_jsonl, write_jsonl
    src = list(read_jsonl(args.results))
    results, summaries = [], []
    for item in src:
        if item.get('status') != 'ok' or item.get('wiki_status') != 'not_detected' or not item.get('observations'):
            continue
        matches, errors = grouped_candidates(item, args.min_overlap, args.top_n)
        results.extend(matches)
        if matches:
            summaries.append({'id':item['id'], 'catalog':item.get('source',''),
                              'wiki_status':item.get('wiki_status',''),
                              'ra_deg':item['ra_deg'], 'dec_deg':item['dec_deg'],
                              'group_count':len(matches), 'best_score':matches[0]['score'],
                              'best_rgb':' / '.join(matches[0]['filters_rgb']),
                              'best_obsids':';'.join(matches[0]['obsids_rgb']),
                              'best_overlap_fraction':matches[0]['common_fraction_of_smallest'],
                              'skipped_observations':sum(errors.values())})
    write_jsonl(args.out, results)
    csv_path = str(Path(args.out).with_suffix('.csv'))
    fields = ['id','catalog','wiki_status','ra_deg','dec_deg','group_count','best_score',
              'best_rgb','best_obsids','best_overlap_fraction','skipped_observations']
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as fh:
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        wr.writerows(sorted(summaries,key=lambda x:x['best_score'],reverse=True))
    print(f'Found {len(results)} valid filter triples among {len(summaries)} targets; {csv_path}')
    print('NOTE: uses MAST s_region, not the downloaded pixel validity masks or full FITS WCS.')


def main():
    p=argparse.ArgumentParser(description='Find overlapping 3-filter Hubble observation groups')
    p.add_argument('--results', default='out/candidates.jsonl')
    p.add_argument('--out', default='out/filter_groups.jsonl')
    p.add_argument('--min-overlap', type=float, default=0.35)
    p.add_argument('--top-n', type=int, default=3)
    args=p.parse_args()
    if not 0 < args.min_overlap <= 1: p.error('--min-overlap must be in (0,1]')
    run(args)

if __name__ == '__main__': main()
