"""Trusted-PC, preview-only investigation. No science FITS downloads."""
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace
from runtime import atomic_json, write_rows


def match_pair(item, minimum=.35):
    from footprint_groups import observation_geometries, angular_wavelength_nm
    from shapely.geometry import Point
    obs, errors = observation_geometries(item)
    best = None
    for a,b in itertools.combinations(obs,2):
        if a['obsid']==b['obsid'] or a['filter']==b['filter']: continue
        if angular_wavelength_nm(a['filter'])==angular_wavelength_nm(b['filter']): continue
        common=a['geom'].intersection(b['geom'])
        fraction=common.area/min(a['geom'].area,b['geom'].area)
        if fraction<minimum or not common.covers(Point(0,0)): continue
        if best is None or fraction>best['overlap_fraction']:
            best={'filters':[a['filter'],b['filter']], 'obsids':[str(a['obsid']),str(b['obsid'])],
                  'overlap_fraction':fraction,'basis':'MAST footprint; target center covered; pixels unverified'}
    return best,errors


def download_preview(obsids, destination):
    from astroquery.mast import Observations
    from PIL import Image
    for obsid in obsids:
        products=Observations.get_product_list(obsid)
        for index,row in enumerate(products):
            if str(row['productType']).upper()!='PREVIEW': continue
            if not str(row['productFilename']).lower().endswith(('.jpg','.jpeg','.png')): continue
            manifest=Observations.download_products(products[[index]],download_dir=str(destination.parent/'downloads'))
            col=next(c for c in manifest.colnames if c.lower().replace(' ','')=='localpath')
            with Image.open(str(manifest[col][0])) as im:
                im=im.convert('RGB');im.thumbnail((512,512));im.save(destination)
            return {'obsid':obsid,'dataURI':str(row['dataURI']),'file':destination.name}
    return None


def notify(text,credentials=None):
    """Only trusted local environment holds Telegram credentials. Never log URLs."""
    credentials=credentials or {}
    token=credentials.get('token');chat=credentials.get('chat')
    if not token or not chat:return 'desktop_only'
    import requests
    try:
        with requests.Session() as session:
            session.trust_env=False
            r=session.post('https://api.telegram.org/bot'+token+'/sendMessage',
                           json={'chat_id':chat,'text':text},timeout=20)
            if r.status_code!=200 or not r.json().get('ok'):return 'failed'
        return 'sent'
    except Exception:return 'failed'


def investigate(item,root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    ident=hashlib.sha256(str(item['id']).encode()).hexdigest()[:24]
    report_path=root/(ident+'.json')
    if report_path.exists():return json.loads(report_path.read_text(encoding='utf-8'))
    pair,errors=match_pair(item)
    report={'status':'no_match','match':pair,'geometry_skipped':errors,'publication_status':'not_assessed'}
    if pair:
        report['status']='matched'
        previews=root/'previews';previews.mkdir(exist_ok=True)
        try:
            provenance=download_preview(pair['obsids'],previews/(ident+'.jpg'))
            report['preview']=provenance
            if provenance:
                from ranking import run
                resultfile=root/'ranking_input.jsonl';write_rows(resultfile,[{'id':ident,'priority_score':0}])
                run(SimpleNamespace(previews=previews,results=resultfile,out=root/'ranking.csv',
                                    model=os.getenv('HUBBLE_DINO_MODEL','facebook/dinov2-small'),revision='main'))
                with (root/'ranking.csv').open(encoding='utf-8-sig') as f:
                    report['ranking']=next(r for r in csv.DictReader(f) if r['id']==ident)
            else:report['status']='matched_no_preview'
        except Exception as exc:
            report['status']='matched_processing_failed';report['error']=type(exc).__name__
        report['notification']='coordinator_pending'
    atomic_json(report_path,report)
    return report
