# SPDX-License-Identifier: AGPL-3.0-only
"""Headless Sketchfab->GTA prop normalizer. Runs INSIDE Blender.

Usage:
    blender --background --factory-startup --python blender_worker.py -- <job_dir>

<job_dir> must contain job.json written by orchestrator.py:
    {"source_file": str, "asset_name": str, "sidecar": {...},
     "job_dir": str, "stream_dir": str, "textures_dir": str}

Writes <job_dir>/worker_result.json. Implements PIPELINE_SPEC.json EC_02-EC_09
plus Sollumz drawable/ytyp/export. EC_01 (archive handling) is the
orchestrator's job -- this script receives one resolved 3D file.
"""
import json
import math
import os
import re
import sys
import traceback

import bpy
from mathutils import Matrix, Vector, Euler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESULT = {"status": "SUCCESS", "warnings": [], "errors": [],
          "dims": None, "tris": 0, "exported_files": [], "materials": [],
          "motion_mode": "static", "artifacts": [], "inspect": {},
          "sollumz_version": None, "texture_mode": "embed",
          "asset_type": "prop", "target_generation": "legacy",
          "files": [], "unsupported": [], "skipped": []}

# target_generation -> export_assets target_versions enum set.
GEN_TARGETS = {"legacy": {"GEN8"}, "enhanced": {"GEN9"},
               "both": {"GEN8", "GEN9"}}

NAME_RE = re.compile(r"^[a-z0-9_]{3,48}$")


def sanitize_target_name(raw, asset_name):
    """Coerce a sidecar/derived label into a valid GTA archetype name.

    Returns the sanitized name prefixed with asset_name when missing, or
    None when nothing valid can be produced (caller fails ERR_PREFLIGHT).
    """
    s = re.sub(r"[^a-z0-9_]", "_", str(raw).lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s:
        return None
    if not s.startswith(asset_name):
        s = f"{asset_name}_{s}"
    s = s[:48].rstrip("_")
    return s if NAME_RE.fullmatch(s) else None


def warn(code):
    RESULT["warnings"].append(code)
    print(f"[worker] {code}")


def fail(code, exc=None):
    RESULT["status"] = "FAILED"
    RESULT["errors"].append(code)
    if exc is not None:
        RESULT["errors"].append("".join(traceback.format_exception(exc))[-4000:])
    print(f"[worker] FAIL {code}")


def write_result(job_dir):
    path = os.path.join(job_dir, "worker_result.json")
    with open(path, "w") as fh:
        json.dump(RESULT, fh, indent=2)


def build_inspect():
    """Pre-mutation source audit (gtax -inspect style): what did we actually
    import? Logged into worker_result -> build_report for provenance."""
    scene = bpy.context.scene
    ms = [o for o in scene.objects if o.type == "MESH"]
    rng = None
    for a in bpy.data.actions:
        r = a.frame_range
        rng = r if rng is None else (min(rng[0], r[0]), max(rng[1], r[1]))
    _, _, dims = world_bbox(ms) if ms else (None, None, Vector())
    return {
        "mesh_count": len(ms),
        "source_meshes": [{"name": o.name, "verts": len(o.data.vertices),
                           "tris": tri_count(o)} for o in ms],
        "materials": [m.name for m in bpy.data.materials],
        "images": [{"name": i.name, "size": [int(i.size[0]), int(i.size[1])],
                    "packed": i.packed_file is not None}
                   for i in bpy.data.images
                   if i.name not in {"Render Result", "Viewer Node"}],
        "animation": {
            "actions": len(bpy.data.actions),
            "armatures": sum(1 for o in scene.objects if o.type == "ARMATURE"),
            "shape_keyed": sum(1 for o in ms if o.data.shape_keys),
            "frame_range": list(rng) if rng else None},
        "bounds_m": ([round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)]
                     if ms else None),
    }


def world_bbox(objs):
    """Combined world-space bbox of mesh objects -> (min_v, max_v, dims)."""
    mins = Vector((float("inf"),) * 3)
    maxs = Vector((float("-inf"),) * 3)
    for o in objs:
        for corner in o.bound_box:
            w = o.matrix_world @ Vector(corner)
            mins = Vector(map(min, mins, w))
            maxs = Vector(map(max, maxs, w))
    return mins, maxs, maxs - mins


def deselect_all():
    for o in bpy.context.scene.objects:
        o.select_set(False)


def select_only(objs):
    deselect_all()
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def import_source(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        fail(f"ERR_UNSUPPORTED_EXT:{ext}")
        return False
    drop_importer_junk()
    return True


def drop_importer_junk():
    """Remove objects the glTF importer generates that aren't source content:
    the hidden 'glTF_not_exported' collection holds the bone-display icosphere
    (io_scene_gltf2 node.py armature_display). It is NOT a scene mesh."""
    special = bpy.data.collections.get("glTF_not_exported")
    if special is None:
        return
    for o in list(special.objects):
        if all(c is special for c in o.users_collection):
            bpy.data.objects.remove(o, do_unlink=True)
        else:
            special.objects.unlink(o)
    try:
        bpy.data.collections.remove(special)
    except Exception:
        pass


def strip_animation_and_armatures():
    """EC_02: bake skinned meshes to frame-1 pose, kill all animation data."""
    scene = bpy.context.scene
    had_anim = (len(bpy.data.actions) > 0 or
                any(o.type == "ARMATURE" for o in scene.objects) or
                any(o.data and getattr(o.data, "shape_keys", None) for o in scene.objects
                    if o.type == "MESH") or
                any(o.animation_data and o.animation_data.nla_tracks for o in scene.objects))

    scene.frame_set(1)
    for o in list(scene.objects):
        if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers):
            select_only([o])
            try:
                bpy.ops.object.convert(target="MESH")
            except Exception as exc:
                warn(f"WARN_ARMATURE_BAKE_FAILED:{o.name}:{exc}")

    for o in scene.objects:
        if o.animation_data:
            o.animation_data_clear()
        if o.type == "MESH" and o.data.shape_keys:
            select_only([o])
            try:
                bpy.ops.object.shape_key_remove(all=True)
            except Exception:
                o.shape_key_clear()
    for act in list(bpy.data.actions):
        bpy.data.actions.remove(act)
    for o in list(scene.objects):
        if o.type == "ARMATURE":
            bpy.data.objects.remove(o, do_unlink=True)

    if had_anim:
        warn("WARN_STRIPPED_ANIMATION_OR_ARMATURE")


def remove_non_mesh_junk():
    """EC_03: delete cameras/lights/empties; convert curves etc. to mesh."""
    scene = bpy.context.scene
    for o in list(scene.objects):
        if o.type in {"CAMERA", "LIGHT", "SPEAKER", "GPENCIL", "GREASEPENCIL",
                      "GREASEPENCIL_LINEART", "LIGHT_PROBE", "LATTICE"}:
            bpy.data.objects.remove(o, do_unlink=True)
    for o in list(scene.objects):
        if o.type in {"CURVE", "SURFACE", "FONT", "META"}:
            select_only([o])
            try:
                bpy.ops.object.convert(target="MESH")
            except Exception:
                bpy.data.objects.remove(o, do_unlink=True)
    for o in scene.objects:
        if o.type == "MESH" and o.parent:
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw
    for o in list(scene.objects):
        if o.type == "EMPTY":
            bpy.data.objects.remove(o, do_unlink=True)


def meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def drop_degenerate():
    for o in list(meshes()):
        if len(o.data.vertices) == 0 or len(o.data.polygons) == 0:
            bpy.data.objects.remove(o, do_unlink=True)


def apply_all_transforms(objs):
    neg_scale = {}
    for o in objs:
        neg_scale[o.name] = any(s < 0 for s in o.scale)
    select_only(objs)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for o in objs:
        if neg_scale.get(o.name):
            o.data.flip_normals()
            warn(f"WARN_FLIPPED_NORMALS:{o.name}")


def fix_scale_and_rotation(objs, sidecar):
    """EC_05: unit heuristics, explicit scale/rotation. Assumes transforms applied."""
    _, _, dims = world_bbox(objs)
    factor = 1.0
    th = sidecar.get("target_height_meters")
    if th:
        if dims.z > 0:
            factor = th / dims.z
    else:
        m = max(dims.x, dims.y, dims.z)
        if m > 100.0:
            factor = 0.01
            warn("WARN_AUTO_SCALED_CM_TO_M")
        elif m < 0.005:
            factor = 100.0
            warn("WARN_AUTO_SCALED_UP")
    factor *= float(sidecar.get("scale_multiplier", 1.0))
    if abs(factor - 1.0) > 1e-9:
        for o in objs:
            o.scale = (o.scale.x * factor, o.scale.y * factor, o.scale.z * factor)
        apply_all_transforms(objs)

    rot = sidecar.get("rotation_offset_deg") or [0.0, 0.0, 0.0]
    if any(abs(r) > 1e-9 for r in rot):
        import math
        for o in objs:
            o.rotation_euler = (math.radians(rot[0]), math.radians(rot[1]),
                                math.radians(rot[2]))
        apply_all_transforms(objs)


def ground_pivot(objs, mode):
    """EC_06: translate mesh data so the pivot sits where the mode wants."""
    if mode == "keep_origin":
        return
    mins, maxs, _ = world_bbox(objs)
    if mode == "bottom_center":
        off = Vector((-(mins.x + maxs.x) / 2.0, -(mins.y + maxs.y) / 2.0, -mins.z))
    else:  # center
        off = -(mins + maxs) / 2.0
    for o in objs:
        for v in o.data.vertices:
            v.co += off
        o.data.update()


def tri_count(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    return sum(len(t.vertices) - 2 for t in mesh.loop_triangles)


def enforce_poly_budget(obj, max_tris):
    """EC_07: hard-cap quarantine or auto-decimate."""
    tris = tri_count(obj)
    if tris <= max_tris:
        return tris
    if tris > max_tris * 6:
        fail("ERR_POLYCOUNT_EXCEEDS_HARD_CAP")
        return tris
    mod = obj.modifiers.new("auto_decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = max_tris / tris
    mod.use_collapse_triangulate = True
    select_only([obj])
    bpy.ops.object.modifier_apply(modifier=mod.name)
    warn("WARN_AUTO_DECIMATED")
    return tri_count(obj)


def ensure_uv(obj):
    if len(obj.data.uv_layers) > 0:
        return
    select_only([obj])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.02)
    except TypeError:
        bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode="OBJECT")
    warn("WARN_GENERATED_SMART_UV")


def fix_mesh_attributes(obj):
    """Give the mesh the UV/color attributes Sollumz shaders expect.

    Guarded per-call: these helpers live in Sollumz.tools.meshhelper and their
    presence was confirmed by inspect_sollumz.py on Sollumz 2.9.0, but we don't
    hard-fail if a future add-on version renames them.
    """
    try:
        from Sollumz.tools import meshhelper
    except ImportError as exc:
        warn(f"WARN_MESHHELPER_IMPORT:{obj.name}:{exc}")
        return
    # IMPORTANT: run AFTER convert_materials. The helpers derive "which
    # TexCoordN/ColourN channels does this mesh need" from the Sollumz shader
    # (ShaderManager lookup); before conversion materials aren't SHADER-typed
    # so the used-channel set is empty and nothing is renamed -- the drawable
    # then exports with no TexCoord0 and renders untextured/black.
    for fn_name in ("mesh_add_missing_uv_maps", "mesh_rename_uv_maps_by_order",
                    "mesh_add_missing_color_attrs",
                    "mesh_rename_color_attrs_by_order"):
        fn = getattr(meshhelper, fn_name, None)
        if fn is None:
            continue
        try:
            fn(obj.data)
        except Exception as exc:
            warn(f"WARN_MESH_ATTR_FIX_FAILED:{fn_name}:{exc}")
    # belt-and-suspenders: if the shader needed TexCoord0 but no layer carries
    # that name yet, force-rename the first UV layer (GLTF imports call it
    # "UVMap"; Sollumz only exports layers literally named TexCoordN).
    try:
        used = meshhelper.get_mesh_used_texcoords_indices(obj.data)
        if 0 in used and obj.data.uv_layers and \
                "TexCoord0" not in obj.data.uv_layers:
            obj.data.uv_layers[0].name = "TexCoord0"
            warn(f"WARN_UV_RENAME_FALLBACK:{obj.name}")
    except Exception as exc:
        warn(f"WARN_UV_FALLBACK_FAILED:{obj.name}:{exc}")


def find_texconv():
    """texconv.exe (DirectXTex) for PNG->DDS BC compression."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("SFP_TEXCONV", ""),
                 os.path.join(here, "bin", "texconv.exe"),
                 os.path.join(here, "..", "bin", "texconv.exe")):
        if cand and os.path.isfile(cand):
            return cand
    return None


TEXCONV = find_texconv()


def image_is_normal_map(img):
    """True if img feeds a NormalMap node in any material (pre-conversion)."""
    for mat in bpy.data.materials:
        if not mat.use_nodes:
            continue
        for n in mat.node_tree.nodes:
            if n.bl_idname != "ShaderNodeNormalMap":
                continue
            link = n.inputs["Color"].links[0] if n.inputs["Color"].links else None
            if (link and link.from_node.bl_idname == "ShaderNodeTexImage"
                    and link.from_node.image == img):
                return True
    return False


def to_dds(png_path, out_dir, fmt):
    """texconv png -> .dds next to the png. Returns dds path or None."""
    if TEXCONV is None:
        return None
    import subprocess
    dds = os.path.join(out_dir, os.path.splitext(os.path.basename(png_path))[0] + ".dds")
    r = subprocess.run([TEXCONV, "-y", "-f", fmt, "-pow2", "-o", out_dir, png_path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(dds):
        warn(f"WARN_TEXCONV_FAILED:{os.path.basename(png_path)}:{r.stdout.strip()[-200:]}")
        return None
    return dds


def repath_image_to_dds(img, dds_path):
    """Point the image at the .dds file so Sollumz embeds real DDS bytes."""
    try:
        if getattr(img, "packed_file", None) is not None:
            img.unpack(method="REMOVE")
    except Exception:
        try:
            img.packed_file = None
        except Exception:
            pass
    img.filepath_raw = dds_path
    try:
        if img.source != "FILE":
            img.source = "FILE"
    except Exception:
        pass


def prepare_textures(textures_dir, max_res):
    """EC_08: downscale to POT<=max_res, save PNG provenance, convert to DDS.

    Runs BEFORE Sollumz material conversion so sampler nodes inherit images
    already backed by .dds files (the only format RAGE embeds).
    """
    os.makedirs(textures_dir, exist_ok=True)
    if TEXCONV is None:
        warn("WARN_NO_TEXCONV_DDS_SKIPPED")
    used_stems = {}
    for img in bpy.data.images:
        if img.name in {"Render Result", "Viewer Node"} or img.size[0] == 0:
            continue
        w, h = img.size
        if w > max_res or h > max_res:
            scale = max_res / max(w, h)
            nw, nh = int(w * scale), int(h * scale)
            for pot in (2048, 1024, 512, 256, 128, 64, 32, 16):
                if nw >= pot:
                    nw = pot
                    break
            for pot in (2048, 1024, 512, 256, 128, 64, 32, 16):
                if nh >= pot:
                    nh = pot
                    break
            img.scale(max(nw, 16), max(nh, 16))
            warn(f"WARN_TEXTURE_DOWNSCALED:{img.name}")
        # unique sanitized stem: 'albedo.png'/'albedo.tga' must not collide
        stem = re.sub(r"[^A-Za-z0-9_-]", "_",
                      os.path.splitext(img.name)[0]) or "tex"
        n_seen = used_stems.get(stem, 0)
        used_stems[stem] = n_seen + 1
        if n_seen:
            stem = f"{stem}_{n_seen}"
        png = os.path.join(textures_dir, f"{stem}.png")
        img.filepath_raw = png
        img.file_format = "PNG"
        try:
            img.save()
        except RuntimeError:
            img.save_render(png)
        if TEXCONV is None:
            continue
        if image_is_normal_map(img):
            fmt = "BC7_UNORM"
        elif img.channels == 4:
            fmt = "BC3_UNORM"
        else:
            fmt = "BC1_UNORM"
        dds = to_dds(png, textures_dir, fmt)
        if dds:
            repath_image_to_dds(img, dds)


def _bsdf_base_color(mat):
    if not mat.use_nodes:
        return tuple(mat.diffuse_color)
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeBsdfPrincipled":
            return tuple(n.inputs["Base Color"].default_value)
    return tuple(mat.diffuse_color)


def _lin_to_srgb(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _solid_png(path, rgba):
    img = bpy.data.images.new(os.path.basename(path), width=16, height=16, alpha=True)
    px = list(rgba[:4])
    while len(px) < 4:
        px.append(1.0)
    # Base Color is linear; img.pixels saves verbatim in background mode,
    # so convert to sRGB explicitly or the flat texture bakes ~2x too dark.
    px = [_lin_to_srgb(c) for c in px[:3]] + [px[3]]
    img.pixels = px * (16 * 16)
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    return img


def _solid_texture(path, rgba):
    """Flat-color 16x16 texture, DDS-backed when texconv is available."""
    img = _solid_png(path, rgba)
    dds = to_dds(path, os.path.dirname(path), "BC3_UNORM")
    if dds:
        repath_image_to_dds(img, dds)
    return img


def convert_materials(objs, textures_dir):
    """EC_08: turn imported materials into Sollumz shader materials."""
    from Sollumz.ydr.operators.materials import MaterialConverter
    from Sollumz.ydr.shader_materials import create_shader
    from Sollumz.sollumz_properties import MaterialType

    done = {}
    for obj in objs:
        for i, slot in enumerate(obj.material_slots):
            mat = slot.material
            if mat is None:
                continue
            if mat.name in done:
                # shared datablock converted already -- remap THIS slot too,
                # else exporter aborts the drawable ("no Sollumz materials")
                slot.material = done[mat.name]
                continue
            done[mat.name] = mat
            if getattr(mat, "sollum_type", None) == MaterialType.SHADER:
                continue
            new_mat = None
            try:
                new_mat = MaterialConverter(obj, mat).auto_convert()
            except Exception as exc:
                warn(f"WARN_AUTO_MATERIAL_CONVERT_FAILED:{mat.name}:{exc}")
            if new_mat is None:
                try:
                    new_mat = create_shader("default.sps")
                    img_node = next(
                        (n for n in new_mat.node_tree.nodes
                         if n.bl_idname == "ShaderNodeTexImage"
                         and "diffuse" in n.name.lower()),
                        None)
                    if img_node is not None:
                        src_img = None
                        if mat.use_nodes:
                            for n in mat.node_tree.nodes:
                                if (n.bl_idname == "ShaderNodeTexImage" and n.image
                                        and n.image.size[0] > 0):
                                    src_img = n.image
                                    break
                        if src_img is None:
                            src_img = _solid_texture(
                                os.path.join(textures_dir, f"{mat.name}_flat.png"),
                                _bsdf_base_color(mat))
                            warn(f"WARN_GENERATED_FLAT_TEXTURE:{mat.name}")
                        img_node.image = src_img
                    new_mat.name = f"{mat.name}_sollumz"
                    slot.material = new_mat
                except Exception as exc:
                    warn(f"WARN_MATERIAL_FALLBACK_FAILED:{mat.name}:{exc}")
            if new_mat is not None:
                done[mat.name] = new_mat
                # TEX-1 diagnostic + sanity: record what auto_convert actually
                # produced, and never ship a material that silently lost the
                # source's texture (exported .ydr renders matte black).
                tex_nodes = [n for n in new_mat.node_tree.nodes
                             if n.bl_idname == "ShaderNodeTexImage"]
                src_img = None
                if mat.use_nodes:
                    for n in mat.node_tree.nodes:
                        if (n.bl_idname == "ShaderNodeTexImage" and n.image
                                and n.image.size[0] > 0):
                            src_img = n.image
                            break
                has_img = any(n.image is not None and n.image.size[0] > 0
                              for n in tex_nodes)
                RESULT.setdefault("material_diag", []).append({
                    "source": mat.name, "converted": new_mat.name,
                    "tex_nodes": len(tex_nodes),
                    "tex_with_image": sum(1 for n in tex_nodes
                                          if n.image is not None),
                    "source_had_image": src_img is not None,
                })
                if not has_img:
                    img_node = next(
                        (n for n in tex_nodes
                         if "diffuse" in n.name.lower()), None)
                    if img_node is None:
                        img_node = next((n for n in tex_nodes), None)
                    if src_img is not None:
                        if img_node is not None:
                            img_node.image = src_img
                            warn(f"WARN_CONVERTED_UNTEXTURED:{mat.name}:"
                                 "auto_convert dropped the texture; rewired "
                                 "source image into sampler node")
                        else:
                            warn(f"WARN_CONVERTED_UNTEXTURED:{mat.name}:"
                                 "no texture sampler node in converted shader")
                    elif img_node is not None:
                        # untextured source (flat baseColor) — bake a solid
                        # texture so the drawable is not rendered matte black
                        img_node.image = _solid_texture(
                            os.path.join(textures_dir,
                                         f"{mat.name}_flat.png"),
                            _bsdf_base_color(mat))
                        warn(f"WARN_GENERATED_FLAT_TEXTURE:{mat.name}")
                for n in tex_nodes:
                    try:
                        n.texture_properties.embedded = True
                    except Exception:
                        pass
                RESULT["materials"].append(new_mat.name)


def build_collision(visual_obj, asset_name, mode):
    """EC_09: build a collision mesh per mode -> BOUND_GEOMETRYBVH composite."""
    if mode == "none":
        return None
    from Sollumz.tools.boundhelper import convert_obj_to_composite
    from Sollumz.ybn.collision_materials import create_collision_material_from_index
    from Sollumz.sollumz_properties import SollumType

    col = visual_obj.copy()
    col.data = visual_obj.data.copy()
    bpy.context.scene.collection.objects.link(col)
    col.name = f"{asset_name}_col"

    select_only([col])
    if mode == "box":
        import bmesh
        bm = bmesh.new()
        mins, maxs, _ = world_bbox([visual_obj])
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=maxs - mins, verts=bm.verts)
        bmesh.ops.translate(bm, vec=(mins + maxs) / 2.0, verts=bm.verts)
        bm.to_mesh(col.data)
        bm.free()
    elif mode == "convex_hull":
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.convex_hull()
        bpy.ops.object.mode_set(mode="OBJECT")
    elif mode == "mesh_decimated":
        while tri_count(col) > 1500:
            mod = col.modifiers.new("col_decimate", "DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = 1500.0 / tri_count(col)
            mod.use_collapse_triangulate = True
            select_only([col])
            bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.delete_loose()
        bpy.ops.object.mode_set(mode="OBJECT")

    col.data.materials.clear()
    col.data.materials.append(create_collision_material_from_index(0))
    composite = convert_obj_to_composite(col, SollumType.BOUND_GEOMETRYBVH,
                                       "General (Default)")
    return composite


def collect_tree(root):
    out = [root]
    for c in root.children:
        out.extend(collect_tree(c))
    return out


def make_external_txd(job):
    """texture_mode='ytd': route converted materials' images into a scene
    texture dictionary (external .ytd) instead of embedding in each .ydr."""
    scene = bpy.context.scene
    asset_name = job["asset_name"]
    try:
        txd = scene.sz_txds.new_texture_dictionary(f"{asset_name}_txd")
    except Exception as exc:
        fail("ERR_TEXTURE_MODE_YTD_FAILED", exc)
        return None
    seen = set()
    for mat in bpy.data.materials:
        if not getattr(mat, "use_nodes", False):
            continue
        for n in mat.node_tree.nodes:
            if n.bl_idname != "ShaderNodeTexImage" or not n.image:
                continue
            try:
                n.texture_properties.embedded = False
            except Exception:
                pass
            img = n.image
            if img.name in seen or img.size[0] == 0:
                continue
            seen.add(img.name)
            slot = txd.textures.add()
            slot.image = img  # name derives from filepath stem automatically
    RESULT["texture_mode"] = "ytd"
    return txd.name


def to_drawable_and_export(obj, asset_name, stream_dir, collision_mode, ytyp,
                           lod_dist=150.0, hd_tex_dist=60.0, flags="32",
                           txd_name=None, target_versions=None):
    """Sollumz: drawable convert, add archetype to shared ytyp, export."""
    from Sollumz.sollumz_properties import ArchetypeType, SollumType

    scene = bpy.context.scene
    scene.create_seperate_drawables = False
    scene.auto_create_embedded_col = False
    scene.center_drawable_to_selection = False

    mins, maxs, dims = world_bbox([obj])
    center = (mins + maxs) / 2.0

    col_composite = build_collision(obj, asset_name, collision_mode)

    select_only([obj])
    bpy.ops.sollumz.converttodrawable()
    drawable = next(
        (o for o in scene.objects
         if o.sollum_type == SollumType.DRAWABLE and obj in collect_tree(o)),
        None)
    if drawable is None:
        fail("ERR_NO_DRAWABLE_CREATED")
        return False
    drawable.name = asset_name

    if col_composite is not None:
        col_composite.parent = drawable

    arch = ytyp.new_archetype(ArchetypeType.BASE)
    arch.name = asset_name
    arch.asset = drawable          # update_asset fills asset_type + dictionaries
    arch.lod_dist = lod_dist
    arch.hd_texture_dist = hd_tex_dist
    arch.flags.total = flags
    arch.bb_min = tuple(mins)
    arch.bb_max = tuple(maxs)
    arch.bs_center = tuple(center)
    arch.bs_radius = max(dims) / 2.0
    if txd_name:
        arch.texture_dictionary = txd_name

    select_only(collect_tree(drawable))
    # NATIVE -> compiled .ydr/.ytyp/.ytd (RSC7) for FiveM; CWXML -> .xml
    # sidecars the orchestrator uses for readable gate validation.
    bpy.ops.sollumz.export_assets(
        directory=stream_dir,
        direct_export=True,
        use_custom_settings=True,
        target_formats={"NATIVE", "CWXML"},
        target_versions=target_versions or {"GEN8"},
        limit_to_selected=True,
        export_ytyps=True,
        export_ytyps_include="ALL",
        export_ytds=txd_name is not None,
        export_ytds_include="ALL",
    )
    return True


def record_stream_files(stream_dir):
    """Read back every emitted native file's RSC header.

    The RSC7 magic is shared by Legacy(Gen8) and Enhanced(Gen9); the uint32 at
    offset 4 is the generation discriminator the orchestrator gates on.
    """
    import struct
    out = []
    for dirpath, _dn, fs in os.walk(stream_dir):
        for f in sorted(fs):
            if f.endswith(".xml"):
                continue
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, stream_dir).replace(os.sep, "/")
            entry = {"file": rel, "size": os.path.getsize(p),
                     "magic": None, "rsc_version": None}
            try:
                with open(p, "rb") as fh:
                    hdr = fh.read(8)
                entry["magic"] = hdr[:4].decode("ascii", "replace")
                if len(hdr) == 8 and hdr[:4] == b"RSC7":
                    entry["rsc_version"] = struct.unpack("<I", hdr[4:8])[0]
            except OSError:
                pass
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Motion porting (MOTION_SPEC.md): snapshot_sequence + multi_part_prop_assembly.
# Vehicle-rig modes are quarantined, never attempted headless.
# ---------------------------------------------------------------------------

MOTION_VEHICLE_MODES = {"door_control", "extra_toggle", "vtol_tilt_rotor",
                        "manual_yft_ycd"}
# Project convention for manifest rotations: [x,y,z] degrees decomposed as YXZ.
# This is NOT verified AttachEntityToEntity-native output -- the native takes an
# explicit rotationOrder int and the game's standard layout (order 2) is ZXY.
# Consumers must map our declared YXZ_deg values to their rotationOrder.
FIVEM_EULER_ORDER = "YXZ"
MANIFEST_RUNTIME_STATUS = "manifest_only_requires_consumer"


def purge_non_eval_junk():
    """EC_03 variant for motion modes: removes junk that can't deform meshes,
    but KEEPS empties, parents and armatures until poses are baked."""
    scene = bpy.context.scene
    for o in list(scene.objects):
        if o.type in {"CAMERA", "LIGHT", "SPEAKER", "GPENCIL", "GREASEPENCIL",
                      "GREASEPENCIL_LINEART", "LIGHT_PROBE", "LATTICE"}:
            bpy.data.objects.remove(o, do_unlink=True)
    for o in list(scene.objects):
        if o.type in {"CURVE", "SURFACE", "FONT", "META"}:
            select_only([o])
            try:
                bpy.ops.object.convert(target="MESH")
            except Exception:
                bpy.data.objects.remove(o, do_unlink=True)


def purge_animation_datablocks():
    for o in bpy.context.scene.objects:
        if o.animation_data:
            o.animation_data_clear()
        if o.type == "MESH" and o.data.shape_keys:
            o.shape_key_clear()
    for act in list(bpy.data.actions):
        bpy.data.actions.remove(act)


def apply_exclude_objects(sidecar):
    """Drop user-named objects + their subtrees after import (all modes).

    `exclude_objects` entries are fnmatch patterns matched against object
    names. This is the honest way to kill Sketchfab display bases, env
    spheres and scaffolding that junk-purge correctly keeps for animation.
    """
    import fnmatch
    pats = [str(p) for p in (sidecar.get("exclude_objects") or [])]
    if not pats:
        return
    roots = [o for o in bpy.context.scene.objects
             if any(fnmatch.fnmatchcase(o.name, p) for p in pats)]
    doomed = set()
    stack = list(roots)
    while stack:
        o = stack.pop()
        if o in doomed:
            continue
        doomed.add(o)
        stack.extend(o.children)
    for o in doomed:
        warn(f"WARN_EXCLUDED_OBJECT:{o.name}")
    for o in list(doomed):
        bpy.data.objects.remove(o, do_unlink=True)


def resolve_clip_playback(mc):
    """`motion_config.keep_actions`: substring list selecting clip-sets.

    Sketchfab exporters duplicate every Blender action across every animated
    node as `<Node>|<Action>` muted NLA strips; only one strip per object can
    evaluate. For each object we keep the strips/actions whose name contains
    any keep substring (unmuted + made active) and mute the rest, restoring
    ONE coordinated source animation for sampling.
    """
    keeps = mc.get("keep_actions")
    if not keeps:
        # No explicit selection: the glTF importer parks imported clips in
        # MUTED NLA strips and activates only one animation file-wide.
        # Per-object clips with exactly ONE distinct action name can play
        # unambiguously (parallel per-object animations, e.g. drone blades)
        # -- activate those. Objects with several distinct clips keep all
        # muted: choosing would be a guess (keep_actions exists for that).
        for o in bpy.context.scene.objects:
            ad = o.animation_data
            if ad is None or ad.action is not None:
                continue
            names = {s.action.name for t in ad.nla_tracks
                     for s in t.strips if s.action}
            if len(names) == 1:
                act = bpy.data.actions.get(next(iter(names)))
                if act:
                    ad.action = act
                    for t in ad.nla_tracks:
                        t.mute = True
                    print("[worker] auto-activated single clip:",
                          o.name, "->", act.name)
            elif len(names) > 1:
                # Sketchfab convention: action suffix "<Node>Action" names the
                # node it animates. If exactly one candidate's action-name
                # part matches this object's name, that's the node's own
                # deploy clip -- activate it; else stay muted + warn.
                mine = {n for n in names
                        if len(o.name) >= 3
                        and o.name.lower() in n.split("|")[-1].lower()}
                if len(mine) == 1:
                    act = bpy.data.actions.get(next(iter(mine)))
                    if act:
                        ad.action = act
                        for t in ad.nla_tracks:
                            t.mute = True
                        print("[worker] self-matched clip:", o.name,
                              "->", act.name)
                        continue
                warn("WARN_AMBIGUOUS_ACTIONS:" + o.name + ":" +
                     ",".join(sorted(names)))
        return
    pats = [str(k).lower() for k in keeps]
    hit_any = False
    for o in bpy.context.scene.objects:
        ad = o.animation_data
        if ad is None:
            continue
        keep_names = set()
        cand = []
        if ad.action:
            cand.append(ad.action)
        for t in ad.nla_tracks:
            for s in t.strips:
                if s.action:
                    cand.append(s.action)
        for a in cand:
            if a and any(p in a.name.lower() for p in pats):
                keep_names.add(a.name)
        if not keep_names:
            ad.action = None
            for t in ad.nla_tracks:
                t.mute = True
            continue
        hit_any = True
        kept = [a for a in cand if a.name in keep_names]
        ad.action = kept[0]
        for t in ad.nla_tracks:
            t.mute = all(s.action is None or s.action.name not in keep_names
                         for s in t.strips)
        print("[worker] keep_actions:", o.name, "->", kept[0].name)
    if not hit_any:
        fail("ERR_KEEP_ACTIONS_NO_MATCH:" + ",".join(str(k) for k in keeps))


def frame_window(mc):
    """Resolve {start,end} frame ints -> (start,end) or None after fail()."""
    fw = mc.get("frame_window") or {}
    start, end = fw.get("start_frame"), fw.get("end_frame")
    if start is None or end is None:
        rng = None
        for a in bpy.data.actions:
            r = a.frame_range
            rng = r if rng is None else (min(rng[0], r[0]), max(rng[1], r[1]))
        if rng is None:
            rng = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
        start = int(round(rng[0])) if start is None else int(start)
        end = int(round(rng[1])) if end is None else int(end)
    if int(end) <= int(start):
        fail("ERR_FRAME_WINDOW_EMPTY")
        return None
    return int(start), int(end)


def eval_baked_obj(src):
    """Evaluated (modifiers+deform applied) world-space mesh copy of src.

    Verts are baked into world coordinates and the new object's matrix is
    reset to identity so nothing double-applies on join/export.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    ev = src.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(ev, depsgraph=dg)
    me.transform(ev.matrix_world)
    ob = bpy.data.objects.new(src.name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.matrix_world = Matrix.Identity(4)
    return ob


def eval_world_centroid(src):
    """Mean of evaluated world verts -> Vector or None (degenerate)."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = src.evaluated_get(dg)
    me = ev.to_mesh()
    n = len(me.vertices)
    if n == 0:
        ev.to_mesh_clear()
        return None
    mw = ev.matrix_world
    c = Vector()
    for v in me.vertices:
        c += mw @ v.co
    ev.to_mesh_clear()
    return c / n


def matrices_close(a, b, eps=1e-5):
    return all(abs(a[i][j] - b[i][j]) <= eps for i in range(4) for j in range(4))


def normalization_transforms(objs, sidecar):
    """EC_05 as matrices on already-baked world verts.

    Returns (N, R): N = R_offset @ uniform_scale applied to vertices/pivots;
    R (rotation only) for converting sampled rotations into normalized space:
    absolute rotations left-multiply (rot' = R @ rot), deltas conjugate
    (delta' = R @ delta @ R^-1) -- equivalent, per MOTION_SPEC safeguard S1.
    """
    _, _, dims = world_bbox(objs)
    factor = 1.0
    th = sidecar.get("target_height_meters")
    if th:
        if dims.z > 0:
            factor = float(th) / dims.z
    else:
        m = max(dims.x, dims.y, dims.z)
        if m > 100.0:
            factor = 0.01
            warn("WARN_AUTO_SCALED_CM_TO_M")
        elif m < 0.005:
            factor = 100.0
            warn("WARN_AUTO_SCALED_UP")
    factor *= float(sidecar.get("scale_multiplier", 1.0))
    rot = sidecar.get("rotation_offset_deg") or [0.0, 0.0, 0.0]
    rx, ry, rz = (math.radians(float(r)) for r in rot)
    R = (Matrix.Rotation(rz, 4, "Z") @ Matrix.Rotation(ry, 4, "Y")
         @ Matrix.Rotation(rx, 4, "X"))
    return R @ Matrix.Scale(factor, 4), R


def fivem_euler(rot_mat):
    """Rotation matrix -> [x,y,z] degrees in FiveM/AttachEntityToEntity order.

    MOTION_SPEC safeguard S3: Blender XYZ eulers exported raw make parts swing
    on wrong axes; convert via YXZ and degrees.
    """
    e = rot_mat.to_euler(FIVEM_EULER_ORDER)
    return [round(math.degrees(e.x), 4), round(math.degrees(e.y), 4),
            round(math.degrees(e.z), 4)]


def lua_literal(v):
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "{ " + ", ".join(lua_literal(x) for x in v) + " }"
    if isinstance(v, dict):
        return "{ " + ", ".join(
            f'["{k}"] = {lua_literal(x)}' for k, x in v.items()) + " }"
    return "nil"


def emit_manifest(job_dir, filename, header_comment, data):
    out_dir = os.path.join(job_dir, "lua_out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        for line in header_comment.strip().splitlines():
            fh.write(f"-- {line}\n")
        fh.write("return " + lua_literal(data) + "\n")
    try:
        with open(path + ".json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except (TypeError, ValueError):
        pass
    RESULT["artifacts"].append(filename)
    return path


def delete_non_baked(baked_objs):
    keep = set(baked_objs)
    for o in list(bpy.context.scene.objects):
        if o not in keep:
            bpy.data.objects.remove(o, do_unlink=True)


def motion_snapshot_sequence(job, mc):
    """Bake world geometry at sampled frames -> <base>_snapNN drawables."""
    scene = bpy.context.scene
    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    cfg = mc.get("snapshot_sequence") or {}

    window = frame_window(mc)
    if window is None:
        return None
    f_start, f_end = window

    sample_type = cfg.get("sample_type", "ratio")
    raw = cfg.get("samples") or [0.0, 0.5, 1.0]
    if len(raw) > 12:
        fail("ERR_TOO_MANY_SAMPLES")
        return None
    frames = []
    for s in raw:
        f = int(round(f_start + float(s) * (f_end - f_start))) \
            if sample_type == "ratio" else int(s)
        if not (f_start <= f <= f_end):
            fail(f"ERR_FRAME_OUT_OF_BOUNDS:{f}")
            return None
        frames.append(f)

    scene.frame_set(f_start)
    purge_non_eval_junk()
    src = meshes()
    if not src:
        fail("ERR_NO_MESH_AFTER_CLEANUP")
        return None

    snaps = []
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        baked = [b for b in (eval_baked_obj(o) for o in src)
                 if len(b.data.vertices) and len(b.data.polygons)]
        if not baked:
            fail(f"ERR_NO_GEOMETRY_AT_FRAME:{f}")
            return None
        snaps.append(baked)

    delete_non_baked([o for s in snaps for o in s])
    purge_animation_datablocks()
    warn("WARN_STRIPPED_ANIMATION_OR_ARMATURE")

    all_objs = [o for s in snaps for o in s]
    N, _ = normalization_transforms(all_objs, sidecar)
    for o in all_objs:
        o.data.transform(N)
        o.data.update()

    policy = cfg.get("pivot_policy", "consistent_origin")
    if policy != "keep_origin":
        mins, maxs, _ = world_bbox(all_objs)
        off = Vector((-(mins.x + maxs.x) / 2.0, -(mins.y + maxs.y) / 2.0,
                      -mins.z))
        for o in all_objs:
            for v in o.data.vertices:
                v.co += off
            o.data.update()

    targets = []
    for i, snap in enumerate(snaps):
        if len(snap) > 1:
            select_only(snap)
            bpy.ops.object.join()
        ob = snap[0]
        ob.name = f"{asset_name}_snap{i:02d}"
        targets.append((ob.name, ob))

    hold = int(cfg.get("intermediate_hold_ms", 100))
    steps = [{"model": t[0],
              "hold_ms": hold if 0 < i < len(targets) - 1 else -1}
             for i, t in enumerate(targets)]
    emit_manifest(job["job_dir"], "sequence_manifest.lua",
                  "Auto-generated snapshot flipbook (MOTION_SPEC method 1).\n"
                  "hold_ms -1 = hold until scripted trigger. Play in order with\n"
                  "CreateObject swaps or AttachEntityToEntity on a carrier.\n"
                  "runtime_status: manifest only - a runtime consumer is required.",
                  {"mode": "snapshot_sequence",
                   "runtime_status": MANIFEST_RUNTIME_STATUS,
                   "frame_window": {"start_frame": f_start,
                                    "end_frame": f_end},
                   "pivot_policy": policy,
                   "steps": steps})
    return targets


def resolve_bone_delta(obj, bone_samples):
    """Armature-deformed part: find the single driving bone's world matrices.

    Returns (hinge_vec, W0, W1) or None when ambiguous/zero-delta
    (MOTION_SPEC safeguard S2).
    """
    arm_names = {m.object.name for m in obj.modifiers
                 if m.type == "ARMATURE" and m.object}
    if obj.parent_type == "BONE" and obj.parent:
        arm_names.add(obj.parent.name)
    moved = []
    for an in arm_names:
        for bn, (w0, w1) in bone_samples.get(an, {}).items():
            if not matrices_close(w0, w1):
                moved.append((an, bn, w0, w1))
    if len(moved) != 1:
        return None
    _, _, w0, w1 = moved[0]
    return w0.to_translation(), w0, w1


def rotation_delta_q(a_mw, b_mw):
    """Quaternion of b @ a^-1 -- the world-space rotation between poses."""
    return (b_mw @ a_mw.inverted()).to_quaternion()


def spin_metrics(samples):
    """Per-frame world matrices -> (signed_deg, cum_deg, axis, travel,
    tail_rate). Guardrail G2: a rotor's NET rotation wraps to <360 deg, so
    endpoints can't see it -- but unsigned cumulative also lies (a
    170deg-out-and-back hinge accumulates 340deg, and a deploy arm can
    sweep 900deg+ one way). tail_rate is the mean deg/frame over the last
    ~1/8 of deltas: a rotor is still moving when the clip ends, a deploy
    swing settles.
    """
    cum = 0.0
    signed = 0.0
    axis_acc = Vector((0.0, 0.0, 0.0))
    t0 = samples[0].to_translation()
    travel = 0.0
    qs = [rotation_delta_q(samples[i], samples[i + 1])
          for i in range(len(samples) - 1)]
    for q in qs:
        ax = q.axis
        if ax.length > 1e-9:
            ax = ax.normalized()
            if ax.dot(axis_acc) < 0:
                ax = -ax
            axis_acc += ax * math.degrees(q.angle)
    axis = axis_acc.normalized() if axis_acc.length > 1e-6 else None
    for i, q in enumerate(qs):
        ang = math.degrees(q.angle)
        cum += ang
        if axis is not None:
            signed += ang if q.axis.normalized().dot(axis) >= 0 else -ang
        travel = max(travel,
                     (samples[i + 1].to_translation() - t0).length)
    tail = max(1, len(qs) // 8)
    tail_rate = (sum(math.degrees(q.angle) for q in qs[-tail:]) / tail
                 if qs else 0.0)
    return signed, cum, axis, travel, tail_rate


def spin_axis_point(mw_a, mw_b, axis):
    """Instantaneous centre of rotation for delta mw_b @ mw_a^-1 about axis.

    Solves (I - R) p = q restricted to the plane perpendicular to `axis`
    (the q component along the axis is always zero for a rigid rotation).
    Returns the point on the axis line nearest the world origin, or None
    when the delta is too small to solve stably.
    """
    T = mw_b @ mw_a.inverted()
    R = T.to_3x3()
    q = T.to_translation()
    a = axis.normalized()
    # signed angle of this delta about +a
    cos_t = max(-1.0, min(1.0, (R[0][0] + R[1][1] + R[2][2] - 1.0) / 2.0))
    theta = math.acos(cos_t)
    qd = rotation_delta_q(mw_a, mw_b)
    if qd.axis.dot(a) < 0:
        theta = -theta
    if abs(theta) < math.radians(2.0):
        return None
    u = a.orthogonal().normalized()
    v = a.cross(u)
    c = 1.0 - math.cos(theta)
    s = math.sin(theta)
    qu, qv = q.dot(u), q.dot(v)
    det = c * c + s * s
    if det < 1e-12:
        return None
    alpha = (c * qu - s * qv) / det
    beta = (s * qu + c * qv) / det
    return alpha * u + beta * v


def motion_multi_part(job, mc):
    """Split into <base>_body + hinge <base>_partNN + spin <base>_propNN."""
    scene = bpy.context.scene
    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    cfg = mc.get("assembly_config") or {}
    part_map = mc.get("part_map") or {}
    max_parts = int(cfg.get("max_parts", 12))
    spin_min_deg = float(cfg.get("spin_min_deg", 300.0))
    spin_max_travel = float(cfg.get("spin_max_travel_m", 0.05))
    spin_tail_min = float(cfg.get("spin_tail_min_dpf", 0.1))
    spin_patterns = [str(x).lower() for x in cfg.get(
        "spin_name_patterns",
        ["blade", "rotor", "prop", "propeller", "fan", "turb", "impeller"])]

    window = frame_window(mc)
    if window is None:
        return None
    f_start, f_end = window
    fps = float(scene.render.fps or scene.render.fps_base or 24)
    window_s = max((f_end - f_start) / fps, 1e-6)

    # --- SAMPLE FIRST (safeguard S1): nothing applied/stripped yet.
    # Per-frame sampling (stride>=1) is REQUIRED: a rotor net-rotates <360deg
    # between window endpoints, so endpoint deltas can't see the spin.
    stride = max(1, (f_end - f_start) // 600)
    sample_frames = list(range(f_start, f_end + 1, stride))
    if sample_frames[-1] != f_end:
        sample_frames.append(f_end)

    scene.frame_set(f_start)
    bpy.context.view_layer.update()
    purge_non_eval_junk()
    src = meshes()
    if not src:
        fail("ERR_NO_MESH_AFTER_CLEANUP")
        return None

    mw_samples = {o.name: [o.matrix_world.copy()] for o in src}
    c0 = {o.name: eval_world_centroid(o) for o in src}
    bones0 = {a.name: {b.name: a.matrix_world.copy() @ b.matrix.copy()
                       for b in a.pose.bones}
              for a in scene.objects if a.type == "ARMATURE"}
    bones1 = {}
    c1 = {}

    for f in sample_frames[1:]:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for o in src:
            mw_samples[o.name].append(o.matrix_world.copy())
        if f == sample_frames[-1]:
            c1 = {o.name: eval_world_centroid(o) for o in src}
            bones1 = {a.name: {b.name: a.matrix_world.copy() @ b.matrix.copy()
                               for b in a.pose.bones}
                      for a in scene.objects if a.type == "ARMATURE"}
    mw0 = {n: s[0] for n, s in mw_samples.items()}
    mw1 = {n: s[-1] for n, s in mw_samples.items()}
    bone_samples = {an: {bn: (bones0[an][bn], bones1[an][bn])
                         for bn in bones0.get(an, {}) if bn in bones1.get(an, {})}
                    for an in bones0}
    src_by_name = {o.name: o for o in src}
    moved = {n: any(not matrices_close(s[0], w) for w in s[1:])
             for n, s in mw_samples.items()}

    scene.frame_set(f_start)

    # --- classify: part_map groups (list OR dict values) first, then auto
    # movers (safeguard S4: static loose meshes merge into _body). ---
    groups = []        # {label|None, members, kind|None, axis_cfg, rpm_cfg}
    member_of = {}
    forced_body = set()  # part_map "_body": meshes pinned into the carrier
    forced_order = []
    if part_map:
        known = {o.name for o in src}
        for label, spec in part_map.items():
            if label == "_body":
                members = (list(spec.get("meshes") or [])
                           if isinstance(spec, dict) else list(spec))
                missing = [m for m in members if m not in known]
                if missing:
                    fail("ERR_UNMAPPED_PART:" + ",".join(sorted(missing)))
                    return None
                forced_body.update(members)
                forced_order.extend(members)
                continue
            if isinstance(spec, dict):
                members = list(spec.get("meshes") or [])
                if spec.get("mesh") and spec["mesh"] not in members:
                    members.append(spec["mesh"])
                g = {"label": label, "members": members,
                     "kind": spec.get("kind"),
                     "axis_cfg": spec.get("spin_axis"),
                     "rpm_cfg": spec.get("spin_rpm"),
                     "pivot_cfg": spec.get("pivot")}
            else:
                g = {"label": label, "members": list(spec), "kind": None,
                     "axis_cfg": None, "rpm_cfg": None}
            missing = [m for m in g["members"] if m not in known]
            if missing:
                fail("ERR_UNMAPPED_PART:" + ",".join(sorted(missing)))
                return None
            overlap = [m for m in g["members"] if m in member_of]
            if overlap:
                fail("ERR_UNMAPPED_PART:" + ",".join(sorted(overlap)))
                return None
            for m in g["members"]:
                member_of[m] = g
            groups.append(g)
    for o in src:
        if o.name in member_of or o.name in forced_body:
            continue
        geo_moved = (c0[o.name] is not None and c1.get(o.name) is not None
                     and (c0[o.name] - c1[o.name]).length > 1e-4)
        if moved[o.name] or geo_moved:
            g = {"label": None, "members": [o.name], "kind": None,
                 "axis_cfg": None, "rpm_cfg": None, "pivot_cfg": None}
            member_of[o.name] = g
            groups.append(g)

    body_names = [o.name for o in src
                  if o.name not in member_of or o.name in forced_body]
    # Carrier-delta correction (moving-body sources): deployed part values
    # must sit in the body's own frame, so world-at-deployed is mapped back
    # through the body's world delta D_b = W_ref_end @ W_ref_start^-1.
    # Static carrier -> D_b_inv is identity -> output identical to before.
    D_b_inv = Matrix.Identity(4)
    if body_names:
        ref = forced_order[0] if forced_order else body_names[0]
        D_b_inv = mw_samples[ref][0] @ mw_samples[ref][-1].inverted()
        _dq = D_b_inv.to_quaternion()
        if math.degrees(_dq.angle) > 1.0 or \
                D_b_inv.to_translation().length > 0.01:
            warn(f"WARN_CARRIER_MOTION_RELATIVIZED:{ref}:"
                 f"{math.degrees(_dq.angle):.1f}deg")
    if len(groups) > max_parts:
        fail("ERR_TOO_MANY_PARTS")
        return None
    if groups and not body_names:
        fail("ERR_NO_STATIC_BODY")
        return None

    # --- kind resolution + per-group geometry, BEFORE bake deletes sources.
    # Spin detection needs >300deg cumulative world rotation (G2); explicit
    # kind wins; dict params (spin_axis/spin_rpm) imply spin intent. ---
    spin_info = {}   # group index -> {axis_w, pivot_w, rpm}
    part_geom = {}   # group index -> (hinge, W0, W1) from first MOVED member
    for gi, g in enumerate(groups):
        n = g["members"][0]
        signed, cum, axis_w, _trav, tail_rate = spin_metrics(mw_samples[n])
        net = abs(signed)
        has_cfg_params = g["axis_cfg"] is not None or g["rpm_cfg"] is not None
        if g["kind"] is None:
            # Auto-spin needs ONE-DIRECTIONAL net rotation >= spin_min_deg,
            # still turning when the clip ends (rotors don't settle), AND
            # evidence it lives on a hub: either artist-named like a rotor
            # or nearly translationless relative to the carrier. A swinging
            # deploy arm can exceed the angle threshold one-way (grunt's
            # plates sweep 1347deg) but decelerates to a stop.
            if net >= spin_min_deg:
                name_hit = any(p in n.lower() for p in spin_patterns)
                rel_travel = _trav
                if not name_hit and body_names:
                    ref_s = mw_samples[ref]
                    rel0 = mw_samples[n][0].to_translation()
                    rel_travel = max(
                        ((ref_s[0] @ ref_s[k].inverted() @ mw_samples[n][k])
                         .to_translation() - rel0).length
                        for k in range(len(ref_s)))
                moving = tail_rate >= spin_tail_min
                if moving and (name_hit or rel_travel <= spin_max_travel):
                    g["kind"] = "spin"
                    warn(f"WARN_AUTO_SPIN_PART:{n}")
                else:
                    warn(f"WARN_HIGH_ROTATION_HINGE:{n}:{net:.0f}deg")
                    g["kind"] = "hinge"
            elif has_cfg_params:
                g["kind"] = "spin"
            else:
                g["kind"] = "hinge"
        if g["kind"] == "spin":
            rpm = g["rpm_cfg"]
            if rpm is None or str(rpm) == "auto":
                rpm = net / 360.0 / window_s * 60.0 if net >= spin_min_deg \
                    else None
                if rpm is None:
                    fail("ERR_SPIN_RPM_UNRESOLVED:" + n)
                    return None
            if net >= spin_min_deg and axis_w is not None:
                p_line = None
                for i in range(len(mw_samples[n]) - 1):
                    p_line = spin_axis_point(mw_samples[n][i],
                                             mw_samples[n][i + 1], axis_w)
                    if p_line is not None:
                        break
                cent = c0.get(n)
                if p_line is not None and cent is not None:
                    pivot_w = p_line + axis_w * ((cent - p_line).dot(axis_w))
                elif p_line is not None:
                    pivot_w = p_line
                else:
                    pivot_w = mw0[n].to_translation()
                    warn(f"WARN_SPIN_PIVOT_FALLBACK:{n}")
                # keep (axis,rpm) sign-consistent: negative net means the
                # rotor turns against the mean axis -> flip so rpm>0 holds
                axis_use = axis_w if signed >= 0 else -axis_w
            else:
                if g["axis_cfg"] is None or str(g["axis_cfg"]) == "auto":
                    fail("ERR_SPIN_AXIS_UNRESOLVED:" + n)
                    return None
                axis_use = Vector([float(x) for x in g["axis_cfg"]])
                if axis_use.length < 1e-9:
                    fail("ERR_SPIN_AXIS_UNRESOLVED:" + n)
                    return None
                axis_use = axis_use.normalized()
                pivot_w = mw0[n].to_translation()
            spin_info[gi] = {"axis_w": axis_use, "pivot_w": pivot_w,
                             "rpm": float(rpm), "W0": mw0[n]}
            continue
        # hinge: first MOVED member drives the group's delta; static mapped
        # members join rigidly at folded pose (documented group semantics).
        driver = None
        for m in g["members"]:
            if moved[m] or (c0.get(m) is not None and c1.get(m) is not None
                            and (c0[m] - c1[m]).length > 1e-4):
                driver = m
                break
        if driver is None:
            w0 = mw0[n]
            part_geom[gi] = (w0.to_translation(), w0, w0.copy())
        elif not matrices_close(mw0[driver], mw1[driver]):
            part_geom[gi] = (mw0[driver].to_translation(),
                             mw0[driver], mw1[driver])
        else:
            res = resolve_bone_delta(src_by_name[driver], bone_samples)
            if res is None:
                fail("ERR_ARMATURE_DEFORM_REQUIRES_SNAPSHOT_MODE")
                return None
            part_geom[gi] = res

    # --- bake folded-pose world geometry for everything. ---
    baked = {o.name: eval_baked_obj(o) for o in src}
    baked = {n: b for n, b in baked.items()
             if len(b.data.vertices) and len(b.data.polygons)}
    delete_non_baked(list(baked.values()))
    purge_animation_datablocks()
    warn("WARN_STRIPPED_ANIMATION_OR_ARMATURE")

    # --- body first: join static meshes. NOTE: join() deletes non-active
    # objects, so every name->obj lookup must happen before this and the live
    # object set must be re-fetched via meshes() afterward. ---
    targets = []
    body_off = Vector((0, 0, 0))
    if body_names:
        bobjs = [baked[n] for n in body_names if n in baked]
        if len(bobjs) > 1:
            select_only(bobjs)
            bpy.ops.object.join()
        body = bobjs[0]
        body.name = f"{asset_name}_body"
    else:
        # fully static scene masquerading as multi-part: join all into body
        select_only(list(baked.values()))
        bpy.ops.object.join()
        body = bpy.context.view_layer.objects.active
        body.name = f"{asset_name}_body"
        targets.append((body.name, body))
        warn("WARN_ALL_STATIC_MERGED_TO_BODY")

    all_objs = meshes()
    N, R = normalization_transforms(all_objs, sidecar)
    if mc.get("rig_recipe"):
        # workbench recipe: objects were baked in raw world space, so apply
        # the exact composed transform the user saw -- never re-normalize
        N = recipe_transform_matrix(mc["rig_recipe"])
        R = N.to_quaternion().to_matrix().to_4x4()
    for o in all_objs:
        o.data.transform(N)
        o.data.update()

    if body_names:
        mode = sidecar.get("pivot_mode", "bottom_center")
        if mode != "keep_origin":
            mins, maxs, _ = world_bbox([body])
            body_off = (Vector((-(mins.x + maxs.x) / 2.0,
                                -(mins.y + maxs.y) / 2.0, -mins.z))
                        if mode == "bottom_center" else -(mins + maxs) / 2.0)
        if body_off.length > 0:
            for o in all_objs:
                for v in o.data.vertices:
                    v.co += body_off
                o.data.update()
        targets.append((body.name, body))

    # --- parts: grouped join, pivot recenter, per-kind manifest entries. ---
    parts_meta = []
    part_i = prop_i = 0
    for gi, g in enumerate(groups):
        bobs = [baked[m] for m in g["members"] if m in baked]
        if not bobs:
            continue
        if len(bobs) > 1:
            select_only(bobs)
            bpy.ops.object.join()
        b = bobs[0]
        if g["kind"] == "spin":
            prop_i += 1
            info = spin_info[gi]
            if g.get("pivot_cfg"):
                # workbench recipe pivot: already in normalized recipe space
                pivot_n = Vector(g["pivot_cfg"])
            else:
                pivot_n = N @ info["pivot_w"] + body_off
            axis_body = (R.to_3x3() @ info["axis_w"]).normalized()
            label = g["label"] or f"{asset_name}_prop{prop_i:02d}"
            entry = {"kind": "spin",
                     "source_objects": list(g["members"]),
                     "attach_offset": [round(pivot_n.x, 4), round(pivot_n.y, 4),
                                       round(pivot_n.z, 4)],
                     "initial_rot": fivem_euler(R @ info["W0"]),
                     "spin_axis": [round(axis_body.x, 4), round(axis_body.y, 4),
                                   round(axis_body.z, 4)],
                     "spin_rpm": round(info["rpm"], 2)}
            hinge_n = pivot_n
        else:
            part_i += 1
            hinge, W0, W1 = part_geom[gi]
            W1_rel = D_b_inv @ W1
            if g.get("pivot_cfg"):
                # workbench recipe pivot: already in normalized recipe space
                hinge_n = Vector(g["pivot_cfg"])
                dep_n = hinge_n
            else:
                hinge_n = N @ hinge + body_off
                dep_n = N @ W1_rel.to_translation() + body_off
            label = g["label"] or f"{asset_name}_part{part_i:02d}"
            entry = {"kind": "hinge",
                     "source_objects": list(g["members"]),
                     "attach_offset": [round(hinge_n.x, 4), round(hinge_n.y, 4),
                                       round(hinge_n.z, 4)],
                     "deployed_offset": [round(dep_n.x, 4), round(dep_n.y, 4),
                                         round(dep_n.z, 4)],
                     "folded_rot": fivem_euler(R @ W0),
                     "deployed_rot": fivem_euler(R @ W1_rel)}
        for v in b.data.vertices:
            v.co -= hinge_n
        b.data.update()
        label = sanitize_target_name(label, asset_name)
        if label is None:
            fail("ERR_PREFLIGHT_BAD_NAME:" + str(g["label"]))
            return None
        b.name = label
        entry["model"] = label
        targets.append((b.name, b))
        parts_meta.append(entry)

    if parts_meta:
        emit_manifest(job["job_dir"], "assembly_manifest.lua",
                      "Auto-generated multi-part assembly (MOTION_SPEC method 2).\n"
                      "Rotations are degrees in the declared YXZ_deg project\n"
                      "convention -- application order Y,X,Z (M=Rz*Rx*Ry),\n"
                      "which maps to AttachEntityToEntity rotationOrder=4\n"
                      "(eRotationOrder ROT_YXZ), NOT order 2.\n"
                      "attach_offset / deployed_offset are body-local space\n"
                      "(body grounded at 0,0,0).\n"
                      "Usage: attach each part to the body entity at\n"
                      "attach_offset with folded_rot/initial_rot. kind=hinge:\n"
                      "interpolate offset->deployed_offset + rot->deployed_rot\n"
                      "over transition_ms. kind=spin: rotate continuously\n"
                      "about attach_offset on spin_axis at spin_rpm (G2 -\n"
                      "never a folded/deployed hinge angle).\n"
                      "runtime_status: manifest only - a runtime consumer is required.",
                      {"mode": "multi_part_prop_assembly",
                       "runtime_status": MANIFEST_RUNTIME_STATUS,
                       "rotation_order": "YXZ_deg",
                       "frame_window": {"start_frame": f_start,
                                        "end_frame": f_end},
                       "ai_carrier_archetype": cfg.get(
                           "ai_carrier_archetype", "frogger"),
                       "transition_ms": int(cfg.get("transition_ms", 1200)),
                       "body": targets[0][0],
                       "parts": parts_meta})
    return targets


# ---------------------------------------------------------------------------
# ped_clothing: rigged meshes + shared skeleton -> skinned drawable dict (.ydd).
# Never .ydr/.ytyp -- no prop fallback, no strip, no join.
# ---------------------------------------------------------------------------

def ped_remove_junk(arm):
    """EC_03 ped variant: kill non-mesh junk but keep the armature, its
    ancestor empties, and existing mesh/armature parenting intact."""
    scene = bpy.context.scene
    for o in list(scene.objects):
        if o.type in {"CAMERA", "LIGHT", "SPEAKER", "GPENCIL", "GREASEPENCIL",
                      "GREASEPENCIL_LINEART", "LIGHT_PROBE", "LATTICE"}:
            bpy.data.objects.remove(o, do_unlink=True)
    for o in list(scene.objects):
        if o.type in {"CURVE", "SURFACE", "FONT", "META"}:
            select_only([o])
            try:
                bpy.ops.object.convert(target="MESH")
            except Exception:
                bpy.data.objects.remove(o, do_unlink=True)
    keep = set()
    p = arm.parent
    while p is not None:
        keep.add(p)
        p = p.parent
    for o in list(scene.objects):
        if o.type != "EMPTY" or o in keep:
            continue
        for c in list(o.children):
            mw = c.matrix_world.copy()
            c.parent = o.parent
            c.matrix_world = mw
        bpy.data.objects.remove(o, do_unlink=True)


def transform_armature_rest(arm, M):
    """Rigid-map every bone's rest transform by M.

    THE ONLY permitted edit_bones mutation: bone rest data lives in armature
    space and cannot be folded through mesh vertices. head/tail are set from
    M @ old_frame, then roll is compensated so each bone's full local frame
    equals M @ old_frame (head/tail alone lose the X/Z orientation under
    rotation).
    """
    if matrices_close(M, Matrix.Identity(4)):
        return
    select_only([arm])
    bpy.ops.object.mode_set(mode="EDIT")
    desired = {}
    for eb in arm.data.edit_bones:
        m_new = M @ eb.matrix.copy()
        desired[eb.name] = m_new
        eb.head = m_new.to_translation()
        eb.tail = m_new @ Vector((0.0, eb.length, 0.0))
    bpy.context.view_layer.update()
    for eb in arm.data.edit_bones:
        m_new = desired[eb.name]
        y = eb.tail - eb.head
        if y.length < 1e-8:
            continue
        y = y.normalized()
        cur_x = (eb.matrix.to_3x3() @ Vector((1, 0, 0))).normalized()
        want_x = (m_new.to_3x3().normalized() @ Vector((1, 0, 0))
                  ).normalized()
        cx = cur_x - y * cur_x.dot(y)
        wx = want_x - y * want_x.dot(y)
        if cx.length < 1e-8 or wx.length < 1e-8:
            continue
        cx.normalize()
        wx.normalize()
        eb.roll += math.atan2(cx.cross(wx).dot(y), cx.dot(wx))
    bpy.ops.object.mode_set(mode="OBJECT")


def ped_validate_weights(obj, arm, require):
    """Actual weight assignments vs the skeleton -- not just vgroup existence."""
    bone_names = {b.name for b in arm.data.bones}
    unknown = [g.name for g in obj.vertex_groups if g.name not in bone_names]
    if unknown:
        fail(f"ERR_PED_SKELETON_MISMATCH:{obj.name}:{unknown[0]}")
        return False
    matched = [g for g in obj.vertex_groups if g.name in bone_names]
    weighted = 0
    over4 = False
    for v in obj.data.vertices:
        live = [e for e in v.groups if e.weight > 0]
        if live:
            weighted += 1
        if len(live) > 4:
            over4 = True
    if not matched or weighted == 0:
        if require:
            fail(f"ERR_PED_NO_WEIGHTS:{obj.name}")
            return False
        warn(f"WARN_PED_MESH_UNWEIGHTED:{obj.name}")
        return True
    if over4:
        for v in obj.data.vertices:
            live = sorted((e for e in v.groups if e.weight > 0),
                          key=lambda e: e.weight, reverse=True)
            for e in live[4:]:
                obj.vertex_groups[e.group].remove([v.index])
        warn(f"WARN_WEIGHT_INFLUENCES_TRUNCATED:{obj.name}")
    return True


def ped_normal_push(obj, meters):
    """clothing_fix.normal_push_meters: displace verts along smooth normals."""
    me = obj.data
    me.update()
    for v in me.vertices:
        v.co += v.normal.normalized() * meters
    me.update()


def ped_skeleton_bounds_from_xml(ydd_xml):
    """(mins, maxs) of bone rest positions in a CWXML .ydd sidecar, or None."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(ydd_xml).getroot()
    except ET.ParseError:
        return None
    pts = []
    for el in root.iter():
        tag = el.tag.lower()
        if "translat" not in tag and tag != "position":
            continue
        try:
            pts.append(Vector((float(el.get("x", el.get("X", 0)) or 0),
                               float(el.get("y", el.get("Y", 0)) or 0),
                               float(el.get("z", el.get("Z", 0)) or 0))))
        except (TypeError, ValueError):
            continue
    if not pts:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    return Vector((min(xs), min(ys), min(zs))), \
        Vector((max(xs), max(ys), max(zs)))


def process_ped(job):
    """ped_clothing pipeline: rigged meshes + shared skeleton -> .ydd.

    Forbidden vs the prop path: strip_animation_and_armatures, join(),
    automatic shape-key removal, transform_apply on rigged meshes, vertex
    decimation, any re-parenting except the matrix_world-preserving attach of
    meshes to their armature (required for drawable-model discovery).
    """
    from Sollumz.sollumz_properties import LODLevel, SollumType

    scene = bpy.context.scene
    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    stream_dir = job["stream_dir"]
    clothing_fix = sidecar.get("clothing_fix") or {}

    if clothing_fix.get("cloth"):
        # cloth/YLD has no verified authoring path in this toolchain
        RESULT["unsupported"].append("cloth_physics")
        fail("ERR_PED_CLOTH_PHYSICS_UNSUPPORTED")
        return False

    arms = [o for o in scene.objects if o.type == "ARMATURE"]
    if not arms:
        fail("ERR_PED_NO_ARMATURE")
        return False
    if len(arms) > 1:
        fail("ERR_PED_MULTI_ARMATURE")
        return False
    arm = arms[0]

    ped_remove_junk(arm)
    drop_degenerate()
    ms = meshes()
    if not ms:
        fail("ERR_NO_MESH_AFTER_CLEANUP")
        return False

    require = bool(sidecar.get("require_weights", True))
    for o in ms:
        if not ped_validate_weights(o, arm, require):
            return False
        if not any(m.type == "ARMATURE" for m in o.modifiers):
            mod = o.modifiers.new("sfp_armature", "ARMATURE")
            mod.object = arm

    push = float(clothing_fix.get("normal_push_meters") or 0.0)
    if abs(push) > 1e-12:
        for o in ms:
            if o.data.shape_keys:
                select_only([o])
                try:
                    bpy.ops.object.shape_key_remove(all=True)
                except Exception:
                    o.shape_key_clear()
                warn(f"WARN_SHAPE_KEYS_REMOVED_FOR_CLOTHING_FIX:{o.name}")
        for o in ms:
            ped_normal_push(o, push)

    # normalize: bake world transform into verts; rigid-map skeleton equally
    # so exported bind pose and skeleton stay consistent.
    for o in ms:
        mw = o.matrix_world.copy()
        o.data.transform(mw)
        o.data.update()
        o.matrix_world = Matrix.Identity(4)
    N, _R = normalization_transforms(ms, sidecar)
    pivot = sidecar.get("pivot_mode", "bottom_center")
    off = Vector((0.0, 0.0, 0.0))
    if pivot != "keep_origin":
        mins, maxs, _ = world_bbox(ms)
        off = (Vector((-(mins.x + maxs.x) / 2.0, -(mins.y + maxs.y) / 2.0,
                      -mins.z))
               if pivot == "bottom_center" else -(mins + maxs) / 2.0)
    M = Matrix.Translation(off) @ N
    for o in ms:
        o.data.transform(M)
        o.data.update()
    transform_armature_rest(arm, M @ arm.matrix_world)
    arm.matrix_world = Matrix.Identity(4)

    max_tris = int(sidecar.get("max_triangles", 35000))
    for o in ms:
        if tri_count(o) > max_tris:
            # decimation destroys vertex weights -- fail, never decimate
            fail(f"ERR_POLYCOUNT_EXCEEDS_HARD_CAP:{o.name}")
            return False
    RESULT["skipped"].append("auto_decimate:forbidden_for_skinned")
    return ped_export_finalize(job, arm, ms)


def preflight_lint(obj, name):
    """Hard-fail checks before converttodrawable -- catches the conditions
    Sollumz's exporter would silently warn/abort on."""
    from Sollumz.sollumz_properties import MaterialType

    me = obj.data
    if len(me.vertices) == 0 or len(me.polygons) == 0:
        fail("ERR_PREFLIGHT_EMPTY_MESH:" + name)
        return False
    if len(me.uv_layers) == 0:
        fail("ERR_PREFLIGHT_NO_UV:" + name)
        return False
    if not obj.material_slots:
        fail("ERR_PREFLIGHT_NO_SHADER_MAT:" + name)
        return False
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or getattr(mat, "sollum_type", None) != MaterialType.SHADER:
            fail("ERR_PREFLIGHT_NO_SHADER_MAT:" + name)
            return False
        for n in mat.node_tree.nodes:
            if n.bl_idname == "ShaderNodeTexImage" and n.image:
                img = n.image
                if img.size[0] == 0 or img.size[1] == 0:
                    fail(f"ERR_PREFLIGHT_MISSING_TEXTURE:{name}:{img.name}")
                    return False
                fp = bpy.path.abspath(img.filepath) if img.filepath else ""
                if fp and not os.path.isfile(fp):
                    fail(f"ERR_PREFLIGHT_MISSING_TEXTURE:{name}:{fp}")
                    return False
    _, _, dims = world_bbox([obj])
    if max(dims.x, dims.y, dims.z) <= 0:
        fail("ERR_PREFLIGHT_EMPTY_BOUNDS:" + name)
        return False
    return True


def process_targets(job, targets, ytyp, target_versions=None,
                    non_body_collision=None):
    """Shared tail: poly budget -> UVs/attrs -> textures -> materials -> export.

    non_body_collision (multi_part, G2): collision mode applied to every
    target after index 0 (targets[0] is always the _body drawable). Spin
    blades and hinged shells carry no collision -- it stays on the body."""
    sidecar = job["sidecar"]
    stream_dir = job["stream_dir"]
    max_tris = int(sidecar.get("max_triangles", 35000))
    for _, obj in targets:
        tris = enforce_poly_budget(obj, max_tris)
        if RESULT["status"] == "FAILED":
            return False
        RESULT["tris"] += tris
        ensure_uv(obj)

    prepare_textures(job["textures_dir"], int(sidecar.get("max_texture_res", 2048)))
    convert_materials([o for _, o in targets], job["textures_dir"])
    for _, obj in targets:
        fix_mesh_attributes(obj)  # needs Sollumz shader materials present

    texture_mode = sidecar.get("texture_mode", "embed")
    txd_name = None
    if texture_mode == "ytd":
        txd_name = make_external_txd(job)
        if txd_name is None:
            return False
    elif texture_mode != "embed":
        fail("ERR_UNKNOWN_TEXTURE_MODE:" + str(texture_mode))
        return False

    for i, (name, obj) in enumerate(targets):
        if not NAME_RE.fullmatch(name):
            fail("ERR_PREFLIGHT_BAD_NAME:" + name)
            return False
        if not preflight_lint(obj, name):
            return False
        cmode = (sidecar.get("collision_mode", "convex_hull")
                 if i == 0 or non_body_collision is None
                 else non_body_collision)
        if not to_drawable_and_export(
                obj, name, stream_dir, cmode, ytyp,
                lod_dist=float(sidecar.get("lod_dist", 150.0)),
                hd_tex_dist=float(sidecar.get("hd_texture_dist", 60.0)),
                flags=str(sidecar.get("archetype_flags", "32")),
                txd_name=txd_name, target_versions=target_versions):
            return False
        # exporter can log-abort a drawable without raising -- verify artifact
        # exists and is RSC7 (size floor is the orchestrator's Gate 1 job)
        found = [os.path.join(dp, f"{name}.ydr")
                 for dp, _dn, fs in os.walk(stream_dir)
                 if f"{name}.ydr" in fs]
        ok = bool(found)
        if ok:
            with open(found[0], "rb") as fh:
                ok = fh.read(4) == b"RSC7"
        if not ok:
            fail(f"ERR_EXPORT_ARTIFACT_MISSING:{name}.ydr")
            return False
    return True


# ---------------------------------------------------------------------------
# Rig & Animation Workbench: preview dump + rig_recipe consumption.
# Contract: reference/RIG_WORKBENCH.md. Preview space == compile space --
# a recipe carries the exact transforms the user saw in the canvas and the
# compile path applies them verbatim instead of auto-bounds scaling.
# ---------------------------------------------------------------------------

def _transform_to_matrix(t):
    """{rot_z_deg, scale, offset} -> Matrix: v' = Rz(rot) @ v * scale + off."""
    rz = math.radians(float((t or {}).get("rot_z_deg", 0.0)))
    s = float((t or {}).get("scale", 1.0))
    off = Vector((t or {}).get("offset", (0.0, 0.0, 0.0)))
    return Matrix.Translation(off) @ Matrix.Scale(s, 4) @ Matrix.Rotation(rz, 4, 'Z')


def recipe_transform_matrix(recipe):
    """user_transform is expressed in already-normalized preview space, so the
    composed matrix maps raw baked verts -> final space."""
    return (_transform_to_matrix(recipe.get("user_transform"))
            @ _transform_to_matrix(recipe.get("preview_transform")))


def get_rig_recipe(sidecar):
    r = sidecar.get("rig_recipe")
    if isinstance(r, dict) and r.get("parts"):
        return r
    return None


def recipe_delete_trash(recipe):
    """Delete recipe trash parts by exact source name. Runs before any join."""
    trash = {p["name"] for p in recipe["parts"] if p.get("role") == "trash"}
    if not trash:
        return
    present = {o.name for o in bpy.context.scene.objects}
    missing = trash - present
    killed = 0
    for o in list(bpy.context.scene.objects):
        if o.name in trash:
            bpy.data.objects.remove(o, do_unlink=True)
            killed += 1
    warn(f"WARN_RECIPE_TRASH:{killed}_parts:{len(trash)}_requested")
    if missing:
        warn("WARN_RECIPE_TRASH_MISSING:" + ",".join(sorted(missing)))


def _default_ped_donor():
    """Donor ped .ydd resolution order: SFP_PED_DONOR_YDD env var ->
    setup.json "ped_donor_ydd" -> donor/donor.json "ydd". The donor is a
    GTA-derived asset and is NEVER shipped -- see SETUP.md for how to
    export one from your own install."""
    env = os.environ.get("SFP_PED_DONOR_YDD")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cfg in (os.path.join(here, "setup.json"),
                    os.path.join(here, "donor", "donor.json")):
        try:
            with open(cfg, encoding="utf-8") as fh:
                data = json.load(fh)
            ydd = data.get("ped_donor_ydd") or data.get("ydd")
        except Exception:
            continue
        if ydd and os.path.isfile(ydd):
            return ydd
    return None


def run_dump_preview(src, out_json, ped_donor, asset_type, target_height,
                     excludes):
    """--dump-preview: import -> bake -> normalized preview_scene.json. No
    texture decode, no material conversion, no Sollumz export."""
    import preview_bake
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="Sollumz")
    if not import_source(src):
        sys.exit(1)
    apply_exclude_objects({"exclude_objects": excludes})
    purge_non_eval_junk()

    donor_arm = None
    pre = set(bpy.context.scene.objects)
    if asset_type == "ped" and ped_donor and os.path.isfile(ped_donor):
        try:
            bpy.ops.sollumz.import_assets(
                directory=os.path.dirname(ped_donor),
                files=[{"name": os.path.basename(ped_donor)}])
            donor_arm = next((o for o in bpy.context.scene.objects
                              if o not in pre and o.type == "ARMATURE"), None)
        except Exception as exc:
            print("[preview] donor import failed:", exc)

    # original source objects (pre-donor) keep their names for anim tracks
    src_objs = {o.name: o for o in bpy.context.scene.objects
                if o.type == 'MESH' and not o.hide_viewport
                and (donor_arm is None or o in pre)}
    baked, parts_info = preview_bake.snapshot_evaluated_world_meshes(
        bpy.context)
    # the donor's skinned ped mesh bakes alongside the source -- drop it so
    # it doesn't pollute role guesses, islands or the normalization bounds
    if donor_arm is not None:
        drop = {i for i, p in enumerate(parts_info)
                if p["bind_kind"].startswith("SKINNED:" + donor_arm.name)}
        baked = [o for i, o in enumerate(baked) if i not in drop]
        parts_info = [p for i, p in enumerate(parts_info) if i not in drop]

    transform = preview_bake.export_preview_scene_json(
        bpy.context, baked, parts_info, donor_arm, out_json,
        asset_type=asset_type, target_height=target_height,
        src_objs=src_objs)
    print("[preview] wrote", out_json,
          "parts:", len(parts_info), "transform:", transform)


def _anim_capture(recipe):
    a = recipe.get("anim_capture")
    if not isinstance(a, dict):
        return None
    if a.get("mode") not in (None, "none") or a.get("rest_sec") is not None:
        return a
    return None


def _capture_window(anim):
    scene = bpy.context.scene
    fps = float(scene.render.fps or scene.render.fps_base or 30.0)
    f0 = int(round(float(anim.get("in_sec", 0.0)) * fps))
    f1 = int(round(float(anim.get("out_sec", 0.0)) * fps))
    if f1 <= f0:
        return None, None, None
    hz = float(anim.get("sample_hz") or 20)
    step = max(1, int(round(fps / max(hz, 1))))
    return fps, f0, list(range(f0, f1 + 1, step))


def _eval_mw(obj, dg):
    try:
        return obj.evaluated_get(dg).matrix_world
    except Exception:
        return obj.matrix_world


def capture_clip_slice_multi(job, recipe, anim):
    """clip_slice for multi-part/prop: triad-sample each non-trash part's
    rigid transform across [in,out] (correct for armature-skinned parts,
    whose matrix_world never moves), express root-relative tracks in
    recipe space anchored at rest_sec -> clips/<clip_name>.json + lua."""
    import preview_bake
    fps, f0, frames = _capture_window(anim)
    if frames is None:
        warn("WARN_CLIP_WINDOW_EMPTY")
        return
    scene = bpy.context.scene
    parts = {p["name"]: p for p in recipe["parts"]
             if p.get("role") != "trash"}
    objs = {o.name: o for o in scene.objects if o.name in parts}
    if not objs:
        warn("WARN_CLIP_NO_PARTS")
        return
    root = next((p["name"] for p in recipe["parts"]
                 if p.get("role") in ("hull", "chassis")), None)
    if root not in objs:
        root = next(iter(objs))
    M = recipe_transform_matrix(recipe)
    R_m = M.to_3x3().to_quaternion().to_matrix()

    # reference pose: rest_sec frame (the frame the meshes get baked at)
    rest_f = f0
    if anim.get("rest_sec") is not None:
        rest_f = int(round(float(anim["rest_sec"]) * fps))
    cur = scene.frame_current
    scene.frame_set(rest_f)
    bpy.context.view_layer.update()
    sampler = preview_bake.PartTransformSampler(objs.values())
    sampler.prepare(bpy.context)
    rest_snap = sampler.sample(bpy.context)

    times = []
    raw = {n: [] for n in objs}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        times.append(round((f - f0) / fps, 4))
        snap = sampler.sample(bpy.context)
        for n in objs:
            raw[n].append(snap.get(n))
    scene.frame_set(cur)
    bpy.context.view_layer.update()

    # root-relative transforms anchored at the rest frame: the exported
    # meshes are baked in rest geometry, so deltas are measured from rest
    def rel(snap, name):
        s = snap.get(name)
        sr = snap.get(root)
        if s is None or sr is None:
            return None
        c, R = s
        cr, Rr = sr
        Rrel = Rr.transposed() @ R
        crel = Rr.transposed() @ (c - cr)
        cn = M @ crel
        return ([round(cn.x, 4), round(cn.y, 4), round(cn.z, 4)]
                + [round(a, 4) for a in
                   (R_m @ Rrel).to_euler()])

    clip = {"name": anim.get("clip_name") or "clip", "fps": fps,
            "root": root, "times": times,
            "rest_frame": rest_f, "tracks": {}}
    for n in objs:
        track = []
        for i in range(len(times)):
            snap_i = {k: raw[k][i] for k in objs}
            track.append(rel(snap_i, n))
        clip["tracks"][n] = track
    clip["rest"] = {n: rel(rest_snap, n) for n in objs}

    cdir = os.path.join(job["job_dir"], "clips")
    os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, clip["name"] + ".json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(clip, fh, separators=(",", ":"))
    RESULT["artifacts"].append("clips/" + clip["name"] + ".json")
    emit_manifest(job["job_dir"], clip["name"] + "_player.lua",
                  "WorkBench clip_slice capture (reference/RIG_WORKBENCH.md).\n"
                  "tracks are root-part-relative [x,y,z,rx,ry,rz] samples in\n"
                  "recipe space at clip fps; a FiveM client consumer\n"
                  "interpolates AttachEntityToEntity offsets/rotations\n"
                  "between samples on GetGameTimer().\n"
                  "runtime_status: manifest only - a runtime consumer is required.",
                  {"clip": clip["name"], "root": root, "fps": fps,
                   "samples": len(times), "parts": sorted(clip["tracks"]),
                   "runtime_status": MANIFEST_RUNTIME_STATUS})


def capture_drone_flight(job, recipe, anim):
    """drone_reactive_flight: measure chassis pitch/roll + thruster hinge
    rotation across [in,out], least-squares fit counter-tilt gains, emit a
    client_flight.lua controller manifest + kinematic_chain entries."""
    import numpy as np
    fps, f0, frames = _capture_window(anim)
    if frames is None:
        warn("WARN_CLIP_WINDOW_EMPTY")
        return
    scene = bpy.context.scene
    parts = {p["name"]: p for p in recipe["parts"]
             if p.get("role") != "trash"}
    chassis = next((p["name"] for p in recipe["parts"]
                    if p.get("role") in ("hull", "chassis")), None)
    thrust = [p["name"] for p in recipe["parts"]
              if p.get("role") in ("rotor", "thruster_tilt", "gimbal")]
    objs = {o.name: o for o in scene.objects
            if o.name in parts and (o.name == chassis or o.name in thrust)}
    if chassis not in objs or not thrust:
        warn("WARN_DRONE_FLIGHT_NEEDS_CHASSIS_AND_THRUSTER")
        return
    import preview_bake
    rest_f = f0
    if anim.get("rest_sec") is not None:
        rest_f = int(round(float(anim["rest_sec"]) * fps))
    cur = scene.frame_current
    scene.frame_set(rest_f)
    bpy.context.view_layer.update()
    sampler = preview_bake.PartTransformSampler(objs.values())
    sampler.prepare(bpy.context)
    ch_e, ch_pos, f_ok = [], [], []
    th = {n: [] for n in thrust}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        snap = sampler.sample(bpy.context)
        s_ch = snap.get(chassis)
        if s_ch is None:
            continue
        f_ok.append(f)
        c_ch, R_ch = s_ch
        e = R_ch.to_euler()          # chassis rotation delta vs rest
        ch_e.append((e.x, e.y, e.z))
        ch_pos.append(c_ch.copy())
        for n in thrust:
            s_t = snap.get(n)
            if s_t is None:
                continue
            # thruster rotation beyond what the chassis itself did --
            # the counter-tilt signal
            pe = (R_ch.transposed() @ s_t[1]).to_euler()
            th[n].append((pe.x, pe.y, pe.z))
    scene.frame_set(cur)
    bpy.context.view_layer.update()

    pitch = np.array([e[0] for e in ch_e])
    roll = np.array([e[1] for e in ch_e])
    A = np.stack([pitch, roll, np.ones(len(ch_e))], 1)
    gains = {}
    for n, es in th.items():
        if len(es) < 3 or float(np.abs(pitch).max() + np.abs(roll).max()) < 0.02:
            gains[n] = {"k_pitch": -1.2, "k_roll": -1.0, "fitted": False}
            continue
        ex = np.array([e[0] for e in es])
        ey = np.array([e[1] for e in es])
        gx, _res, _r, _s = np.linalg.lstsq(A, ex, rcond=None)
        gy, _res2, _r2, _s2 = np.linalg.lstsq(A, ey, rcond=None)
        gains[n] = {"k_pitch": round(float(gx[0]), 4),
                    "k_roll": round(float(gy[1]), 4),
                    "bias": [round(float(gx[2]), 4), round(float(gy[2]), 4)],
                    "fitted": True}
    # hover loop with chassis XY drift stripped (idle stays at origin)
    xs = np.array([p.x for p in ch_pos])
    ys = np.array([p.y for p in ch_pos])
    zs = np.array([p.z for p in ch_pos])
    times = [round((f - f0) / fps, 4) for f in f_ok]
    hover = {"times": times,
             "z_osc": [round(float(z - zs[0]), 4) for z in zs],
             "pitch": [round(float(a), 4) for a in pitch],
             "roll": [round(float(b), 4) for b in roll]}
    pivots = {p["name"]: p.get("pivot") for p in recipe["parts"]
              if p["name"] in th}
    # source names -> exported .ydr labels: co-moving merges fuse multiple
    # source parts into one entity, so the flight controller must reference
    # the merged model name or AttachEntityToEntity can't find it
    mc = job.get("sidecar", {}).get("motion_config") or {}
    pmap = mc.get("part_map") or {}
    merged = {}
    for label, spec in pmap.items():
        if isinstance(spec, list):
            for n in spec:                     # "_body" is a bare name list
                merged[n] = (label, None)
            continue
        for n in spec.get("meshes", []):
            merged[n] = (label, spec.get("pivot"))
    asset = job.get("asset_name", "")
    thrusters = []
    seen_labels = set()
    for n in th:
        label, piv = merged.get(n, (n, pivots.get(n)))
        if label in seen_labels:
            continue
        seen_labels.add(label)
        thrusters.append({"part": label,
                          "model": (asset + "_" + label)
                                   if asset else label,
                          "pivot": piv, "axis": [0, 0, 1],
                          "source_objects": [m for m in th
                                             if merged.get(m, (m,))[0]
                                             == label]})
    gains = {merged.get(n, (n,))[0]: g for n, g in gains.items()}
    data = {"mode": "drone_reactive_flight",
            "chassis": chassis,
            "chassis_model": (asset + "_body") if asset else chassis,
            "gains": gains,
            "thrusters": thrusters,
            "hover_loop": hover,
            "drift_stripped_xy": [round(float(xs[0]), 4),
                                  round(float(ys[0]), 4)],
            "note": "per-frame: read chassis GetEntityRotation + "
                    "GetEntitySpeedVector; tilt each thruster opposite to "
                    "pitch/roll/accel via gains to visually preserve thrust",
            "runtime_status": MANIFEST_RUNTIME_STATUS}
    emit_manifest(job["job_dir"], "client_flight.lua",
                  "WorkBench reactive flight controller "
                  "(reference/RIG_WORKBENCH.md). Data table consumed by a "
                  "FiveM client script; not executable by itself.\n"
                  "runtime_status: manifest only - a runtime consumer is required.",
                  data)
    RESULT["kinematic_chain"] = {
        "chassis": chassis,
        "thrusters": [{"part": n, **g} for n, g in gains.items()]}


def capture_ped_clip(job, recipe, anim, base, bnames, src_objs):
    """clip_slice for ped: vertex-cluster retarget. For each donor bone's
    representative verts, track the deformed position through the driving
    source object's nearest pose bone (or the object's own rigid matrix
    when it wasn't skinned), solve best-fit rotation per frame via SVD,
    emit clips/<name>.json. .ycd is unprobed -> WARN_YCD_UNPROBED."""
    import numpy as np
    fps, f0, frames = _capture_window(anim)
    if frames is None:
        warn("WARN_CLIP_WINDOW_EMPTY")
        return
    scene = bpy.context.scene
    M_inv = recipe_transform_matrix(recipe).inverted()
    dg = bpy.context.evaluated_depsgraph_get()

    # donor bone -> representative final-mesh vert indices (<=32, weight>0.5)
    vidx = {}
    part_of_vert = {}
    for vi in range(len(base.data.vertices)):
        best, bw = None, 0.25
        src_part = None
        for g in base.data.vertices[vi].groups:
            gname = base.vertex_groups[g.group].name
            if gname.startswith("__part_"):
                src_part = gname[len("__part_"):]
            elif g.weight > bw:
                bw, best = g.weight, gname
        if best is not None:
            vidx.setdefault(best, []).append(vi)
            if src_part:
                part_of_vert[vi] = src_part
    vidx = {bn: vs[::max(1, len(vs) // 32)][:32]
            for bn, vs in vidx.items() if bn in bnames and vs}
    if not vidx:
        warn("WARN_CLIP_NO_WEIGHTED_VERTS")
        return

    # source driver per part: armature modifier, or parent-chain armature,
    # else the part's own rigid world matrix
    driver = {}
    for o in scene.objects:
        if o.type != 'MESH' or o.name not in src_objs:
            continue
        arm_mod = next((m for m in o.modifiers
                        if m.type == 'ARMATURE' and m.object), None)
        if arm_mod:
            driver[o.name] = ("arm", arm_mod.object)
            continue
        p = o.parent
        while p is not None and p.type != 'ARMATURE':
            p = p.parent
        driver[o.name] = ("arm", p) if p else ("obj", o)

    # vert -> source bone segment index (nearest pose bone at rest frame)
    bm = np.array(base.matrix_world)
    vcof = np.empty(len(base.data.vertices) * 3)
    base.data.vertices.foreach_get('co', vcof)
    vcof.shape = (-1, 3)
    vfinal = (bm[:3, :3] @ vcof.T).T + bm[:3, 3]          # recipe space
    vsrc = np.array([M_inv @ Vector(v) for v in vfinal])  # source space
    verts_used = sorted({vi for vs in vidx.values() for vi in vs})
    drivers_used = {}
    vbone = {}
    for vi in verts_used:
        part = part_of_vert.get(vi)
        if part is None or part not in driver:
            vbone[vi] = ("obj", None, None)
            continue
        kind, drv = driver[part]
        if kind == "obj":
            vbone[vi] = ("obj", drv.name, None)
            continue
        arm = drv
        pb_head, pb_tail = [], []
        for pb in arm.pose.bones:
            wm = arm.matrix_world @ pb.matrix
            hd = wm.to_translation()
            tl = wm @ Vector((0, pb.length, 0))
            pb_head.append((hd.x, hd.y, hd.z))
            pb_tail.append((tl.x, tl.y, tl.z))
        if arm.name not in drivers_used:
            drivers_used[arm.name] = {
                "arm": arm,
                "H": np.array(pb_head), "T": np.array(pb_tail)}
        D = drivers_used[arm.name]
        AB = D["T"] - D["H"]
        ab2 = np.maximum((AB * AB).sum(1), 1e-12)
        tt = np.clip(((vsrc[vi] - D["H"]) * AB).sum(1) / ab2, 0, 1)
        close = D["H"] + tt[:, None] * AB
        d2 = ((vsrc[vi] - close) ** 2).sum(1)
        bi = int(d2.argmin())
        vbone[vi] = ("arm", arm.name, arm.pose.bones[bi].name)

    # rest snapshots: scene is still parked at the bake (rest) frame here
    arm_names = {an for (_k, an, _bn) in vbone.values()
                 if an and an in drivers_used}
    arms_needed = [drivers_used[n] for n in arm_names]
    rest_pose = {}
    for D in arms_needed:
        arm = D["arm"]
        rest_pose[arm.name] = {pb.name: (arm.matrix_world @ pb.matrix).copy()
                               for pb in arm.pose.bones}
    obj_rest = {}
    for part, (kind, drv) in driver.items():
        if kind == "obj":
            obj_rest[drv.name] = _eval_mw(drv, dg).copy()

    # per-frame: deform each representative vert through its driver, then
    # SVD-solve donor bone rotation on the cluster
    cur = scene.frame_current
    out = {"name": anim.get("clip_name") or "clip", "fps": fps,
           "times": [], "bones": {bn: [] for bn in vidx}}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        out["times"].append(round((f - f0) / fps, 4))
        vdef = {}
        for vi in verts_used:
            kind, an, bn = vbone[vi]
            if kind == "obj" and an in obj_rest:
                o = driver[an][1]
                m_now = _eval_mw(o, dg)
                vdef[vi] = m_now @ obj_rest[an].inverted() @ Vector(vsrc[vi])
            elif kind == "arm":
                wm = an_ = None
                for D in arms_needed:
                    if D["arm"].name == an:
                        wm = D["arm"].matrix_world @ \
                            D["arm"].pose.bones[bn].matrix
                        break
                if wm is not None:
                    vdef[vi] = (wm @ rest_pose[an][bn].inverted()
                                @ Vector(vsrc[vi]))
        # SVD rotation per donor bone on this frame's cluster
        for bn, vs in vidx.items():
            pts_r = np.array([vsrc[vi] for vi in vs if vi in vdef])
            pts_d = np.array([vdef[vi] for vi in vs if vi in vdef])
            if len(pts_r) < 3:
                out["bones"][bn].append(None)
                continue
            cr, cd = pts_r.mean(0), pts_d.mean(0)
            H = (pts_r - cr).T @ (pts_d - cd)
            U, _s, Vt = np.linalg.svd(H)
            R = Vt.T @ U.T
            if np.linalg.det(R) < 0:
                Vt[-1] *= -1
                R = Vt.T @ U.T
            e = Matrix(R.tolist()).to_euler()
            t = cd - np.asarray(R @ cr)
            out["bones"][bn].append(
                [round(float(t[0]), 4), round(float(t[1]), 4),
                 round(float(t[2]), 4),
                 round(e.x, 4), round(e.y, 4), round(e.z, 4)])
    scene.frame_set(cur)
    bpy.context.view_layer.update()

    cdir = os.path.join(job["job_dir"], "clips")
    os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, out["name"] + ".json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    RESULT["artifacts"].append("clips/" + out["name"] + ".json")
    warn("WARN_YCD_UNPROBED:clip exported as clips/" + out["name"] + ".json")



def process_ped_recipe(job, recipe):
    """rig_recipe ped compile: donor GTA skeleton + strict rigid/smooth order.

    1) trash already deleted by caller; 2) apply recipe transform;
    3) partition smooth vs rigid; 4) smooth: join+decimate+donor weight
       transfer (<=4, normalized); 5) rigid: single-bone 100% groups;
    6) join rigid into smooth (name-merged groups keep the split).
    """
    import numpy as np
    import preview_bake
    from Sollumz.sollumz_properties import SollumType, LODLevel

    scene = bpy.context.scene
    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    anim = _anim_capture(recipe)

    # anim_capture.rest_sec: bake/rig the pose at the user's chosen frame
    # (a robot that starts crouched at 0s and stands at 1.85s gets bound
    # standing, not folded)
    if anim is not None and anim.get("rest_sec") is not None:
        fps = float(scene.render.fps or scene.render.fps_base or 30.0)
        scene.frame_set(int(round(float(anim["rest_sec"]) * fps)))
        bpy.context.view_layer.update()

    donor_ydd = sidecar.get("ped_donor_ydd") or _default_ped_donor()
    if not donor_ydd or not os.path.isfile(donor_ydd):
        fail("ERR_PED_DONOR_MISSING:set SFP_PED_DONOR_YDD or donor/donor.json"
             " (see SETUP.md) got:" + str(donor_ydd))
        return False
    pre = set(scene.objects)
    bpy.ops.sollumz.import_assets(
        directory=os.path.dirname(donor_ydd),
        files=[{"name": os.path.basename(donor_ydd)}])
    imported = [o for o in scene.objects if o not in pre]
    arm = next((o for o in imported if o.type == "ARMATURE"), None)
    if arm is None:
        fail("ERR_PED_DONOR_NO_ARMATURE")
        return False
    # keep only the donor armature; its drawable dict/mesh hierarchy is
    # rebuilt below and source scene objects die in the bake step
    for o in imported:
        if o is not arm:
            bpy.data.objects.remove(o, do_unlink=True)

    baked, _info = preview_bake.snapshot_evaluated_world_meshes(bpy.context)
    if not baked:
        fail("ERR_NO_MESH_AFTER_CLEANUP")
        return False
    recipe_parts = {p["name"]: p for p in recipe["parts"]}
    kept = []
    keep_set = set()
    for obj in baked:
        src = obj.name[len("baked_"):]
        rp = recipe_parts.get(src)
        if rp is not None and rp.get("role") == "trash":
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        kept.append((obj, rp))
        keep_set.add(obj)
    # remove source-scene originals (bake produced copies); keep donor arm.
    # clip_slice keeps source objects alive until after the vertex-cluster
    # capture -- their evaluated pose drives the retargeted donor keys.
    defer_src = (anim is not None
                 and anim.get("mode") == "clip_slice")
    dead = []
    for o in list(scene.objects):
        if o is arm or o in keep_set:
            continue
        if defer_src and o.type in ("MESH", "ARMATURE"):
            dead.append(o)
            continue
        bpy.data.objects.remove(o, do_unlink=True)
    arm.parent = None
    arm.matrix_world = Matrix.Identity(4)

    M = recipe_transform_matrix(recipe)
    for obj, _rp in kept:
        obj.data.transform(M)
        obj.data.update()
        # tag each vert with its source part so post-join clip capture can
        # still map verts back to their driving source object/armature
        obj.vertex_groups.clear()
        if len(obj.data.vertices):
            src = obj.name[len("baked_"):]
            obj.vertex_groups.new(name="__part_" + src).add(
                list(range(len(obj.data.vertices))), 1.0, 'REPLACE')

    smooth = [o for o, rp in kept if (rp or {}).get("role") != "rigid_bone"]
    rigid = [(o, rp) for o, rp in kept
             if (rp or {}).get("role") == "rigid_bone"]
    if not smooth and not rigid:
        fail("ERR_RECIPE_ALL_TRASHED")
        return False

    # proportional decimation to the total budget -- before weights exist
    max_tris = int(sidecar.get("max_triangles", 70000))
    total = sum(len(o.data.polygons) for o, _ in kept)
    if total > max_tris:
        ratio = max_tris / float(total)
        for o, _rp in kept:
            if len(o.data.polygons) < 100:
                continue
            mod = o.modifiers.new("dec", 'DECIMATE')
            mod.ratio = max(0.05, ratio)
            bpy.context.view_layer.objects.active = o
            try:
                bpy.ops.object.modifier_apply(modifier='dec')
            except Exception:
                o.modifiers.remove(mod)

    bpy.context.view_layer.update()
    heads, tails, bnames = [], [], []
    for b in arm.data.bones:
        h = arm.matrix_world @ b.head_local
        t = arm.matrix_world @ b.tail_local
        heads.append((h.x, h.y, h.z))
        tails.append((t.x, t.y, t.z))
        bnames.append(b.name)
    H = np.array(heads)
    T = np.array(tails)
    AB = T - H
    AB2 = np.maximum((AB * AB).sum(1), 1e-12)
    bidx = {n: i for i, n in enumerate(bnames)}

    def bone_dists(co):  # (n,3) -> (n,B) point-to-segment distances
        tt = np.clip(((co[:, None, :] - H[None, :, :])
                      * AB[None, :, :]).sum(2) / AB2[None, :], 0, 1)
        close = H[None, :, :] + tt[:, :, None] * AB[None, :, :]
        return np.sqrt(((co[:, None, :] - close) ** 2).sum(2))

    def verts_co(o):
        nv = len(o.data.vertices)
        co = np.empty(nv * 3)
        o.data.vertices.foreach_get('co', co)
        co.shape = (-1, 3)
        return np.array(o.matrix_world)[:3, :3] @ co.T + \
            np.array(o.matrix_world)[:3, 3:4]

    def nearest_bone(pt):
        d = bone_dists(np.array([pt]))[0]
        return bnames[int(d.argmin())]

    # join smooth into one mesh BEFORE weighting
    base = None
    if smooth:
        if len(smooth) > 1:
            select_only(smooth)
            bpy.context.view_layer.objects.active = smooth[0]
            bpy.ops.object.join()
        base = smooth[0]
        for grp in list(base.vertex_groups):  # keep __part_* vert tags
            if not grp.name.startswith("__part_"):
                base.vertex_groups.remove(grp)
        co = verts_co(base).T  # (nv,3) world
        nv = len(base.data.vertices)
        B = len(bnames)
        w_all = np.empty((nv, B))
        for s in range(0, nv, 20000):
            d = bone_dists(co[s:s + 20000])
            w_all[s:s + 20000] = 1.0 / np.maximum(d, 1e-4) ** 2
        k = min(4, B)
        idx = np.argpartition(-w_all, k - 1, axis=1)[:, :k]
        wt = np.take_along_axis(w_all, idx, 1)
        wt /= np.maximum(wt.sum(1, keepdims=True), 1e-12)
        # quantize to 0.05 buckets so we can batch vg.add() (per-vertex
        # RNA assignment is the known slow path)
        q = np.round(wt * 20) / 20.0
        buckets = {}
        for vi in range(nv):
            for j in range(k):
                wv = float(q[vi, j])
                if wv <= 0.0:
                    continue
                buckets.setdefault((bnames[int(idx[vi, j])], wv),
                                   []).append(vi)
        vgs = {}
        for (bn, wv), vis in buckets.items():
            vg = vgs.get(bn)
            if vg is None:
                vg = base.vertex_groups.new(name=bn)
                vgs[bn] = vg
            vg.add(vis, wv, 'ADD')
        bpy.context.view_layer.objects.active = base
        bpy.ops.object.vertex_group_normalize_all()

    # rigid: one vgroup per part, weight 1.0 on every vert
    for o, rp in rigid:
        for grp in list(o.vertex_groups):  # keep __part_* vert tags
            if not grp.name.startswith("__part_"):
                o.vertex_groups.remove(grp)
        lock = (rp or {}).get("bone_lock") or ""
        if lock not in bidx:
            co = verts_co(o)
            lock = nearest_bone(co.mean(1))
        o.vertex_groups.new(name=lock).add(
            list(range(len(o.data.vertices))), 1.0, 'REPLACE')

    # merge rigid into smooth AFTER both weight schemes exist
    if base is None:
        base = rigid[0][0]
    if rigid:
        rest = [o for o, _ in rigid if o is not base]
        if rest:
            select_only(rest + [base])
            bpy.context.view_layer.objects.active = base
            bpy.ops.object.join()
    if defer_src:
        capture_ped_clip(job, recipe, anim, base, bnames,
                         {o.name for o in dead})
        for o in dead:
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
    for grp in list(base.vertex_groups):
        if grp.name not in bidx:
            base.vertex_groups.remove(grp)
    base.name = asset_name + "_mesh"
    arm.name = asset_name
    return ped_export_finalize(job, arm, [base])


def ped_export_finalize(job, arm, ms):
    """Shared ped tail: attrs -> textures -> drawable dict -> export+verify."""
    from Sollumz.sollumz_properties import LODLevel, SollumType
    scene = bpy.context.scene
    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    stream_dir = job["stream_dir"]

    tris = 0
    for o in ms:
        tris += tri_count(o)
        ensure_uv(o)
    RESULT["tris"] += tris

    prepare_textures(job["textures_dir"],
                     int(sidecar.get("max_texture_res", 2048)))
    convert_materials(ms, job["textures_dir"])
    for o in ms:
        fix_mesh_attributes(o)

    txd_name = None
    texture_mode = sidecar.get("texture_mode", "embed")
    if texture_mode == "ytd":
        txd_name = make_external_txd(job)
        if txd_name is None:
            return False
    elif texture_mode != "embed":
        fail("ERR_UNKNOWN_TEXTURE_MODE:" + str(texture_mode))
        return False

    for o in ms:
        if not preflight_lint(o, o.name):
            return False

    root = bpy.data.objects.new(asset_name, None)
    scene.collection.objects.link(root)
    root.sollum_type = SollumType.DRAWABLE_DICTIONARY
    arm.name = asset_name
    arm.sollum_type = SollumType.DRAWABLE
    arm.parent = root
    for o in ms:
        mw = o.matrix_world.copy()
        o.parent = arm
        o.matrix_world = mw
        o.sollum_type = SollumType.DRAWABLE_MODEL
        o.sz_lods.get_lod(LODLevel.HIGH).mesh = o.data
        o.sz_lods.active_lod_level = LODLevel.HIGH

    select_only(collect_tree(root))
    gen = sidecar.get("target_generation", "legacy")
    bpy.ops.sollumz.export_assets(
        directory=stream_dir,
        direct_export=True,
        use_custom_settings=True,
        target_formats={"NATIVE", "CWXML"},
        target_versions=GEN_TARGETS.get(gen, {"GEN8"}),
        limit_to_selected=True,
        export_ytyps=False,
        export_ytds=txd_name is not None,
        export_ytds_include="ALL",
    )

    ydd_files = [os.path.join(dp, f)
                 for dp, _dn, fs in os.walk(stream_dir) for f in fs
                 if f.endswith(".ydd")]
    ok = bool(ydd_files) and all(os.path.getsize(p) > 0 for p in ydd_files)
    if ok:
        with open(ydd_files[0], "rb") as fh:
            ok = fh.read(4) == b"RSC7"
    if not ok:
        fail(f"ERR_EXPORT_ARTIFACT_MISSING:{asset_name}.ydd")
        return False

    ydd_xml = next((os.path.join(dp, f)
                    for dp, _dn, fs in os.walk(stream_dir) for f in fs
                    if f.endswith(".ydd.xml")), None)
    sb = ped_skeleton_bounds_from_xml(ydd_xml) if ydd_xml else None
    if sb is not None:
        mins, maxs, _ = world_bbox(ms)
        smin, smax = sb
        span = max((maxs - mins).length, 0.001)
        if (smin - mins).length > span or (smax - maxs).length > span:
            fail("ERR_PED_TRANSFORM_MISMATCH")
            return False
    RESULT["dims"] = None
    return True


def _comoving_clusters(recipe, context):
    """Union non-trash parts into rigid clusters for .ydr export. Two parts
    of the same role fuse iff their relative motion is rigid-body-invariant
    across sampled frames: identical relative rotations AND centroid offsets
    transported by R_i(t): c_j(t)-c_i(t) == R_i(t) @ (c_j(0)-c_i(0)). This
    fuses material-split primitives (Cylinder.055_DarkMetal_0 + _Copper_1)
    that share a limb but sit at different pivots.

    Multi-instance roles (rotor/thruster_tilt/gimbal) additionally require a
    shared driver or 2cm padded-AABB overlap, so 4 separate quadcopter
    rotors on a static GLB don't fuse into one mesh."""
    import preview_bake
    scene = context.scene
    parts = [p for p in recipe["parts"] if p.get("role") != "trash"]
    objs = {p["name"]: scene.objects.get(p["name"]) for p in parts}
    objs = {n: o for n, o in objs.items() if o is not None}
    names = list(objs)
    parent = {n: n for n in names}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    role_of = {p["name"]: p.get("role") for p in parts}
    MULTI = {"rotor", "thruster_tilt", "gimbal"}
    M = recipe_transform_matrix(recipe)

    # recipe-space AABBs for the multi-instance spatial guard
    aabb = {}
    for n, o in objs.items():
        cs = [M @ (o.matrix_world @ Vector(c)) for c in o.bound_box]
        aabb[n] = ([min(c[k] for c in cs) for k in range(3)],
                   [max(c[k] for c in cs) for k in range(3)])

    # sample rigid motion via driving matrices (no per-frame mesh evals)
    rng = None
    for a in bpy.data.actions:
        r = a.frame_range
        rng = r if rng is None else (min(rng[0], r[0]),
                                     max(rng[1], r[1]))
    if rng is None:
        rng = (scene.frame_start, scene.frame_end)
    sampler = preview_bake.DriverTransformSampler(objs.values())
    cur = scene.frame_current
    scene.frame_set(int(rng[0]))
    bpy.context.view_layer.update()
    sampler.prepare(context)
    snaps = [sampler.sample(context)]
    if rng[1] - rng[0] > 1:
        nf = min(6, int(rng[1] - rng[0]))
        for i in range(1, nf + 1):
            scene.frame_set(int(rng[0] + (rng[1] - rng[0]) * i / nf))
            bpy.context.view_layer.update()
            snaps.append(sampler.sample(context))
    scene.frame_set(cur)
    bpy.context.view_layer.update()

    def spatial_ok(ni, nj):
        if role_of.get(ni) not in MULTI:
            return True
        di, dj = sampler.driver_of(ni), sampler.driver_of(nj)
        if di is not None and di == dj and di[0] != 'triad':
            return True
        (ai, bi), (aj, bj) = aabb[ni], aabb[nj]
        pad = 0.02
        return all(ai[k] - pad <= bj[k] and bi[k] + pad >= aj[k]
                   for k in range(3))

    def frob(A, B):
        return max(abs(A[r][c] - B[r][c])
                   for r in range(3) for c in range(3))

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ni, nj = names[i], names[j]
            if find(ni) == find(nj) or \
                    role_of.get(ni) != role_of.get(nj) or \
                    not spatial_ok(ni, nj):
                continue
            s0i, s0j = snaps[0].get(ni), snaps[0].get(nj)
            if s0i is None or s0j is None:
                continue
            rest_off = s0j[0] - s0i[0]
            ok = True
            for s in snaps:
                si, sj = s.get(ni), s.get(nj)
                if si is None or sj is None:
                    ok = False
                    break
                if frob(si[1], sj[1]) > 1e-3:
                    ok = False
                    break
                if ((sj[0] - si[0]) - si[1] @ rest_off).length > 1e-3:
                    ok = False
                    break
            if ok:
                union(ni, nj)

    clusters = {}
    for n in names:
        clusters.setdefault(find(n), []).append(n)
    return list(clusters.values()), sampler, aabb


def _cluster_pivot(members, pivot_of, role_of, sampler, aabb, M):
    """Merged-group pivot: explicit user pivot > driving bone head (recipe
    space) > cluster AABB centre."""
    for n in members:
        if pivot_of.get(n) and role_of.get(n) not in ("hull", "chassis"):
            return pivot_of[n]
    if sampler is not None:
        for n in members:
            d = sampler.driver_of(n)
            if d and d[0] == 'bone':
                arm = d[1]
                b = arm.data.bones.get(d[2])
                if b is not None:
                    return list(M @ (arm.matrix_world @ b.head_local))
    if aabb is None:
        return None
    mins = [min(aabb[n][0][k] for n in members) for k in range(3)]
    maxs = [max(aabb[n][1][k] for n in members) for k in range(3)]
    return [(mins[k] + maxs[k]) * 0.5 for k in range(3)]


def recipe_to_part_map(recipe, mc, context=None):
    """Translate rig_recipe roles into motion_multi_part part_map groups.
    Pivots stay in recipe (normalized) space -- motion_multi_part consumes
    them verbatim when mc['rig_recipe'] is set. Non-hull parts are fused
    into co-moving clusters so a 300-part mech becomes ~16 .ydrs."""
    pivot_of = {p["name"]: p.get("pivot") for p in recipe["parts"]}
    role_of = {p["name"]: p.get("role") for p in recipe["parts"]}
    rest_rot_of = {p["name"]: p.get("rest_rot") for p in recipe["parts"]}
    body = [p["name"] for p in recipe["parts"]
            if p.get("role") in ("hull", "chassis") or p.get("role") is None]
    if context is not None:
        clusters, sampler, aabb = _comoving_clusters(recipe, context)
        M = recipe_transform_matrix(recipe)
    else:
        clusters = [[p["name"]] for p in recipe["parts"]
                    if p.get("role") != "trash"]
        sampler, aabb, M = None, None, None
    part_map = {}
    if body:
        part_map["_body"] = body
    kind_of = {"rotor": "spin", "gimbal": "hinge", "turret": "hinge",
               "barrel": "hinge", "thruster_tilt": "hinge"}
    axis_of = {"rotor": [0, 0, 1], "gimbal": [1, 0, 0],
               "turret": [0, 0, 1], "barrel": [1, 0, 0],
               "thruster_tilt": [1, 0, 0]}
    per_role = {}
    for members in clusters:
        roles = {role_of.get(n) for n in members}
        if roles <= {"hull", "chassis", None}:
            continue  # already pinned into _body
        role = next((r for r in roles if r), "part")
        per_role.setdefault(role, []).append(members)
    for role, grps in per_role.items():
        for k, members in enumerate(grps):
            # multi-instance clusters must never share a .ydr name: 4 rotor
            # pods -> rotor_0..rotor_3, twin nacelles -> thruster_tilt_0/1
            label = role if len(grps) == 1 else f"{role}_{k}"
            piv = _cluster_pivot(members, pivot_of, role_of,
                                 sampler, aabb, M)
            # optional rest_rot [rx,ry,z] deg (XYZ) bakes a cluster re-pose
            # about its pivot into the exported verts -- e.g. ducted props
            # modeled edge-on get laid flat without runtime posing
            rr = next((rest_rot_of.get(n) for n in members
                       if rest_rot_of.get(n)), None)
            if rr and M is not None and any(abs(float(a)) > 1e-6
                                            for a in rr):
                Rr = Euler(tuple(math.radians(float(a)) for a in rr[:3]),
                           'XYZ').to_matrix().to_4x4()
                P = Matrix.Translation(Vector(piv))
                W = M.inverted() @ (P @ Rr @ P.inverted()) @ M
                for n in members:
                    o = bpy.data.objects.get(n)
                    if o is not None:
                        o.matrix_world = W @ o.matrix_world
            part_map[label] = {
                "meshes": members,
                "kind": kind_of.get(role, "hinge"),
                "spin_axis": axis_of.get(role),
                "spin_rpm": "auto" if role == "rotor" else None,
                "pivot": piv,
            }
    mc["part_map"] = part_map
    mc["rig_recipe"] = recipe


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    if argv and argv[0] == "--dump-preview":
        src = argv[1]
        out_json = argv[2]
        ped_donor = None
        asset_type = "prop"
        target_height = 1.9
        excludes = []
        i = 3
        while i < len(argv):
            if argv[i] == "--ped-donor":
                ped_donor = argv[i + 1]; i += 2
            elif argv[i] == "--asset-type":
                asset_type = argv[i + 1]; i += 2
            elif argv[i] == "--height":
                target_height = float(argv[i + 1]); i += 2
            elif argv[i] == "--exclude":
                excludes.append(argv[i + 1]); i += 2
            else:
                i += 1
        if ped_donor is None:
            ped_donor = _default_ped_donor()
        run_dump_preview(src, out_json, ped_donor, asset_type,
                         target_height, excludes)
        sys.exit(0)
    job_dir = os.path.abspath(argv[0])
    with open(os.path.join(job_dir, "job.json")) as fh:
        job = json.load(fh)

    sidecar = job["sidecar"]
    asset_name = job["asset_name"]
    stream_dir = job["stream_dir"]
    textures_dir = job["textures_dir"]
    os.makedirs(stream_dir, exist_ok=True)
    # stale .ydr/.yft from a previous compile (e.g. renamed rotor cluster)
    # must not survive into the packaged resource
    for root, _dirs, files in os.walk(stream_dir):
        for stale in files:
            if stale.lower().endswith((".ydr", ".yft", ".ydd", ".ytd",
                                       ".ydr.xml", ".yft.xml", ".ydd.xml")):
                try:
                    os.remove(os.path.join(root, stale))
                except OSError:
                    pass

    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="Sollumz")
    except Exception as exc:
        fail("ERR_SOLLUMZ_ENABLE", exc)
        write_result(job_dir)
        sys.exit(1)

    try:
        import Sollumz
        ver = Sollumz.bl_info.get("version")
        RESULT["sollumz_version"] = list(ver) if ver else None
        if tuple(ver or ()) != (2, 9, 0):
            warn(f"WARN_SOLLUMZ_VERSION:{ver}")
    except Exception:
        pass

    try:
        if not import_source(job["source_file"]):
            write_result(job_dir)
            sys.exit(1)

        mc = sidecar.get("motion_config") or {}
        apply_exclude_objects(sidecar)
        recipe = get_rig_recipe(sidecar)
        if recipe is not None:
            recipe_delete_trash(recipe)
            resolve_clip_playback(mc)   # activate clips BEFORE sampling
            anim = _anim_capture(recipe)
            if (mc.get("motion_mode") or "static") == \
                    "multi_part_prop_assembly":
                recipe_to_part_map(recipe, mc, context=bpy.context)
                if anim is not None:
                    # folded/bake pose lands at the user's rest frame
                    fps = float(bpy.context.scene.render.fps
                                or bpy.context.scene.render.fps_base or 30.0)
                    rest = anim.get("rest_sec")
                    out_f = int(round(float(anim.get("out_sec", 0)) * fps))
                    start_f = (int(round(float(rest) * fps))
                               if rest is not None
                               else int(round(float(anim.get("in_sec", 0))
                                              * fps)))
                    if out_f > start_f:
                        mc["frame_window"] = {"start_frame": start_f,
                                              "end_frame": out_f}
                    # capture passes run on the unmutated scene
                    if anim.get("mode") == "clip_slice":
                        capture_clip_slice_multi(job, recipe, anim)
                    elif anim.get("mode") == "drone_reactive_flight":
                        capture_drone_flight(job, recipe, anim)
            if anim is not None and anim.get("rest_sec") is not None:
                fps = float(bpy.context.scene.render.fps
                            or bpy.context.scene.render.fps_base or 30.0)
                RESULT["rest_frame"] = int(round(
                    float(anim["rest_sec"]) * fps))
            RESULT["rig_recipe"] = {
                "parts": len(recipe["parts"]),
                "trash": sum(1 for p in recipe["parts"]
                             if p.get("role") == "trash")}
        resolve_clip_playback(mc)

        RESULT["inspect"] = build_inspect()

        mode = mc.get("motion_mode") or "static"
        RESULT["motion_mode"] = mode
        asset_type = sidecar.get("asset_type", "prop")
        RESULT["asset_type"] = asset_type
        gen = sidecar.get("target_generation", "legacy")
        RESULT["target_generation"] = gen

        is_ped = asset_type == "ped" or mode == "ped_clothing"
        targets = []

        if mode in MOTION_VEHICLE_MODES:
            # MOTION_SPEC: vehicle .yft/.ycd rigging is never attempted headless
            fail("ERR_MOTION_MODE_REQUIRES_VEHICLE_RIG:" + mode)
            write_result(job_dir)
            sys.exit(1)
        elif asset_type == "ped" and mode not in ("static", "ped_clothing"):
            # explicit ped designation + a prop-motion mode is a conflict,
            # not a guess we get to resolve silently
            fail("ERR_ASSET_TYPE_CONFLICT:ped+" + mode)
            write_result(job_dir)
            sys.exit(1)
        elif mode == "ped_clothing" or (asset_type == "ped"
                                      and mode == "static"):
            if recipe is not None:
                if not process_ped_recipe(job, recipe):
                    write_result(job_dir)
                    sys.exit(1)
            elif not process_ped(job):
                write_result(job_dir)
                sys.exit(1)
        elif mode == "snapshot_sequence":
            targets = motion_snapshot_sequence(job, mc)
            if targets is None:
                write_result(job_dir)
                sys.exit(1)
        elif mode == "multi_part_prop_assembly":
            targets = motion_multi_part(job, mc)
            if targets is None:
                write_result(job_dir)
                sys.exit(1)
        elif mode != "static":
            fail("ERR_UNKNOWN_MOTION_MODE:" + str(mode))
            write_result(job_dir)
            sys.exit(1)
        else:
            strip_animation_and_armatures()          # EC_02
            remove_non_mesh_junk()                   # EC_03
            drop_degenerate()                        # EC_04 (first half)

            objs = meshes()
            if not objs:
                fail("ERR_NO_MESH_AFTER_CLEANUP")
                write_result(job_dir)
                sys.exit(1)

            split = bool(sidecar.get("split_multi_prop", False))
            if not split and len(objs) > 1:          # EC_04: join to one prop
                select_only(objs)
                bpy.ops.object.join()
                objs = meshes()
            elif split and len(objs) > 25:
                fail("ERR_TOO_MANY_SUBPROPS")
                write_result(job_dir)
                sys.exit(1)

            apply_all_transforms(objs)               # EC_05 part 1
            if recipe is not None:
                # workbench recipe: apply the exact transform the user saw
                # in the canvas -- never auto-bounds scale again
                M = recipe_transform_matrix(recipe)
                for o in objs:
                    o.data.transform(M)
                    o.data.update()
            else:
                fix_scale_and_rotation(objs, sidecar)    # EC_05 part 2
                ground_pivot(objs, sidecar.get(
                    "pivot_mode", "bottom_center"))      # EC_06

            t_objs = sorted(objs, key=lambda o: o.name) if split else objs[:1]
            targets = [(asset_name if not split else f"{asset_name}_{i + 1:02d}", o)
                       for i, o in enumerate(t_objs)]

        ok = True
        if targets:
            scene = bpy.context.scene
            ytyp = scene.ytyps.add()
            ytyp.name = asset_name
            scene.ytyp_index = len(scene.ytyps) - 1
            non_body = None
            if mode == "multi_part_prop_assembly":
                non_body = (mc.get("assembly_config") or {}).get(
                    "part_collision", "none")
            ok = process_targets(job, targets, ytyp,
                                 target_versions=GEN_TARGETS.get(gen),
                                 non_body_collision=non_body)

        if ok:
            mins, maxs, dims = world_bbox([o for o in meshes()])
            RESULT["dims"] = [round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)]
            RESULT["exported_files"] = sorted(
                os.path.relpath(os.path.join(dp, f), stream_dir)
                for dp, _dn, fs in os.walk(stream_dir) for f in fs)
            RESULT["files"] = record_stream_files(stream_dir)
    except Exception as exc:
        fail("ERR_WORKER_EXCEPTION", exc)

    write_result(job_dir)
    sys.exit(0 if RESULT["status"] == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
