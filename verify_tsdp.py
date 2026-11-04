"""Headless contract check using TSDP's actual CSV/URL parsing definitions.

No GUI, downloads or Telegram calls are executed. Pass a trusted local checkout.
"""
import argparse
import ast
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote,unquote,urlparse,parse_qs

def check(source, csv_path):
    source=Path(source)
    ns={'Path':Path,'quote':quote,'unquote':unquote,'urlparse':urlparse,'parse_qs':parse_qs,
        're':re,'LOGGER':logging.getLogger('contract'),'EXPECTED_OPERATION_ERRORS':(ValueError,TypeError,KeyError,AttributeError)}
    modules=[('mast.py','parse_mast_csv'),('mast_importer_rows.py','MastImporterRowsMixin'),
             ('mast_importer_products.py','MastImporterProductsMixin'),('mast_importer_actions.py','MastImporterActionsMixin')]
    for filename,name in modules:
        path=source/'tsdp2'/'downloads'/filename
        tree=ast.parse(path.read_text(encoding='utf-8-sig'))
        node=next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==name)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    harness=type('ImporterContract',(ns['MastImporterRowsMixin'],ns['MastImporterProductsMixin'],ns['MastImporterActionsMixin']),{})()
    rows=ns['parse_mast_csv'](str(csv_path))
    urls=harness._urls_from_csv(rows,['any'])
    expected={(harness._mast_download_url(r['dataURI']),harness._filename_from_uri(r['dataURI'])) for r in rows}
    if not rows or set(urls) != expected:raise AssertionError('TSDP did not accept every exact exported product')
    return {'csv_rows':len(rows),'distinct_queued_products':len(urls),'status':'passed',
            'scope':'actual TSDP CSV parser and URL selection; GUI/download not executed'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tsdp-source',required=True);p.add_argument('--csv',required=True);a=p.parse_args()
    print(json.dumps(check(a.tsdp_source,a.csv),indent=2))
