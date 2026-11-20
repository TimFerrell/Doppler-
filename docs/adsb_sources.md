# ADS-B data sources

What dopplergeo needs from ADS-B: per-aircraft positions with **ground speed, track and vertical
rate**, ideally every 1–2 s, plus the ICAO type code (to drop jets), for the time and area of
the recording. Checked November 2026.

## Recommendation

| Use | Source | How in dopplergeo |
|---|---|---|
| Past days, any US area (e.g. **all of Florida**) | **adsb.lol open archive**: free, ODbL, one release per UTC day | Data tab → *Rehydrate a past day* (state presets), with *Keep updated* for an ongoing daily feed. CLI: `dopplergeo fetch-adsblol` |
| Ongoing, best quality, your own recordings | **Your own receiver** (RTL-SDR + readsb/tar1090, ~$40–100) | Data tab → *Collect live* → receiver URL. CLI: `dopplergeo collect --source receiver` |
| Ongoing for an area, no receiver | **adsb.lol point API** (free, no key today) | Data tab → *Collect live* → adsb.lol. CLI: `dopplergeo collect --source adsb.lol` |
| Ongoing, alternative | airplanes.live point API (1 req/s, non-commercial) | same, `airplanes.live` (returned HTTP 403 from a cloud host in testing; may work from home) |

For a self-recorded test, start a live collector (receiver or adsb.lol) **before** you record, so
the full-cadence tracks exist. The archive is the fallback for any past day, and the only practical
way to cover a whole state.

## adsb.lol open archive (historical, daily)

* Where: GitHub releases of `adsblol/globe_history_<YEAR>` (e.g. `globe_history_2026`). One release per
  UTC day per instance, tag `v<YYYY>.<MM>.<DD>-planes-readsb-<variant>-<n>`, e.g.
  `v2026.11.18-planes-readsb-prod-0`; variants `prod`, `staging`, `mlatonly`. adsb.lol recommends
  `prod`, falling back to `staging` if prod looks short (dopplergeo does this).
* Assets: a tar split into parts `.tar.aa`, `.tar.ab`, … (2 GB each). 18 Nov 2026 was 3.57 GB in two
  parts. A day is published roughly a day later.
* Contents: `traces/<last 2 hex>/trace_full_<hex>.json` (gzip-compressed, no `.gz` suffix) and
  `heatmap/`. Trace format (readsb `README-json.md`): top level `icao`, `r`, `t` (type), `desc`,
  `timestamp`; each point `[dt, lat, lon, alt_ft|"ground"|null, gs_kt, track, flags, vrate_fpm,
  aircraft|null, source, geom_alt_ft, geom_vrate_fpm, ias, roll]`; `flags & 8` means geometric altitude.
* Cadence: traces are thinned. A point is kept on change, about every 20 s in straight flight, so
  archive datasets use a 120 s track-gap allowance instead of 30 s. Expect somewhat weaker results
  than with full-rate live data, because curving flight between points is interpolated linearly.
* Volume measured here: a 0.7° × 0.9° Denver box gave 452,261 rows (2,691 flights) for one day;
  streaming and filtering the whole day took about 5 minutes on a fast link.
* License: **ODbL 1.0**. Attribute adsb.lol; databases you derive and share must stay open.
* Docs: https://www.adsb.lol/docs/open-data/historical/

## adsb.lol live API (ongoing, by area)

* `GET https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}` (radius ≤ 250 nm). Responses are
  readsb-style `ac` entries with `gs`, `track`, `baro_rate`/`geom_rate`, `alt_geom`, `t` (type),
  `seen_pos`; `now` is in **milliseconds**.
* No key required at present (adsb.lol says a feeder-based key may be required in future). Rate limits
  are dynamic: polling every 2 s drew HTTP 429, so dopplergeo polls no faster than every 5 s and backs
  off on 429.
* Privacy: the provider sees the area you poll.
* Docs: https://api.adsb.lol/docs

## airplanes.live API

* `GET https://api.airplanes.live/v2/point/{lat}/{lon}/{radius_nm}`, 1 request/s, non-commercial,
  no SLA (https://airplanes.live/api-guide). Same response shape as adsb.lol.

## Your own receiver

* readsb or dump1090-fa with tar1090 serves `aircraft.json` (for example
  `http://<pi>/tar1090/data/aircraft.json`, or `/data/aircraft.json`). Poll it every second for full-rate
  tracks with nothing leaving your network. tar1090 also keeps `history_*.json`; import those in the
  Data tab.
* ADS-B Exchange's guide to recording a local receiver: https://www.adsbexchange.com/?p=39245

## Other sources considered

* **OpenSky Network**: full history through Trino (SQL), free for research after an access request.
  Coverage over Florida is thinner than the community aggregators. Not integrated.
* **ADS-B Exchange**: historical `readsb-hist` / traces. Only the 1st of each month is free (samples);
  the rest is commercial. Not integrated.

## CLI quick reference

```bash
# all of Florida for a day range, appended into one library dataset
dopplergeo fetch-adsblol --date 2026-11-15 --end-date 2026-11-18 \
  --bbox 24.2,-87.8,31.2,-79.8 --out data/adsb/library/adsblol-florida

# ongoing: your receiver, or adsb.lol around a point (Ctrl-C to stop)
dopplergeo collect --source receiver --url http://adsb.local/tar1090/data/aircraft.json --dataset receiver-home
dopplergeo collect --source adsb.lol --center 28.54,-81.38 --radius-nm 40 --interval 5 --dataset orlando-live
```

The web server re-checks subscribed archive datasets every hour while it runs. For unattended daily
updates without the server, schedule `fetch-adsblol` for yesterday's date with cron or launchd.
