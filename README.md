# Hubble Scanner

A Windows desktop workspace for Hubble archive research: catalog searches,
Wikipedia imagery attribution checks, MAST footprints, FITS quality review,
optional DINOv2 ranking and an adaptive distributed worker queue.

## Start

On 64-bit Windows, download [HubbleScanner-Setup.exe](https://github.com/TASHHHHH/hubble-scanner/releases/latest/download/HubbleScanner-Setup.exe), run it, then open **Hubble Scanner** from Start. Setup includes Python and downloads the app dependencies. Select the AI option for adaptive workers.

For a source checkout, follow the steps below.

Install Python 3.11 with Tcl/Tk, then run:

```powershell
.\setup.ps1
.\launch.ps1
```

Use `setup.ps1 -AI` for optional pretrained DINOv2 dependencies. First ranking also
downloads model weights. Public-data queries need no paid API key.
Work is saved in `~/HubbleScout`, outside this repository.

## Desktop workflow

1. Try **Offline demo** first. Its products are synthetic, not download requests.
2. **Build catalog**, then **Import catalog & seed queue** with its JSONL file.
3. **Join local pool**. Qualifying Wikipedia results feed MAST investigations.
4. **Collect results**, then load `results/candidates.jsonl` under Candidates.
5. Inspect target evidence, export TSDP CSV, or rank available archive previews.
6. Download chosen science products through TSDP/MAST; select them in FITS review.

Use a separate workspace per catalog/configuration. TSDP export resolves real
science product URIs for [TSDP Import MAST CSV](https://github.com/TASHHHHH/tash-space-data-preprocessor),
defaults to ten groups, and reports incomplete groups separately. This app finds
RGB candidates; it does not perform image reduction or generate final RGB images.

## Whole-frame search

Use **Scan → Search SpaceCatalog in MAST** to seed 72 MAST archive query bands into
the multi-PC queue. Connected workers query independent RA ranges in parallel. After
they finish, choose **Collect results** to merge the observations, verify/download
the pinned SpaceCatalog object and designation files, and perform the footprint and
STScI Papertrack crossmatch on the coordinator. Results are saved as
`results/spacecatalog_hst.jsonl` and `.csv`. The scan looks for public
HST image footprints covering each target center and lists observation IDs and filters.
It also writes the archive-matched subset to `results/spacecatalog_hst.review.csv`,
one observation per row in `results/spacecatalog_hst.observations.csv`,
unique program-paper links to `results/spacecatalog_hst.papers.csv`, and catalog-name
matches to `results/spacecatalog_hst.target_papers.csv`, then cross-checks
matched observations' proposal IDs against the [STScI Papertrack HST bibliography](https://archive.stsci.edu/papertrack/docs/public_search.html).
Results include program IDs, associated paper bibcodes, and whether a catalog name
appears in a paper title. A paper linked to the same proposal is program-level evidence;
it does not prove that the paper studied this particular object. Likewise, no Papertrack
match is not proof that an object has never been published. Check paper records and
the [bibliography methodology](https://archive.stsci.edu/hst/bibliography/basic_methodology.html)
before making a novelty claim.
Start manual triage with `spacecatalog_hst.review.csv`: filter for
`no_published_science_paper_found_for_programs`, then use its program IDs to find papers
in `spacecatalog_hst.papers.csv`. Open their ADS links and check the actual target and
datasets used. `target_papers.csv` is a shortcut for cases where a catalog name occurs
in a linked paper title.

Imported rows retain their source name, URL, license, and retrieval date. SpaceCatalog
passes through upstream licensing terms, so consult the per-row source fields before
redistributing catalog data or results.

The imported release is [SpaceCatalog DR1](https://github.com/space-catalog/data-releases/releases/tag/dr1).
Archive coverage comes from public HST science image footprints in the [MAST HST archive](https://archive.stsci.edu/missions-and-data/hst).

In **FITS Quality**, select **Search whole frames** and choose two or more overlapping
local science FITS files. Select **Open field report** when the scan finishes.
Each row shows a sky position and centered cutouts labeled with the input exposure
and filter. The JSON next to the report includes the input paths and zero-based pixel coordinates.

The search looks across each usable science chip for positive peaks at least five local
noise estimates above background, with support from adjacent pixels. It checks available
DQ, ERR and WHT masks, then matches peaks from different exposure IDs within 0.2 arcseconds.
The report shows up to 200 groups, sorted by the number of matching exposures and then
by the weakest peak contrast in the group. Faint diffuse features, one-exposure sources
and frames with poorly aligned WCS can be missed. Matches require review for artifacts
and prior identification.

## Multiple PCs

On the same network, open **Workers → Create worker invite** on the coordinator.
Send the invite privately to the second PC and select it under **Workers → Join with invite**.
Keep the coordinator open and create a separate invite for each worker.

See [Adaptive distributed scan](DISTRIBUTED_SCAN.md). All authorized PCs can switch
between Wikipedia and MAST/DINOv2 investigations according to backlog levels.
Ten unfinished candidates allow two investigators, twenty allow three, up to five.
Other PCs continue Wikipedia scanning. Telegram is sent only by the coordinator,
whose token is encrypted with Windows current-user DPAPI and never sent to workers.

After **Search SpaceCatalog in MAST** finishes, **Queue no-paper targets for PC review**
adds targets with archive coverage but no linked STScI science paper (or a missing
proposal ID) to the shared queue. Connected adaptive PCs review their MAST filters and
previews in parallel. Results are written to `results/archive_review_results.jsonl`;
the publication finding remains a proposal-level hint for manual paper review.
Outside PCs require HTTPS and individual revocable credentials. Existing wiki-only
tokens remain restricted until reissued. Update every PC before starting this mode.

## Research limits

- Messier, NGC and IC through OpenNGC; optional Arp and Sharpless via VizieR;
  custom catalogs through `scanner.py catalog --extra-csv`.
- Wikipedia `not_detected` means matching attribution keywords were not found.
  It is not proof that Hubble imagery is absent.
- Automatic investigation requires two overlapping bands covering the target center.
  RGB export identifies three-filter groups. FITS WCS and DQ/ERR/WHT checks add
  pixel evidence; common coverage is sampled. Review instrument-specific flags.
- DINOv2 ranks preview diversity, not astronomical novelty. Archive previews
  are single-band browse images, not constructed RGB composites.
- Papertrack links refereed HST science papers to proposal IDs. Proposal-level links
  can include papers about other targets in the same program; a missing match cannot
  prove an object is unpublished. Review linked papers and search the literature before
  any novelty claim.

## Development

```powershell
python -m unittest discover -v
python demo.py --out out/demo
python cluster_smoke.py --out out/cluster-smoke
```

Use a fresh output folder for each cluster smoke test. The Windows workflow runs
the test suite, offline demo and ten-worker smoke test. See [validation](VALIDATION.md)
for test scope and deployment limitations.

Architecture: `app.py` owns the Tkinter UI and background subprocess runner;
`coordinator.py` owns SQLite/HTTP scheduling; `cluster_worker.py` manages leased
tasks. `scanner.py`, `footprint_groups.py`, `fits_quality.py` and `ranking.py`
implement research stages. Long-running work stays off the GUI thread.

## Documentation

- [Distributed setup and worker levels](DISTRIBUTED_SCAN.md)
- [FITS quality checks and CLI reference](CLUSTER_AND_QUALITY.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
