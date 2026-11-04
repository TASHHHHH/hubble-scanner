# Desktop preview validation — 2026-11-04

- Windows / Python 3.11: 68 automated tests passed in the adaptive release.
- Adaptive HTTP assignment, level scale-down, centralized notification outbox,
  Windows DPAPI encryption/decryption and tamper rejection are covered.
- Desktop workflow test: synthetic demo, candidate loading/filtering, blocked
  synthetic TSDP download export and FITS report display.
- Task runner: pipeline failure stops subsequent commands; cancellation stops
  the child process; paths/arguments with spaces are preserved.
- Actual HTTP coordinator and ten local worker processes: 24 submitted jobs,
  12 unique jobs, 12 completed executions, all ten workers used (5.44 seconds).
- Visible app: scan layout inspected and offline demo clicked to completion.
- Existing backend tests cover geometry, quality masks, resumability, alias
  deduplication, authentication and lease fencing.

This release's UI validation used synthetic data. Earlier backend live checks
queried Wikipedia/MAST and inspected a real NICMOS FITS file; a complete real
three-band reduction and deployment across ten physical PCs remain unverified.
Hosted GitHub Actions has not yet run. No discovery/publication claim is made.
