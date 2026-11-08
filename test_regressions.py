import argparse
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import scanner
import runtime
import footprint_groups as fg
import fits_verify
from ranking import diversity_scores
from mast_support import product_rows

REGION='POLYGON ICRS 9.99 19.99 10.01 19.99 10.01 20.01 9.99 20.01'

def item():
    return {'id':'SYNTHETIC','ra_deg':10,'dec_deg':20,'major_arcmin':2,'status':'ok',
        'wiki_status':'not_detected','observations':[
        {'obsid':str(i),'filter':f,'s_region':REGION,'exptime':100}
        for i,f in enumerate(['F435W','F606W','F814W'])]}

def make_fits(path, filt, centers=(10,), dq=False):
    from astropy.io import fits
    from astropy.wcs import WCS
    primary=fits.PrimaryHDU();primary.header['FILTER']=filt
    hdus=[primary]
    for ra in centers:
        w=WCS(naxis=2);w.wcs.crpix=[10,10];w.wcs.cdelt=[-0.001,0.001]
        w.wcs.crval=[ra,20];w.wcs.ctype=['RA---TAN','DEC--TAN']
        hdus.append(fits.ImageHDU(np.ones((20,20),dtype=np.float32),header=w.to_header(),name='DQ' if dq else 'SCI'))
    fits.HDUList(hdus).writeto(path)

class ResumeTests(unittest.TestCase):
    def test_mast_error_is_retried_then_success_is_skipped(self):
        from astropy.table import Table
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=item()
            scanner.write_jsonl(root/'cat',[target])
            scanner.write_jsonl(root/'wiki',[{'id':target['id'],'wiki_status':'not_detected'}])
            scanner.write_jsonl(root/'mast',[{'id':target['id'],'status':'ERROR'}])
            args=argparse.Namespace(catalog=root/'cat',wiki_glob=str(root/'wiki'),out=root/'mast',worker=0,workers=1,limit=0,max_radius_arcmin=12,max_obs_saved=0,delay=0)
            with patch('mast_support.query_observations',return_value=Table(names=['obs_collection','dataproduct_type'])) as query:
                scanner.mast(args);scanner.mast(args)
                self.assertEqual(query.call_count,1)
            self.assertEqual(list(scanner.read_jsonl(root/'mast'))[-1]['status'],'ok')

    def test_mast_failures_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=item()
            scanner.write_jsonl(root/'cat',[target]);scanner.write_jsonl(root/'wiki',[{'id':target['id'],'wiki_status':'not_detected'}])
            args=argparse.Namespace(catalog=root/'cat',wiki_glob=str(root/'wiki'),out=root/'mast',worker=0,workers=1,limit=0,max_radius_arcmin=12,max_obs_saved=0,delay=0)
            with patch('mast_support.query_observations',side_effect=RuntimeError('offline')):
                with self.assertRaises(SystemExit) as ex:scanner.mast(args)
                self.assertEqual(ex.exception.code,1)

    def test_wiki_incomplete_is_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=item();target['wiki_titles']=['SYNTHETIC']
            scanner.write_jsonl(root/'cat',[target]);scanner.write_jsonl(root/'wiki',[{'id':target['id'],'wiki_status':'incomplete'}])
            args=argparse.Namespace(catalog=root/'cat',out=root/'wiki',worker=0,workers=1,limit=0,batch_size=8,delay=0)
            with patch.object(scanner,'session'),patch.object(scanner,'wiki_images',return_value={'SYNTHETIC':[]}) as query:
                scanner.wiki(args);scanner.wiki(args)
                self.assertEqual(query.call_count,1)
    def test_truncated_tail_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'rows.jsonl';p.write_bytes(b'{"id":"a"}\n{"id":')
            self.assertEqual(list(runtime.read_rows(p)),[{'id':'a'}])
            runtime.write_rows(p,[{'id':'b'}],True)
            self.assertEqual([r['id'] for r in runtime.read_rows(p)],['a','b'])

    def test_interior_corruption_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'rows';p.write_text('broken\n{}\n')
            with self.assertRaises(ValueError):list(runtime.read_rows(p))

    def test_valid_tail_kept(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'rows';p.write_text('{"id":"a"}')
            runtime.write_rows(p,[{'id':'b'}],True)
            self.assertEqual(len(list(runtime.read_rows(p))),2)

    def test_manifest_rejects_changes_and_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as td:
            cat=Path(td)/'catalog';cat.write_text('{}')
            args=argparse.Namespace(out=str(Path(td)/'out'),catalog=str(cat),workers=8)
            with runtime.job_guard(args):
                with self.assertRaises(RuntimeError):
                    with runtime.job_guard(args):pass
            args.workers=9
            with self.assertRaises(ValueError):
                with runtime.job_guard(args):pass

    def test_cache_hit_and_failed_call(self):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'HUBBLE_CACHE_DIR':td}):
            from unittest.mock import Mock
            producer=Mock(return_value={'ok':1})
            for _ in range(2):runtime.cached_call('test','key',producer)
            self.assertEqual(producer.call_count,1)
            producer.side_effect=RuntimeError('offline')
            with self.assertRaises(RuntimeError):runtime.cached_call('test','bad',producer)
            self.assertEqual(len(list(Path(td).rglob('*.json'))),1)

    def test_partitions_eight_through_ten(self):
        objects=[{'id':str(i)} for i in range(1000)]
        for count in (8,9,10):
            ids=[r['id'] for worker in range(count) for r in scanner.shard(objects,worker,count)]
            self.assertEqual(len(ids),len(set(ids)));self.assertEqual(len(ids),1000)
        with self.assertRaises(ValueError):scanner.shard(objects,0,0)

class WikiTests(unittest.TestCase):
    def test_stsci_alone_is_not_hubble(self):
        response={'query':{'pages':[{'title':'File:Webb.jpg','imageinfo':[{'extmetadata':{'Credit':{'value':'NASA STScI JWST'}}}]}]}}
        with patch.object(scanner,'get_json',return_value=response):
            self.assertEqual(scanner.commons_metadata(None,['File:Webb.jpg'],0),{})
    def test_missing_metadata_is_unknown(self):
        with patch.object(scanner,'get_json',return_value={'query':{'pages':[{'title':'File:x.jpg','missing':True}]}}):
            self.assertEqual(scanner.commons_metadata(None,['File:x.jpg'],0),{})

    def test_local_file_attribution_and_normalization(self):
        response={'query':{'normalized':[{'from':'File:x.jpg','to':'File:X.jpg'}], 'pages':[
            {'title':'File:X.jpg','imageinfo':[{'extmetadata':{'Credit':{'value':'NASA / ESA Hubble'}}}]}]}}
        with patch.object(scanner,'get_json',return_value=response):
            self.assertTrue(scanner.commons_metadata(None,['File:x.jpg'],0)['File:x.jpg'])

    def test_pagination(self):
        responses=[{'query':{'pages':[{'title':'X','images':[{'title':'File:1.jpg'}]}]},'continue':{'imcontinue':'x','continue':'||'}},
                   {'query':{'pages':[{'title':'X','templates':[{'title':'Template:Infobox galaxy'}],'images':[{'title':'File:2.jpg'}]}]}}]
        with patch.object(scanner,'get_json',side_effect=responses):
            self.assertEqual(len(scanner.wiki_images(None,['X'],0)['X']),2)

    def test_title_batch_limit(self):
        def response(s,url,params,delay):
            self.assertLessEqual(len(params['titles'].split('|')),50)
            return {'query':{'pages':[{'title':t} for t in params['titles'].split('|')]}}
        with patch.object(scanner,'get_json',side_effect=response):
            self.assertEqual(len(scanner.wiki_images(None,[str(i) for i in range(105)],0)),105)

class GeometryTests(unittest.TestCase):
    def test_mast_multiple_chips_keep_gap(self):
        raw='POLYGON 9.9 20 9.91 20 9.91 20.01 9.9 20.01 POLYGON 10.1 20 10.11 20 10.11 20.01 10.1 20.01'
        geom,method=fg.parse_sregion(raw,10,20)
        from shapely.geometry import Point
        self.assertEqual(method,'MAST_multipolygon')
        self.assertFalse(geom.covers(Point(0,0)))

    def test_catalog_schemas(self):
        from catalogs import convert_builtin
        arp=convert_builtin({'Arp':1,'RAJ2000':'00 01 00','DEJ2000':'+20 00 00','Size':2},'arp')
        self.assertEqual(arp['id'],'ARP001');self.assertAlmostEqual(arp['ra_deg'],0.25,places=4)
        sh=convert_builtin({'Sh2':1,'RA1900':'00 00 00','DE1900':'+20 00 00','Diam':5},'sh2')
        self.assertEqual(sh['id'],'SH2-001');self.assertGreater(sh['ra_deg'],1)
    def test_rgb_and_uv_ir_order(self):
        group=fg.grouped_candidates(item())[0][0]
        self.assertEqual(group['filters_rgb'],['F814W','F606W','F435W'])
        self.assertEqual(fg.angular_wavelength_nm('F122M'),122)
        self.assertEqual(fg.angular_wavelength_nm('F110W'),1100)
        self.assertEqual(fg.angular_wavelength_nm('F222M'),2220)
        self.assertEqual(fg.filter_names('FQ508N'),['FQ508N'])
        self.assertEqual(fg.angular_wavelength_nm('FQ508N'),508)

    def test_quad_filter_can_join_overlapping_color_groups(self):
        x=item();x['observations']=[
            {'obsid':'fq','filter':'FQ508N','s_region':REGION,'exptime':100},
            {'obsid':'red','filter':'F814W','s_region':REGION,'exptime':100},
            {'obsid':'blue','filter':'F275W','s_region':REGION,'exptime':100}]
        groups,_=fg.grouped_candidates(x)
        self.assertTrue(any('FQ508N' in group['filters_rgb'] for group in groups))

    def test_ambiguous_multi_filter_not_three_exposures(self):
        x=item();x['observations']=[{'obsid':'1','filter':'F435W;F606W;F814W','s_region':REGION}]
        self.assertFalse(fg.grouped_candidates(x)[0])

    def test_nonfinite_and_invalid_polygon(self):
        self.assertIsNone(fg.parse_sregion('POLYGON ICRS nan 0 1 0 1 1',0,0)[0])
        self.assertIsNone(fg.parse_sregion('POLYGON ICRS 0 0 1 1 0 1 1 0',0,0)[0])

    def test_target_outside_common_area(self):
        x=item();x['ra_deg']=50
        self.assertFalse(fg.grouped_candidates(x)[0])

    def test_all_science_extensions(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[str(Path(td)/f'{f}.fits') for f in ('F435W','F606W','F814W')]
            make_fits(paths[0],'F435W',(30,10))
            make_fits(paths[1],'F606W');make_fits(paths[2],'F814W')
            result=fits_verify.verify(paths)
            self.assertEqual(len(result['files'][0]['extensions']),2)
            self.assertEqual(result['groups'][0]['hdu_rgb'],[1,1,2])
            self.assertFalse(result['groups'][0]['pixel_validity_verified'])

    def test_dq_only_and_nonoverlap(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[]
            for i,f in enumerate(('F435W','F606W','F814W')):
                path=str(Path(td)/f'{f}.fits');make_fits(path,f,(10+i,));paths.append(path)
            self.assertFalse(fits_verify.verify(paths)['groups'])
            p=Path(td)/'dq.fits';make_fits(p,'F435W',dq=True)
            self.assertFalse(fits_verify.inspect_file(p)['extensions'])

    def test_coordinate_validation(self):
        x=item();x['ra_deg']=float('nan')
        with self.assertRaises(ValueError):scanner.validate_object(x)

class ExportRankingTests(unittest.TestCase):
    def test_exact_product_export(self):
        x=item();group=fg.grouped_candidates(x)[0][0]
        def provider(obsid):
            return [{'productType':'SCIENCE','productSubGroupDescription':'DRC','obsID':obsid,
                     'productFilename':f'{obsid}_drc.fits','dataURI':f'mast:HST/product/{obsid}_drc.fits'},
                    {'productType':'SCIENCE','productSubGroupDescription':'DRC','obsID':'999',
                     'productFilename':'unrelated_drc.fits','dataURI':'mast:HST/product/unrelated_drc.fits'}]
        rows=product_rows(x,group,0,provider=provider)
        self.assertEqual(len(rows),3);self.assertEqual([r['rgb_channel'] for r in rows],list('RGB'))
        self.assertTrue(all(r['dataURI'].endswith('_drc.fits') for r in rows))

    def test_missing_product_fails_whole_group(self):
        x=item();g=fg.grouped_candidates(x)[0][0]
        with self.assertRaises(ValueError):product_rows(x,g,0,provider=lambda _:[])

    def test_diversity(self):
        np.testing.assert_allclose(diversity_scores([[1,0],[1,0],[0,1]],block=1),[0,0,1])
        np.testing.assert_equal(diversity_scores([[1,0]]),[0])
        with self.assertRaises(ValueError):diversity_scores([[0,0]])

if __name__ == '__main__':unittest.main()
