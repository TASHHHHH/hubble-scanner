"""Private-chat Telegram commands for monitoring and controlling the coordinator."""
import json
import threading
import time
from pathlib import Path

_collect_lock=threading.Lock()


def _seconds(value):
    try:return f'{float(value):g}s'
    except (TypeError,ValueError):return '?s'


def _stage_set(name):
    if name=='archive':return {'archive_band','archive_review','mast'}
    if name in {'wiki','mast','archive_review','archive_band','quality'}:return {name}
    if name=='all':return {'all'}
    raise ValueError('Use all, archive, wiki, mast, archive_review, archive_band, or quality.')


def _summary(queue):
    status=queue.status();counts={}
    for row in status['counts']:counts.setdefault(row['stage'],{})[row['state']]=row['count']
    paused=status['paused_stages']
    state='PAUSED' if 'all' in paused else ('running; paused '+', '.join(paused) if paused else 'running')
    lines=['Coordinator: '+state,
           f"Catalog targets: {status['targets']}",
           f"Adaptive capacity: {status['level']['investigators']} (backlog {status['level']['backlog']})"]
    archive=counts.get('archive_band',{})
    if archive:lines.append(f"Archive sweep bands: {archive.get('done',0)}/72 done; {archive.get('running',0)} searching; {archive.get('ready',0)} waiting")
    active=[w for w in status['workers'] if w['state']=='working']
    online=[w for w in status['workers'] if w['state']=='online']
    lines.append(f"Workers: {len(active)} working, {len(online)} online and idle")
    for worker in active[:8]:
        payload=worker.get('current_payload') or {};stage=worker.get('current_stage')
        obj=payload.get('object') or {}
        detail=obj.get('id') or (f"RA {payload.get('ra_min')}–{payload.get('ra_max')}°" if stage=='archive_band' else '')
        lines.append(f"• {worker['name']}: {stage} {detail}".strip())
    if not active and not online:lines.append('No workers have checked in recently.')
    return '\n'.join(lines)[:3800]


def _queue_text(queue):
    status=queue.status()
    if not status['counts']:return 'Queue is empty. Use /search to seed the HST archive sweep.'
    lines=['Queue (stage: done / running / waiting / failed / held)']
    stages={}
    for row in status['counts']:stages.setdefault(row['stage'],{}).update({row['state']:row['count']})
    for stage,counts in sorted(stages.items()):
        lines.append(f"{stage}: {counts.get('done',0)} / {counts.get('running',0)} / {counts.get('ready',0)} / {counts.get('failed',0)} / {counts.get('interrupted',0)}")
    if status['paused_stages']:lines.append('Paused: '+', '.join(status['paused_stages']))
    return '\n'.join(lines)


def _workers_text(queue):
    workers=queue.status()['workers']
    if not workers:return 'No worker PCs have checked in yet. Keep the worker app open after joining its invite.'
    lines=['Worker PCs (idle means connected and polling):']
    for worker in workers[:15]:
        payload=worker.get('current_payload') or {};obj=payload.get('object') or {}
        stage=worker.get('current_stage')
        task=obj.get('id') or (f"RA {payload.get('ra_min')}–{payload.get('ra_max')}°" if stage=='archive_band' else '')
        seen=max(0,int(time.time()-worker['last_seen']))
        lines.append(f"• {worker['name']}: {worker['state']}"+(f" — {stage} {task}" if stage else '')+f" (seen {seen}s ago)")
    return '\n'.join(lines)


def _candidate_text(queue,ident):
    item=queue.candidate_detail(ident)
    if not item:return f"No queued target found for {ident}. Try /recent."
    lines=[f"{item['id']} — {item.get('object_type') or 'target'}",
           f"Coordinates: RA {item.get('ra_deg','?')}°, Dec {item.get('dec_deg','?')}°",
           f"Wikipedia: {item.get('wiki_status','unknown')} | MAST: {item['mast_state']}",
           f"HST observations: {item['observation_count']} | filters: {', '.join(item.get('filters',[])) or 'none'}"]
    observations=item.get('observations',[])
    for obs in observations[:12]:
        if isinstance(obs,dict):
            lines.append(f"• obsid {obs.get('obsid','?')} | MAST ID {obs.get('obs_id','?')} | {obs.get('filter',obs.get('filters','?'))} | {obs.get('instrument',obs.get('instrument_name','?'))} | {_seconds(obs.get('exptime',obs.get('t_exptime')))}")
        else:lines.append(f'• obsid {obs}')
    if item.get('observation_list_truncated'):lines.append('Observation list is truncated; see MAST for the full list.')
    match=(item.get('investigation') or {}).get('match')
    if match:lines.append('Matched preview pair: '+', '.join(match.get('obsids',[])))
    status=item.get('publication_status','not_assessed')
    lines.append('Publication check: '+status.replace('_',' '))
    if item.get('mast_program_ids'):lines.append('HST programs: '+', '.join(item['mast_program_ids']))
    if item.get('program_bibcodes'):lines.append('Program-linked papers: '+', '.join(item['program_bibcodes'][:8]))
    if item.get('object_title_bibcodes'):lines.append('Target name appears in paper title: '+', '.join(item['object_title_bibcodes'][:8]))
    if item.get('mast_search_url'):lines.append('MAST: '+item['mast_search_url'])
    lines.append('Program links/title matches are leads for manual paper review, not proof of novelty.')
    return '\n'.join(lines)[:3900]


def _recent_text(queue,limit=8):
    rows=queue.recent_candidates(limit)
    if not rows:return 'No completed MAST target searches yet.'
    lines=['Recent completed MAST searches:']
    for row in rows:
        obs=row.get('observations',[]);ids=', '.join(str(x.get('obsid','?')) if isinstance(x,dict) else str(x) for x in obs[:5]) or 'no observations'
        if len(obs)>5:ids+=f" (+{len(obs)-5})"
        lines.append(f"• {row.get('id')}: {row.get('observation_count',0)} observations; {', '.join(row.get('filters',[])) or 'no filters'}; obsid {ids}")
    return '\n'.join(lines)[:3900]


def _no_name_match_messages(queue):
    """Format every completed ADS no-name-match object for Telegram delivery."""
    path=Path(queue.path).parent/'results'/'spacecatalog_hst.literature.jsonl'
    if not path.exists():return ['No ADS literature results found yet.']
    latest={}
    try:
        content=path.read_bytes()
        time.sleep(.15)
        confirmed=path.read_bytes()
        if confirmed!=content:content=confirmed
        for line in content.decode('utf-8',errors='ignore').splitlines():
            try:item=json.loads(line)
            except (json.JSONDecodeError,TypeError):continue
            if item.get('id'):
                # The scanner appends checkpoints and can retry an object. Keep
                # its final row so a target is listed once with its latest status.
                latest[item['id']]=item
    except OSError as exc:
        return [f'Could not read ADS results: {str(exc)[:300]}']
    rows=[]
    for item in latest.values():
        if item.get('literature_status')!='no_name_match':continue
        # Keep the catalog identity first. ADS search_names may start with a
        # temporary MAST field target, obscuring the object to review.
        canonical=item.get('wiki_titles') or item.get('aliases') or []
        searched=item.get('search_names') or []
        ident=str(item.get('id','unknown'))
        label=str(canonical[0] if canonical else (searched[0] if searched else ident))[:120]
        extras=[]
        extras.extend(str(x) for x in (item.get('mast_target_names') or []) if x)
        extras.extend(str(x) for x in searched if x and str(x).casefold()!=label.casefold())
        seen=set()
        extras=[x for x in extras if not (x.casefold() in seen or seen.add(x.casefold()))]
        suffix=(' | ADS terms: '+', '.join(extras[:3])[:180]) if extras else ''
        rows.append(f"• {label} ({ident}){suffix}")
    if not rows:return ['No objects currently have status no_name_match.']
    header=f'Objects with no ADS name match: {len(rows)}\n'
    footer='\nADS found no name match for the terms shown. Missing aliases can cause false misses; this is not proof the object or its HST data is unpublished.'
    messages=[];chunk=header
    for row in rows:
        if len(chunk)+len(row)+len(footer)>3800:
            messages.append(chunk.rstrip())
            chunk='More no-name-match objects:\n'
        chunk+=row+'\n'
    if chunk.strip():messages.append((chunk.rstrip()+footer)[:3900])
    return messages


def _collect(queue,credentials,reply):
    if not _collect_lock.acquire(blocking=False):
        reply('A collection is already running.')
        return
    def run():
        try:
            from catalogs import spacecatalog_rows
            from archive_sweep import scan
            root=Path(queue.path).parent;out=root/'results';out.mkdir(parents=True,exist_ok=True)
            stats=queue.export(out)
            objects=spacecatalog_rows(root/'.cache')
            rows=scan(objects,out/'spacecatalog_hst.jsonl',root/'.cache'/'spacecatalog-hst',out/'archive_observations.jsonl')
            hits=sum(bool(row.get('mast_observation_count')) for row in rows)
            reply(f"Collection complete: {hits} of {len(rows)} catalog objects have public HST footprint matches. Results: {out}\nPaper links are proposal-level evidence and need manual review.")
        except Exception as exc:
            reply(f"Collection failed: {str(exc)[:700]}")
        finally:_collect_lock.release()
    threading.Thread(target=run,name='telegram-archive-collect',daemon=True).start()
    reply('Collection started on the coordinator. I’ll message you when the crossmatch and paper lookup finish.')


def handle_command(queue,credentials,text,reply=None):
    """Run one already-authorized private-chat command and return its reply."""
    parts=(text or '').strip().split()
    if not parts:return None
    command=parts[0].split('@',1)[0].lower().lstrip('/')
    args=parts[1:]
    if command in ('help','start'):
        return ('Hubble Scanner controls\n/status — what is running, worker PCs, archive progress\n'
                '/queue — stage counts\n/workers — connected/active PCs\n/recent — recent MAST results with obsids\n'
                '/candidate ID — details, filters, instrument and obsids\n/obsid ID — find a target by observation ID\n'
                '/nomatches or /nonmatchesall — send all completed objects with no ADS name match; long lists are split only to fit Telegram\n'
                '/search — queue the 72-band HST sweep\n/collect — merge finished archive bands and check Papertrack\n'
                '/pause [all|archive|wiki] — stop new claims (running tasks finish)\n'
                '/resume [all|archive|wiki] — resume claims\n/limit N — investigator concurrency, 1–10\n/test — send a Telegram test message\n'
                'Publication still needs object-level manual review.')
    if command=='test':return 'Hubble Scanner Telegram control is connected.'
    if command=='status':return _summary(queue)
    if command=='queue':return _queue_text(queue)
    if command=='workers':return _workers_text(queue)
    if command=='recent':
        try:limit=int(args[0]) if args else 8
        except ValueError:return 'Usage: /recent [1–20]'
        if not 1<=limit<=20:return 'Usage: /recent [1–20]'
        return _recent_text(queue,limit)
    if command in ('nomatches','nonmatches','no_name_matches','nonmatchesall'):
        messages=_no_name_match_messages(queue)
        if reply:
            for message in messages:reply(message)
            return None
        return messages[0] if len(messages)==1 else messages[0]+'\n(Additional results are sent as separate Telegram messages.)'
    if command=='candidate':return _candidate_text(queue,args[0]) if args else 'Usage: /candidate OBJECT_ID'
    if command=='obsid':
        if not args:return 'Usage: /obsid OBSERVATION_ID'
        found=queue.observation_detail(args[0])
        return (f"obsid {args[0]} belongs to {found['id']} | {found.get('filter','?')} | {found.get('instrument','?')} | {_seconds(found.get('exptime'))}\n"
                f"MAST observation: {found.get('obs_id','')}") if found else f"No completed target search contains obsid {args[0]} yet."
    if command in ('pause','stop'):
        name=args[0].lower() if args else 'all'
        try:stages=_stage_set(name)
        except ValueError as exc:return str(exc)
        current=set(queue.status()['paused_stages']);queue.set_paused(current|stages)
        return 'Paused new claims for '+(', '.join(sorted(stages)))+'. Running tasks will finish; /resume to continue.'
    if command=='resume':
        name=args[0].lower() if args else 'all'
        try:stages=_stage_set(name)
        except ValueError as exc:return str(exc)
        current=set(queue.status()['paused_stages'])
        if 'all' in stages:current.clear()
        else:current.difference_update(stages)
        queue.set_paused(current)
        return 'Worker claims resumed.' if not current else 'Resumed '+', '.join(sorted(stages))+'. Still paused: '+', '.join(sorted(current))
    if command=='limit':
        if len(args)!=1 or not args[0].isdigit():return 'Usage: /limit 1–10'
        try:queue.set_worker_limit(int(args[0]))
        except ValueError as exc:return str(exc)
        return f"Adaptive investigator limit set to {args[0]}. Running jobs continue; the new limit applies to new claims."
    if command in ('search','archive'):
        count=queue.seed_archive_bands()
        return f"HST archive sweep has {count} five-degree RA bands. New bands are queued; completed bands stay completed. Connected workers claim available jobs."
    if command=='collect':
        status=queue.status();bands=[x for x in status['counts'] if x['stage']=='archive_band']
        done=next((x['count'] for x in bands if x['state']=='done'),0)
        if done!=72:return f"Not ready to collect: {done}/72 archive bands are complete. Use /status to see what is still running."
        if reply:_collect(queue,credentials,reply);return None
        return 'Collection needs a running Telegram service.'
    return 'Unknown command. Send /help for controls.'


def _authorized(message,credentials):
    chat=message.get('chat') or {};sender=message.get('from') or {}
    configured=str(credentials.get('chat',''))
    # Remote control is restricted to the configured private chat/account.
    return (chat.get('type')=='private' and str(chat.get('id',''))==configured
            and str(sender.get('id',''))==configured)


def poll(queue,stop):
    """Poll Bot API updates; notification delivery remains in its own thread."""
    try:
        import requests
        from secret_store import load
        from investigator import notify
    except Exception:return
    credentials=load()
    if not credentials:return
    token=credentials['token'];base='https://api.telegram.org/bot'+token
    session=requests.Session();session.trust_env=False;offset=None
    commands=[{'command':'help','description':'Show Telegram controls'},
              {'command':'status','description':'See progress and active searches'},
              {'command':'queue','description':'Show jobs by stage'},
              {'command':'workers','description':'See connected worker PCs'},
              {'command':'recent','description':'Recent HST results and obsids'},
              {'command':'candidate','description':'Show a target’s HST evidence'},
              {'command':'obsid','description':'Look up an HST observation ID'},
              {'command':'nomatches','description':'Send all objects with no ADS name match'},
              {'command':'nonmatches','description':'Alias for /nomatches'},
              {'command':'nonmatchesall','description':'Send the full no-name-match list'},
              {'command':'search','description':'Queue the all-sky HST sweep'},
              {'command':'collect','description':'Collect completed archive bands'},
              {'command':'pause','description':'Pause new worker claims'},
              {'command':'resume','description':'Resume worker claims'},
              {'command':'limit','description':'Set investigator limit, 1–10'},
              {'command':'test','description':'Test Telegram control'}]
    try:session.post(base+'/setMyCommands',json={'commands':commands},timeout=(5,10))
    except Exception:pass
    try:
        # Discard messages sent before the coordinator started; never replay stale commands.
        response=session.get(base+'/getUpdates',params={'offset':-1,'timeout':0},timeout=(5,10))
        data=response.json()
        if data.get('ok') and data.get('result'):
            offset=max(item['update_id'] for item in data['result'])+1
    except Exception:pass
    warned_conflict=False
    while not stop.is_set():
        credentials=load() or credentials
        if credentials['token']!=token:
            token=credentials['token'];base='https://api.telegram.org/bot'+token;offset=None
            try:session.post(base+'/setMyCommands',json={'commands':commands},timeout=(5,10))
            except Exception:pass
        params={'timeout':20}
        if offset is not None:params['offset']=offset
        try:
            response=session.get(base+'/getUpdates',params=params,timeout=(5,30))
            data=response.json()
            if response.status_code==409 or not data.get('ok'):
                if response.status_code==409 and not warned_conflict:
                    notify('Telegram control polling conflicts with another getUpdates session or an active webhook. Close other pollers and run only one coordinator.',credentials)
                    warned_conflict=True
                stop.wait(5);continue
            response.raise_for_status()
            for update in data.get('result',[]):
                offset=update['update_id']+1
                message=update.get('message') or update.get('edited_message') or {}
                if not _authorized(message,credentials):continue
                answer=handle_command(queue,credentials,message.get('text',''),
                                      reply=lambda text:notify(text,credentials))
                if answer:notify(answer,credentials)
        except Exception:
            stop.wait(5)
