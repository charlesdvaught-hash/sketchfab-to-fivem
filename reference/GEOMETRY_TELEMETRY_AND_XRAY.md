# Geometry Telemetry Gates + 25ms X-Ray Renderer (Argon reference)

Why AABB lies and what replaces it, plus a zero-dependency PNG contact-sheet
renderer usable inside blender_worker.py at every pipeline stage.

## 1. Why bounding boxes lie

AABB (max−min) samples only 2 extreme verts per axis. If 99% of a mesh
collapses onto a plane while 4 strays remain at the extremes, AABB still
reports a healthy human-sized box. Every "PASS" that rendered as wires was
an AABB pass.

## 2. The three replacement gates (<40ms, run at EVERY stage)

### A. Edge-length ratio — catches wire/spike explosions
When binding/joining goes wrong, triangle-mate verts get pulled to different
coordinate spaces → razor-thin triangles.
- Compute median edge length E50 and max E_max.
- Healthy ~1.9m character: E50≈0.008m, E_max<0.25m.
- FAIL (ERR_SPIKE_EXPLOSION) if `E_max > 0.35×height` or `E_max/E50 > 40`.

### B. P90−P10 inter-decile span — catches "9cm wall" collapse
Sort vert coords per axis; measure middle-80% mass (P90−P10) instead of
min/max. Stray wires only hit P00–P05/P95–P100. If AABB(Y)=0.60m but
P90−P10(Y)=0.04m, the core mass collapsed to a flat wall.

### C. Coarse voxel occupancy — catches exploded rigs
10×10×10 grid over the AABB; count occupied cells.
- Healthy humanoid/prop: 12–35% occupancy.
- Collapsed wires / one distant outlier: <3%.

## 3. dump_stage_xray — pure-stdlib orthographic renderer

Projects world-space verts+edges into a 3-panel Front(XZ)/Side(YZ)/Top(XY)
PNG in ~25ms: cyan edges = normal, bright red = spike edges
(length > max(12×median, 0.18×span)). No render engine, no GPU. Call it after
import, part-filter, rotate, rig, and pre-export pose-torture
(rotate SKEL_L_Calf + SKEL_L_Forearm 45°, eval, re-xray — spike_ratio jump =
rigging bug caught without Sollumz).

Reference implementation lives in this file's companion:
`tools/sketchfab_pipeline/xray.py` (stdlib PNG writer + dump_stage_xray,
returns aabb_dims, core_p90_p10_dims, median_edge, max_edge, spike_ratio).

## 4. Application per asset class

### Static props (.ydr)
Evaluated bake → join → recenter verts X_c=0, Y_c=0, Z_min=0 → identity
object transform → vertex colors (1,1,1,1).

### Peds / clothing (.ydd)
Evaluated bake → multi-character spatial filter → auto-orient via PCA +
humanoid aspect heuristic (longest axis = up; widest upper-body span = L-R;
narrowest torso = front-back) → align to donor rest → purge vgroups →
donor weight transfer → ≤4 weights + normalize → pre-export pose torture +
xray.

### Drones / multi-part props
Moving sub-part (rotor, gimbal) MUST get its own origin: subtract its pivot
pi=(xi,yi,zi) (rotor blade XY center; gimbal mount joint) from its verts so
exported <name>_rotor.ydr spins around its own shaft, not the chassis belly.
Emit attach_offset + spin_axis into the resource manifest for
AttachEntityToEntity.

### Tanks / aim-tracking turrets
3 rigid groups: hull / turret(yaw +Z) / barrel(pitch +X).
- Turret pivot = center of the turret RING on the hull roof — NOT the
  turret bbox center (rear bustle/long barrel shifts it → wobble).
- Barrel pivot = rear trunnion inside mantlet (~85–90% back along barrel
  axis) — NEVER barrel midpoint (breech punches through roof at elevation).
- Muzzle socket = extreme forward tip, stored as offset from barrel pivot.
- Native .yft bone names: turret_1base (ring center), turret_1barrel
  (trunnion, child), weapon_1a/turret_1muzzle (tip, child).
- Or modular .ydr + Lua: kinematic_chain manifest with per-part offsets,
  axes, pitch_limits, muzzle_offset; client converts aim vector to
  hull-local via GetOffsetFromEntityGivenWorldCoords, applies
  yaw=atan2(-dx,dy) to turret, pitch=atan2(dz,dist2D) clamped to barrel.

## 5. Auto-orient heuristic (Selterus lesson)

Never assume source axes. Determine up/forward from the geometry:
- up axis = longest principal axis of the body cluster (~1.7–2m peds).
- L-R axis = widest upper-body span (arms/shoulders).
- depth axis = narrowest torso P90−P10 at 55–75% height.
Validate by xray before transforming — Selterus was already Z-up; a guessed
Rx90 flattened it twice.
