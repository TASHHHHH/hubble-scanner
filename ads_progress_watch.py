"""Send name-level Telegram updates for an already-running ADS literature scan.

The scanner checkpoints its JSONL after each object. This small watcher lets a
running scan keep its current process while adding name lists to each 10-result
Telegram update; it exits when that scanner process exits.
"""
import argparse
import csv
import ctypes
import json
import os
import time
import uuid
from pathlib import Path
from ctypes import wintypes


def read_completed(path):
    rows = {}
    try:
        with Path(path).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if row.get('id') and row.get('literature_status') in ('papers_found', 'no_name_match'):
                    rows[row['id']] = row
    except OSError:
        pass
    return rows


class ResultsTail:
    """Read the initial checkpoint once, then consume only appended JSONL rows."""
    def __init__(self, path):
        self.path = Path(path)
        self.items = read_completed(self.path)
        try:
            self.offset = self.path.stat().st_size
        except OSError:
            self.offset = 0

    def refresh(self):
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                # The scanner rewrites the sorted result file once at exit.
                self.items = read_completed(self.path)
                self.offset = size
                return self.items, True
            if size == self.offset:
                return self.items, False
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                data = stream.read(size - self.offset)
        except OSError:
            return self.items, False
        # Keep an incomplete final line for the next poll.
        boundary = data.rfind(b'\n')
        if boundary < 0:
            return self.items, False
        complete = data[:boundary + 1]
        self.offset += len(complete)
        changed = False
        for line in complete.splitlines():
            try:
                row = json.loads(line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            if row.get('id') and row.get('literature_status') in ('papers_found', 'no_name_match'):
                self.items[row['id']] = row
                changed = True
        return self.items, changed


def process_alive(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def display_name(row):
    names = row.get('wiki_titles') or row.get('aliases') or row.get('search_names') or []
    return str(names[0] if names else row.get('id', 'unknown'))


def _atomic_csv(path, fields, rows):
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    try:
        os.replace(temporary, path)
        return path
    except PermissionError:
        # Excel can hold a CSV without delete sharing. Preserve the refresh in
        # a sidecar and, crucially, do not let a locked review sheet kill the
        # Telegram progress watcher.
        fallback = path.with_name(f'{path.stem}.live{path.suffix}')
        os.replace(temporary, fallback)
        return fallback


def write_progress_exports(results, items, include_papers=True):
    """Publish reviewable in-progress target and paper tables atomically."""
    path = Path(results)
    targets_path = path.with_name(path.stem + '.progress.csv')
    papers_path = path.with_name(path.stem + '.progress_papers.csv')
    leads_path = path.with_name(path.stem + '.progress_review_leads.csv')
    unmatched_path = path.with_name(path.stem + '.no_name_review.csv')
    target_fields = ('object_id', 'object_name', 'literature_status', 'publication_status',
                     'program_paper_count', 'mast_observation_count', 'mast_program_ids',
                     'ads_num_found', 'search_names', 'ads_query')
    paper_fields = ('object_id', 'object_names', 'literature_status', 'bibcode', 'title',
                    'first_author', 'year', 'journal', 'doi', 'ads_url', 'refereed', 'abstract')
    target_rows, paper_rows, lead_rows, unmatched_rows = [], [], [], []
    for item in items.values():
        target = {
            'object_id': item.get('id', ''),
            'object_name': display_name(item),
            'literature_status': item.get('literature_status', ''),
            'publication_status': item.get('publication_status', ''),
            'program_paper_count': item.get('program_paper_count', ''),
            'mast_observation_count': item.get('mast_observation_count', ''),
            'mast_program_ids': ', '.join(map(str, item.get('mast_program_ids') or [])),
            'ads_num_found': item.get('ads_num_found', ''),
            'search_names': ' | '.join(item.get('search_names') or []),
            'ads_query': item.get('ads_query', ''),
        }
        target_rows.append(target)
        if item.get('literature_status') == 'no_name_match':
            unmatched_rows.append({
                'object_name': target['object_name'],
                'object_id': target['object_id'],
                'searched_names': target['search_names'],
                'mast_target_names': ' | '.join(map(str, item.get('mast_target_names') or [])),
                'mast_observation_count': target['mast_observation_count'],
                'mast_program_ids': target['mast_program_ids'],
                'papertrack_program_paper_count': target['program_paper_count'],
                'publication_status': target['publication_status'],
                'ads_num_found': item.get('ads_num_found', ''),
                'review_warning': 'No ADS name match is not evidence of unpublished HST data; verify MAST target aliases, actual footprint and program papers.',
            })
        if item.get('publication_status') == 'no_published_science_paper_found_for_programs':
            lead = dict(target)
            lead['review_note'] = 'No Papertrack-linked program paper found; manually check proposal papers and whether they use these observations.'
            lead['ads_sample_titles'] = ' | '.join(
                str(paper.get('title') or '') for paper in (item.get('papers') or [])[:3])
            lead_rows.append(lead)
        if include_papers:
            for paper in item.get('papers') or []:
                paper_rows.append({
                    'object_id': item.get('id', ''),
                    'object_names': ' | '.join(item.get('search_names') or []),
                    'literature_status': item.get('literature_status', ''),
                    **paper,
                })
    written = [_atomic_csv(targets_path, target_fields, target_rows)]
    if include_papers:
        written.append(_atomic_csv(papers_path, paper_fields, paper_rows))
    written.append(_atomic_csv(leads_path, target_fields + ('review_note', 'ads_sample_titles'), lead_rows))
    unmatched_fields = ('object_name', 'object_id', 'searched_names', 'mast_target_names',
                        'mast_observation_count', 'mast_program_ids',
                        'papertrack_program_paper_count', 'publication_status',
                        'ads_num_found', 'review_warning')
    written.append(_atomic_csv(unmatched_path, unmatched_fields, unmatched_rows))
    return [p for p in written if p.stem.endswith('.live')]


def run(pid, results, interval=3):
    from investigator import notify
    from secret_store import load

    credentials = load()
    if not credentials:
        return 2
    tail = ResultsTail(results)
    previous = tail.items
    # Attach immediately. The scan may already be in flight when the watcher
    # starts; waiting for one quiet second can mistake an ADS timeout/backoff
    # for the process finishing and permanently stop tracking a live run.
    seen = set(previous)
    pending = []
    total_at_start = len(seen)
    reported = 0
    fallback_exports = write_progress_exports(results, previous, include_papers=True)
    for path in fallback_exports:
        print(f'Export is open/locked; live copy written to {path}', flush=True)
    print(f'In-progress results: {Path(results).with_name(Path(results).stem + ".progress.csv")}', flush=True)
    while process_alive(pid):
        time.sleep(interval)
        current, changed = tail.refresh()
        if not changed:
            continue
        fallback_exports = write_progress_exports(results, current, include_papers=False)
        for path in fallback_exports:
            print(f'Export is open/locked; live copy written to {path}', flush=True)
        current_keys = set(current)
        # Ignore partial snapshots while the scanner appends JSONL checkpoints.
        if len(current_keys) < len(seen):
            continue
        fresh = [row for key, row in current.items() if key not in seen]
        for row in fresh:
            seen.add(row['id'])
            pending.append(row)
        while len(pending) >= 10:
            batch, pending = pending[:10], pending[10:]
            names = [display_name(row) for row in batch
                     if row.get('literature_status') == 'no_name_match']
            body = ', '.join(names) if names else 'None in these 10 searches.'
            reported += 10
            start_count = total_at_start + reported - 9
            end_count = total_at_start + reported
            message = (f'ADS scan: {end_count} completed total '
                       f'(searches {start_count}–{end_count}).\n'
                       f'No ADS name matches in this batch: {body}\n'
                       'Search used catalog-provided names; missing aliases can cause false no-name results.')
            delivery = notify(message, credentials)
            print(f'Telegram name update {delivery}; exported {total_at_start + reported} completed targets.', flush=True)
    # Flush the final partial batch when the stable wrapper exits.
    current = read_completed(results)
    fresh = [row for key, row in current.items() if key not in seen]
    pending.extend(fresh)
    if pending:
        names = [display_name(row) for row in pending
                 if row.get('literature_status') == 'no_name_match']
        body = ', '.join(names) if names else 'None in the final partial batch.'
        start_count = total_at_start + reported + 1
        end_count = total_at_start + reported + len(pending)
        message = (f'ADS scan finished: {end_count} completed total '
                   f'(searches {start_count}–{end_count}).\n'
                   f'No ADS name matches in this final batch: {body}\n'
                   'Search used catalog-provided names; missing aliases can cause false misses.')
        delivery = notify(message, credentials)
        write_progress_exports(results, current)
        print(f'Final Telegram update {delivery}; exported {end_count} completed targets.', flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True,
                        help='PID of the persistent scan wrapper, not its restartable child')
    parser.add_argument('--results', required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.pid, args.results))


if __name__ == '__main__':
    main()
