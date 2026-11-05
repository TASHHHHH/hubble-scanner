"""Refresh the SIMBAD cross-identifications bundled with the expanded catalog."""
import csv
import io
from pathlib import Path
import re
import tempfile
import requests

# SIMBAD prefix, familiar catalog name. Only numbered objects and letter suffixes.
FAMILIES = (
    ('HH ', 'HH'), ('Barnard ', 'Barnard'), ('LDN ', 'LDN'), ('LBN ', 'LBN'),
    ('VDB ', 'vdB'), ('Cl Collinder ', 'Collinder'), ('Cl Melotte ', 'Melotte'),
    ('APG ', 'Arp'), ('SH  2-', 'Sh2'), ('GUM ', 'Gum'), ('RCW ', 'RCW'), ('Ced ', 'Cederblad'),
)
OUT = Path(__file__).resolve().with_name('simbad-identifiers.csv')


def fetch(prefix, label):
    query = ("SELECT i.id,b.oid,b.main_id,b.ra,b.dec FROM ident AS i JOIN basic AS b "
             "ON b.oid=i.oidref WHERE b.ra IS NOT NULL AND b.dec IS NOT NULL "
             f"AND i.id LIKE '{prefix}%' ORDER BY id")
    response = requests.get('https://simbad.cfa.harvard.edu/simbad/sim-tap/sync',
                            params=dict(request='doQuery', lang='ADQL', format='csv',
                                        maxrec=20000, query=query), timeout=60)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    if reader.fieldnames != ['id','oid','main_id','ra','dec']:
        raise ValueError(f'{label}: unexpected SIMBAD response')
    raw = list(reader)
    if len(raw) >= 20000:
        raise ValueError(f'{label}: possible truncated query')
    records = []
    for row in raw:
        suffix = row['id'][len(prefix):].strip()
        if not re.fullmatch(r'[0-9]+[A-Za-z]?', suffix):
            continue
        records.append((label, suffix, row['oid'], ' '.join(row['main_id'].split()), row['ra'], row['dec']))
    if not records:
        raise ValueError(f'{label}: no usable coordinates')
    return records


def main():
    records = []
    for prefix,label in FAMILIES:
        batch = fetch(prefix,label)
        records.extend(batch)
        print(label, len(batch), flush=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=OUT.parent,
                                     prefix='snapshot-', suffix='.tmp', delete=False) as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(('family','number','oid','main_id','ra_deg','dec_deg'))
        writer.writerows(records)
    Path(f.name).replace(OUT)


if __name__ == '__main__':
    main()