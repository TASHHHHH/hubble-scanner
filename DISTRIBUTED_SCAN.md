# Adaptive distributed scan

## Backlog levels

The coordinator counts unique unfinished archive-band, MAST and archive-review jobs (ready plus running), not
all historical Wikipedia gaps or catalog aliases. All PCs with adaptive credentials
can investigate. Each worker runs one task at a time.

| Unfinished candidates | Maximum concurrent investigators |
| --- | --- |
| 0 | No investigation work |
| 1–9 | 1 |
| 10–19 | 2 |
| 20–29 | 3 |
| 30–39 | 4 |
| 40+ | 5 |

Unused workers check Wikipedia. Active work is never killed when a level falls;
new MAST claims wait until capacity is available. A slow PC does not block the
others. Claims and level checks share one SQLite transaction, preventing duplicate
assignment or concurrent capacity overruns. Expired tasks remain held for review.
The GUI Workers tab displays backlog and investigator capacity.

## Connect PCs on the same network

1. Install the same scanner version with `setup.ps1 -AI` on each PC and open the app. On the coordinator, import the catalog and seed its queue.
2. On the coordinator, open **Workers → Create worker invite**. If prompted, choose its address on the shared network. Enter a unique worker name and save the invite. Keep the app open.
3. Transfer the invite privately to that PC. Open **Workers → Join with invite** there and select the file. Repeat with a separate invite for each additional PC.

Each invite contains a worker credential. Anyone with the file can act as that worker. Open invites only from your coordinator. You can reuse the file when the queue has more work during the same coordinator session. After restarting the coordinator, create fresh invites. Reusing the same worker name revokes its previous credential.

If the worker cannot connect, check **View Logs**. Check that both PCs are on the same network and the coordinator allows incoming TCP port 8766 on its private network. For a certificate error after a restart, transfer a fresh invite.

## Manual coordinator setup

Install with `setup.ps1 -AI`, import a catalog, seed it and start the local
coordinator. Under **Workers → Manual connection settings**, select **Issue worker token** once for each PC. New credentials allow
Wikipedia and MAST investigation only, not quality tasks, administration, or other
workers' jobs. Old wiki-only credentials stay restricted; reissue them to opt in.
Each PC should have its own token and workspace.

For outside PCs, run the TLS listener on the SAME local queue.sqlite:

```powershell
$env:HUBBLE_QUEUE_TOKEN = '<separate random administrator secret, at least 24 characters>'
.\.venv\Scripts\python.exe secure_coordinator.py --db "$HOME\HubbleScout\queue.sqlite" serve --cert C:\certs\fullchain.pem --key C:\certs\privkey.pem
```

Use a valid hostname certificate and reachable HTTPS port 8766. Do not expose
GUI HTTP port 8765, which is localhost-only. Domain routing, certificate issuance
and firewall changes are deployment-specific and have not been performed here.
The two listeners share queue state; do not run older coordinator versions against
it. Stop every old app/worker instance before upgrading.

## Every worker PC

Install AI dependencies (`setup.ps1 -AI`), enter its unique token and
`https://YOUR-HOST:8766`, then click **Join adaptive pool**. Alternatively:

```powershell
$env:HUBBLE_QUEUE_TOKEN = '<this PC worker credential>'
.\.venv\Scripts\python.exe cluster_worker.py --server https://YOUR-HOST:8766 --stages archive_band archive_review mast wiki --work-dir work\adaptive
```

The coordinator fills available investigation slots first, then assigns Wikipedia
jobs. One task must finish before the worker changes roles. The coordinator PC can
also participate using **Join local pool**. A private CA can be supplied through
HUBBLE_CA_BUNDLE; TLS verification is never disabled. Tokens entered manually are masked. Invite files hold the per-PC token. Store any externally managed
worker startup credentials securely; Telegram credentials are a different secret.

The **Search SpaceCatalog in MAST** button seeds 72 independent 5-degree RA archive
queries. Local and invited PCs claim these jobs from the shared queue. Use **Collect
results** after they finish; it merges the returned observations and performs the
catalog footprint crossmatch and STScI Papertrack lookup on the coordinator. Raw
archive results are saved to `results/archive_observations.jsonl`.

MAST investigations require two distinct bands whose common footprint covers the
target center. Only archive JPEG/PNG previews are downloaded, never science FITS.
After the SpaceCatalog scan, choose **Queue no-paper targets for PC review** on the
coordinator. Connected adaptive workers process those targets in parallel. Review
results are exported to `results/archive_review_results.jsonl`. The bibliography
finding is proposal-level evidence and workers do not determine object novelty.
DINOv2 executes on the investigating PC; its diversity score is relative to that
worker's local preview collection, not a globally comparable novelty score. The
worker sends observations and the report to the coordinator. Preview files remain
on the worker. MAST workers wait five seconds after each completed task.

## Encrypted, coordinator-only Telegram

On the coordinator choose **Encrypted Telegram settings** under Workers, and enter
the bot token and destination chat ID. Or run `python secret_store.py`, which uses
a hidden token prompt. Settings are stored under the current user's LocalAppData,
outside this repository, using Windows DPAPI current-user encryption. They are
never sent to a worker or included in a task payload. Previously documented
Telegram environment variables are no longer used; remove them from startup scripts.

The coordinator also polls Telegram for commands in the configured private chat.
Send `/help` for commands: `/status`, `/queue`, `/workers`, `/recent`,
`/candidate OBJECT_ID`, `/obsid OBSERVATION_ID`, `/search`, `/collect`,
`/pause [all|archive|wiki]`, `/resume [all|archive|wiki]`, `/limit 1-10`, and `/test`.
Run one coordinator process per bot token; another `getUpdates` poller can cause a
Telegram 409 conflict. `/pause` stops new task claims, while already-running tasks
finish normally. The workers list shows active and recently polling PCs. Target
details include MAST observation IDs, filters, instruments, and exposure times.

DPAPI protects the saved file, not a compromised coordinator: software running as
the same Windows user can decrypt it. Keep the coordinator account trusted.
Reference: https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata

Only the coordinator's notification service loads the token. It creates a durable
outbox entry for each accepted matched MAST job and sends after the investigation
finishes. Messages use the catalog object and recomputed footprint match, not a
worker-supplied arbitrary message. Workers' astronomy results still require trust
and review; access restrictions cannot prove remote evidence is truthful.

The GUI or TLS coordinator must stay running to drain the outbox. Multiple local
listeners atomically share it. Failed sends are marked `failed`. A crash during
sending leaves `sending`, meaning delivery is uncertain. Neither is retried
silently, to avoid duplicate alerts. Without configured encrypted settings, entries
remain `pending`; the live list and desktop bell still work. Saved credentials do
not trigger a test message automatically.

## Live list and recovery

The local Live List refreshes every three seconds, showing up to 2,000 recent
attribution gaps, MAST state, matched filters, processing and delivery status.
It is a live table and bell, not a Windows push notification service. Publication
status always remains not assessed. Missing previews and processing failures are
explicit; they do not establish image quality or novelty.

Revoke a worker from the coordinator:

```powershell
.\.venv\Scripts\python.exe secure_coordinator.py --db "$HOME\HubbleScout\queue.sqlite" revoke PC-NAME
```

Reissuing the same name also revokes its old credential. Confirm an expired worker
has stopped before manually requeueing its held task. Separate databases do not
coordinate. Install this updated source on every PC, not only the coordinator.

## Validation

84 tests pass on Windows, including real HTTP adaptive assignments (two MAST jobs
and one Wikipedia fallback at backlog 10), duplicate fencing, draining on scale-down,
credential roles, centralized idempotent notifications, and actual Windows DPAPI
round-trip/tamper rejection. DINOv2 was previously run on CPU with a synthetic
preview. Internet TLS deployment, multiple physical PCs and live Telegram delivery
still need your deployment configuration and have not been live-tested here.
