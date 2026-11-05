"""Search local Hubble science frames for coincident sources across their full shared field."""
import argparse
from collections import defaultdict
from html import escape
from pathlib import Path
import tempfile

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import pixel_to_skycoord
from PIL import Image
from scipy.ndimage import maximum_filter, convolve
from scipy.spatial import cKDTree

from fits_quality import companions, section, validity
from fits_verify import inspect_file
from runtime import atomic_json


def detect(hdul, plane):
    """Tile with a halo so peaks on tile boundaries are evaluated in context."""
    hdu = hdul[plane['hdu']]
    masks, issues = companions(hdul, plane['hdu'])
    if issues:
        raise ValueError(', '.join(issues))
    ny, nx = plane['shape']
    peaks = []
    for y in range(0, ny, 256):
        for x in range(0, nx, 256):
            y0, x0 = max(0, y-4), max(0, x-4)
            sl = np.s_[y0:min(ny,y+260), x0:min(nx,x+260)]
            data = np.asarray(hdu.section[sl], dtype=float)
            valid, _ = validity(data, {k:section(v, sl, data.shape) for k,v in masks.items()}, -1)
            values = data[valid]
            if values.size < 16:
                continue
            median = np.median(values)
            noise = 1.4826 * np.median(np.abs(values - median))
            if noise <= 0:
                continue
            z = np.where(valid, (data - median) / noise, -np.inf)
            support = convolve((z > 2).astype(np.uint8), np.ones((3,3)), mode='constant')
            yy, xx = np.nonzero((z >= 5) & (z == maximum_filter(z, size=7)) & (support >= 3))
            for iy, ix in zip(yy, xx):
                py, px = int(iy+y0), int(ix+x0)
                if y <= py < y+256 and x <= px < x+256:
                    peaks.append((px, py, float(z[iy, ix])))
    if not peaks:
        return []
    xs, ys, scores = zip(*peaks)
    coords = pixel_to_skycoord(np.array(xs), np.array(ys), WCS(hdu.header, hdul).celestial).icrs
    return [dict(path=plane['path'], exposure=str(hdul[0].header.get('ROOTNAME') or Path(plane['path']).name),
                 hdu=plane['hdu'], filter=plane['filters'][0], x=x, y=y, ra=float(ra), dec=float(dec),
                 peak_contrast=score)
            for x,y,score,ra,dec in zip(xs,ys,scores,coords.ra.deg,coords.dec.deg)
            if np.isfinite(ra) and np.isfinite(dec)]


def matches(points, radius):
    if not points:
        return []
    ra, dec = np.deg2rad([(p['ra'],p['dec']) for p in points]).T
    vectors = np.column_stack((np.cos(dec)*np.cos(ra), np.cos(dec)*np.sin(ra), np.sin(dec)))
    tree = cKDTree(vectors)
    chord = 2*np.sin(np.deg2rad(radius/3600)/2)
    used = set()
    groups = []
    for i in sorted(range(len(points)), key=lambda i: -points[i]['peak_contrast']):
        if i in used:
            continue
        neighbors = sorted(tree.query_ball_point(vectors[i], chord),
                           key=lambda j: np.linalg.norm(vectors[j]-vectors[i]))
        members = {points[i]['exposure']: i}
        for j in neighbors:
            if j not in used:
                members.setdefault(points[j]['exposure'], j)
        if len(members) >= 2:
            used.update(members.values())
            groups.append([points[j] for j in members.values()])
    return sorted(groups, key=lambda g: (-len(g), -min(p['peak_contrast'] for p in g)))


def write_cutouts(groups, folder):
    by_file = defaultdict(list)
    for i, group in enumerate(groups, 1):
        for j, point in enumerate(group, 1):
            point['cutout'] = f'candidate-{i}-{j}.png'
            by_file[point['path']].append(point)
    for path, points in by_file.items():
        with fits.open(path, memmap=False) as hdul:
            for p in points:
                hdu = hdul[p['hdu']]
                x,y = p['x'],p['y']
                ny, nx = hdu.shape
                sl = np.s_[max(0,y-24):min(ny,y+25), max(0,x-24):min(nx,x+25)]
                data = np.asarray(hdu.section[sl], dtype=float)
                lo,hi = np.percentile(data[np.isfinite(data)], [5,99])
                pixels = np.clip((np.nan_to_num(data, nan=lo)-lo)/max(hi-lo, 1e-20), 0, 1)
                canvas = Image.new('L', (49, 49))
                image = Image.fromarray((pixels[::-1]*255).astype('uint8'))
                canvas.paste(image, (max(0,24-x), max(0,y+25-ny)))
                canvas.save(folder/p['cutout'])


def scan(paths, output, radius=0.2):
    paths = list(dict.fromkeys(Path(p).resolve() for p in paths))
    if len(paths) < 2:
        raise ValueError('Choose at least two different science FITS files with overlapping coverage.')
    if not 0 < radius <= 5:
        raise ValueError('Match radius must be above zero and at most 5 arcseconds.')
    points, warnings = [], []
    for path in paths:
        inspection = inspect_file(path)
        warnings.extend(f'{path.name}: {r["error"]}' for r in inspection['errors'])
        if not inspection['extensions']:
            raise ValueError(f'{path.name}: no usable science plane. ' + '; '.join(warnings))
        with fits.open(path, memmap=False) as hdul:
            for plane in inspection['extensions']:
                points.extend(detect(hdul, plane))
    groups = matches(points, radius)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='field-', dir=output))
    selected = groups[:200]
    write_cutouts(selected, folder)
    report = {'files':list(map(str,paths)), 'radius_arcsec':radius, 'matched_count':len(groups),
              'warnings':warnings, 'candidates':selected}
    atomic_json(folder/'candidates.json', report)
    rows = []
    for i,g in enumerate(selected,1):
        images = ''.join(f'<figure><img width="120" src="{p["cutout"]}"><figcaption>'
                         f'{escape(p["exposure"])} · {escape(p["filter"])} · HDU {p["hdu"]}</figcaption></figure>' for p in g)
        rows.append(f'<section><h2>{i}. RA {g[0]["ra"]:.6f}°, Dec {g[0]["dec"]:.6f}° (ICRS)</h2>{images}</section>')
    page = ('<!doctype html><meta charset="utf-8"><title>Whole-frame candidates</title>'
            '<style>body{font:16px system-ui;max-width:960px;margin:2em auto}figure{display:inline-block;margin:1em}img{image-rendering:pixelated}</style>'
            f'<h1>Whole-frame candidates</h1><p>{len(groups)} matched sources. Showing {len(selected)}. '
            f'Match radius {radius:g} arcsec. These are sources for visual review. Novelty has not been assessed.</p>'
            + ''.join(f'<p>{escape(w)}</p>' for w in warnings) + ''.join(rows))
    (folder/'index.html').write_text(page, encoding='utf-8')
    atomic_json(output/'latest.json', {'report':str(folder/'index.html')})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+')
    parser.add_argument('--out', required=True)
    parser.add_argument('--radius', type=float, default=0.2, help='Cross-frame match radius in arcseconds')
    args = parser.parse_args()
    result = scan(args.files, args.out, args.radius)
    print(f"Matched {result['matched_count']} sources. Open the whole-frame report in FITS Quality.")
