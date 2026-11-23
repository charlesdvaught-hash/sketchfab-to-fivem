# Testing — beta build renders for external diagnosis

Renders baked by the Sketchfab→FiveM pipeline vs official Sketchfab
thumbnails (REFERENCE_*.jpg — ground truth from the model pages).

## The question

`buster_drone` is published as a 5-part articulated prop (body, rotor_0/1,
thruster_tilt_0/1). In the REFERENCE image both ducted rotor rings flank
the body symmetrically at shoulder height. In `buster_assembly_deployed_*`
one nacelle sits under the hull and the right fan floats far off-body.
Is this a pivot offset, a wrong sampled animation frame used for
placement, or the source rest pose?

## Files

- `buster_assembly_{deployed,mid,folded}_{iso,side,top}.png` — multi-part
  assembly sampled at three animation times
- `sf_sf_buster_drone2_{body,rotor_0,rotor_1,thruster_tilt_0,thruster_tilt_1}_*.png`
  — per-part renders (each part is geometrically correct)
- `sf_sf_robot_humanoid_*.png` — humanoid prop build (shape OK, but grey
  vs the red/white REFERENCE — texture binding suspect)
- `military_mech_{000,045,090}.png` — mech source turntable (matches
  REFERENCE silhouette; published as rigid-bone ped, no render gate)
- `REFERENCE_*.jpg` — official Sketchfab thumbnails for the three models

## RESOLVED — double-rotation fix

`buster_FIXED_*.png` are the same assembly renders after canonicalising
part verts to pivot-local orientation (`blender_worker.py` parts loop:
verts now `D @ B⁻¹ @ (v − hinge)`, emitted basis compensated by `D⁻¹`
for `rest_rot`). Folded pose now matches REFERENCE_buster.jpg.
Root cause: verts carried baked world rotation W0 while the manifest
also emitted W0 as folded/initial_rot -> applied twice.

## RESOLVED (round 2) — pivot/body offset + UV export + tooling

`buster_FIX2_*.png` — after fixing `pivot_cfg` missing the `body_off`
grounding shift (spin+hinge branches) and hinge `dep_n` losing the
driver's travel delta. Measured part `.ydr.xml` bbox centres went from
~1.3 m (off-origin, rotor would ORBIT on spin) to ~0.01 m. Verified by
`tools/diagnose_trs.py` (PASS2/3: internal offset + lever-arm swing).
Manifest now bilaterally symmetric (thruster attach x = +0.803/-0.891).

`humanoid_FIXED_textured.png` — the grey render was NOT a missing
texture; the exported `.ydr` carried all-zero UVs. Sollumz 2.9 expects
the layer name `UVMap 0`; its helper created an empty shell under that
name while the real UVs sat in `UVMap`, and our fallback renamed the
real layer to `TexCoord0` (a name 2.9 doesn't read). New rescue path
(`WARN_UV_RESCUE_EMPTY`) drops the empty shell and renames the
data-bearing layer. Binary `.ydr` now shows UV range 0.028-0.974.

New tools (adapted from an external review deliverable):
- `tools/diagnose_trs.py` — 3-pass source-vs-export check
  (muted-vs-active NLA, .ydr internal offset, manifest lever arm)
- `tools/rigid_plate_qa.py` — rigid-bone ped QA: welded island purity,
  cross-lateral bind detection, seam adjacency vs skeleton hop distance,
  FK-isolation penetration sweep. Ran on military_mech: flagged a
  crotch-region cluster bound to SKEL_R_Thigh on the +x side
  (12 CROSS_LATERAL_BIND, 180 DISJOINT_KINEMATIC_SEAM hits).

Also: `--dump-preview` now activates clips (resolve_clip_playback) so
workbench pivots match the compile pose; render_check gate is honest
(`render_check_failed` recorded, never claimed); new advisory gate
`WARN_PART_PIVOT_OFFSET` fires when a part .ydr bbox centre >0.15 m.

## FULL PASS — all four beta models vs Sketchfab references

- `buster_FINAL_*.png` — folded + deployed both bilaterally symmetric,
  rings flanking hull, blades inside rings. Part .ydr centres ~0.01 m.
- `humanoid_FIXED_*.png` — full albedo (red/white atlas + decals) after
  the UVMap-0 rescue; matches REFERENCE_humanoid.jpg.
- `police_drone_*.png` vs `REFERENCE_police.jpg` — textured body, POLICE
  RECON decals, red lens ring, whip antenna all present. Diagnostic
  material doesn't reproduce the emissive blue lens glow.
- `mech_PED_iso.png` vs REFERENCE_mech.jpg — silhouette + texture
  correct (dark olive atlas, white chevrons land on the right plates).
  rigid_plate_qa flags a cross-lateral thigh cluster in the bone map
  (binding defect, invisible in a static render).
- `render_check.py` now also renders .ydd.xml peds and exits nonzero
  when a resource has nothing to render (no more vacuous passes).

## RESOLVED (Phase 3) — see PHASE3_RESULTS.md

Round 3 of external review (Argon). Fixes + measured results are in
`PHASE3_RESULTS.md`; the deltas on what Phase 2 documented:

- **Stale `bound_box` float bug** — `world_bbox` read pre-transform
  bounds after `data.transform(N)` without a `view_layer.update()`,
  so `body_off` lifted buster +1.315 m off the ground. Fixed; body
  `.ydr` now grounds at `min.z = 0.0`, `attach_offset` == recipe pivot.
- **Contract A verified exactly** — new PASS3b in `diagnose_trs.py`
  measures max-vertex manifest-vs-source error: **0.0000 m** on all four
  buster moving parts. (The tool is now 5 passes, not 3 — PASS1/2/3 +
  PASS3b indexed/chamfer max-vertex + PASS4 mid-pose lerp-vs-source.)
- **Mech cross-lateral cluster FIXED, not just flagged** — `Cube.006`
  relocked `SKEL_R_Thigh` → `SKEL_Pelvis`; `CROSS_LATERAL_BIND` 12→0.
  `nearest_bone` now derives the lateral sign from the armature rest
  pose (not hardcoded +x=L) and midline-straddling bboxes lock to
  `SKEL_Pelvis`. Islands are attributed to source parts via the new
  `stream/<asset>.partmap.json` (the `__part_*` vgroups don't survive
  `.ydd` export).
- **FK_PENETRATION** 1373→54 (real vert-containment, advisory);
  **DISJOINT_KINEMATIC_SEAM** 180→121 (all 2-hop joint-adjacency noise).
- **PASS4 measured the real motion gap** — buster hinge parts hit
  1.75–2.13 m mid-frame error: staggered sub-clip timing, not arc sag.
  The 2-pose manifest can't express it; the open design question
  (quantized keyframes vs `parent_part` hierarchy) is in MOTION_SPEC.md.
- Plus: hemisphere-slerp guard, NLA `is_solo` clear, `Color 1`
  attribute rescue (1-indexed — not `Color 0`), wider UV retarget.

## Phase 4 — adaptive manifest keyframes (`PHASE4_KEYFRAMES.md`)

- **Argon Option (c) implemented**: hinge parts emit
  `keyframes[{t,pos,rot}]` — adaptive SE(3) RDP stops (tol 0.02 m, cap 5;
  `keyframe_mode`: adaptive/uniform/off). Buster's stops cluster at
  t≈0.22–0.30 — the real deploy phase; spin/static parts get none.
- **D4-ramped basis**: `rest_rot` compensation slerps to identity across
  `t` — endpoints stay bit-identical to `folded_rot`/`deployed_rot`.
- **PASS4 is now tautology-proof**: probes between every adjacent stop +
  fixed sweep. Buster single-member hinges: **≤0.015 m** (was 1.75–2.13 m).
- **PASS4 tool fix**: true verts now carrier-relativized (`ref0@refk⁻¹`)
  — mid-clip carrier motion was phantom error.
- **Leg caveat**: 8-member chains plateau ~0.25 m — independently-animated
  segments can't live in one rigid `.ydr`; split via `part_map`.
- **All `bound_box` reads → vert min/max** (F-04; also fixes a latent
  snapshot-mode stale-bounds site).
- **FK_PENETRATION 54→0** via BVHTree 2-of-3-axis ray enclosure (F-05) —
  all prior hits were open-shell false positives.
