import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from runtime import read_rows,write_rows
from literature_search import make_query,names_for,run,search_ads


class LiteratureSearchTests(unittest.TestCase):
    def test_query_uses_ads_api_supported_full_text_field(self):
        query=make_query(['NGC 224','Andromeda Galaxy'])
        self.assertIn('database:astronomy',query)
        self.assertIn('full:("NGC 224" OR "Andromeda Galaxy")',query)
        self.assertNotIn('object:',query)

    def test_search_names_exclude_catalog_source_and_internal_id(self):
        self.assertEqual(names_for({'wiki_titles':['HIP 23692'],'aliases':['HD 123'],
            'source_name':'HYG Database v4.1','id':'SC-hip-23692'}),['HIP 23692','HD 123'])

    def test_zero_archive_matches_fails_instead_of_reporting_zero_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.jsonl';out=Path(tmp)/'out.jsonl'
            write_rows(source,[{'id':'NGC0001','mast_observation_count':0}])
            out.write_text('previous checkpoint\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'No archive-matched objects'):
                run(source,out,'secret',Path(tmp)/'cache',delay=0)
            self.assertEqual(out.read_text(encoding='utf-8'),'previous checkpoint\n')

    def test_api_result_is_cached_and_normalized_to_paper_details(self):
        session=Mock();response=Mock();response.status_code=200;response.raise_for_status.return_value=None
        response.json.return_value={'response':{'numFound':1,'docs':[{
            'bibcode':'2025ApJ...123..456A','title':['A title'],'author':['A. Author'],
            'pubdate':'2025-01','pub':'ApJ','doi':['10.1234/example'],
            'abstract':['A short abstract'],'property':['refereed']}]}}
        session.get.return_value=response
        with tempfile.TemporaryDirectory() as tmp:
            result=search_ads('database:astronomy AND full:"NGC 224"','secret',tmp,session)
            again=search_ads('database:astronomy AND full:"NGC 224"','secret',tmp,session)
        self.assertEqual(result,again)
        self.assertEqual(session.get.call_count,1)
        self.assertEqual(result['docs'][0]['bibcode'],'2025ApJ...123..456A')

    def test_bad_query_reports_ads_error_detail_without_retrying(self):
        session=Mock();response=Mock();response.status_code=400
        response.json.return_value={'error':'Syntax error near field group'}
        session.get.return_value=response
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError,'Syntax error near field group'):
            search_ads('bad query','secret',tmp,session)
        self.assertEqual(session.get.call_count,1)

    def test_rate_limit_waits_until_ads_reset_and_retries_same_target(self):
        session=Mock();limited=Mock();limited.status_code=429
        limited.headers={'X-RateLimit-Reset':'1005'}
        success=Mock();success.status_code=200
        success.json.return_value={'response':{'numFound':0,'docs':[]}}
        session.get.side_effect=[limited,success]
        with tempfile.TemporaryDirectory() as tmp, patch('literature_search.time.time',return_value=1000), \
                patch('literature_search.time.sleep') as sleep:
            result=search_ads('database:astronomy AND full:"HIP 23692"','secret',tmp,session)
        self.assertEqual(result['docs'],[])
        self.assertEqual(session.get.call_count,2)
        sleep.assert_called_once_with(7.0)

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

    def test_run_sends_telegram_progress_each_ten_and_lists_cautious_leads(self):
        session=Mock();calls={'n':0}
        def get(_url,**kwargs):
            calls['n']+=1;response=Mock();response.status_code=200
            response.json.return_value={'response':{'numFound':1,'docs':[{
                'bibcode':f'2025ApJ...123..45{calls["n"]:01d}A','title':['Paper'],
                'author':['A. Author'],'pubdate':'2025-01','pub':'ApJ'}]}}
            if calls['n']%2==0:response.json.return_value={'response':{'numFound':0,'docs':[]}}
            return response
        session.get.side_effect=get
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.jsonl';out=Path(tmp)/'out.jsonl';messages=[]
            write_rows(source,[{'id':f'NGC{i:04d}','wiki_titles':[f'NGC {i}'],
                'mast_observation_count':1,
                'publication_status':'no_published_science_paper_found_for_programs'}
                for i in range(1,13)])
            run(source,out,'secret',Path(tmp)/'cache',delay=0,session=session,notifier=messages.append)
        self.assertEqual(len(messages),2)
        self.assertIn('10/12 objects checked',messages[0])
        self.assertIn('paper matches: 5',messages[0])
        self.assertIn('no name match: 5',messages[0])
        self.assertIn('Potential manual-review leads',messages[0])
        self.assertIn('not confirmed unpublished objects or discoveries',messages[0])
        self.assertIn('12/12 objects checked',messages[1])


if __name__=='__main__':unittest.main()
