# Phase 6 — R-01 leg splits + R-02/R-05 QA upgrades

Dense machine-readable companion: `PHASE6_SPLITS.json`.

## What changed

- **R-01 (leg chains)**: `work/rb_buster/override.json` part_map iterated
  2-way → 3-way → **full per-member** (8 single-member parts per leg chain,
  `leg_{l,r,f}0..7`). `assembly_config`: `max_parts=40`,
  `keyframe_max_stops=50`. Result: 36 parts; all legs rigid by
  construction; PASS3c vacuous for legs.
- **R-02 (scissor severity)**: `tools/rigid_plate_qa.py` FK_SCISSOR_CLIP
  now reports `pairs=N` (tri-tri overlap count) per island pair per pose.
- **R-05 (seam false positives)**: new `_islands_close()` — BVH
  `find_nearest` sampling (≤32 pts, ≥2 within pad) gates
  DISJOINT_KINEMATIC_SEAM; conservative AABB fallback when island lacks
  polys. Measured min surface distance appended to each seam line.
- **PASS4**: prefers emitted chained `quat` over euler `rot`
  reconstruction (zero measured delta; removes euler branch-hop risk on
  >180° swing hinges).

## Measured results (buster, 36 parts)

| Gate | Phase 5 | Phase 6 |
|---|---|---|
| PASS4 worst probe | 0.1015 m (leg_l) | **0.0214 m** (0 EXCEEDS) |
| PASS4 EXCEEDS (>0.03) | several | **0** |
| PASS3b max vert | ≤0.0001 m | ≤0.0001 m (all 36) |
| PASS3c leg residual | 0.18–0.64 m/member | vacuous (no multi-member legs) |
| WARN_NONRIGID_CHAIN legs | 3 chains | 0 |
| Mech seams | 121 | **2** (both genuine ~5mm proximities) |
| Mech scissor | 41 | 41, now `pairs=` ranked |
| CLB / FK_PEN / MISMATCH | 0 / 0 / 0 | 0 / 0 / 0 |

Keyframe starvation was the distal-link limiter: same parts went
0.257 m → 0.173 m → 0.047 m → 0.021 m as `keyframe_max_stops` went
5 → 12 → 30 → 50. Terminal links still cap at 50.

## Remaining residuals

See `PHASE6_SPLITS.json → remaining_issues`: 41 scissor clips need
design classification (pairs ranking now available; foot-vs-foot
cross-limb is the suspicious class), 2 genuine seams on mech shoulder
islands, 36-part/50-kf runtime cost is latent until a consumer exists,
and leg_f0-2 spin classification needs a visual check vs the reference.

Evidence renders: `buster_PHASE6_{folded,mid,deployed}_iso.png`.
