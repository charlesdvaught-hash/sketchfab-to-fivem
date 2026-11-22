# Rig & Animation Workbench — contract

Interactive preview → hand-rig → compile loop that replaces the
Blender→Sollumz→FiveM cycle for *spatial* questions: which parts are stray
junk, which belong to a turret vs a barrel, whether armor should lock rigid
to one bone or stretch smoothly. Everything up to compile runs in the
dashboard at 60 FPS; Blender only runs twice (once to dump, once to build).

## Invariant: preview space == compile space

`--dump-preview` normalizes before dumping and emits
`preview_transform {rot_z_deg, scale, offset}` (compose order
`v' = Rz(rot) @ v * scale + offset`). Any refit in the canvas produces
`user_transform`, expressed in already-normalized space. At compile,
`recipe_transform_matrix = M(user) @ M(preview)` is applied to the
raw baked vertices and **all auto-bounds scaling is skipped**. A pivot the
user placed in the canvas lands at the same millimeter in the export.

## Data flow

1. `POST /api/preview {file|path, asset_type}` →
   `blender --background --python blender_worker.py -- --dump-preview <src>
   <out.json> [--asset-type ped --ped-donor <ydd> --height 1.9 --exclude pat]`
   - `preview_bake.snapshot_evaluated_world_meshes()`: evaluated depsgraph
     world-space bake (armature poses, bone/node parenting, unit scales all
     resolved; negative-determinant meshes get flipped normals; NO trust in
     raw `obj.data.vertices` or rest poses).
   - No texture decode / material conversion / texconv. Part colors come
     from Principled base-color factor or a name hash.
   - Donor ped mesh is excluded from the dumped parts; donor bones are
     dumped in the same normalized space.
   - JSON: `{asset_type, preview_transform, parts[], bones[]}` where each
     part is `{id, name, bind_kind, island_id, role, bone_lock, pivot,
     color, v, f}` decimated to ~10k tris total.
2. `GET /api/preview?job=<id>` → `{"status":"running"}` or the JSON.
   Canvas: orbit/zoom, click/ctrl/shift-marquee select, island select,
   role assignment (ped: `smooth_skin`/`rigid_bone`+bone; prop:
   `hull`/`turret`/`barrel`/`rotor`/`gimbal`; `trash`), pivot edit,
   `rest_rot` [rx,ry,rz] degrees XYZ — baked into the cluster's verts about
   its pivot at compile (re-pose authored orientations, e.g. edge-on ducted
   props laid flat),
   `Re-Fit to Active Parts`, live anims (`tank_aim` mouse tracking,
   `drone_spin`, `ped_walk`/`ped_squat` FK with red spike edges >~35 cm).
3. `POST /api/compile-rig {file|path, override, rig_recipe}` →
   recipe is merged into `override.json` as `rig_recipe` and the normal
   orchestrator job runs. `worker_result.json` echoes `rig_recipe` counts.

## Worker recipe semantics (blender_worker.py)

- `role:"trash"` parts are deleted by exact source name before any join.
- Static: `recipe_transform_matrix` replaces `fix_scale_and_rotation` +
  `ground_pivot` (verts transformed, not re-fit).
- Ped (`asset_type:"ped"` + recipe): strict order —
  1. donor GTA armature imported (`ped_donor_ydd` sidecar or
     `donor/donor.json`);
  2. source meshes baked in world space, trash dropped, recipe transform
     applied;
  3. smooth parts join → proportional decimate to `max_triangles` budget →
     inverse-square distance weights to the donor skeleton, quantized to
     0.05 buckets, ≤4 influences, normalized;
  4. each rigid part gets one vgroup (`bone_lock` or nearest bone) at
     weight 1.0;
  5. rigid joins into the smooth mesh last — name-merged groups keep the
     rigid/smooth split uncontaminated. Then `ped_export_finalize` (shared
     with `process_ped`): attrs → textures → drawable dict → export.
- `multi_part_prop_assembly`: recipe maps to `part_map` (`hull`/`chassis` →
  `_body`; `rotor`→spin axis +Z auto rpm; `turret`/`gimbal`/`barrel`→hinge),
  `mc["rig_recipe"]` swaps the normalization matrix for the recipe matrix,
  and group `pivot` values are consumed **verbatim** (already recipe space)
  as `attach_offset`/`deployed_offset`.

## Failure modes this kills

- Stray 20 m cables / second character squashing the fit (island select +
  trash + refit).
- Rigid mech plates rubber-bending at joints (`rigid_bone` locks; fabric
  stays `smooth_skin`).
- Pivot coordinates drifting between canvas and export (the invariant).
- Weight-transfer overwriting rigid groups (strict compile order).
- 3 s dashboard poll clobbering selections (inbox `<select>` only rebuilds
  on signature change; tab persists via `sig_tab`).
