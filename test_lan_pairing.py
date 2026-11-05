import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

import requests

from cluster_worker import request
from coordinator import Queue
from lan_pairing import read_invite, start_listener
from runtime import atomic_json


class InviteTests(unittest.TestCase):
    def test_worker_uses_invited_certificate_and_revocable_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            queue = Queue(workspace / 'queue.sqlite')
            server, certificate = start_listener(queue, '127.0.0.1', 0, secrets.token_urlsafe())
            try:
                invite = workspace / 'pc.hubble-invite'
                atomic_json(invite, {'format': 'hubble-lan-1', 'address': '127.0.0.1',
                                     'certificate': certificate, 'token': queue.worker_token('pc', 'adaptive')})
                url, token, ca = read_invite(invite, workspace)
                self.assertEqual(url, 'https://127.0.0.1:8766')
                url = f'https://127.0.0.1:{server.server_port}'
                with patch.dict(os.environ, HUBBLE_CA_BUNDLE=str(ca)):
                    self.assertIn('counts', request(url, token, '/status', {}))
                    queue.revoke_worker('pc')
                    with self.assertRaises(requests.HTTPError) as rejected:
                        request(url, token, '/status', {})
                    self.assertEqual(rejected.exception.response.status_code, 401)
            finally:
                server.shutdown()
                server.server_close()
