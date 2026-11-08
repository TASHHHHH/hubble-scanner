import argparse
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alias_resolution import (QueryCache, component, unique, normalized, resolve, query_spellings,
                              load_input, run, simbad_candidates)


def candidate(name='PGC 123', source='SIMBAD', ra=10, dec=20, aliases=None):
    return dict(canonical_designation=name, source=source, ra_deg=ra, dec_deg=dec,
                object_type='G', query_name=name, aliases=aliases or [name])


class AliasTests(unittest.TestCase):
    def test_components_are_distinct(self):
        self.assertNotEqual(normalized('IC2184 NED01'), normalized('IC2184 NED02'))
        self.assertNotEqual(normalized('Gl 280B'), normalized('Gl 280'))
        self.assertNotEqual(normalized('KMT-2019-BLG-0253L'), normalized('KMT-2019-BLG-0253L b'))
        self.assertEqual(component('KMT-2019-BLG-0253L b')['kind'], 'planet')
        self.assertEqual(component('GJ 4021B')['label'], 'B')

    def test_spelling_and_duplicate_identifiers(self):
        self.assertEqual(len(unique(['NGC0520', 'NGC 520', 'NGC 520 NED01'])), 2)
        self.assertEqual(len(unique(['Gl 280B', 'GJ 280B'])), 2)
        self.assertIn('GJ 4021 B', query_spellings('GJ 4021B'))
        self.assertNotIn('GJ 4021', query_spellings('GJ 4021B'))

    def test_missing_and_service_errors_differ(self):
        row = dict(input_row=1, object_name='PGC 123')
        self.assertEqual(resolve(row, [{'status': 'ok'}], [])['resolution_status'], 'not_found')
        self.assertEqual(resolve(row, [{'status': 'service_error'}], [])['resolution_status'], 'service_error')

    def test_parent_cannot_replace_component(self):
        row = dict(input_row=1, object_name='IC2184 NED01')
        result = resolve(row, [], [candidate('IC 2184', 'NED', aliases=['IC2184 NED01', 'IC 2184'])])
        self.assertEqual(result['resolution_status'], 'ambiguous')
        self.assertEqual(result['confirmed_aliases'], [])

    def test_component_canonical_accepted(self):
        result = resolve(dict(input_row=1, object_name='IC2184 NED01'), [],
                         [candidate('IC 2184 NED01', 'NED')])
        self.assertEqual(result['resolution_status'], 'confirmed')

    def test_coordinates_veto_but_never_establish_identity(self):
        row = dict(input_row=1, object_name='PGC 123', ra_deg=10, dec_deg=20)
        result = resolve(row, [], [candidate(ra=11)])
        self.assertEqual(result['resolution_status'], 'ambiguous')
        self.assertEqual(result['confirmed_aliases'], [])
        result = resolve(row, [], [candidate(), candidate('PGC 124')])
        self.assertEqual(result['resolution_status'], 'ambiguous')

    def test_exact_response_mapping_not_position(self):
        evidence = {'status': 'ok', 'data': {'records': [
            {'user_specified_id': 'X', 'main_id': 'Y', 'ids': 'Y|Z', 'ra': 0, 'dec': 0},
            {'user_specified_id': 'missing', 'main_id': None}]}}
        self.assertEqual(simbad_candidates(evidence, 'missing'), [])
        self.assertEqual(simbad_candidates(evidence, 'X')[0]['aliases'], ['Y', 'Z'])

    @patch('alias_resolution.time.sleep')
    def test_retry_and_durable_cache(self, sleep):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QueryCache(tmp, retries=2, delay=0)
            calls = []
            def fetch():
                calls.append(1)
                if len(calls) == 1: raise TimeoutError('temporary')
                return {'records': []}
            result = cache.call('fixture', {'name': 'x'}, fetch)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(len(result['attempts']), 2)
            self.assertTrue(cache.call('fixture', {'name': 'x'}, fetch)['cache_hit'])
            self.assertEqual(len(calls), 2)

    @patch('alias_resolution.time.sleep')
    def test_error_cache_retried(self, sleep):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QueryCache(tmp, retries=1, delay=0)
            def fail(): raise TimeoutError('temporary')
            self.assertEqual(cache.call('fixture', {}, fail)['status'], 'service_error')
            self.assertEqual(cache.call('fixture', {}, lambda: [])['status'], 'ok')

    def test_rows_order_duplicate_inputs_count_and_resume_guard(self):
        class Fixtures:
            def simbad(self, names):
                return {'status': 'ok', 'data': {'records': []}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.csv'
            path.write_text('object_name,object_id\nstar,z\nstar,z\nother,a\n')
            with self.assertRaises(ValueError): load_input(path, expected_count=174)
            args = argparse.Namespace(input=path, out=Path(tmp)/'out', catalog=None, expected_count=3,
                max_separation_arcsec=3, simbad_server='fixture', batch_size=2)
            results = run(args, Fixtures())
            self.assertEqual([r['original_designation'] for r in results], ['star', 'star', 'other'])
            self.assertEqual([r['input_row'] for r in results], [1, 2, 3])
            with (args.out/'aliases.csv').open(encoding='utf-8-sig') as f:
                self.assertEqual(len(list(csv.DictReader(f))), 3)
            path.write_text('object_name,object_id\nchanged,z\nstar,z\nother,a\n')
            with self.assertRaises(ValueError): run(args, Fixtures())
            self.assertFalse((args.out/'resolve.lock').exists())


if __name__ == '__main__': unittest.main()
