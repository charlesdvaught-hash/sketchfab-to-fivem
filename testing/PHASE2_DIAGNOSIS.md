# Phase 2 — external-diagnosis follow-up: 4 more defects found + fixed

Round 2 of the Buster investigation, driven by two external reviews
(Argon: source-linked diagnosis; M Max: runnable diagnostic workbench).
All claims below are verified against code and emitted binaries — line
numbers refer to `blender_worker.py` / `render_check.py` /
`orchestrator.py` at HEAD.

## Fixed defects

### 1. Recipe pivots missing the grounding shift (the residual Buster bug)

`eval_baked_obj` bakes verts into world space; `body_off` (the
bottom-center grounding shift) is applied to every vertex — but the two
`pivot_cfg` branches (spin ~1597, hinge ~1618) used the raw recipe pivot
WITHOUT `body_off`. Result: every recipe-pivot part's `.ydr` carried an
internal offset of `B⁻¹ @ body_off` ≈ **1.3 m** from its declared origin.

Folded renders cancelled it (offset × rotation⁻¹ lands back), but:
- deployed poses swung the residual around the pivot
- rotor spin would **orbit** the fan around a point 1.3 m away instead
  of spinning in place — invisible in renders, broken in-game

Fix: `pivot_n`/`hinge_n` now add `body_off`; `dep_n` for the recipe path
keeps the driver's world travel delta (`hinge_n + N₃ @ (W1_rel.t − hinge)`)
instead of collapsing to the folded pivot.

Measured after fix — every part `.ydr.xml` bbox centre:

| part | before | after |
|---|---|---|
| rotor_0 / rotor_1 | ~1.33 m | **0.010 m** |
| thruster_tilt_0 / _1 | ~1.31 m | **0.011 m** |

Manifest attach offsets are now bilaterally symmetric
(thruster x = +0.803/−0.891, rotor x = +0.866/−0.955).

### 2. Preview vs compile pose mismatch (stale workbench pivots)

`run_dump_preview` never called `resolve_clip_playback` — the glTF
importer parks clips in MUTED NLA strips, so the workbench measured
pivots/islands in the muted pose while the compile baked the
activated-clip pose at `f_start`. Preview now activates clips and
evaluates at `frame_window()`'s start frame. (Buster showed zero
divergence on re-measure; the fix matters for assets whose muted/active
poses differ.)

### 3. `find_tex_image` early-return (render_check.py:141)

`if r:` on a `(None, None)` miss tuple is truthy — the first childless
material group short-circuited the whole texture search.

### 4. Dishonest render gate (orchestrator.py:1126)

`checks_passed` appended `"render_check"` unconditionally — even after a
nonzero exit or timeout. Now `render_check` only on returncode 0, else
`render_check_failed`. Additionally `render_check` used to vacuously
pass ped `.ydd` resources (no `.ydr.xml` to match → nothing rendered,
exit 0): `.ydd.xml` is now covered and a resource with zero drawable
xmls exits nonzero.

## The big one nobody called: all-zero UVs on EVERY export

The humanoid's flat-white render was not a missing texture — the
exported `.ydr` carried **all-zero UVs** (every loop = (0,0), so the
whole mesh samples one atlas pixel).

Root cause — naming convention drift: Sollumz 2.9's
`get_uv_map_name(0)` returns `"UVMap 0"` (with a space). Its
`mesh_add_missing_uv_maps` helper therefore created an **empty**
`"UVMap 0"` shell, while the real UVs stayed in the glTF-style `"UVMap"`
layer — and our fallback renamed the real layer to `TexCoord0`, a name
2.9 doesn't read either. The exporter wrote the empty shell.

`WARN_UV_RESCUE_EMPTY` now drops empty target shells and renames the
data-bearing layer, using the addon's own `get_uv_map_name` so the fix
tracks Sollumz versions. Fired on **all four** test models — the bug
was pipeline-wide, invisible wherever renders use flat colors.

## New permanent checks + tools

- `WARN_PART_PIVOT_OFFSET` (orchestrator): advisory warning when a part
  `.ydr` bbox centre is >0.15 m from origin — would have caught both
  Buster-era bugs automatically.
- `tools/diagnose_trs.py` (vendored from M Max, runnable): 3-pass
  source-vs-export check — muted-vs-active NLA divergence, `.ydr`
  internal offset, manifest lever-arm swing. Buster receipt:
  PASS1 zero divergence, PASS2 ~0.01 m centres, PASS3 0.015 m.
- `tools/rigid_plate_qa.py` (vendored from M Max, completed here —
  the zip shipped 2 of its 4 advertised checks and no runner; also
  fixed a Blender-5.2 `Bone.matrix` 3×3 crash and welded-vertex island
  decomposition since `.ydd` split-normal meshes explode into
  per-face micro-islands): island bone purity, cross-lateral L/R
  binding, seam-adjacency vs skeleton hop-distance, FK-isolation
  penetration sweep.

## rigid_plate_qa on military_mech (first real run)

Flagged a real binding defect the old `autorig_check` is structurally
blind to (edge-stretch is trivially 1.0 on rigid plates): **12
CROSS_LATERAL_BIND + 180 DISJOINT_KINEMATIC_SEAM** — a cluster of
inner-thigh/groin plates on +x bound to `SKEL_R_Thigh`. Invisible in
static renders, will visibly shear when the leg animates. Recipe
`bone_lock`s in the workbench need correcting near the midline.

## Full beta pass (all four test models)

| model | result |
|---|---|
| `buster_FINAL_folded_iso.png` / `_deployed_iso.png` | symmetric, matches REFERENCE_buster |
| `humanoid_FIXED_*.png` | full red/white albedo + decals, matches REFERENCE_humanoid |
| `police_drone_*.png` | textured + decals vs REFERENCE_police (no emissive lens glow — diagnostic material only) |
| `mech_PED_iso.png` | correct silhouette + dark olive atlas; bone-map defect flagged separately by rigid_plate_qa |

`test_harness.py`: ALL CHECKS PASSED.

## Format for external findings

External reviewers are welcome to debug the pipeline however they like —
read source, run it, write throwaway harnesses. **But deliverables should
be a single dense `.json` report, not a reimplementation of the
pipeline** (no apps, workbenches, or ported converters). Shape:

```json
{
  "reviewer": "name",
  "pipeline_commit": "sha or date",
  "findings": [
    {
      "id": "F1",
      "severity": "bug|warn|info",
      "area": "transforms|uv|textures|rig|manifest|gates|preview",
      "claim": "one-sentence defect statement",
      "evidence": "measured value / code ref / file+line",
      "source_lines": ["blender_worker.py:1597"],
      "repro": "asset + steps, or 'code inspection'",
      "suggested_fix": "concise patch description",
      "confidence": "measured|code-verified|hypothesis"
    }
  ],
  "open_questions": ["..."],
  "artifacts_checked": ["sf_sf_buster_drone2", "..."]
}
```

Verified findings get implemented + credited; hypotheses get tested.
