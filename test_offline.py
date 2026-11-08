"""Offline logic smoke tests. No requests to Wikimedia or MAST."""
import tempfile
import unittest
import csv
from pathlib import Path
from unittest.mock import patch
import scanner


class CatalogUnitTests(unittest.TestCase):
    def test_spacecatalog_import_checksums_and_preserves_designations_and_license(self):
        import gzip
        import hashlib
        import io
        from catalogs import spacecatalog_rows
        tables={
            'objects':'slug,name,category,object_type,ra_deg,dec_deg,source\n'
                     'ngc-224,Andromeda Galaxy,galaxy,Spiral,10.7,41.3,openngc\n',
            'designations':'slug,designation,kind\nngc-224,NGC 224,primary\nngc-224,M 31,alias\n',
            'sources':'id,name,url,license,retrieved_at\nopenngc,OpenNGC,https://example.test,CC-BY-SA-4.0,2026-01-01\n'}
        files={}
        for name,text in tables.items():
            raw=text.encode()
            if name!='sources':
                raw=io.BytesIO()
                with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as zipped:zipped.write(text.encode())
                raw=raw.getvalue()
            files[name]=(raw,hashlib.sha256(raw).hexdigest())
        spec={name:(f'https://example.test/{name}.csv'+('.gz' if name!='sources' else ''),digest)
              for name,(raw,digest) in files.items()}
        def download(url,**_kwargs):
            from unittest.mock import Mock
            result=Mock();result.raise_for_status.return_value=None
            result.content=files[Path(url).stem.replace('.csv','')][0]
            return result
        with tempfile.TemporaryDirectory() as tmp, patch('catalogs.SPACECATALOG_FILES',spec), patch('requests.get',side_effect=download):
            rows=spacecatalog_rows(tmp)
        self.assertEqual(rows[0]['wiki_titles'],['Andromeda Galaxy','NGC 224','M 31'])
        self.assertEqual(rows[0]['source_license'],'CC-BY-SA-4.0')
        self.assertEqual(rows[0]['spacecatalog_slug'],'ngc-224')

    def test_ngc_and_messier_csv(self):
        sample = 'Name;Type;RA;Dec;MajAx;M\nNGC0224;G;00:42:44.35;+41:16:08.6;177.83;031\n'
        row = scanner.read_ngc_csv(sample)[0]
        self.assertEqual(row['Name'], 'NGC0224')
        self.assertEqual(scanner.firstnum(row['M']), 31)

    def test_sharding_is_lossless(self):
        objs = [{'id': f'NGC{i:04d}'} for i in range(200)]
        parts = [scanner.shard(objs, i, 9) for i in range(9)]
        self.assertEqual(sum(map(len, parts)), 200)
        self.assertEqual(len({o['id'] for p in parts for o in p}), 200)

    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'states.jsonl')
            scanner.write_jsonl(path, [{'id':'NGC0224','wiki_status':'not_detected'}])
            scanner.write_jsonl(path, [{'id':'NGC0001','wiki_status':'hubble_detected'}], append=True)
            self.assertEqual(len(list(scanner.read_jsonl(path))), 2)

    def test_wikipedia_page_image_detection(self):
        with patch.object(scanner, 'get_json') as fn:
            fn.return_value = {'query': {'redirects': [{'from':'Messier 31','to':'Andromeda Galaxy'}],
                                         'pages': [{'title':'Andromeda Galaxy', 'templates':[{'title':'Template:Infobox galaxy'}], 'images': [{'title':'File:Andromeda galaxy Hubble.jpg'}]}]}}
            found = scanner.wiki_images(None, ['Messier 31'], 0)
            self.assertIn('File:Andromeda galaxy Hubble.jpg', found['Messier 31'])


class UpgradeTests(unittest.TestCase):
    def test_empty_archive_export_refuses_to_overwrite_previous_scan(self):
        from archive_sweep import scan
        from runtime import write_rows
        target={'id':'NGC9999','ra_deg':10,'dec_deg':20,'major_arcmin':1}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);observations=root/'archive_observations.jsonl'
            write_rows(observations,[])
            out=root/'spacecatalog_hst.jsonl';out.write_text('previous valid scan\n',encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'refusing to overwrite'):
                scan([target],out,root/'cache',observations)
            self.assertEqual(out.read_text(encoding='utf-8'),'previous valid scan\n')

    def test_mast_crossmatch_uses_footprints_and_program_bibliography(self):
        from archive_sweep import scan
        target={'id':'NGC9999','ra_deg':10,'dec_deg':20,'major_arcmin':1,'source':'fixture',
                'wiki_titles':['NGC 9999']}
        observation={'target_name':'NGC9999','obsid':'42','obs_id':'hst42','s_ra':'10','s_dec':'20',
                     'proposal_id':'10152','filters':'F606W;F814W',
                     's_region':'POLYGON ICRS 9.9 19.9 10.1 19.9 10.1 20.1 9.9 20.1',
                     'instrument_name':'ACS/WFC','t_exptime':'100'}
        papers=[{'bibcode':'2025ApJ...123..456A','title':'NGC 9999: a test result',
                 'publication_date':'2025-01-01','journal':'ApJ','refereed':True,
                 'paper_type':'SCIENCE','programs':['10152']}]
        with tempfile.TemporaryDirectory() as tmp, patch('archive_sweep.fetch_archive',return_value=[observation]), \
             patch('archive_sweep.fetch_papers',return_value=papers):
            rows=scan([target],Path(tmp)/'results.jsonl',Path(tmp)/'cache')
            papers_csv=Path(tmp)/'results.papers.csv'
            with papers_csv.open(encoding='utf-8-sig',newline='') as stream:
                paper_rows=list(csv.DictReader(stream))
            target_papers_csv=Path(tmp)/'results.target_papers.csv'
            with target_papers_csv.open(encoding='utf-8-sig',newline='') as stream:
                target_papers=list(csv.DictReader(stream))
            review_csv=Path(tmp)/'results.review.csv'
            with review_csv.open(encoding='utf-8-sig',newline='') as stream:
                review_rows=list(csv.DictReader(stream))
            observations_csv=Path(tmp)/'results.observations.csv'
            with observations_csv.open(encoding='utf-8-sig',newline='') as stream:
                observation_rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['mast_filters'],['F606W','F814W'])
        self.assertEqual(rows[0]['mast_target_names'],['NGC9999'])
        self.assertEqual(rows[0]['publication_status'],'program_has_published_science_paper')
        self.assertTrue(rows[0]['object_name_in_paper_title'])
        self.assertEqual(rows[0]['program_bibcodes'],['2025ApJ...123..456A'])
        self.assertEqual(len(paper_rows),1)
        self.assertEqual(paper_rows[0]['bibcode'],'2025ApJ...123..456A')
        self.assertIn('ui.adsabs.harvard.edu',paper_rows[0]['ads_url'])
        self.assertEqual(len(target_papers),1)
        self.assertEqual(target_papers[0]['object_id'],'NGC9999')
        self.assertEqual(len(review_rows),1)
        self.assertEqual(review_rows[0]['mast_program_ids'],'["10152"]')
        self.assertEqual(review_rows[0]['mast_target_names'],'["NGC9999"]')
        self.assertEqual(len(observation_rows),1)
        self.assertEqual(observation_rows[0]['obsid'],'42')
        self.assertEqual(observation_rows[0]['target_name'],'NGC9999')

    def test_all_sky_crossmatch_keeps_zero_ra_and_declination(self):
        from archive_sweep import scan
        target={'id':'SC-zero','ra_deg':0,'dec_deg':0,'major_arcmin':1,
                'wiki_titles':['Zero Coordinate Object']}
        observation={'target_name':'Zero Coordinate Object','obsid':'zero','s_ra':'0','s_dec':'0',
                     'proposal_id':'10152','filters':'F606W',
                     's_region':'POLYGON ICRS 359.9 -0.1 0.1 -0.1 0.1 0.1 359.9 0.1'}
        with tempfile.TemporaryDirectory() as tmp, patch('archive_sweep.fetch_archive',return_value=[observation]), \
             patch('archive_sweep.fetch_papers',return_value=[]):
            rows=scan([target],Path(tmp)/'results.jsonl',Path(tmp)/'cache')
        self.assertEqual(rows[0]['mast_observation_count'],1)

    def test_papertrack_fetch_uses_program_filter_and_persistent_cache(self):
        from archive_sweep import fetch_papers
        from unittest.mock import Mock
        paper={'bibcode':'2025ApJ...123..456A','title':'Example','programs':['10152'],
               'paper_type':'SCIENCE','refereed':True}
        session=Mock()
        session.headers={}
        response=Mock();response.raise_for_status.return_value=None;response.json.return_value=[paper]
        session.post.return_value=response
        with tempfile.TemporaryDirectory() as tmp:
            first=fetch_papers(['10152'],tmp,session)
            second=fetch_papers(['10152'],tmp,session)
        self.assertEqual(first,second)
        self.assertEqual(session.post.call_count,1)
        body=session.post.call_args.kwargs['json']
        self.assertEqual(body['mission'],'HST')
        self.assertEqual(body['hst_programs'],'10152')
        self.assertEqual(body['papertypes'],['SCIENCE'])

    def test_no_program_paper_is_reported_as_a_limited_search_result(self):
        from archive_sweep import scan
        target={'id':'NGC9999','ra_deg':10,'dec_deg':20,'major_arcmin':1}
        observation={'obsid':'42','proposal_id':'10152','s_ra':'10','s_dec':'20','filters':'F606W',
            's_region':'POLYGON ICRS 9.9 19.9 10.1 19.9 10.1 20.1 9.9 20.1'}
        with tempfile.TemporaryDirectory() as tmp, patch('archive_sweep.fetch_archive',return_value=[observation]), \
             patch('archive_sweep.fetch_papers',return_value=[]):
            row=scan([target],Path(tmp)/'result.jsonl',Path(tmp)/'cache')[0]
        self.assertEqual(row['publication_status'],'no_published_science_paper_found_for_programs')
        self.assertFalse(row['object_name_in_paper_title'])

    def test_wide_catalog_joins_canonical_ngc_and_keeps_herbig_haro_aliases(self):
        import csv
        from catalogs import add_wide_catalogs
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot.csv'
            with path.open('w',encoding='utf-8',newline='') as f:
                writer=csv.writer(f);writer.writerow(('family','number','oid','main_id','ra_deg','dec_deg'))
                writer.writerow(('HH','1','1','HH 1','84.08','-6.75'))
                writer.writerow(('HH','1A','1','HH 1','84.08','-6.75'))
                writer.writerow(('Arp','1','2','NGC 224','10','20'))
            base={'id':'NGC0224','ra_deg':10,'dec_deg':20,'wiki_titles':['NGC 224'],'aliases':[]}
            rows=[base]
            add_wide_catalogs(rows,path)
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['wiki_titles'],['NGC 224','Arp 1'])
            self.assertIn('Herbig-Haro 1A',rows[1]['wiki_titles'])
            self.assertEqual(rows[1]['simbad_oid'],'1')

    def test_disambiguation_page_cannot_supply_image_evidence(self):
        pages = [{'title':'H21', 'pageprops':{'disambiguation':''},
                  'images':[{'title':'File:Hubble.jpg'}]},
                 {'title':'HMS H21', 'templates':[{'title':'Template:Infobox ship'}],
                  'images':[{'title':'File:Hubble.jpg'}]},
                 {'title':'Double Cluster', 'templates':[{'title':'Template:Infobox open cluster'}], 'images':[]}]
        with patch.object(scanner, 'get_json', return_value={'query':{'pages':pages}}) as api:
            found = scanner.wiki_images(None, ['H21', 'HMS H21', 'Double Cluster'], 0)
        self.assertEqual(api.call_args.args[2]['ppprop'], 'disambiguation')
        self.assertIsNone(found['H21'])
        self.assertIsNone(found['HMS H21'])
        self.assertEqual(found['Double Cluster'], [])

    def test_current_openngc_csv_comma_delimiter(self):
        sample='Name,Type,RA,Dec,MajAx,M\nIC0001,G,00:08:27.0,+27:43:03,1.2,\n'
        self.assertEqual(scanner.read_ngc_csv(sample)[0]['Name'],'IC0001')

    def test_polygon_overlap_and_filter_order(self):
        import footprint_groups as fg
        footprint='POLYGON ICRS 10 20 10.1 20 10.1 20.1 10 20.1'
        item={'id':'NGC9999','ra_deg':10.05,'dec_deg':20.05,'observations':[
            {'obsid':'1','filter':'F814W','s_region':footprint},
            {'obsid':'2','filter':'F435W','s_region':footprint},
            {'obsid':'3','filter':'F606W','s_region':footprint}]}
        groups,errors=fg.grouped_candidates(item)
        self.assertEqual(groups[0]['filters_rgb'],['F814W','F606W','F435W'])
        self.assertGreater(groups[0]['common_fraction_of_smallest'],0.95)
        self.assertFalse(errors)

    def test_nonoverlap_rejected(self):
        import footprint_groups as fg
        regions=['POLYGON ICRS 10 20 10.01 20 10.01 20.01 10 20.01',
                 'POLYGON ICRS 11 20 11.01 20 11.01 20.01 11 20.01',
                 'POLYGON ICRS 12 20 12.01 20 12.01 20.01 12 20.01']
        item={'id':'IC0999','ra_deg':10,'dec_deg':20,'observations':[
           {'obsid':str(i),'filter':f,'s_region':s}
           for i,(f,s) in enumerate(zip(['F435W','F606W','F814W'],regions))]}
        groups,_=fg.grouped_candidates(item)
        self.assertEqual(groups,[])

    def test_wraparound_projection(self):
        import footprint_groups as fg
        x,y=fg.project_gnomonic(0.01,20,359.99,20)
        self.assertLess(abs(x),2)

    def test_unrecognized_stcs_fails_closed(self):
        import footprint_groups as fg
        geom,status=fg.parse_sregion('UNION ICRS (POLYGON ICRS 10 20 11 20 11 21)',10,20)
        self.assertIsNone(geom)
        self.assertEqual(status,'unsupported_stcs')

if __name__ == '__main__':
    unittest.main()

