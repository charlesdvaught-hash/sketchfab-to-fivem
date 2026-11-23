---
name: sketchfab-pipeline
description: Sketchfab→FiveM converter invariants — Sollumz 2.9 attribute names, Blender 5.2 bone math, NLA evaluation, Contract A transforms, and the QA gate run order. Use when touching blender_worker.py, render_check.py, tools/, or diagnosing any export defect.
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

# Sketchfab→FiveM pipeline — proven invariants

Every item below has a measured receipt in `testing/`. Trust these over
external references for THIS toolchain (Blender 5.2.1 LTS + Sollumz 2.9.0);
docs elsewhere describe other versions.

## Sollumz 2.9 attribute naming (measured, not docs)

- UV layers must be named `UVMap 0` — **zero-indexed, with a space**.
  `get_uv_map_name(i)` → `f"UVMap {i}"`. NOT `TexCoord0` (that name is
  dead to the 2.9 exporter — exports all-zero UVs, whole mesh samples one
  atlas pixel → flat color renders).
- Vertex-color attrs are `Color 1`, `Color 2`, … — **1-indexed** via
  `get_color_attr_name(i)` → `f"Color {i+1}"`. NOT `Color 0`.
- **Empty-shell trap**: Sollumz helpers create an EMPTY layer under the
  expected name while real data sits under `UVMap`/`COLOR_0`. Rescue =
  drop the empty shell, rename the data layer to the expected name,
  mark it active/active_render, retarget `ShaderNodeUVMap.uv_map`.
  Logged as `WARN_UV_RESCUE_EMPTY`. Detect "empty" by all-zero coords
  (max |u|,|v| < 1e-6), NOT bbox span — palette UVs are legitimately flat.

## Blender 5.2 API pitfalls

- `Bone.matrix` is **3×3** parent-relative (was 4×4 in older versions) —
  any `4x4 @ 3x3` op throws. Use `bone.matrix_local` (4×4 armature-space
  rest matrix) for FK skinning deltas:
  `M_delta = pose_bone.matrix @ bone.matrix_local.inverted()`.
- glTF import leaves animation strips **muted on NLA tracks**, not in
  `ad.action`. Before sampling pivots/poses: `resolve_clip_playback`
  (unmute strips+tracks, clear `is_solo` — a soloed track suppresses the
  active action — set `ad.action`) AND `scene.frame_set(f_start)` to
  match the compile pose. Muted-pose measurements ≠ compile-pose.
- `obj.bound_box` is a **lazy RNA cache** — NOT invalidated after
  `mesh.data.transform()` / vertex mutation until a depsgraph pass.
  All bounds reads use `vertices.foreach_get('co')` min/max + 8-corner
  `matrix_world` transform (F-04: two separate stale-bounds bugs found).

## Contract A transform math (our choice — do not mix with Contract B)

- Canonicalized verts: `v = D_rot @ (B⁻¹ @ (v − hinge_n))` — each part's
  verts live in pivot-local orientation.
- Emitted manifest rotations are **absolute basis**, not deltas;
  `folded_rot`/`deployed_rot`/`initial_rot` all share one frame.
  `rest_rot` stays inside canonical verts; emitted basis compensated
  by `D_rot⁻¹`.
- `pivot_cfg` pivots MUST get `+body_off` (verts get it — a pivot that
  doesn't leaves ~1.3 m internal offsets: orbit-instead-of-spin).
- Deployed hinge: `dep_n = hinge_n + N.to_3x3() @ (W1_rel.to_translation()
  − hinge)` — preserves driver travel, not naive `body_off` shifts.

## Manifest consumer contract

- **Sequential** hemisphere-normalize for multi-stop tracks: for `i>0`,
  `dot(q[i-1],q[i]) < 0 → negate q[i]` — chaining per adjacent step keeps
  cumulative >180° tracks on the short path; anchoring to `q[0]` snaps
  the tail. The RDP evaluator applies the same rule per candidate
  interval, not just on the emitted list.
- Hinge parts carry `keyframes[{t,pos,rot,quat,repose?}]` — adaptive
  SE(3) RDP stops (`keyframe_mode`: adaptive/uniform/off;
  `keyframe_tol_m`, `keyframe_max_stops`). Interpolate piecewise: bracket
  `t` in `[t_i,t_{i+1}]`, lerp `pos`, slerp `rot`/`quat` at local alpha.
  `quat` is sequentially hemisphere-chained for direct nlerp.
  `keyframes[0]` ≡ folded, `keyframes[-1]` ≡ deployed — the
  `rest_rot`/`D_rot⁻¹` compensation slerp-ramps to identity across
  **motion progress** `s(k)` (not clock t), so the re-pose completes with
  physical deployment; `rest_rot` parts also emit `rest_rot_deg` +
  per-keyframe `repose` (= s_k). Absent `keyframes` → 2-pose fallback.
- Multi-member groups (`group_fit=procrustes`, default) use a weighted
  Kabsch whole-group delta (driver member ×`group_fit_proximal_weight`=3,
  normalized weights, det<0 fix, delta space) instead of the single
  driver matrix — halves joined-chain error; endpoint ε-snap (<1e-4)
  keeps rigid groups bit-identical; fitted groups emit group best-fit
  `deployed_*`. `WARN_NONRIGID_CHAIN` reports
  `interior_dev`/`endpoint_dev`; endpoint_dev >0.02 m is permanent →
  `part_map` split. Carrier refs are `_rigid_se3`-normalized everywhere.
- Mid-pose renders draw the piecewise path; `keyframes` stops are exact
  source poses in manifest space — only inter-stop interpolation is
  approximate. Joined parts with independently-animated members (buster
  legs, 8 segs/part) now plateau ~0.10 m interior / up to ~0.64 m
  distal at deployed — split them in `part_map` to go lower.

## QA gates — run order after any transform/export change

1. `python test_harness.py` — must print `ALL CHECKS PASSED`.
2. `tools/diagnose_trs.py` on the shipped resource — PASS1 muted/active
   NLA divergence ≈0, PASS2 `.ydr` part centers ≈0 (<0.05 m),
   PASS3 lever-arm swing ≈0, PASS3b indexed/chamfer max-vertex
   manifest-vs-source reconstruction ≈0 (0.0000 m on buster = Contract A
   exact), PASS4 interior probes — midpoints between every adjacent
   keyframe stop + fixed sweep, piecewise-predicted vs carrier-relative
   TRUE source (≤0.015 m on buster single-member hinges; ~0.10 m plateau
   on multi-member leg chains = non-rigid join limit, not interp error).
   PASS3c: deployed-pose per-member max-vert residual — honest endpoint
   error for non-rigid chains.
3. `tools/rigid_plate_qa.py` on ped `.ydd` — **weld verts first** (raw
   `.ydd` split normals = 1 island/face); flags: `CROSS_LATERAL_BIND`
   (±x geometry on contralateral `SKEL_L/R_*` = real defect),
   `DISJOINT_KINEMATIC_SEAM` (welded edge, bones >1 hop apart — 1 hop is
   a legitimate joint, do NOT flag), FK penetration (**BVHTree 2-of-3-axis
   bidirectional ray enclosure** on welded island faces — open C-shells
   can't false-positive; 54→0 on the mech) **plus `FK_SCISSOR_CLIP`**
   (`BVHTree.overlap` tri-tri pairs on posed islands — flat/open-sheet
   crossings the ray test can't see; 41 on the mech, unreviewed;
   `pairs=N` ranks severity — foot-vs-foot cross-limb is the
   suspicious class), `DISJOINT_KINEMATIC_SEAM` is gated by
   `_islands_close` BVH nearest-distance (≥2 sampled verts within pad;
   121→2 false-positive collapse on the mech),
   `PART_BONE_MISMATCH` (islands
   attributed via `stream/<asset>.partmap.json` — `__part_*` vgroups
   don't survive `.ydd` export).
4. Assembly renders vs `testing/REFERENCE_*.jpg` Sketchfab thumbs.
- `WARN_PART_PIVOT_OFFSET` (>0.15 m `.ydr` bbox-center) = pivot bug.
- `WARN_RENDER_CHECK` is advisory only; `render_check` must only appear
  in `checks_passed` on returncode 0 — gate dishonesty was a real bug.

## Pointers

`AGENTS.md` (file map + skills table) · `reference/RAGE_SOLLUMZ_CONTRACTS.md` ·
`reference/RIG_WORKBENCH.md` (workbench data contract) ·
`testing/PHASE2_DIAGNOSIS.md` + `testing/PHASE3_RESULTS.md` +
`testing/PHASE4_KEYFRAMES.md` (audit history +
measured results + evidence links).

## Installed reference packs (sibling skills, MIT)

Reference material installed 2026-11-22 — generic-Blender guidance, NOT
Sollumz/FiveM-aware. Our invariants above override them on any conflict.

- `blender-asset-validation/` (ifbars/blender-agent-studio @18330d1):
  `scripts/inspect_asset.py` runs on source `.glb` pre-conversion — per-mesh
  non-manifold edges, degenerate faces, loose elements, connected
  components, world bounds. Verified working headless under 5.2.1.
  `render_evidence.py` = standardized hero + 6-view + contact sheet.
  Its `references/quality-gates.md` has a `gate/defect/warning` taxonomy
  worth mirroring in failure reports.
- `blender-character-rigging/` + siblings `blender-core`, `blender-asset-qa`,
  `blender-animation`, `blender-export-pipelines` (EL4CTEO/blender-skills
  @1a1cc6c): `bind_mesh.py` = heat weights with automatic **voxel-proxy
  fallback** for non-manifold meshes (donor/autorig path candidate);
  `stress_poses.py` = deformation stress frames (complements
  `rigid_plate_qa.py` FK checks); `rig_check.py` profiles are
  unity/unreal/roblox only — **no GTA skeleton**, don't run it expecting
  SKEL_* validation. `blender-core/references/api-changes-5x.md` = more
  5.0 API deltas beyond the ones measured above.
