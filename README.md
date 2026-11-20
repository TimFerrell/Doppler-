# dopplergeo — acoustic aircraft Doppler geolocation

Recovers where an audio recording was made by matching the Doppler curves of propeller-aircraft
engine tones (fundamentals ~80–300 Hz) against ADS-B flight tracks. The methodology is
reconstructed from a public educational video (colsto, *"I discovered a sound that leaks your
exact location"*); no original code was available.

> **Authorization constraint (part of the spec):** only recordings **owned by, or explicitly
> authorized to, the operator** may be analysed. `measure`, `locate` and `locate-area` refuse to
> run unless `--attest-authorized` is passed or `authorization.attest_owned_or_authorized: true`
> is set, and the attestation is written into every report. Synthetic fixtures are exempt.

## Status

| Phase | State |
|---|---|
| 0 — synthetic end-to-end | **built; acceptance passes** (see "Phase 0 results") |
| A — real self-recorded data | **built; passes on a file-based synthetic fixture; awaiting the operator's real recording for acceptance** |
| B — area search | **not started** — gated by the brief on Phase A passing on a real recording (`locate-area` exits with that message) |

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
pytest -q            # offline, deterministic, ~1-2 min on 4 cores
```

## Web interface

```bash
dopplergeo serve            # then open http://127.0.0.1:8765/  (run from the repository root)
dopplergeo --config my.yaml serve --port 8765 --jobs-dir data/out/web
```

A local web front end to the same pipeline, with five tabs:

* **Locate**: choose a recording (from your recordings, or upload audio or **video**: MOV/MP4 audio is
  extracted with the bundled ffmpeg and the start time is read from its metadata) and ADS-B data (a
  library dataset, or upload a `.parquet` or tar1090/readsb logs). Set the search region by clicking the US
  map, then add optional start time, elevation/DEM, flyover times and a known location.
* **Record**: record from this computer's microphone (echo cancellation, noise suppression and auto-gain
  off; raw 16 kHz PCM; live 0–500 Hz spectrogram). The UTC start comes from the computer clock. You can
  also **import a video or audio file** into the recordings library.
* **ADS-B data**: the data library. *Rehydrate* past days from the adsb.lol open archive for a box or
  a **whole state** (e.g. Florida), optionally **kept updated daily**. *Collect live* from your own
  receiver or the adsb.lol / airplanes.live point APIs. *Import* receiver logs. Preview any dataset on
  the map. Sources and trade-offs: [docs/adsb_sources.md](docs/adsb_sources.md).
* **Measure**: Doppler-curve fits only. **Synthetic demo**: Phase 0 validation.

Results show on a **US map**: candidate cells coloured by score, the flight tracks (those used for the
#1 answer highlighted), the search region, the best point and the known location, plus a US overview
inset. The basemap is bundled (US Census state/county outlines via us-atlas, Natural Earth places;
Leaflet 1.9.4), so viewing results sends nothing anywhere. An optional *Street map* layer (OpenStreetMap
tiles) is off by default and asks before loading, since it reveals the viewed area to openstreetmap.org.

Jobs run one at a time with a live log. Each gives a report, CSVs and a results `.zip`, and jobs survive
restarts. Every job runs the same CLI commands as the command line; the authorization attestation is a
required checkbox. The server binds to `127.0.0.1` by default and goes online only for rehydrating and
live collection. Binding to another address logs a warning: there is no login.

## CLI

```bash
dopplergeo synth-demo --out data/out/synth [--seeds 1,2,3]
dopplergeo ingest-adsb --src DIR_OR_FILE --out data/adsb/day.parquet [--type-db icao_types.csv]
dopplergeo measure --audio rec.wav [--events "61.5,981"] [--out measurements.yaml] --attest-authorized
dopplergeo --config my.yaml locate --audio rec.wav --region center=LAT,LON,radius_km=R \
           --adsb data/adsb/day.parquet --out data/out/run1 --attest-authorized [--truth-latlon LAT,LON]
dopplergeo locate-area ...   # Phase B: not built yet (gated)
dopplergeo serve [--host 127.0.0.1] [--port 8765] [--jobs-dir data/out/web]
dopplergeo fetch-adsblol --date 2026-11-18 [--end-date ...] --bbox 24.2,-87.8,31.2,-79.8 --out data/adsb/library/fl
dopplergeo collect --source receiver|adsb.lol|airplanes.live ... --dataset NAME
dopplergeo extract-audio --video clip.mov --out clip.wav     # prints the recording time from metadata
```

Every run writes `out/<run>/report.md` (inputs + SHA-256, attestation, per-event fits, candidate
counts per stage, ranking table, margins, timings, plots), `candidates.csv` (all scored points,
QGIS-importable: `lat`,`lon` columns, EPSG:4326), `best_per_combination.csv`, and PNGs
(`overlay_e*.png` spectrogram + modeled/true curves, `heatmap.png`, `heatmap_zoom.png`).

All paths and constants live in the config (`config.example.yaml` mirrors the defaults exactly;
unknown keys are rejected).

### Phase A workflow (your own recording)

1. Record with a phone/mic at a known spot, note the start time (UTC) if you can.
2. Export your receiver's logs: tar1090 `history_*.json` or a series of readsb `aircraft.json`
   snapshots (plain or `.gz`), then `dopplergeo ingest-adsb --src <dir> --out data/adsb/x.parquet`.
   Rows with a position but no `gs`/`track`/vertical rate **fail the ingest loudly**; set
   `adsb.on_missing_velocity: drop` to discard them explicitly.
3. Config: `audio.start_utc` (optional, ISO-8601 with `Z`; only used to pre-select candidate
   flights), `elevation.dem_path` (GeoTIFF, MSL metres) or `elevation.constant_m`,
   `authorization.*`, and `detect.manual_times_s` if auto-detection misses a flyover.
4. `dopplergeo locate ... --truth-latlon <your known location>` → the report states the #1 error,
   the rank of the true spot, the margin to #2, per-event fits and wall time.

## Physics (as implemented)

* ECEF via pyproj (WGS84). ADS-B altitude and DEM elevation are both treated as ellipsoidal
  heights; their common geoid offset cancels to centimetres in slant range.
* Slant distance `D = |r_ac − r_obs|`; radial velocity `v_r = v·(r_obs − r_ac)/D` (> 0 approaching);
  `f = f0·c/(c − v_r)`; `c = 343 m/s` or `331.3·sqrt(1 + T/273.15)`.
* **Retarded time:** `t_e = t_h − |r(t_e) − r_obs|/c`, fixed-point iteration from
  `t_h − D(t_h)/c`, stop at `|Δ| < 1e-4 s`, ≤ 20 iterations; iterates are clamped to the track
  coverage (the map is a contraction, so this preserves convergence) and solutions outside it are NaN.
  Verified against the straight-line closed form to < 1 ms (unit test), and shown to be essential:
  the naive curve is not a time-shifted version of the right one (shape error > 1 Hz after the
  best shift) and scores < 50 % of the retarded curve at the true location (tests).
* Tracks: per flight, resampled to a uniform 1 s grid by linear interpolation in ECEF (positions)
  and ENU→ECEF-rotated ADS-B velocities; gaps > 30 s split segments; `gs/track/baro_rate` →
  m/s with the brief's factors.
* Stage-0 fit: per-column ridge maximum (parabolic sub-bin) in the band, robust
  `least_squares` (soft-L1) of the straight-line model `(f0, v, d, t_cpa)` with bounds and
  multi-start. The model is evaluated at the **retarded** time via the closed form, so it is exact
  for straight passes and `t_cpa_heard = t_cpa + d/c` is the time the `f = f0` sound is heard.
* Scoring: summed whitened brightness under the modeled curve; event 1 sweeps dt ±3 s and
  f0 ±1.5 Hz; events i>1 are **locked**: `offset = measured_cpa_1 − modeled_retarded_cpa_1`,
  event i placed at `modeled_retarded_cpa_i + offset`, f0 sweep only. Total = Σ events;
  best point per flight combination, then global rank.

### Deviations / additions to the brief (all configurable)

* **df sweeps f0, multiplicatively.** The heard curve is proportional to f0, so a df shift
  rescales the curve by `(f0+df)/f0`. (An additive frequency shift was the first implementation
  and measurably biased the location.)
* **Brightness = linear power / per-row median** (`score.brightness: snr`) instead of dB: the
  incoherent matched-filter statistic. dB brightness let noise-dominated columns swamp the
  location signal. `db` is still available.
* **Sub-lattice polish + quadratic frequency interpolation.** The score peak in (dt, df) is a
  narrow diagonal ridge (~0.1 s × 0.1 Hz). On the brief's 0.1 s / 0.1 Hz lattice alone, and with
  linear between-bin interpolation (bin-centre bias ≈ 2 %), quantisation error was the same size as
  the score difference between the true point and one 100 m away. The sweep runs coarse (0.5/0.5)
  → the brief's 0.1/0.1 lattice → a 2-D ±1-step polish at 1/5 step; exact against an exhaustive
  sweep in tests.
* FFT zero-padding ×4 (`spectrogram.pad_factor`), window/hop as specified.
* Repo root is the `doppler-geo/` layout; extra modules: `config.py`, `synth.py`, `pipeline.py`.
* Python 3.13 in the build container (brief: 3.12; nothing 3.13-specific is used).

## Phase 0 results

See `docs/phase0_results.md` (regenerate with
`dopplergeo synth-demo --out data/out/synth3 --seeds 1,2,3`).

## Known limitations

* **Single-event along-track ambiguity.** With dt free over ±3 s, moving the observer along the
  track is almost exactly a time shift; only the aircraft's acceleration/climb breaks it. On
  single-event synthetic fixtures the true point is (essentially) the score maximum, but the
  along-track score profile is flat to ~1e-4 over ±300 m, so noise decides where the maximum lands.
  Multi-event locked scoring (Phase A) removes this; single-clip results should be read as a line
  segment, not a point.
* Tracks that revisit a spot (orbits, pattern work) are represented by one CPA per segment.
* One dominant ridge per event window is assumed (overlapping flyovers need manual windows).
