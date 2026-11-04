import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from coordinator import Queue,make_server
from fits_quality import assess,companions,flags

def target(name='NGC0001',ra=10):
    return {'id':name,'ra_deg':ra,'dec_deg':20,'major_arcmin':2,'wiki_titles':['NGC 1']}

def image(path,filt='F435W',dq=None,weight=None,err=None,science=None):
    primary=fits.PrimaryHDU();primary.header['FILTER']=filt;primary.header['INSTRUME']='ACS'
    w=WCS(naxis=2);w.wcs.crpix=[16.5,16.5];w.wcs.crval=[10,20]
    w.wcs.cdelt=[-0.001,0.001];w.wcs.ctype=['RA---TAN','DEC--TAN']
    science=np.zeros((32,32),dtype=np.float32) if science is None else science
    h=fits.ImageHDU(science,header=w.to_header(),name='SCI');h.header['EXTVER']=1
    hdus=[primary,h]
    for name,array in [('DQ',dq),('WHT',weight),('ERR',err)]:
        if array is not None:
            h=fits.ImageHDU(array,name=name);h.header['EXTVER']=1;hdus.append(h)
    fits.HDUList(hdus).writeto(path)

class QualityTests(unittest.TestCase):
    def test_unsupported_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'empty.fits';fits.PrimaryHDU().writeto(p)
            r=assess([str(p)]);self.assertTrue(r['errors']);self.assertFalse(r['planes'])

    def test_mask_shape_and_error_units_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'f.fits';image(p,err=np.ones((32,32)),weight=np.ones((4,4)))
            with fits.open(p,mode='update') as hdul:
                hdul['SCI'].header['BUNIT']='ELECTRONS';hdul['ERR'].header['BUNIT']='COUNTS'
            r=assess([str(p)]);self.assertEqual(set(r['planes'][0]['mask_issues']),{'ERR_unit_mismatch','incompatible_WHT_shape'})
            self.assertNotIn('median_sci_over_err',r['planes'][0]['metrics'])

    def test_zero_pixels_valid_and_masks_counted(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'f.fits';dq=np.zeros((32,32),dtype=np.uint16);dq[0,0]=256;dq[0,1]=4096
            data=np.zeros((32,32),dtype=np.float32);data[0,2]=np.nan
            image(p,dq=dq,science=data,err=np.ones((32,32)))
            r=assess([str(p)]);self.assertFalse(r['errors']);p=r['planes'][0]
            self.assertEqual(p['valid_pixels'],1021);self.assertEqual(p['nonfinite'],1)
            self.assertEqual(p['saturation_flagged'],1);self.assertEqual(p['cosmic_ray_flagged'],1)
            self.assertEqual(p['metrics']['median'],0)

    def test_shared_coverage_rejects_weight_gap(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[]
            for i,f in enumerate(('F435W','F606W','F814W')):
                p=Path(td)/f'{i}.fits';w=np.ones((32,32));w[:,0:24]=0 if i==0 else 1
                image(p,f,weight=w);paths.append(str(p))
            r=assess(paths,grid=32)
            self.assertFalse(r['errors']);self.assertEqual(len(r['groups']),1)
            self.assertLess(r['groups'][0]['sampled_common_valid_fraction'],0.3)
            self.assertEqual(r['groups'][0]['quality_status'],'rejected_low_common_coverage')
            self.assertFalse(r['groups'][0]['pixel_validity_verified'])

    def test_clean_common_coverage_passes(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[]
            for i,f in enumerate(('F435W','F606W','F814W')):
                p=Path(td)/f'{i}.fits';image(p,f,dq=np.zeros((32,32),dtype=np.uint16));paths.append(str(p))
            r=assess(paths,grid=32)
            self.assertFalse(r['errors']);self.assertEqual(r['groups'][0]['quality_status'],'passes_coverage_screen')
            self.assertEqual(r['groups'][0]['sampled_common_valid_fraction'],1)

    def test_missing_masks_requires_review(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[]
            for i,f in enumerate(('F435W','F606W','F814W')):
                p=Path(td)/f'{i}.fits';image(p,f);paths.append(str(p))
            r=assess(paths,grid=16)
            self.assertEqual(r['groups'][0]['quality_status'],'review_required')
            self.assertIsNone(r['planes'][0]['saturation_flagged'])

    def test_extver_and_constant_masks(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'f.fits';image(p)
            with fits.open(p,mode='update') as hdul:
                wrong=fits.ImageHDU(np.ones((32,32),dtype=np.int16),name='DQ');wrong.header['EXTVER']=2;hdul.append(wrong)
                constant=fits.ImageHDU(name='ERR');constant.header['EXTVER']=1;constant.header['PIXVALUE']=2.;hdul.append(constant)
            r=assess([str(p)]);self.assertEqual(r['planes'][0]['valid_pixels'],1024)
            self.assertEqual(r['planes'][0]['available_masks'],['ERR'])

    def test_bad_bits_override(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'f.fits';image(p,dq=np.ones((32,32),dtype=np.uint16)*4)
            self.assertEqual(assess([str(p)])['planes'][0]['valid_pixels'],0)
            self.assertEqual(assess([str(p)],bad_bits=256)['planes'][0]['valid_pixels'],1024)

    def test_unknown_instrument_flags_not_guessed(self):
        self.assertEqual(flags({'INSTRUME':'NICMOS'}),(None,None))
        self.assertEqual(flags({'INSTRUME':'WFC3','DETECTOR':'IR'}),(256,5120))

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.q=Queue(self.root/'queue.db',lease_seconds=3,wiki_slots=10)
    def tearDown(self):self.temp.cleanup()
    def test_import_previous_searches_prevents_new_claims(self):
        o=target();self.q.seed([o])
        wiki={'id':o['id'],'wiki_status':'not_detected','wiki_titles_checked':o['wiki_titles'],'checked_at_unix':time.time()}
        mast={'id':o['id'],'status':'ok','radius_arcmin':1.,'observations':[],'checked_at_unix':time.time()}
        result=self.q.import_results([o],[wiki],[mast])
        self.assertEqual(result,{'wiki':1,'mast':1});self.assertIsNone(self.q.claim('PC'))
        self.assertEqual(self.q.import_results([o],[wiki],[mast]),{'wiki':0,'mast':0})

    def test_import_mismatch_rolls_back(self):
        o=target();self.q.seed([o])
        wiki={'id':o['id'],'wiki_status':'not_detected','wiki_titles_checked':['other'],'checked_at_unix':time.time()}
        with self.assertRaises(ValueError):self.q.import_results([o],[wiki],[])
        self.assertEqual(self.q.status()['counts'][0]['state'],'ready')

    def test_quality_dedup_by_content_and_reject_changed_input(self):
        from cluster_task import execute
        a=self.root/'a.fits';b=self.root/'b.fits';image(a);b.write_bytes(a.read_bytes())
        self.q.seed_quality('a',[a]);self.q.seed_quality('b',[b]);j=self.q.claim('PC')
        self.assertIsNone(self.q.claim('other'))
        with fits.open(a,mode='update') as hdul:hdul[0].header['TEST']='changed'
        with self.assertRaises(ValueError):execute(j,self.root)

    def test_failed_jobs_exhaust_retry_budget(self):
        self.q.max_attempts=1;self.q.seed([target()]);j=self.q.claim('PC')
        self.q.finish(j['key'],j['token'],error='Service unavailable')
        self.assertIsNone(self.q.claim('PC2'));self.assertEqual(self.q.status()['attention'][0]['state'],'failed')
    def test_repeated_seed_and_alias_no_duplicate_job(self):
        from coordinator import designation
        self.assertEqual(designation('SH2-001'),designation('Sh2 1'))
        self.assertNotEqual(designation('NGC123A'),designation('NGC123'))
        self.q.seed([target(),target('NGC1')]);self.q.seed([target(),target('NGC1')])
        j=self.q.claim('PC1');self.assertIsNone(self.q.claim('PC2'))
        self.q.finish(j['key'],j['token'],{'wiki_status':'not_detected'})
        m=self.q.claim('PC2');self.assertEqual(m['stage'],'mast');self.assertIsNone(self.q.claim('PC3'))
        self.q.finish(m['key'],m['token'],{'status':'ok','observations':[]})
        r=self.q.export(self.root/'out');self.assertEqual(r['mast_completed'],2)

    def test_concurrent_claims_are_unique(self):
        objects=[]
        for i in range(40):
            o=target(f'OBJ{i}',ra=i);o['wiki_titles']=[f'Object {i}'];objects.append(o)
        self.q.seed(objects)
        def claim(i):
            result=[]
            while True:
                j=self.q.claim(f'PC{i}')
                if not j:break
                result.append(j['key']);self.q.finish(j['key'],j['token'],{'wiki_status':'no_article'})
            return result
        with ThreadPoolExecutor(10) as pool:keys=[k for group in pool.map(claim,range(10)) for k in group]
        self.assertEqual(len(keys),40);self.assertEqual(len(set(keys)),40)

    def test_expired_lease_held_and_fenced(self):
        self.q.seed([target()]);j=self.q.claim('PC1')
        with self.q.db() as c:c.execute('UPDATE jobs SET lease_until=0')
        self.assertIsNone(self.q.claim('PC2'));self.assertEqual(self.q.status()['attention'][0]['state'],'interrupted')
        with self.assertRaises(ValueError):self.q.requeue(j['key'])
        with self.assertRaises(ValueError):self.q.finish(j['key'],j['token'],{'wiki_status':'no_article'})
        self.q.requeue(j['key'],True);new=self.q.claim('PC2')
        self.assertNotEqual(new['token'],j['token'])
        with self.assertRaises(ValueError):self.q.heartbeat(j['key'],j['token'])

    def test_restart_and_idempotent_completion(self):
        self.q.seed([target()]);j=self.q.claim('PC1')
        q=Queue(self.root/'queue.db',lease_seconds=3)
        self.assertIsNone(q.claim('PC2'));q.heartbeat(j['key'],j['token'])
        q.finish(j['key'],j['token'],{'wiki_status':'no_article'})
        self.assertTrue(q.finish(j['key'],j['token'],{'wiki_status':'no_article'})['already_completed'])

    def test_config_change_rejected(self):
        self.q.seed([target()])
        with self.assertRaises(ValueError):self.q.seed([target()],max_radius=30)
        with self.assertRaises(ValueError):self.q.seed([target(ra=11)])

    def test_same_sky_search_dedup_different_wiki_pages(self):
        a=target('IC1');b=target('NGC1');b['wiki_titles']=['Different title']
        self.q.seed([a,b])
        for _ in range(2):
            j=self.q.claim('PC',['wiki']);self.q.finish(j['key'],j['token'],{'wiki_status':'not_detected'})
        self.assertEqual(sum(r['count'] for r in self.q.status()['counts'] if r['stage']=='mast'),1)

    def test_incomplete_result_retries_not_success(self):
        self.q.seed([target()]);j=self.q.claim('PC')
        self.q.finish(j['key'],j['token'],{'wiki_status':'incomplete'})
        self.assertEqual(self.q.status()['counts'][0]['state'],'ready')
        self.assertEqual(self.q.export(self.root/'out')['wiki_completed'],0)

    def test_http_auth_and_fencing(self):
        from cluster_worker import request
        import requests
        server=make_server(self.q,'127.0.0.1',0,'x'*32)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            with self.assertRaises(requests.HTTPError):request(url,'wrong','/status',{})
            self.q.seed([target()]);j=request(url,'x'*32,'/claim',{'worker':'PC'})
            request(url,'x'*32,'/heartbeat',{'key':j['key'],'token':j['token']})
            request(url,'x'*32,'/finish',{'key':j['key'],'token':j['token'],'result':{'wiki_status':'no_article'}})
            self.assertEqual(request(url,'x'*32,'/status',{})['counts'][0]['state'],'done')
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
