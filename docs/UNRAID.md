# Self-host dopplergeo on Unraid

Deployed on Tower (192.168.0.18, Unraid, kernel 6.12.x) on 2026-11-20. Agent notes: `CLAUDE.md`.

**LAN ONLY.** The web UI on port 8765 has no login and no auth. Do not put it behind a reverse
proxy, do not port-forward it, do not expose it to the internet.

## How updates work

The git checkout on the cache pool is bind-mounted into the container at `/app`. The container's
default command, `dopplergeo supervise`, checks the GitHub branch every 30 s. When a new commit
appears it waits for running web jobs, fast-forwards the checkout, reinstalls Python dependencies if
they changed, runs a quick self-test, and restarts **only the web app**. A commit that fails the
self-test is rolled back and skipped. The container itself keeps running, so `docker exec` work
such as backfills is not interrupted.

- Status: the footer of every UI page, `curl -s http://127.0.0.1:8765/api/status`, or
  `/mnt/cache/dopplergeo/data/out/updater.json`.
- **Never edit code in `/mnt/cache/dopplergeo/src`**: local edits (or local commits) block
  auto-update until removed. Changes go to GitHub.
- The image only needs rebuilding when `Dockerfile.local` changes; the footer then says "image
  rebuild recommended". Rebuild and recreate at a quiet moment: recreating kills `docker exec`
  workers.

## Build (on the Unraid host)

The image is built locally; there is no registry.

    cd /mnt/cache/dopplergeo/src
    git fetch origin
    git switch claude/acoustic-aircraft-doppler-geo-eukxq4
    git reset --hard origin/claude/acoustic-aircraft-doppler-geo-eukxq4   # only if the checkout must be reset to GitHub
    chown -R 99:100 /mnt/cache/dopplergeo/src
    docker build -f Dockerfile.local -t dopplergeo:local /mnt/cache/dopplergeo/src

Note: the stock 40 GB docker.img is too small for this build (~5 GB of layers on top of an existing
fleet). It was raised to 100 GB on 2026-11-20:

    /etc/rc.d/rc.docker stop && truncate -s 100G /mnt/cache/system/docker/docker.img \
      && /etc/rc.d/rc.docker start && sleep 5 && losetup -c /dev/loop2 \
      && btrfs filesystem resize max /var/lib/docker

## Run

    docker run -d --name dopplergeo --restart unless-stopped -u 99:100 \
      -p 8765:8765 -e TZ=UTC --memory 20g \
      --health-cmd 'curl -sf http://127.0.0.1:8765/ || exit 1' \
      --health-interval 60s --health-timeout 10s --health-retries 3 --health-start-period 60s \
      -v /mnt/cache/dopplergeo/src:/app \
      -v /mnt/user/adsb:/app/data/adsb \
      -v /mnt/cache/dopplergeo/data/audio:/app/data/audio \
      -v /mnt/cache/dopplergeo/data/out:/app/data/out \
      -v /mnt/cache/dopplergeo/data/dem:/app/data/dem \
      dopplergeo:local

The image's default command is
`dopplergeo supervise --interval 30 -- serve --host 0.0.0.0 --port 8765`.

If the GitHub repository is private, the supervisor needs a read-only token:
1. Put a fine-grained token with *contents: read* on this repository in
   `/mnt/cache/dopplergeo/secrets/gh_token` (owner 99:100, mode 600).
2. Add `-v /mnt/cache/dopplergeo/secrets/gh_token:/run/secrets/gh_token:ro` to the run command.
3. Append the command (Unraid template "Post Arguments"):
   `dopplergeo supervise --interval 30 --token-file /run/secrets/gh_token -- serve --host 0.0.0.0 --port 8765`

A dockerMan template mirroring this lives at
`/boot/config/plugins/dockerMan/templates-user/my-dopplergeo.xml`, so the container shows up
properly in the Docker tab (WebUI button, paths, etc.). The GUI "Update" button cannot rebuild a
local-only image: rebuild via the CLI first, then recreate the container (GUI or CLI).

## Data layout

| container path | host path | share | Unraid cache setting | holds | growth |
|---|---|---|---|---|---|
| /app | /mnt/cache/dopplergeo/src | `dopplergeo` | cache only | the git checkout (code) | small |
| /app/data/adsb | /mnt/user/adsb | `adsb` | **cache primary, array secondary** ("prefer", 100 GiB floor) | flight-track parquet library | ~1.7 GB/day (US, jets kept) |
| /app/data/audio | /mnt/cache/dopplergeo/data/audio | `dopplergeo` | **cache only** | recordings | small |
| /app/data/out | /mnt/cache/dopplergeo/data/out | `dopplergeo` | **cache only** | job output, reports, updater state | small |
| /app/data/dem | /mnt/cache/dopplergeo/data/dem | `dopplergeo` | **cache only** | terrain tiles (optional) | static |

Everything is owned by `nobody:users` (99:100); the container runs as 99:100. The `adsb` volume
goes through shfs (`/mnt/user/...`), so writes overflow to the array if the cache ever drops below
the floor; pyarrow mmap reads over shfs are verified working. The other paths are direct cache-pool
paths (fast, and the `dopplergeo` share's cache-only pin keeps the 6-hourly mover away).

## Verify after any change

    docker exec dopplergeo python -m pytest -q tests            # 52 passed (as of commit 4113d57)
    docker exec dopplergeo dopplergeo synth-demo --out /app/data/out/selftest --seeds 1
    # expect: "seed 1: #1 error < 150 m"  (55.8 m)
    curl -s http://127.0.0.1:8765/api/status                    # updater.status "up to date"
    curl -s -o /dev/null -w '%{http_code}' http://192.168.0.18:8765/   # 200

## ADS-B data: continental US library

The library used for locating is `library/us`: the continental US box `24.4,-125.0,49.5,-66.5`,
with jets kept. Each archive day is one ~3.6 GB download of the whole world. It is streamed and
filtered on the fly, so nothing but the filtered result is ever written to disk (about 1.7 GB per US
day).

Backfill or add days from the command line (day-disjoint parallel runs into one dataset are safe):

    docker exec dopplergeo dopplergeo fetch-adsblol \
      --date 2026-11-17 --end-date 2026-11-18 \
      --bbox 24.4,-125.0,49.5,-66.5 \
      --out /app/data/adsb/library/us

Make a CLI-filled dataset visible in the web UI and keep it updated daily (the web app fetches
each newly published day while the container runs):

    docker exec dopplergeo dopplergeo library-register --name us \
      --bbox 24.4,-125.0,49.5,-66.5 --region "Continental US" --subscribe

Progress of every download (UI or CLI) shows live in the UI's *Flight data* tab. `--skip-jets` keeps
jet traffic out of a library to save space (jets are dropped at locate time anyway); `--bbox world`
keeps every region. Archive days are published about a day after the UTC day they cover.
