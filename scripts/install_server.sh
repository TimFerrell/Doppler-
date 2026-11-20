#!/usr/bin/env bash
# One-shot install of dopplergeo on a Linux server, with a systemd service for the web app.
# Usage (as the user that should own the data, with sudo rights):
#   bash scripts/install_server.sh [/path/to/data-drive]
# The optional argument puts data/ (ADS-B library, recordings, results) on that drive via a symlink.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 12), "need Python 3.12+"' \
  || { echo "Python 3.12+ required (set PYTHON=/path/to/python3.12)"; exit 1; }

if [ "${1:-}" != "" ]; then
  mkdir -p "$1/dopplergeo-data"
  if [ -d data ] && [ ! -L data ]; then cp -rn data/. "$1/dopplergeo-data/" && rm -rf data; fi
  ln -sfn "$1/dopplergeo-data" data
  echo "data/ -> $1/dopplergeo-data"
fi
git checkout -- data/icao_jet_types.csv 2>/dev/null || true
test -f data/icao_jet_types.csv || git show HEAD:data/icao_jet_types.csv > data/icao_jet_types.csv

"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q -e .
.venv/bin/python -m pytest -q tests -x -k "not integration" || echo "WARNING: some tests failed"

sed -e "s#__USER__#$(id -un)#" -e "s#__DIR__#$DIR#" deploy/dopplergeo.service \
  | sudo tee /etc/systemd/system/dopplergeo.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now dopplergeo
sleep 3
systemctl --no-pager --lines=5 status dopplergeo || true
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "dopplergeo is running: http://${IP:-<server-ip>}:8765/  (LAN only: it has no login)"
echo "Logs: journalctl -u dopplergeo -f     Update: git pull && .venv/bin/pip install -e . && sudo systemctl restart dopplergeo"
