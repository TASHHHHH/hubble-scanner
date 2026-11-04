# v3.1: shared jobs and FITS quality screening

All PCs connect to **one coordinator database on one master PC**. They claim jobs; they no longer need worker indices or copies of the whole catalog. Use this mode instead of simultaneously launching the old static `run-worker.ps1` commands for the same scan.

## 1. Set up the master

Install with `setup.ps1`. Keep the master awake while workers run. In PowerShell, from the extracted scanner folder:

```powershell
$py = '.\.venv\Scripts\python.exe'
# Generate one random token. Copy the resulting value privately to every worker.
$env:HUBBLE_QUEUE_TOKEN = (& $py -c "import secrets; print(secrets.token_urlsafe(32))")
& $py coordinator.py --db out/queue.sqlite seed --catalog out/catalog.jsonl
& $py coordinator.py --db out/queue.sqlite serve --host 0.0.0.0 --port 8765
```

Use the master PC's LAN IP in worker commands. TCP port 8765 must be reachable from those PCs. This HTTP service is for a trusted private LAN; it does not provide TLS. Do not expose it to the Internet. The random token authenticates workers. Requests to the coordinator bypass ambient HTTP proxy settings.

The SQLite database stays on the master's local disk, never an SMB share. It persists completed jobs and ownership across restarts. Back up the database while the server/workers are stopped, including any remaining SQLite WAL files. Restart with the same database and the same token on every PC.

## 2. Start each of the 8–10 PCs

Install the same v3.1 bundle and core requirements on each PC. Each worker needs local writable disk for temporary files and its API cache. It does not need a GPU for searches or FITS checks.

```powershell
$env:HUBBLE_QUEUE_TOKEN = 'paste-the-same-random-token-from-the-master'
.\run-cluster-worker.ps1 -Server http://192.168.1.50:8765
```

Replace the example IP. The default worker label is the computer name. Every PC runs the same command. The coordinator assigns different jobs atomically and maintains at most one Wikipedia task and four MAST tasks concurrently by default, with up to ten quality tasks. Set `serve --mast-slots N` if needed. Workers exit when there are no ready/running jobs; start them again after adding more work.

## 3. What prevents duplicate searches

- Repeated submissions of identical Wikipedia title sets create one job. Catalog formatting variants such as `NGC2484`, `NGC 2484` and `NGC02484` normalize to one designation. Components such as NGC123A remain distinct from NGC123.
- For known cross-catalog identities, an optional `canonical_id` in the JSONL catalog joins their Wikipedia title sets. This must be an established identification; nearby coordinates alone are not treated as proof of identity.
- A MAST job is keyed by the exact ICRS coordinates, query radius, observation limit and queue schema. Matching queries from different catalog objects share one result. Slightly different coordinates/radii are separate queries; no unverified fuzzy merging occurs.
- Each running job has a random ownership token and a renewable lease. A second worker cannot claim it. Old tokens cannot submit or renew someone else's job.
- Completed jobs remain completed when PCs restart or the same catalog is seeded again. Exported records retain each catalog identity and its `coordinator_key`, even when the underlying search ran once.
- FITS quality jobs are deduplicated by file contents (SHA-256), input set and settings. Workers verify the fingerprints and check for file changes during reading.

The guarantee is exclusive ownership of a queued job and rejection of stale results. No distributed client can guarantee that a remote API request executes exactly once if a computer crashes after sending it. This queue favors avoiding duplicate searches: expired jobs are **held**, not automatically reassigned.

## 4. Reuse the existing live scan

Import completed earlier checkpoints before starting workers, so they do not search those objects again. The source catalog must match the seeded objects; complete title sets, retrieval timestamps and MAST radius/truncation are checked.

```powershell
& $py coordinator.py --db out/queue.sqlite import-results `
  --catalog out/catalog.jsonl --wiki-glob 'out/wiki_*.jsonl' --mast-glob 'out/mast_*.jsonl'
```

Use the actual paths to the original scan. Import and reseed are transactional: incompatible catalog/settings fail instead of silently reusing stale results. Do not edit or overwrite the run's input catalog. For intentional refreshes, start a new run database; that explicitly allows new queries.

## 5. Progress, interruption and exports

In another terminal on the master:

```powershell
& $py coordinator.py --db out/queue.sqlite status
& $py coordinator.py --db out/queue.sqlite export --out out/cluster
& $py scanner.py merge --catalog out/cluster/catalog.jsonl `
  --wiki-glob out/cluster/wiki_coordinator.jsonl --mast-glob out/cluster/mast_coordinator.jsonl `
  --out out/cluster/candidates.jsonl
& $py footprint_groups.py --results out/cluster/candidates.jsonl --out out/cluster/filter_groups.jsonl
& $py mast_support.py --results out/cluster/candidates.jsonl `
  --groups out/cluster/filter_groups.jsonl --out out/cluster/tsdp_products.csv
```

If a heartbeat fails, the worker stops its child task and exits. Once the lease expires, the master marks that job `interrupted`. Confirm that the old worker/task has stopped, then use the key printed by `status`:

```powershell
& $py coordinator.py --db out/queue.sqlite requeue --key 'mast:the-job-key' --confirm-worker-stopped
```

Ordinary reported task failures and incomplete Wikipedia scans retry at most three times, with a delay. Exhausted jobs become `failed`. Their worker log is in the job's attempt directory. This avoids both endless retries and silently treating a failed query as a negative result. Workers polling a held job do not take it over automatically.

## 6. FITS quality on one PC

After TSDP downloads the files, run:

```powershell
& $py fits_quality.py red.fits green.fits blue.fits --out out/fits_quality.json
```

The checker reads local files without changing them. It scans science arrays in bounded tiles and pairs DQ/ERR/WHT arrays by `EXTVER`, preserving chip identity. It counts nonfinite pixels, rejected DQ bits, invalid error values and zero/invalid weights. Finite science zeros remain valid. Constant `PIXVALUE` companion arrays are supported. Missing or mismatched masks stay visible in the report.

It reports:

- Exact per-plane valid-pixel counts and fractions under the selected masks.
- Saturation/cosmic-ray **flags**, using documented ACS and WFC3 detector bit definitions. Other instruments remain unknown unless you provide `--saturation-bits` / `--cosmic-bits`. It does not invent a universal bit interpretation or detect unflagged cosmic rays.
- Sampled median, percentiles, neighboring-pixel noise/contrast proxies and SCI/ERR ratios where a compatible error array exists. Flat or low-contrast images carry review warnings. These are not calibrated, background-subtracted source SNR measurements; drizzled noise is correlated.
- A common-sky grid projected through each band's actual WCS and masks. `sampled_common_valid_fraction` measures jointly valid samples inside the geometric three-band intersection, including chip gaps and masked regions.

The default grid is 128×128; use `--grid 256` or `--grid 512` for finer screening. The default minimum shared valid fraction is 0.5 (`--min-valid`). `--bad-bits -1` rejects every nonzero DQ flag conservatively. Set a documented bit mask only when you know which corrected/informational bits can be accepted.

`quality_status` is `rejected_low_common_coverage`, `review_required`, or `passes_coverage_screen`. A coverage pass does not mean ready-to-publish RGB. The report deliberately keeps `pixel_validity_verified=false`: sub-grid defects and unflagged artifacts may remain. Inspect source signal, epochs, PSF, background subtraction and calibration before compositing.

## 7. Distribute quality jobs

Place immutable input FITS on a read-only share reachable by the master and workers at the same UNC paths. Submit each desired RGB input set on the master:

```powershell
& $py coordinator.py --db out/queue.sqlite quality `
  '\\MASTER\HubbleData\red.fits' '\\MASTER\HubbleData\green.fits' '\\MASTER\HubbleData\blue.fits'
```

The master hashes the files when enqueuing. One worker handles each distinct set, validates the input hashes and returns its report. `export` writes `quality_results.jsonl` centrally. Do not put the queue database on that share. Avoid modifying FITS files after submitting a job; changed inputs are rejected and should be submitted as a new task.

## Validation and scientific references

Run `python -m unittest discover -v`. `python cluster_smoke.py --out work/new-cluster-smoke` starts a real local HTTP server and ten worker processes, submitting twelve synthetic FITS tasks twice and checking that only twelve executions occur. Use a fresh output directory.

See `VALIDATION.md` for measured results and limitations. This has not yet been exercised on ten separate physical PCs.

- Astropy section reads: https://docs.astropy.org/en/stable/io/fits/usage/image.html
- ACS data handbook: https://hst-docs.stsci.edu/acsdhb
- WFC3 UVIS calibration/DQ: https://hst-docs.stsci.edu/wfc3dhb/chapter-3-wfc3-data-calibration/3-2-uvis-data-calibration-steps
- WFC3 IR calibration/DQ: https://hst-docs.stsci.edu/wfc3dhb/chapter-3-wfc3-data-calibration/3-3-ir-data-calibration-steps

Publication status remains `not_assessed`; quality and coverage checks do not establish novelty or publication history.
