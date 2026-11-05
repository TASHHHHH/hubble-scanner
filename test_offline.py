"""Offline logic smoke tests. No requests to Wikimedia or MAST."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import scanner


class CatalogUnitTests(unittest.TestCase):
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

