"""Local caches and crash-safe per-worker checkpoints. Never share SQLite over SMB."""
import hashlib
import json
import os
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

VERSION = '3.0'

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def cached_call(namespace, key, producer, ttl=604800):
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    path = Path(os.getenv('HUBBLE_CACHE_DIR', '.cache')) / namespace / (digest + '.json')
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    value = producer()  # failed calls are never cached
    atomic_json(path, value)
    return value

def read_rows(path):
    with Path(path).open('rb') as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b'\n'):
                    warnings.warn(f'Ignoring interrupted final checkpoint row in {path}')
                else:
                    raise ValueError(f'Corrupt checkpoint {path}, line {i+1}')

def write_rows(path, rows, append=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recover only an unterminated tail; malformed complete lines remain errors.
    if append and path.exists():
        with path.open('r+b') as f:
            end=f.seek(0,os.SEEK_END)
            if end:
                f.seek(end-1)
                if f.read(1) != b'\n':
                    pos=end; tail=b''
                    while pos > 0:
                        size=min(pos,65536);pos-=size;f.seek(pos)
                        tail=f.read(size)+tail
                        if b'\n' in tail:
                            tail=tail.rsplit(b'\n',1)[-1];break
                    try:
                        json.loads(tail)
                        f.seek(end);f.write(b'\n')
                    except (ValueError,UnicodeDecodeError):
                        f.truncate(end-len(tail))
    with path.open('a' if append else 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

@contextmanager
def job_guard(args):
    """One writer per output; manifest refuses incompatible resume settings."""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = out.with_suffix(out.suffix + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f'{lock} exists. Stop the other worker; remove a stale lock only after confirming it exited.')
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        config = {k:v for k,v in vars(args).items() if k not in ('func','out','delay','limit')}
        config['version'] = VERSION
        config['catalog_sha256'] = hashlib.sha256(Path(args.catalog).read_bytes()).hexdigest()
        manifest = out.with_suffix(out.suffix + '.manifest.json')
        if manifest.exists():
            if json.loads(manifest.read_text()) != config:
                raise ValueError('Resume configuration/catalog changed; choose a new output path.')
        elif out.exists() and out.stat().st_size:
            raise ValueError('Legacy output has no manifest; choose a new output path to avoid stale results.')
        else:
            atomic_json(manifest, config)
        yield
    finally:
        lock.unlink(missing_ok=True)
