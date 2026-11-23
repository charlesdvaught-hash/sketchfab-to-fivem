# MOTION_SPEC — prop motion modes

How `motion_config` turns a static source into something that moves, what each
mode actually emits, and what is deliberately out of scope. Normative codes
live in `PIPELINE_SPEC.json`; this doc is the contract for the manifests and
their conventions.

## `motion_config` schema

| Field | Default | Meaning |
|---|---|---|
| `motion_mode` | `"static"` | `static` \| `snapshot_sequence` \| `multi_part_prop_assembly` \| `ped_clothing` \| quarantined vehicle modes |
| `frame_window` | whole action range | `{"start_frame": int, "end_frame": int}` — sampled window |
| `samples` | — | `snapshot_sequence`: list of normalized positions `0..1` over the window (2–12 → `ERR_TOO_MANY_SAMPLES` above 12) |
| `keep_actions` | — | list of substrings matched against imported clip/action names; matching clips are activated per object, all others muted (`ERR_KEEP_ACTIONS_NO_MATCH` if nothing matches). Needed for Sketchfab's `OBJ\|CLIPAction` clip-sets |
| `part_map` | auto-detect | `multi_part_prop_assembly` explicit mapping — see below |
| `ai_carrier_archetype` | `"frogger"` | assembly manifest hint only |
| `transition_ms` | 1200 | assembly interpolation duration |
| `pivot_policy` | — | snapshot pivot policy passthrough |

`part_map` has two value shapes (mixed freely):

```jsonc
"part_map": {
  "<part_label>": ["meshA", "meshB"],            // group join: ONE rigid
                                                 // part, hinge = first
                                                 // member's motion
  "<other_label>": {                              // dict form:
    "meshes": ["meshC"],                          //   member meshes
    "kind": "spin",                               //   "spin" | "hinge"
    "spin_axis": [0, 0, 1],                       //   source-space dir | "auto"
    "spin_rpm": 900                               //   float | "auto"
  }
}
```

Named meshes that don't exist, or a mesh in two groups → `ERR_UNMAPPED_PART`.
Labels are sanitized/prefixed into archetype names like auto-generated ones.
Top-level `exclude_objects` (fnmatch patterns on object names) drops objects
+ subtrees after import — kills Sketchfab display bases/env junk in any mode.

`assembly_config` extras: `max_parts` (default 12, counts resulting parts),
`spin_min_deg` (default 300 — signed cumulative rotation that proves a
one-directional rotor), `spin_name_patterns` (default
`blade,rotor,prop,propeller,fan,turb,impeller`),
`spin_max_travel_m` (default 0.05 — hub test: max carrier-relative
translation for an unnamed rotor), `spin_tail_min_dpf` (default 0.1
deg/frame — "still spinning at window end" floor), `part_collision`
(default `"none"` — G2: collision stays on `_body` only).

Frame errors: empty resolved window → `ERR_FRAME_WINDOW_EMPTY`; sample outside
range → `ERR_FRAME_OUT_OF_BOUNDS`; no evaluated geometry → `ERR_NO_GEOMETRY_AT_FRAME`.

## `snapshot_sequence` (method 1 — flipbook)

Samples the evaluated scene at each requested pose, bakes each pose into a
separate static drawable, and emits:

- `stream/<name>_snapNN.ydr` per sample (all gated like any `.ydr`)
- `sequence_manifest.lua` at the resource root:

```lua
return {
  mode = "snapshot_sequence",
  runtime_status = "manifest_only_requires_consumer",
  frame_window = { start_frame = N, end_frame = M },
  pivot_policy = "...",
  steps = {
    { model = "<name>_snap01", hold_ms = -1 }, -- -1 = hold until scripted
    ...
  },
}
```

The manifest is **metadata for a consumer script you still have to write** —
nothing in the resource animates by itself. Intended consumer: CreateObject
swaps or `AttachEntityToEntity` re-parenting onto a carrier entity.

## `multi_part_prop_assembly` (method 2 — hinges + spinners)

The worker samples the scene **per frame** across the window (capped ~600
samples). Each part is one of two kinds:

- **hinge** — a part that changes pose between folded and deployed.
  Manifest: `attach_offset`, `deployed_offset`, `folded_rot`, `deployed_rot`,
  plus `keyframes` — an array of `{t, pos, rot, quat}` stops over `t ∈ [0,1]`
  selected by **adaptive SE(3) RDP** to track the sampled driver trajectory
  (default; see "Keyframe stops" below). `keyframes[0]` ≡ folded pose and
  `keyframes[-1]` ≡ deployed pose bit-for-bit. `quat` is the same basis as
  `[w,x,y,z]`, sequentially hemisphere-chained so runtime consumers nlerp
  adjacent stops directly.
- **spin** (G2 — rotors/propellers/fans) — a part that rotates continuously
  on a hub for the whole window. Manifest: `attach_offset` (the hub — the
  point on the recovered spin axis nearest the part's centroid),
  `initial_rot`, `spin_axis`, `spin_rpm` (derived: signed revs / window
  seconds — an average over the window, so a clip that spins up from rest
  reports the ramp-averaged rate, not cruise). Spin parts are **never**
  emitted as folded→deployed hinges and carry no `deployed_rot`.

Classification per part (explicit wins): `kind` in a dict `part_map` entry →
**auto-detect** (`WARN_AUTO_SPIN_PART`) → dict `spin_axis`/`spin_rpm`
implies spin intent → else hinge. Auto-detect needs ALL THREE:

1. **one-directional signed rotation** ≥ `spin_min_deg` about the dominant
   axis (endpoints lie: a rotor wraps <360° net; unsigned cumulative also
   lies: a there-and-back swing accumulates hundreds of degrees — signed
   cancels the return trip);
2. **still turning at the end** — mean deg/frame over the last ~1/8 of the
   window ≥ `spin_tail_min_dpf` (rotors don't decelerate to a stop; deploy
   swings do — a 1347° one-way sweep that ends dead-still is a hinge and
   logs `WARN_HIGH_ROTATION_HINGE`);
3. **hub evidence** — object name matches `spin_name_patterns`, OR its
   translation stays within `spin_max_travel_m` of its start **in
   carrier-relative space** (a rotor on a liftoff mount may translate with
   the mount — name it or map it).

A *static* spin part (un-animated rotor) needs explicit `spin_axis` +
`spin_rpm` — no axis guessing: missing → `ERR_SPIN_AXIS_UNRESOLVED` /
`ERR_SPIN_RPM_UNRESOLVED`. `<300°` own-origin rotations stay hinges, so
doors keep working.

Naming: `<name>_partNN` hinge · `<name>_propNN` spin · `part_map` labels win.
Multi-mesh groups join rigidly at folded pose; the group's hinge is the first
*moved* member's delta. Spin parts and hinged shells carry **no collision**
(`part_collision`, default `"none"`) — collision stays on `_body`.

`part_map` reserved label `"_body"`: pins its member meshes into the carrier
drawable even when they animate — needed when the *carrier itself* has
keyframes (the grunt drone's body reorients 89° during deploy; without
`_body` every mesh counts as a part). First `_body` member is the carrier
reference; deployed poses are then expressed **relative to the carrier's
world delta** (`WARN_CARRIER_MOTION_RELATIVIZED`), which is exactly what an
`AttachEntityToEntity` consumer needs — the same math is a no-op on static
bodies.

Clip playback: objects whose imported animation sits in exactly one muted
NLA clip are auto-activated (parallel per-object clips). Objects carrying
several distinct clips stay muted and log `WARN_AMBIGUOUS_ACTIONS` —
`keep_actions` selects a clip-set explicitly.

### Keyframe stops (Phase 4 — adaptive RDP)

Every hinge part samples its **driver trajectory** at every frame of the
window — the driving object's `matrix_world`, or the driving bone's
`arm.matrix_world @ pose_bone.matrix` when the member is armature-deformed —
and relativizes it into the carrier's f_start frame:
`W_rel(k) = ref(0) @ ref(k)^-1 @ W(k)` — carrier refs are **rigid-SE3
normalized** (scale/shear stripped) so animated squash on the root can't
inject non-Euclidean distortion into child lever arms. The per-sample
rigid **delta** is `T'(k) = W_rel(k) @ W0⁻¹` (driver path). For
**multi-member hinge groups**, `assembly_config.group_fit` (default
`"procrustes"`) replaces the single-driver delta with a weighted Kabsch
best fit over per-member probe verts (centroid + 6 axis-extremes + stride,
driver member weighted `group_fit_proximal_weight`, default 3×, centroids
normalized by Σw): `T'_fit(k)` maps the group's *whole* f_start
configuration into frame k, centering the rigid approximation across the
chain instead of dumping all error on distal members. An endpoint
**epsilon snap** (<1e-4 m/rad) returns rigid groups to the exact driver
values so their manifests stay bit-identical; fitted groups emit
**fit-based** `deployed_*` (the group's best-fit end pose — `keyframes[-1]`
matches it exactly). Members deformed by armatures can't be fit from
object matrices → driver fallback + `WARN_GROUP_FIT_SKIPPED_DEFORMED`.
Exact per-sample manifest poses are then:

- position: `pos(k) = hinge_n + N₃₃ @ (T'(k) @ hinge − hinge)` — driver
  path reduces to `hinge_n + N₃₃ @ (W_rel(k).translation − hinge)`;
- basis: `R_basis(k) = R @ T'(k) @ W0 @ slerp(D_rot⁻¹ → I, s_k)` — the
  `rest_rot` compensation ramps to identity across **motion progress**
  `s(k) = min(1, pose_dist(T'(k),I) / pose_dist(T'_last,I))`
  (`pose_dist = ‖Δt‖ + 0.2·|θ|`; falls back to `t_k` when the part barely
  moves), so the re-pose completes *with the physical deployment* instead
  of twisting through a stationary hold — `t=0` yields `folded_rot` and
  `t=1` yields `deployed_rot` bit-for-bit while the trajectory stays
  continuous. Parts with a nonzero `rest_rot` also emit `rest_rot_deg`
  and a per-keyframe `repose` (= `s_k`) so diagnostics can conjugate
  source truth by `S @ slerp(I, D_rot, s) @ S⁻¹` about the pivot
  (`S = q_pred @ ramp_k⁻¹`) and measure tracking error instead of
  phantom ramp error.

Stops are then chosen by greedy SE(3) Ramer–Douglas–Peucker over the ≤600
samples: start `[0, last]`, split the worst-error interval at the argmax
frame, repeat until max vertex error ≤ `keyframe_tol_m` (default `0.02`)
or `keyframe_max_stops` (default `5`) is reached. Error is measured on a
≤128-vert canonical subsample: uniform stride **plus the 6 axis-extreme
verts** so tip rotation error is always in the set. `keyframe_mode` in
`assembly_config`: `"adaptive"` (default), `"uniform"` (stops at
0/.25/.5/.75/1 — Argon's option (a) fallback grid), `"off"` (legacy
2-pose manifest). Spin parts, static-driver groups and non-hinge entries
carry no `keyframes`.

Linear-translation parts terminate at 2 stops automatically; buster's
hinges clustered 3 interior stops inside `t ≈ 0.22–0.30` (the actual
deploy phase) and left the settled tail on one interval — the adaptive
behavior is exactly where option (a)'s uniform grid was blind.

**Multi-member caveat**: a joined part is baked rigid at f_start. If its
members carry *independent* curves (buster's 8-segment leg chains), even a
best-fit rigid track can't represent them — the Kabsch fit halves the
worst interior error (0.25 m → ~0.10 m measured on buster legs) but
`WARN_NONRIGID_CHAIN` still reports `interior_dev`/`endpoint_dev`; an
`endpoint_dev` above ~0.02 m is **permanent deployed-pose error** in the
joined .ydr and the warn recommends a `part_map` split (each member gets
its own keyframe track; watch `max_parts`).

Emits `<name>_body.ydr` + `<name>_partNN.ydr` + `<name>_propNN.ydr` and
`assembly_manifest.lua`:

```lua
return {
  mode = "multi_part_prop_assembly",
  runtime_status = "manifest_only_requires_consumer",
  rotation_order = "YXZ_deg",          -- declared PROJECT convention
  frame_window = { start_frame = N, end_frame = M },
  ai_carrier_archetype = "frogger",
  transition_ms = 1200,
  body = "<name>_body",
  parts = {
    { kind = "hinge", model = "<name>_part01", source_objects = {...},
      attach_offset   = {x,y,z},        -- body-local folded hinge pos
      deployed_offset = {x,y,z},        -- body-local deployed hinge pos
      folded_rot  = {x,y,z},            -- degrees, YXZ_deg convention
      deployed_rot= {x,y,z},
      rest_rot_deg = {x,y,z},           -- only when recipe rest_rot != 0
      keyframes = {                     -- adaptive RDP stops (see below)
        { t = 0.0,    pos = {x,y,z}, rot = {x,y,z},
          quat = {w,x,y,z}, repose = 0.0 },             -- == folded pose
        { t = 0.2217, pos = {x,y,z}, rot = {x,y,z},
          quat = {w,x,y,z}, repose = 0.30 },            -- interior stops...
        { t = 1.0,    pos = {x,y,z}, rot = {x,y,z},
          quat = {w,x,y,z}, repose = 1.0 },             -- == deployed pose
      } },
    { kind = "spin", model = "<name>_prop01", source_objects = {...},
      attach_offset = {x,y,z},          -- body-local hub point (on axis)
      initial_rot   = {x,y,z},
      spin_axis     = {x,y,z},          -- unit dir, body-local
      spin_rpm      = 559.2 },
  },
}
```

Failure paths: every mesh moves → `ERR_NO_STATIC_BODY`; >`max_parts` resulting
parts → `ERR_TOO_MANY_PARTS`; part_map names a missing/duplicated mesh →
`ERR_UNMAPPED_PART`; armature-deformed part → `ERR_ARMATURE_DEFORM_REQUIRES_SNAPSHOT_MODE`
(use `snapshot_sequence` instead — bone deltas aren't hinges); static spin
part without resolvable params → `ERR_SPIN_AXIS_UNRESOLVED` /
`ERR_SPIN_RPM_UNRESOLVED`; `keep_actions` matching nothing →
`ERR_KEEP_ACTIONS_NO_MATCH`.

### The rotation convention, stated honestly

`YXZ_deg` is a **project convention**, nothing more. FiveM's
`AttachEntityToEntity` takes an explicit `rotationOrder` integer; the game's
standard layout (order 2) is ZXY. A consumer must map our declared values onto
its chosen `rotationOrder` — this pipeline does not claim native compatibility,
and no FXServer runtime validation has been performed (`runtime_validation =
"NOT_PERFORMED"` in every build_report).

### Manifest consumer contract (what a runtime must assume)

- **Rotations are absolute-basis, not deltas.** `folded_rot`/`deployed_rot`/
  `initial_rot` all share one frame; the part verts already carry `rest_rot`
  baked into canonicalization (Contract A — see `RAGE_SOLLUMZ_CONTRACTS.md`).
  Do NOT compose `rest_rot` onto manifest rotations.
- **Hemisphere-normalize SEQUENTIALLY**: for `i>0`, `dot(q[i-1],q[i]) < 0
  → negate q[i]` — chain each quaternion against its predecessor, never
  anchor to `q[0]`. A cumulative track past 180° legitimately has
  `dot(q[0],q[i]) < 0` at the tail; anchoring flips the tail into a
  360° snap. (`render_check.py` and `diagnose_trs.py` implement the chain;
  the same rule applies inside each candidate RDP interval.)
- **Interpolate `keyframes` piecewise**: bracket `t` in `[t_i, t_{i+1}]`,
  local `alpha = (t − t_i)/(t_{i+1} − t_i)`, lerp `pos`, slerp `rot`.
  Absent `keyframes` (spin parts, `keyframe_mode="off"`, older manifests)
  fall back to `attach_offset → deployed_offset` + folded→deployed slerp
  over `transition_ms`.
- **Mid-pose accuracy is now measured, not assumed.** `_assembly_mid_*`
  renders draw the piecewise path. PASS4 (interior probes between every
  adjacent stop + fixed sweep) on buster: ≤0.015 m on all single-member
  hinges (was 1.75–2.13 m at t=0.5 on the 2-pose path); 8-member leg
  chains sit at ~0.25 m because their segments carry independent curves —
  a part-map granularity limit, not interpolation error (see caveat
  above). `runtime_status` remains `manifest_only_requires_consumer` —
  no FXServer runtime validation has been performed.

## `ped_clothing` (method 3 — skinned)

Not prop motion: this mode runs the ped pipeline — one armature + skinned
meshes → `.ydd` drawable dictionary. See README "Ped clothing". `runtime_status`
for `.ydd` output is `downstream_consumer_required`.

## Quarantined vehicle modes — by design

`door_control`, `extra_toggle`, `vtol_tilt_rotor`, `manual_yft_ycd` all need a
real vehicle skeleton and `.yft` fragment + `.ycd` clip dictionary. That is a
manual rigging discipline; the headless toolchain has no verified authoring
path, so these modes fail fast with `ERR_MOTION_MODE_REQUIRES_VEHICLE_RIG`
rather than faking it.

Unknown mode → `ERR_UNKNOWN_MOTION_MODE`.
