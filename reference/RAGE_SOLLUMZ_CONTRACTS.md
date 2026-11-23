# RAGE / Sollumz / Sketchfab Contracts (Argon reference)

Source: external review (Argon), captured 2026-11-22. Treat as the engine-side
contract for every asset the pipeline emits — props, peds, drones, turrets.

## 1. Solid-Color / Param PBR Materials → .ydd + .ytd

- RAGE shaders have **no uniform BaseColorFactor**. `DiffuseSampler` is a
  required texture sampler. No bound texture → checkerboard/black in-game.
- Sollumz 2.9 has **no untextured shader path**.
- `ytdexport.py::extract_texture_dds_data_source` only accepts **on-disk .dds**
  files. Packed images or `.png` filepaths are silently skipped → 111-byte
  empty `.ytd` stub.

### Flat-material contract
- Generate **16×16 PNGs** (NOT 4×4 — BC1_UNORM block compression works on
  4×4 blocks; 4×4 + mipmaps produces 2×2/1×1 sub-block mips that break tools).
- `texconv.exe -f BC1_UNORM -w 16 -h 16 -m 1 -y -o <tex_dir> <png>`
- Load DDS via `bpy.data.images.load(dds_path)`, `source='FILE'`,
  `filepath=_raw` set, **`image.name` = clean stem** (Sollumz uses
  `image.name` minus extension as the RAGE TextureName in BOTH the .ydd
  Shader entry and .ytd entry). Do NOT `.pack()`.

### Chrome/metal look (RAGE is spec/gloss, not metallic/roughness)
For source metallic>0.5 mats, a flat diffuse alone renders as matte clay.
- Darken baked diffuse: `rgb *= (1.0 - 0.35*metallic)` (keeps hue, avoids
  streetlight blowout).
- On Sollumz shader nodes set: `SpecularFactor = 0.15 + 0.85*metallic`,
  `SpecularFalloffMult = max(10, (1-roughness)*250)`,
  `SpecularFresnel = 0.90 metals / 0.50 dielectrics`.

### Pre-normalize every material before Sollumz auto_convert
auto_convert fails on non-Principled trees (Mix Shader, Velvet/Sheen,
glTF groups, use_nodes=False). Rebuild each source mat as
TexImage→PrincipledBSDF→Output, preserving extracted
(base_rgba, metallic, roughness) + base-color image, THEN auto_convert.

## 2. Streaming limits (FiveM)

- **16 MiB hard ceiling per asset file.** >16MB .ydd fails to stream /
  crashes / invisible. Target ≤14MB, ideally 3–8MB.
- **uint16 vertex buffer limit: 65,535 verts per material sub-mesh.**
- Fix: DECIMATE (COLLAPSE to ≤70k tris for a ped) + merge_by_distance(0.0001)
  before weight transfer/export.

## 3. Vertex color contract (RAGE ≠ albedo)

RAGE uses vertex colors for lighting/physics:
R=dirt/rim/spec scale, G=artificial/night AO, B=natural/sky AO,
A=micromovement/blend mask.
- Black (0,0,0,1) vertex colors → pitch-black model at noon, looks fine in
  Blender. Force `R=1, G=1, B=1, A=1` on props AND peds; never keep
  imported Sketchfab vertex colors.

## 4. Sollumz skin-binding contract (.ydd)

glTF stores per-mesh inverseBindMatrices; RAGE does NOT. RAGE computes
binds from the **armature's EditBone rest matrices**
(`bone.matrix_local.inverted()`). Five iron laws:

1. **Shared identity frame**: dictionary empty, armature drawable, and
   drawable models all at identity transform (loc/rot/scale, parent inverse).
2. **Vertex space == bone rest space**: raw `v.co` must sit visually where
   the rest bones are (1 unit = 1 m). GTA ped axes: +Z up, −Y forward,
   +X ped's left.
3. **Purge ALL original vertex groups** before donor weight transfer —
   leftover groups map to bone index 0 (SKEL_Root) → vertices yanked to
   pelvis = wall of wire spikes.
4. **4-weight cap, sum=1.0**, only valid bone names.
5. **Sub-mesh palette ≤64 bones** (skeleton ≤128/255 by drawable type);
   prune empty vertex groups before export.

## 5. Sketchfab GLB failure modes (all seen on Selterus)

- **Hybrid rigging**: most parts are bone/node-parented (no armature
  modifier); only hoses/cables are skinned. Filtering by armature children
  or `find_armature()` can discard 90% of the body.
- **Multiple characters in one scene** separated by node transforms —
  cluster-filter parts spatially (k=2 islands).
- **Exploded CAD rest pose**: EditBone rest ≠ assembled shape; robot only
  forms in POSE position. Bake the **evaluated depsgraph** (POZE position)
  to world space, never raw `obj.data.vertices`.
- **Negative scale mirrors** (-1.0): after world bake, if
  `matrix_world.determinant() < 0`, `mesh.flip_normals()` — never blind
  `normals_make_consistent` on thin/non-manifold geometry.

### Evaluated-bake helper (contract)
For each mesh: `eval_obj = obj.evaluated_get(depsgraph)`;
`bpy.data.meshes.new_from_object(eval_obj, preserve_all_data_layers=True)`;
`mesh.transform(eval_obj.matrix_world)` (+flip_normals on negative det);
new object at Identity, `vertex_groups.clear()`, classify bind_kind
(SKINNED/BONE_PARENT/NODE_PARENT/ROOT_MESH) for diagnostics.

## 6. texconv flags for FiveM .ytd

- `-pow2` always; cap 1024–2048 (keeps .ytd under 16MB streaming limit);
- `-m 0` full mipmap chain (missing mips → shimmering + broken LOD);
- BC1_UNORM opaque, BC3_UNORM alpha (BC7 needs DX10 header — verify Sollumz
  writes it before using).

## 7. Collision contracts

- Static props: embedded BoundGeometryBVH OK; <1,500 tris, purge degenerate
  triangles (zero-area tri crashes the physics solver on impact).
- Moving/drone props: BVH can't move — use BoundGeometry (convex) or
  BoundBox/Sphere with Mass + Center of Gravity.

## 8. Runtime auto-validation (testserver)

Client resource listens on resource start → for each archetype in
manifest.json: RequestModel (3s timeout), GetModelDimensions (non-zero,
non-NaN), CreateObjectNoOffset + DoesEntityExist, optional screenshot,
delete, report back to orchestrator / write file via server event.

## 9. Measured toolchain invariants (Phase 3)

Verified on the installed toolchain (Blender 5.2.1 + Sollumz 2.9.0) —
these override any external doc written for other versions. Canonical
copy lives in `.devin/skills/sketchfab-pipeline/SKILL.md`.

- **Sollumz 2.9 attribute naming**: UV layers must be `UVMap 0`
  (zero-indexed, with a space — `get_uv_map_name(i)` → `f"UVMap {i}"`);
  vertex-color attrs are `Color 1`, `Color 2`, … (**1-indexed** —
  `get_color_attr_name(i)` → `f"Color {i+1}"`, NOT `Color 0`).
  Both helpers create EMPTY shells under the expected name while real
  data sits under e.g. `UVMap`/`COLOR_0` — detect "empty" by all-zero
  coords, not bbox span; rescue = drop shell, rename donor, retarget
  `ShaderNodeUVMap.uv_map` (empty OR dangling names).
- **Blender 5.2 `Bone.matrix` is 3×3** parent-relative (was 4×4) —
  `bone.matrix_local` is the 4×4 armature-space rest matrix.
  FK delta: `pose_bone.matrix @ bone.matrix_local.inverted()`.
- **glTF NLA import parks clips muted on tracks**, not `ad.action` —
  and a `track.is_solo` suppresses the active action even after unmute.
  `resolve_clip_playback` must unmute strips+tracks, clear `is_solo`,
  set `ad.action`, then `scene.frame_set(f_start)`.
- **`bound_box` is stale** after `mesh.data.transform(M)` until
  `view_layer.update()` — reading it early fed a +1.315 m `body_off`
  float (testing/PHASE3_RESULTS.md). Always update the view layer
  between transforming mesh data and reading bounds.
- **A fetched depsgraph follows scene state** — hold `dg` across a
  `frame_set` and later reads silently sample the new frame. Re-fetch
  `evaluated_get(depsgraph)` after each `frame_set`.
- **Quaternion slerp needs hemisphere normalization** —
  `Euler.to_quaternion()` can emit antipodal pairs → `dot(q0,q1)<0`
  must negate `q1` or the interpolation takes the long path.
- **Vertex color API**: use `mesh.color_attributes` (5.2), not legacy
  `vertex_colors`; byte-color writes go through `color_srgb` in 0–1
  (Sollumz's own `add_mesh_color_attribute` does the same).
