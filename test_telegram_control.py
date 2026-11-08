import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from coordinator import Queue
from telegram_control import _authorized,handle_command,poll


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.q=Queue(Path(self.temp.name)/'queue.sqlite')

    def tearDown(self):self.temp.cleanup()

    def test_remote_pause_resume_limit_and_archive_search(self):
        with self.q.db() as c:self.q.add(c,'wiki:1','wiki',{'object':{'id':'NGC 1'}})
        self.assertIn('Paused new claims',handle_command(self.q,{},'/pause wiki'))
        self.assertIsNone(self.q.claim('pc',['wiki']))
        self.assertIn('resumed',handle_command(self.q,{},'/resume wiki'))
        self.assertIsNotNone(self.q.claim('pc',['wiki']))
        worker_status=self.q.status()['workers']
        self.assertEqual(worker_status[0]['state'],'working')
        self.assertEqual(worker_status[0]['current_stage'],'wiki')
        self.assertIn('limit set to 2',handle_command(self.q,{},'/limit 2'))
        with self.q.db() as c:self.assertEqual(self.q.level(c)['maximum'],2)
        self.assertIn('72 five-degree',handle_command(self.q,{},'/search'))
        self.assertEqual(sum(x['count'] for x in self.q.status()['counts'] if x['stage']=='archive_band'),72)

    def test_candidate_and_obsid_commands_show_hst_evidence(self):
        obj={'id':'NGC1','ra_deg':10,'dec_deg':20,'major_arcmin':2,'wiki_titles':['NGC 1']}
        self.q.seed([obj])
        wiki=self.q.claim('wiki',['wiki'])
        self.q.finish(wiki['key'],wiki['token'],{'wiki_status':'not_detected'})
        mast=self.q.claim('mast-pc',['mast'])
        result={'status':'ok','observation_count':1,'filters':['F606W'],'observations':[
            {'obsid':'12345','obs_id':'hst_12345','filter':'F606W','instrument':'ACS/WFC','exptime':120.0}]}
        self.q.finish(mast['key'],mast['token'],result)
        details=handle_command(self.q,{},'/candidate NGC1')
        self.assertIn('obsid 12345',details);self.assertIn('ACS/WFC',details)
        self.assertIn('belongs to NGC1',handle_command(self.q,{},'/obsid hst_12345'))
        self.assertIn('12345',handle_command(self.q,{},'/recent'))

    def test_nomatches_sends_every_name_in_telegram_sized_chunks(self):
        import json
        out=Path(self.temp.name)/'results';out.mkdir()
        path=out/'spacecatalog_hst.literature.jsonl'
        rows=[{'id':f'SC-{i:04d}','search_names':[f'Object {i:04d}'],
               'literature_status':'no_name_match'} for i in range(240)]
        rows += [{'id':'known','search_names':['Known object'],'literature_status':'papers_found'}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        sent=[]
        self.assertIsNone(handle_command(self.q,{},'/nomatches',reply=sent.append))
        combined='\n'.join(sent)
        self.assertEqual(len(sent),2)
        self.assertIn('Objects with no ADS name match: 240',sent[0])
        for i in range(240):self.assertIn(f'Object {i:04d} (SC-{i:04d})',combined)
        self.assertNotIn('Known object',combined)
        self.assertTrue(all(len(message)<=3900 for message in sent))
        self.assertIn('Missing aliases can cause false misses',sent[-1])

    def test_nonmatchesall_prefers_catalog_name_and_includes_searched_aliases(self):
        import json
        out=Path(self.temp.name)/'results';out.mkdir()
        path=out/'spacecatalog_hst.literature.jsonl'
        row={'id':'SC-target','wiki_titles':['NGC 123'],'aliases':['Other alias'],
             'mast_target_names':['SN 2024abc'],'search_names':['Other alias','SN 2024abc'],
             'literature_status':'no_name_match'}
        path.write_text(json.dumps(row)+'\n',encoding='utf-8')
        sent=[]
        self.assertIsNone(handle_command(self.q,{},'/nonmatchesall',reply=sent.append))
        self.assertEqual(len(sent),1)
        self.assertIn('NGC 123 (SC-target)',sent[0])
        self.assertIn('ADS terms: SN 2024abc, Other alias',sent[0])

    def test_only_configured_private_chat_can_control(self):
        credentials={'chat':'8100371339'}
        allowed={'chat':{'id':8100371339,'type':'private'},'from':{'id':8100371339}}
        group={'chat':{'id':-1001,'type':'supergroup'},'from':{'id':8100371339}}
        other={'chat':{'id':8100371339,'type':'private'},'from':{'id':999}}
        self.assertTrue(_authorized(allowed,credentials))
        self.assertFalse(_authorized(group,credentials));self.assertFalse(_authorized(other,credentials))

    def test_poll_replies_only_to_authorized_sender(self):
        stop=threading.Event();credentials={'token':'secret','chat':'8100371339'}
        class Response:
            status_code=200
            def __init__(self,result):self.result=result
            def json(self):return {'ok':True,'result':self.result}
            def raise_for_status(self):pass
        session=Mock()
        session.get.side_effect=[Response([]),Response([
            {'update_id':1,'message':{'chat':{'id':9,'type':'private'},'from':{'id':9},'text':'/status'}},
            {'update_id':2,'message':{'chat':{'id':8100371339,'type':'private'},'from':{'id':8100371339},'text':'/status'}}])]
        client=Mock();client.trust_env=False;client.get=session.get
        def send(_message,_credentials):stop.set();return 'sent'
        with patch('requests.Session',return_value=client),patch('secret_store.load',return_value=credentials),patch('investigator.notify',side_effect=send) as notify:
            poll(self.q,stop)
        notify.assert_called_once()

    def test_archive_sweep_results_are_searchable_by_target_and_obsid(self):
        import csv
        from runtime import write_rows
        root=Path(self.temp.name);out=root/'results';out.mkdir()
        write_rows(out/'spacecatalog_hst.jsonl',[{'id':'SC-ic-1','ra_deg':12.5,'dec_deg':-3.0,
            'object_type':'nebula','mast_observation_count':1,'mast_filters':['F606W'],
            'mast_program_ids':['123'],'program_bibcodes':['2025A&A...1A..1A'],
            'publication_status':'program_has_published_science_paper','mast_search_url':'https://example.test'}])
        with (out/'spacecatalog_hst.observations.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['object_id','obsid','obs_id','filters','instrument_name','t_exptime'])
            writer.writeheader();writer.writerow({'object_id':'SC-ic-1','obsid':'HST123','obs_id':'hst_123',
                'filters':'F606W','instrument_name':'ACS/WFC','t_exptime':'120'})
        details=handle_command(self.q,{},'/candidate SC-ic-1')
        self.assertIn('obsid HST123',details);self.assertIn('program has published science paper',details)
        self.assertIn('SC-ic-1',handle_command(self.q,{},'/obsid HST123'))


if __name__=='__main__':unittest.main()
