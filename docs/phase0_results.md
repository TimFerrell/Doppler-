# Phase 0 / Phase A synthetic results

Generated on the build container (4 vCPU). Regenerate Phase 0 acceptance with
`dopplergeo synth-demo --out data/out/synth3 --seeds 1,2,3`.

## Phase 0 acceptance (brief defaults, seeds 1-3)

| seed | err_m | truth_rank_nearest | top_score | truth_score | truth_minus_top | wall_s | pass |
|---|---|---|---|---|---|---|---|
| 1 | 55.8 | 1 | 29531.9 | 29533.7 | 1.8 | 14.1 | True |
| 2 | 81.4 | 5 | 31187.6 | 31200.5 | 13.0 | 15.3 | True |
| 3 | 29.8 | 1 | 27019.8 | 27021.0 | 1.2 | 13.9 | True |

`err_m`: distance of the #1 candidate from the true observer. `truth_score`: score of the exact true
location (not a grid point). In all three runs the exact truth outscores every grid candidate.

## Phase 0 robustness (single event, more seeds)

| configuration | seeds | pass (≤150 m) | median err (m) | max err (m) |
|---|---|---|---|---|
| brief defaults (200→100 m, f0 only, dt ±3 s) | 13 | 6/13 | 166 | 315 |
| refine to 25 m (refine_levels: 3) | 10 | 6/10 | 102 | 235 |
| refine to 25 m + harmonics 1,2,3 | 10 | 7/10 | 45 | 188 |
| refine to 25 m + dt ±1 s (trusted clock) | 13 | 13/13 | 54 | 109 |

Single-event along-track position is weakly observable when dt is free over ±3 s: moving the
observer along the track is almost exactly a time shift, broken only by the aircraft's slight
acceleration/climb. The score profile along the track is flat to ~1e-4 over ±300 m, so the noise
realisation decides where the maximum falls. A trusted clock (smaller dt sweep) or a second event fixes it.

## Phase A (file-based synthetic: WAV + tar1090 history JSON, 2 audible props + inaudible prop + jet)

Auto-detected events, locked two-event scoring, 6 km region, 6 candidate flight combinations.

### brief defaults (200→100 m)

| seed | events | err_m | combo_ok | margin_vs_2 | wall_s |
|---|---|---|---|---|---|
| 1 | 2 | 35.8 | True | 0.5 | 60.6 |
| 2 | 2 | 96.7 | True | 0.5 | 60.9 |
| 3 | 2 | 134.8 | True | 0.5 | 71.2 |
| 4 | 2 | 43.6 | True | 0.5 | 32.5 |
| 5 | 2 | 20.2 | True | 0.6 | 37.3 |
| 6 | 2 | 109.5 | True | 0.5 | 30.9 |

### refine_levels: 3 (→25 m)

| seed | events | err_m | combo_ok | margin_vs_2 | wall_s |
|---|---|---|---|---|---|
| 1 | 2 | 32.9 | True | 0.5 | 45.9 |
| 2 | 2 | 40.9 | True | 0.5 | 44.3 |
| 3 | 2 | 96.0 | True | 0.5 | 48.7 |
| 4 | 2 | 39.6 | True | 0.5 | 43.8 |
| 5 | 2 | 8.8 | True | 0.6 | 49.3 |
| 6 | 2 | 56.3 | True | 0.5 | 43.9 |

`margin_vs_2`: relative score margin of the best point over the best point of the runner-up flight combination.
