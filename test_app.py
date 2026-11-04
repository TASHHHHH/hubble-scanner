import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from app import Runner, App, command


def wait(runner):
    deadline = time.monotonic() + 30
    events = []
    while time.monotonic() < deadline:
        try:
            event = runner.events.get(timeout=.1); events.append(event)
            if event[0] == 'done': return events
        except queue.Empty: pass
    runner.stop()
    raise AssertionError('Task timed out')


class RunnerTests(unittest.TestCase):
    def test_failure_does_not_run_next_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner()
            runner.start([[sys.executable, '-c', 'raise SystemExit(7)'],
                          [sys.executable, '-c', 'print("SHOULD_NOT_RUN")']], tmp)
            events = wait(runner)
            self.assertEqual(events[-1], ('done', 7))
            self.assertFalse(any('SHOULD_NOT_RUN' in str(e) for e in events))

    def test_cancel_stops_process_and_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner()
            runner.start([[sys.executable, '-c', 'import time;time.sleep(60)']], tmp)
            deadline = time.monotonic() + 5
            while runner.process is None and time.monotonic() < deadline: time.sleep(.01)
            process = runner.process
            runner.stop()
            self.assertEqual(wait(runner)[-1], ('done', 130))
            self.assertIsNotNone(process.poll())

    def test_subprocess_handles_spaces_and_output(self):
        with tempfile.TemporaryDirectory(prefix='hubble space ') as tmp:
            runner = Runner()
            runner.start([[sys.executable, '-c', 'import sys;print(sys.argv[1])', 'two words']], tmp)
            events = wait(runner)
            self.assertIn(('log', 'two words\n'), events)
            self.assertEqual(events[-1], ('done', 0))


@unittest.skipUnless(os.name == 'nt', 'Windows desktop smoke')
class DesktopTests(unittest.TestCase):
    def test_demo_load_filter_and_quality_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App(tmp); app.withdraw()
            try:
                app.demo()
                deadline = time.monotonic() + 45
                while app.runner.busy and time.monotonic() < deadline:
                    app.update(); time.sleep(.02)
                app.poll(); app.update()
                path = Path(tmp) / 'demo' / 'candidates.jsonl'
                self.assertTrue(path.exists())
                with patch('app.filedialog.askopenfilename', return_value=str(path)):
                    app.load_results()
                self.assertEqual(len(app.table.get_children()), 1)
                app.search.set('no_such_target'); self.assertEqual(len(app.table.get_children()), 0)
                app.search.set(''); self.assertEqual(len(app.table.get_children()), 1)
                with patch('app.messagebox.showinfo') as info:
                    app.tsdp(); info.assert_called_once()
                fits = list((Path(tmp) / 'demo').glob('*.fits'))
                app.run([command('fits_quality', *fits, '--out', Path(tmp) / 'fits_quality.json')])
                deadline = time.monotonic() + 45
                while app.runner.busy and time.monotonic() < deadline:
                    app.update(); time.sleep(.02)
                app.poll(); app.update()
                self.assertTrue((Path(tmp) / 'fits_quality.json').exists())
                self.assertIn('fits-quality-1', app.quality_text.get('1.0', 'end'))
            finally:
                app.runner.stop(); app.destroy()
