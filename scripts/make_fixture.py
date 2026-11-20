#!/usr/bin/env python
"""Synthesize a validation fixture (no external data).

  phase0  : one curved, accelerating prop pass; 120 s mono 16 kHz WAV (f0 + 2 harmonics + white
            noise), unified-schema ADS-B JSONL, truth.yaml, true_curve_*.csv
  phase-a : two audible prop passes (+ an inaudible prop and a jet in the ADS-B), WAV, tar1090
            history_*.json snapshots, truth.yaml and a locate config.yaml

Usage: python scripts/make_fixture.py --kind phase0 --seed 1 --out data/audio/fixture_1 [--config cfg.yaml]
"""
import argparse

from dopplergeo import config, synth

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--kind", choices=["phase0", "phase-a"], default="phase0")
p.add_argument("--seed", type=int, default=1)
p.add_argument("--out", required=True)
p.add_argument("--config")
p.add_argument("--snr-db", type=float, help="override synth.snr_db")
a = p.parse_args()
cfg = config.load_config(a.config)
if a.snr_db is not None:
    cfg["synth"]["snr_db"] = a.snr_db
c = config.speed_of_sound(cfg)
if a.kind == "phase0":
    t = synth.make_phase0_fixture(a.out, a.seed, cfg, c)
else:
    t = synth.make_phase_a_fixture(a.out, a.seed, cfg, c)
print(f"wrote {a.out}: observer {t['observer']}")
