"""Pretrained DINOv2 preview diversity, combined with archive selection scores."""
import csv
import hashlib
import json
import os
from pathlib import Path
import numpy as np
from runtime import atomic_json

def diversity_scores(embeddings, block=128):
    z = np.asarray(embeddings,dtype=np.float32)
    if z.ndim != 2 or not np.isfinite(z).all(): raise ValueError('Invalid embeddings')
    norms=np.linalg.norm(z,axis=1,keepdims=True)
    if np.any(norms == 0): raise ValueError('Zero embedding')
    z=z/norms
    if len(z) == 1: return np.zeros(1)
    scores=[]
    for start in range(0,len(z),block):
        sim=z[start:start+block] @ z.T
        sim[np.arange(len(sim)),np.arange(start,start+len(sim))]=-np.inf
        scores.extend(1-np.clip(sim.max(axis=1),-1,1))
    return np.asarray(scores)

def run(args):
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModel
    from scanner import read_jsonl
    files=sorted(Path(args.previews).glob('*.jpg'))
    if not files: raise SystemExit('No JPEG previews to rank')
    model_cache=str(Path(os.getenv('HUBBLE_CACHE_DIR','.cache'))/'models')
    processor=AutoImageProcessor.from_pretrained(args.model,revision=args.revision,cache_dir=model_cache,use_fast=False,trust_remote_code=False)
    model=AutoModel.from_pretrained(args.model,revision=args.revision,cache_dir=model_cache,trust_remote_code=False,use_safetensors=True).eval()
    if model.config.model_type != 'dinov2': raise ValueError('Expected a DINOv2 model')
    revision=getattr(model.config,'_commit_hash',None) or args.revision
    device='cuda' if torch.cuda.is_available() else 'cpu'
    model.to(device)
    embeddings=[]
    cache=Path(os.getenv('HUBBLE_CACHE_DIR','.cache'))/'embeddings';cache.mkdir(parents=True,exist_ok=True)
    for path in files:
        key=hashlib.sha256(path.read_bytes()+f'{args.model}:{revision}:cls:v1'.encode()).hexdigest()
        dest=cache/(key+'.npy')
        if dest.exists():
            z=np.load(dest,allow_pickle=False)
        else:
            with Image.open(path) as img: batch=processor(images=img.convert('RGB'),return_tensors='pt')
            with torch.inference_mode():
                z=model(**{k:v.to(device) for k,v in batch.items()}).last_hidden_state[0,0].cpu().numpy()
            tmp=dest.with_suffix(f'.{os.getpid()}.tmp')
            with tmp.open('wb') as f: np.save(f,z,allow_pickle=False)
            os.replace(tmp,dest)
        embeddings.append(z)
    scores=diversity_scores(embeddings)
    candidates={x['id']:x for x in read_jsonl(args.results)} if Path(args.results).exists() else {}
    ranked=[]
    for path,score in zip(files,scores):
        metadata=candidates.get(path.stem,{})
        archive=float(metadata.get('priority_score',0))
        ranked.append({'id':path.stem,'preview':str(path),'visual_diversity':round(float(score),6),
                       'archive_score':archive,'combined_score':round(archive+10*float(score),6),
                       'model':args.model,'model_revision':revision,'publication_status':'not_assessed'})
    ranked.sort(key=lambda x:x['combined_score'],reverse=True)
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(ranked[0]));w.writeheader();w.writerows(ranked)
    atomic_json(out.with_suffix('.metadata.json'),{'model':args.model,'revision':revision,'device':device,
        'sample_size':len(files),'meaning':'Nearest-neighbor cosine distance within this preview sample; not novelty or image quality',
        'combination':'archive_score + 10 * visual_diversity','preview_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}})
    print(f'Ranked {len(files)} previews on {device}: {out}')
