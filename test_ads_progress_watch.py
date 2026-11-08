import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ads_progress_watch


class AdsProgressWatchTests(unittest.TestCase):
    def test_locked_csv_uses_live_sidecar_instead_of_stopping_watcher(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'review.csv'
            target.write_text('old\n', encoding='utf-8')
            real_replace = ads_progress_watch.os.replace
            calls = 0

            def replace_with_locked_target(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError('simulated spreadsheet lock')
                return real_replace(source, destination)

            with patch.object(ads_progress_watch.os, 'replace', side_effect=replace_with_locked_target):
                written = ads_progress_watch._atomic_csv(
                    target, ('name',), ({'name': 'NGC 123'},))

            self.assertEqual(written.name, 'review.live.csv')
            self.assertEqual(written.read_text(encoding='utf-8-sig').splitlines(),
                             ['name', 'NGC 123'])
            self.assertEqual(target.read_text(encoding='utf-8'), 'old\n')


if __name__ == '__main__':
    unittest.main()
