import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from runtime import read_rows,write_rows
from literature_search import make_query,run,search_ads


class LiteratureSearchTests(unittest.TestCase):
    def test_query_uses_ads_object_tags_and_exact_text_aliases(self):
        query=make_query(['NGC 224','Andromeda Galaxy'])
        self.assertIn('database:astronomy',query)
        self.assertIn('object:"NGC 224"',query)
        self.assertIn('full:"Andromeda Galaxy"',query)

    def test_api_result_is_cached_and_normalized_to_paper_details(self):
        session=Mock();response=Mock();response.status_code=200;response.raise_for_status.return_value=None
        response.json.return_value={'response':{'numFound':1,'docs':[{
            'bibcode':'2025ApJ...123..456A','title':['A title'],'author':['A. Author'],
            'pubdate':'2025-01','pub':'ApJ','doi':['10.1234/example'],
            'abstract':['A short abstract'],'property':['refereed']}]}}
        session.get.return_value=response
        with tempfile.TemporaryDirectory() as tmp:
            result=search_ads('database:astronomy AND object:"NGC 224"','secret',tmp,session)
            again=search_ads('database:astronomy AND object:"NGC 224"','secret',tmp,session)
        self.assertEqual(result,again)
        self.assertEqual(session.get.call_count,1)
        self.assertEqual(result['docs'][0]['bibcode'],'2025ApJ...123..456A')

    def test_run_distinguishes_papers_no_match_and_lookup_failure(self):
        session=Mock()
        def get(_url,**kwargs):
            query=kwargs['params']['q']
            response=Mock()
            if 'NGC 1' in query:
                response.status_code=200
                response.raise_for_status.return_value=None
                response.json.return_value={'response':{'numFound':1,'docs':[{
                    'bibcode':'2025ApJ...123..456A','title':['NGC 1 study'],
                    'author':['A. Author'],'pubdate':'2025-01','pub':'ApJ'}]}}
            elif 'NGC 2' in query:
                response.status_code=200
                response.raise_for_status.return_value=None
                response.json.return_value={'response':{'numFound':0,'docs':[]}}
            else:
                response.status_code=503;response.text='service temporarily unavailable'
                response.raise_for_status.side_effect=RuntimeError('network down')
            return response
        session.get.side_effect=get
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.jsonl';out=Path(tmp)/'out.jsonl'
            write_rows(source,[
                {'id':'NGC0001','wiki_titles':['NGC 1'],'mast_observation_count':2},
                {'id':'NGC0002','wiki_titles':['NGC 2'],'mast_observation_count':1},
                {'id':'NGC0003','wiki_titles':['NGC 3'],'mast_observation_count':1},
                {'id':'NGC0004','wiki_titles':['NGC 4'],'mast_observation_count':0}])
            counts=run(source,out,'secret',Path(tmp)/'cache',delay=0,session=session)
            rows={r['id']:r for r in read_rows(out)}
            self.assertTrue((Path(tmp)/'out.papers.csv').exists())
        self.assertEqual(counts,{'papers_found':1,'no_name_match':1,'lookup_failed':1})
        self.assertEqual(rows['NGC0001']['literature_status'],'papers_found')
        self.assertEqual(rows['NGC0002']['literature_status'],'no_name_match')
        self.assertEqual(rows['NGC0003']['literature_status'],'lookup_failed')
        self.assertNotIn('NGC0004',rows)


if __name__=='__main__':unittest.main()
