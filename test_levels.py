import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from coordinator import Queue
from test_regressions import item


class LevelsTests(unittest.TestCase):
    def test_levels_claim_exclusivity_and_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=Queue(Path(tmp)/'q');q.configure_levels()
            with q.db() as c:
                for i in range(20):q.add(c,'mast:'+str(i),'mast',{'object':item()})
            self.assertEqual(q.status()['level']['investigators'],3)
            jobs=[q.claim(str(i),['mast']) for i in range(4)]
            self.assertTrue(all(jobs[:3]));self.assertIsNone(jobs[3])
            self.assertTrue(jobs[0]['payload']['investigate'])
            self.assertIsNone(q.claim('0',['mast']))
            with q.db() as c:c.execute("DELETE FROM jobs WHERE state='ready'")
            self.assertEqual(q.status()['level']['investigators'],1)
            # Existing investigations keep their leases when capacity falls.
            q.heartbeat(jobs[0]['key'],jobs[0]['token'])
            self.assertIsNone(q.claim('new',['mast']))

    def test_notification_only_coordinator_and_idempotent(self):
        from notification_service import deliver_one
        with tempfile.TemporaryDirectory() as tmp:
            q=Queue(Path(tmp)/'q');target=item()
            with q.db() as c:q.add(c,'mast:1','mast',{'object':target})
            j=q.claim('worker',['mast'])
            result={**target,'investigation':{'match':True,'status':'matched'}}
            with patch('investigator.match_pair',return_value=({'filters':['F435W','F606W'],
                    'overlap_fraction':1.0,'obsids':['100','101']},[])):
                q.finish(j['key'],j['token'],result);q.finish(j['key'],j['token'],result)
            with patch('secret_store.load',return_value={'token':'PRIVATE','chat':'123'}),patch('investigator.notify',return_value='sent') as send:
                self.assertTrue(deliver_one(q));self.assertFalse(deliver_one(q))
                send.assert_called_once()
                self.assertIn('Observation IDs:',send.call_args.args[0])
            with q.db() as c:
                self.assertEqual(c.execute('SELECT state FROM notifications').fetchone()[0],'sent')
            self.assertNotIn('PRIVATE',str(j))

    def test_permissions_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=Queue(Path(tmp)/'q')
            q.worker_token('old');q.worker_token('new','adaptive')
            self.assertEqual(q.worker_stages('old'),['wiki'])
            self.assertEqual(q.worker_stages('new'),['archive_band','archive_review','mast','wiki'])

    def test_adaptive_http_assignment_and_wiki_fallback(self):
        import requests
        from coordinator import make_server
        with tempfile.TemporaryDirectory() as tmp:
            q=Queue(Path(tmp)/'q');q.configure_levels()
            with q.db() as c:
                for i in range(10):q.add(c,'mast:'+str(i),'mast',{'object':item()})
                q.add(c,'wiki:1','wiki',{'object':item()})
            tokens=[q.worker_token(str(i),'adaptive') for i in range(3)]
            server=make_server(q,'127.0.0.1',0,'a'*32,restricted=True)
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            try:
                jobs=[]
                for token in tokens:
                    response=requests.post(f'http://127.0.0.1:{server.server_port}/claim',json={'stages':['mast','wiki']},headers={'Authorization':'Bearer '+token},timeout=3)
                    response.raise_for_status();jobs.append(response.json())
                self.assertEqual([j['stage'] for j in jobs],['mast','mast','wiki'])
                self.assertEqual(len({j['key'] for j in jobs}),3)
                self.assertTrue(jobs[0]['payload']['investigate'])
            finally:server.shutdown();server.server_close();t.join()

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_dpapi_ciphertext_roundtrip_and_tamper(self):
        from secret_store import save,load
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'secret.dpapi';save('test-private-token','123',p)
            self.assertNotIn(b'test-private-token',p.read_bytes())
            self.assertEqual(load(p)['token'],'test-private-token')
            data=bytearray(p.read_bytes());data[-1]^=1;p.write_bytes(data)
            with self.assertRaises(OSError):load(p)
