# Evidence-bearing alias and publication audit

This is an independent pipeline. It does not migrate the SQLite queue, modify
worker payloads, or reinterpret the existing scanner's publication statuses.

```powershell
python scanner.py resolve-aliases --input candidate_list.csv --out out/aliases --expected-count 185 --catalog original_catalog.jsonl --supplement
python scanner.py audit-publications --aliases out/aliases/aliases.jsonl --out out/publications --expected-count 185
```

Use the actual input count, not a presumed sample size. The November 2026 audit
was requested as 174 objects, but the supplied CSV contains 185 distinct IDs;
the user explicitly authorized all entries. No rows are dropped or deduplicated.

## Resolution

Input CSV requires `object_name`; optional `object_id`, `ra_deg`, and `dec_deg`
are preserved. `--catalog` joins the original JSONL catalog's `id` to
`object_id`, refusing missing/duplicate IDs. It does not join by proximity.
The input and enriched row hashes protect resumes against changed inputs.

SIMBAD uses `astroquery.simbad.Simbad.query_objects` in batches, including main
identifier, identifier list, type, and ICRS coordinates. Mapping uses the
returned `user_specified_id`, never result order. Formatting variants preserve
component suffixes and are queried explicitly. Gl and GJ are not equated by
string normalization. `--simbad-server simbad.harvard.edu` selects the official
mirror when Strasbourg is unavailable.

NED queries the original galaxy designation and preserves NED01/NED02.
With `--supplement`, NED CrossidsOfObject supplies cross-identifications;
catalog-returned canonical names are then batch queried in SIMBAD. VizieR
CNS3, MWSC, and PGC provide targeted supplementary records. OpenNGC is joined
only on a returned exact NGC/IC identity and excluded for component inputs.
Gaia DR3 queries only explicitly returned source IDs for stellar objects;
proper motions propagate Gaia positions to J2000. The Exoplanet Archive PS
table looks up `pl_name` or `hostname` separately, preserving host/planet
relationships. It does not assume that a microlensing event/source is the lens.

Coordinates veto conflicting candidates; they do not create equivalence. The
default threshold is 3 arcseconds. NED FK5 J2000 and supplementary catalog
frames are transformed to ICRS. Input epochs are not assumed known. Old stellar
positions, orbital motion, rounded catalog positions, and extended-galaxy
centroids can therefore remain ambiguous. Raising the threshold is not a
substitute for reviewing those cases. CNS3 HD/DM identifiers are not borrowed
from a parent system for a component.

Outputs:

- `aliases.jsonl`: full evidence, candidates, exact queries/payloads, timestamps,
  input record, coordinate checks, and confirmed alias arrays.
- `aliases.csv`: one row per input, in original order; arrays/objects are JSON.
- `unresolved_aliases.csv`: non-confirmed identities with evidence notes.
- `cache/`: durable service responses and failed attempts; failed calls are
  retryable, successful/empty responses have a 30-day TTL.
- `checkpoints/`, `manifest.json`, `completion.json`: resumability and run provenance.

`confirmed` means an identity is supported by the queried catalog records and
passes current checks. It does not assert that every historical alias exists
in these services. `alias_coverage=incomplete` deliberately does not promise
exhaustive coverage. `not_found` is scoped to successful services actually
queried. A failed service is not a negative result. Conflicting candidates are
retained without promoting their names to confirmed aliases.

Only one writer may use a resolution output directory. Remove a stale
`resolve.lock` only after checking the old process has exited. Resume by
repeating the command; successful service calls are read from cache, while
failed calls are retried. Timeouts, bounded exponential backoff, and a one-second
inter-request delay are configurable. An HTTP 429 stops further requests to
that service for the current run; resume after the service's reset time.

## Publication evidence

ADS credentials come from `HUBBLE_ADS_TOKEN` or the existing Windows DPAPI store.
Tokens are not serialized. Every original/confirmed name is included in ADS
queries, in groups of 30; the six-alias cap in the legacy literature scanner
does not apply. Up to 100 documents per query are retained. Truncation is
explicitly incomplete and cannot justify a negative result.

NASA Image and Video Library and ESA/Hubble image searches check original,
canonical, and returned NGC/IC/UGC/M/Arp names. Remaining aliases are explicitly
listed as unchecked for galleries. These interfaces do not establish exhaustive
release coverage; their negative results cannot establish B or D. Search result
metadata and candidate URLs are stored, but hits are not automatically classified
as official images or HST figures.

Reviewed evidence can be supplied through `--reviews reviews.jsonl`. Each row
identifies `input_row`, `kind` (`official_hst_image`, `paper`, or
`paper_hst_image`), `url`, `excerpt`, and `identity_verified`. HST images require
`hst_image_verified`; a paper image requires `figure` as well. These are review
assertions, not facts inferred by a keyword search.

- A: reviewed official Hubble image, exact identity verified.
- B: reviewed HST paper figure plus completed release checks finding no release.
- C: reviewed relevant paper; an HST image/no-release conclusion is not established.
- D: documented complete searches with zero matches, never a novelty claim.
- E: ambiguous identity, failed/incomplete searches, or evidence awaiting review.

The automated gallery adapter intentionally cannot establish B/D by itself.
Publication CSV is a summary; JSONL contains exact queries, returned metadata,
checked aliases, service failures, and evidence. A program-level bibliography
or a nearby MAST pointing is not evidence about this individual target.

## Tests and limits

Run `python -m unittest discover -v`. New offline fixtures cover component
separation, alternative formatting, duplicate identifiers/rows, missing results,
transient errors/cache behavior, coordinate conflicts, MWSC degree units, CNS3
components, and A--E classification gates. Existing queue/distributed-scanner
tests remain part of discovery. Live astronomy queries are opt-in through the
CLI and are not performed by unit tests.

This implementation does not verify FITS image quality, perform binary orbit
propagation, or automatically inspect paper figures. Candidate visualization
ranking still requires target footprint, calibrated product, filter compatibility,
and data-quality checks. No discovery or unpublished-image claim follows from
this audit.

Service references: [SIMBAD](https://astroquery.readthedocs.io/en/stable/simbad/simbad.html),
[NED API](https://ned.ipac.caltech.edu/Docs::API/),
[CNS3](https://cdsarc.cds.unistra.fr/viz-bin/ReadMe/V/70A?format=html),
[NASA images API](https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf).
