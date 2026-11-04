import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
from coordinator import Queue,make_server
from investigator import match_pair,investigate
from test_regressions import item


class InvestigationTests(unittest.TestCase):
    def test_two_bands_and_center_required(self):
        target=item();target['observations']=target['observations'][:2]
        self.assertEqual(len(match_pair(target)[0]['filters']),2)
        target['observations'][1]['filter']='F435W'
        self.assertIsNone(match_pair(target)[0])
        target=item();target['ra_deg']=10.02
        self.assertIsNone(match_pair(target)[0])

    def test_missing_preview_notifies_once_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp, patch('investigator.download_preview',return_value=None) as download, patch('investigator.notify',return_value='sent') as send:
            first=investigate(item(),tmp);second=investigate(item(),tmp)
            self.assertEqual(first,second)
            self.assertEqual(first['status'],'matched_no_preview')
            download.assert_called_once();send.assert_not_called()
            self.assertEqual(first['notification'],'coordinator_pending')

    def test_remote_token_cannot_claim_mast_or_see_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=Queue(Path(tmp)/'q.sqlite',wiki_slots=10,mast_slots=1)
            target=item();target['wiki_titles']=['Synthetic'];q.seed([target])
            token=q.worker_token('pc1');other=q.worker_token('pc2')
            server=make_server(q,'127.0.0.1',0,'a'*32,restricted=True)
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            url=f'http://127.0.0.1:{server.server_port}'
            def post(path,data,secret=token):
                return requests.post(url+path,json=data,headers={'Authorization':'Bearer '+secret},timeout=3)
            try:
                job=post('/claim',{'worker':'fake','stages':['mast']}).json()
                self.assertEqual(job['stage'],'wiki')
                payload={'key':job['key'],'token':job['token'],'result':{'wiki_status':'not_detected'}}
                self.assertEqual(post('/finish',payload,other).status_code,409)
                self.assertEqual(post('/finish',payload).status_code,200)
                self.assertEqual(post('/candidates',{}).status_code,409)
                self.assertIsNone(post('/claim',{}).json())
                self.assertEqual(len(q.candidates()),1)
                q.revoke_worker('pc1')
                self.assertEqual(post('/status',{}).status_code,401)
                self.assertEqual(q.claim('trusted',['mast'])['stage'],'mast')
                self.assertIsNone(q.claim('second',['mast']))
            finally:server.shutdown();server.server_close();t.join()
