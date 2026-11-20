# dopplergeo: notes for Claude Code / agents

Acoustic aircraft Doppler geolocation. It measures the Doppler pitch curve of propeller-plane
flyovers in a recording and matches them against ADS-B tracks to estimate where the recording was
made. Python 3.12+, Flask UI, numpy/scipy DSP, pandas/pyarrow parquet data layer. There is no
duckdb or polars in the data path. Read `README.md` (usage, physics) and `docs/adsb_sources.md`
(data sources) first.

**Only recordings owned by, or authorized to, the operator may be analysed.** `measure`/`locate`
refuse to run without the attestation (flag/config/UI checkbox). Keep that gate.

## Code map (src/dopplergeo)
- `model.py`: retarded-time forward model (core physics). `measure.py`: ridge, straight-line fit,
  flyover detection. `score.py`: brightness scoring with dt/f0 sweeps. `pipeline.py`: grid search,
  locked multi-event scoring, `run_locate`. `report.py`: report.md and plots.
- `adsb.py`: unified schema, receiver-log ingest, `read_table` (prunes `day=`/`tile_` files by
  bbox/time), track resampling. `sources.py`: adsb.lol archive fetch (world or bbox,
  `--skip-jets`, tiles written as `*.partial` and renamed when complete), live collectors, `Library`
  (meta.json under a file lock), `ArchiveScheduler`.
- `audio.py`: audio and video loading (bundled ffmpeg via imageio-ffmpeg), container-metadata
  start time. `web/`: Flask app, templates, static (Leaflet, US boundaries, recorder).

## CLI (exact)
`synth-demo --out DIR [--seeds 1,2,3]` · `ingest-adsb --src --out` · `measure --audio [--events]` ·
`locate --audio --region center=LAT,LON,radius_km=R --adsb PATH --out DIR [--truth-latlon]` ·
`locate-area` (Phase B: deliberately not built, exits) ·
`fetch-adsblol --date D [--end-date D2] [--bbox S,W,N,E|world] [--skip-jets] --out DIR` ·
`extract-audio --video --out` · `collect --source receiver|adsb.lol|airplanes.live ...` ·
`library-register --name NAME [--bbox ...] [--region ...] [--skip-jets] [--subscribe]` · `serve`.
Real adsb.lol tags look like `v2026.11.19-planes-readsb-prod-0`. Releases appear about a day after
the covered UTC day. Each day is one ~3.6 GB world download (2 parts), whatever bbox you keep.

## Web UI tabs
Top steps: 1 get flight data, 2 record, 3 locate, 4 read the result. Tabs: **3 · Locate**,
**2 · Record** (browser mic, video import), **1 · Flight data** (library, rehydrate incl. "Whole
world", live collect, import), **Measure**, **Synthetic demo**. Job pages show stage progress and a
"What this means" explanation. No login: LAN only.

## Tests
`python -m pytest -q tests`: offline, about 1–2 min, 47 tests at the time of writing. Phase-0 check:
`dopplergeo synth-demo --out /tmp/s --seeds 1` should print "seed 1: #1 error 55.8 m".

## Library data rules
- Layout: `library/<name>/day=YYYY-MM-DD/tile_<lat>_<lon>.parquet` (10° tiles, zstd) plus
  `meta.json`. Datasets filled by the CLI need `dopplergeo library-register --name <name> ...` to
  appear in the web UI; it also rebuilds `days` from the `day=` folders.
- Re-fetching a day deletes its `day=` folder first. **Never run two writers on the same day.**
  Day-disjoint parallel fetches into one dataset are safe (since commit b2d06ba).
- Archive tracks are thinned (~20 s per point in straight flight), so archive datasets use
  `max_gap_s: 120`.

## Deployment: Unraid host "Tower" (192.168.0.18), as reported 2026-11-20
- Repo `/mnt/cache/dopplergeo/src`, branch `claude/acoustic-aircraft-doppler-geo-eukxq4`.
- Image `dopplergeo:local`, built locally: `docker build -f Dockerfile.local -t dopplergeo:local
  /mnt/cache/dopplergeo/src`, then recreate the container (the dockerMan "Update" button cannot
  rebuild it). Container `dopplergeo` runs as 99:100, `--memory 20g`, restart unless-stopped,
  healthcheck on :8765. Template: `/boot/config/plugins/dockerMan/templates-user/my-dopplergeo.xml`.
- Volumes: `/app/data/adsb` → `/mnt/user/adsb` (share `adsb`, cache-prefer);
  `/app/data/audio`, `/app/data/out`, `/app/data/dem` → `/mnt/cache/dopplergeo/data/...`.
  Keep CWD=/app: relative default paths resolve into the mounts.
- Everything Python runs inside the container: `docker exec dopplergeo dopplergeo <cmd>`,
  `docker exec dopplergeo python -m pytest -q tests`.
- **Recreating or restarting the container kills running backfill workers** (they are `docker exec`
  children). Check `/mnt/cache/dopplergeo/data/out/fetch-*-parallel/status.txt` first.
- Backfill driver scripts live in `/mnt/cache/dopplergeo/scripts/` (host, not in git). 8 concurrent
  day-disjoint workers are CPU-bound on the 5600G (6C/12T, ~35–47 MB/s aggregate).
- Unraid: `/root` and `/tmp` are RAM (volatile). Durable: `/mnt/cache`, `/mnt/user`, `/boot`. The
  container has no `ps`: scan `/proc/*/cmdline`, excluding your own PID. Killing the host-side
  `docker exec` client does not reliably kill the process inside the container.
- The host runs many containers (Plex :32400, SAB :8080, OWUI :4242, ...): check `docker ps`
  before publishing ports. Never expose or proxy the UI beyond the LAN.

## Datasets on Tower (2026-11-20)
- `library/us`: CONUS bbox `24.4,-125.0,49.5,-66.5`, jets kept, 30-day backfill
  2026-10-21..2026-11-19 in progress. Measured here: ~1.7 GB/day, ~51 M rows, ~43 k aircraft per day.
  After it finishes: `docker exec dopplergeo dopplergeo library-register --name us
  --bbox 24.4,-125.0,49.5,-66.5 --region "Continental US"`.
- `library/florida`: superseded partial run (~360 MB). Delete only with the owner's OK.

## Open items
- Phase B / date-unknown search (search a date range): designed in chat, not built.
- Alaska/Hawaii are outside the CONUS box: use `--bbox world` (optionally `--skip-jets`) if wanted.
- First real-recording test (Phase A acceptance) against `library/us`.
