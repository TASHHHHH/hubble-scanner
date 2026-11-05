import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from field_search import scan


class FieldSearchTests(unittest.TestCase):
    def test_offset_edge_source_and_masked_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for shift in (0, 3):
                w = WCS(naxis=2)
                w.wcs.crpix = [100+shift, 50]
                w.wcs.cdelt = [-0.00002, 0.00002]
                w.wcs.crval = [359.999, 22]
                w.wcs.ctype = ['RA---TAN','DEC--TAN']
                y, x = np.indices((90, 300))
                data = np.random.default_rng(shift).normal(size=x.shape)
                for px,py in [(256,8), (50,50)]:
                    data += 30*np.exp(-((x-px-shift)**2+(y-py)**2)/3)
                dq = np.zeros(x.shape, dtype=np.int16)
                if shift: dq[45:56,45:62] = 1
                primary = fits.PrimaryHDU()
                primary.header['FILTER'] = 'F606W'
                primary.header['ROOTNAME'] = f'obs{shift}'
                path = root / f'{shift}.fits'
                fits.HDUList([primary, fits.ImageHDU(data, header=w.to_header(), name='SCI'),
                              fits.ImageHDU(dq, name='DQ')]).writeto(path)
                paths.append(path)
            out = root/'reports'
            report = scan(paths,out)
            self.assertEqual(report['matched_count'], 1)
            group = report['candidates'][0]
            self.assertEqual({p['x'] for p in group}, {256,259})
            self.assertEqual({p['exposure'] for p in group}, {'obs0','obs3'})
            page = Path(json.loads((out/'latest.json').read_text())['report'])
            self.assertTrue(page.is_file())
            for p in group: self.assertTrue((page.parent/p['cutout']).is_file())
            with fits.open(paths[1], mode='update') as hdul:
                hdul[0].header['ROOTNAME'] = 'obs0'
            self.assertEqual(scan(paths,out)['matched_count'], 0)
