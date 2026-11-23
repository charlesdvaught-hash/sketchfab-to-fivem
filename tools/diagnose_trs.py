#!/usr/bin/env python3
"""diagnose_trs.py — 4-pass TRS divergence check for multi-part exports

Run inside Blender headless against the source .glb and published resource dir:
  blender --background --factory-startup --python diagnose_trs.py -- \\
      <path/to/model.glb> <path/to/published_resource_dir>

Checks:
  [PASS 1] Raw glTF import (muted NLA strips, which is what --dump-preview
      saw before the fix) vs post-resolve_clip_playback() active pose at f_start.
  [PASS 2] Parses emitted stream/<part>.ydr.xml <BoundingBoxMin/Max> to measure
      each .ydr's internal mesh offset from (0,0,0) (detects missing +body_off
      on pivot_cfg).
  [PASS 3] Lever-arm displacement: (attach_offset + R @ c_ydr) vs
      (attach_offset + c_ydr).
  [PASS 3b] Max-vertex reconstruction: sampled canonical .ydr verts placed via
      (off + R @ v) vs nearest true source world vert at f_start (chamfer).
  [PASS 4] Mid-pose check: manifest offset-lerp + hemisphere-guarded slerp at
      t=0.5 vs TRUE source evaluation at frame_mid — surfaces chord-vs-arc sag
      and staggered sub-clip timing that endpoint checks miss.
"""
import bpy
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from mathutils import Euler, Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
GLB_PATH = os.path.abspath(argv[0])
RES_DIR = os.path.abspath(argv[1])

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB_PATH)

def eval_centroid_and_mw(obj, dg):
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    mw = ev.matrix_world.copy()
    if len(me.vertices) == 0:
        ev.to_mesh_clear()
        return Vector((0, 0, 0)), mw
    c = sum((mw @ v.co for v in me.vertices), Vector()) / len(me.vertices)
    ev.to_mesh_clear()
    return c, mw

# --- PASS 1: Muted NLA (--dump-preview state) vs Active NLA (compile state) ---
dg = bpy.context.evaluated_depsgraph_get()
muted_state = {}
for o in bpy.context.scene.objects:
    if o.type == 'MESH':
        muted_state[o.name] = eval_centroid_and_mw(o, dg)

# Activate per-object NLA strips exactly as blender_worker.resolve_clip_playback()
for o in bpy.context.scene.objects:
    ad = o.animation_data
    if ad is None or ad.action is not None:
        continue
    names = {s.action.name for t in ad.nla_tracks for s in t.strips if s.action}
    if len(names) == 1:
        ad.action = bpy.data.actions.get(next(iter(names)))
        for t in ad.nla_tracks:
            t.mute = True
    elif len(names) > 1:
        mine = {n for n in names if len(o.name) >= 3 and o.name.lower() in n.split("|")[-1].lower()}
        if len(mine) == 1:
            ad.action = bpy.data.actions.get(next(iter(mine)))
            for t in ad.nla_tracks:
                t.mute = True

man_path = os.path.join(RES_DIR, "assembly_manifest.lua.json")
man = json.load(open(man_path, encoding="utf-8"))
f_start = man.get("frame_window", {}).get("start_frame", 0)
bpy.context.scene.frame_set(f_start)
bpy.context.view_layer.update()
dg = bpy.context.evaluated_depsgraph_get()

# --- Normalization frame: manifest quantities live in normalized space
# (N @ raw + body_off). Reconstruct the same transform the worker applied
# so source verts can be compared against manifest-predicted positions.
def _xform_to_matrix(t):
    rz = math.radians(float((t or {}).get("rot_z_deg", 0.0)))
    s = float((t or {}).get("scale", 1.0))
    off = Vector((t or {}).get("offset", (0.0, 0.0, 0.0)))
    return (Matrix.Translation(off) @ Matrix.Scale(s, 4)
            @ Matrix.Rotation(rz, 4, 'Z'))


sidecar_path = os.path.join(os.path.dirname(GLB_PATH), "override.json")
sidecar = (json.load(open(sidecar_path, encoding="utf-8"))
           if os.path.isfile(sidecar_path) else {})
rr = sidecar.get("rig_recipe") or {}
all_meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
member_names = {m for p in man.get("parts", [])
                for m in (p.get("source_objects") or [])}
trash_names = {m for p in rr.get("parts", []) if p.get("role") == "trash"
               for m in ([p.get("name")] if p.get("name") else [])}
body_names = [o.name for o in all_meshes
              if o.name not in member_names and o.name not in trash_names]


def _raw_world_verts(objs, dg):
    out = []
    for o in objs:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        mw = ev.matrix_world
        out.extend(mw @ v.co for v in me.vertices)
        ev.to_mesh_clear()
    return out


if rr.get("parts"):
    N_mat = (_xform_to_matrix(rr.get("user_transform"))
             @ _xform_to_matrix(rr.get("preview_transform")))
else:
    pts = _raw_world_verts(all_meshes, dg)
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts),
                 min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts),
                 max(p.z for p in pts)))
    dims = mx - mn
    factor = 1.0
    th = sidecar.get("target_height_meters")
    if th:
        if dims.z > 0:
            factor = float(th) / dims.z
    else:
        m = max(dims.x, dims.y, dims.z)
        if m > 100.0:
            factor = 0.01
        elif m < 0.005:
            factor = 100.0
    factor *= float(sidecar.get("scale_multiplier", 1.0))
    rot = sidecar.get("rotation_offset_deg") or [0.0, 0.0, 0.0]
    rx, ry, rz = (math.radians(float(r)) for r in rot)
    N_mat = (Matrix.Rotation(rz, 4, "Z") @ Matrix.Rotation(ry, 4, "Y")
             @ Matrix.Rotation(rx, 4, "X")) @ Matrix.Scale(factor, 4)

# body_off: grounding shift applied to ALL verts in normalized space.
body_off = Vector((0, 0, 0))
pivot_mode = sidecar.get("pivot_mode", "bottom_center")
if body_names and pivot_mode != "keep_origin":
    bobjs = [o for o in all_meshes if o.name in set(body_names)]
    bverts = [N_mat @ v for v in _raw_world_verts(bobjs, dg)]
    bmn = Vector((min(p.x for p in bverts), min(p.y for p in bverts),
                  min(p.z for p in bverts)))
    bmx = Vector((max(p.x for p in bverts), max(p.y for p in bverts),
                  max(p.z for p in bverts)))
    body_off = (Vector((-(bmn.x + bmx.x) / 2.0,
                        -(bmn.y + bmx.y) / 2.0, -bmn.z))
                if pivot_mode == "bottom_center" else -(bmn + bmx) / 2.0)


def normalize_src(verts):
    return [N_mat @ v + body_off for v in verts]

print("=== PASS 1: RAW/MUTED (--dump-preview) vs ACTIVE CLIP AT f_start ===")
for o in bpy.context.scene.objects:
    if o.type != 'MESH':
        continue
    c_m, mw_m = muted_state[o.name]
    c_a, mw_a = eval_centroid_and_mw(o, dg)
    dc = (c_a - c_m).length
    dq = math.degrees((mw_a @ mw_m.inverted()).to_quaternion().angle)
    if dc > 1e-3 or dq > 0.1:
        print(f"  [NLA_DIVERGENCE] {o.name}: delta_pos={dc:.4f}m, delta_rot={dq:.2f}deg")

# --- PASS 2 & 3: Compare .ydr.xml AABB center + manifest placement ---
def read_ydr_xml_verts(model_name):
    """Canonical (pivot-local) vertex positions from stream/<model>.ydr.xml."""
    xml_path = os.path.join(RES_DIR, "stream", model_name + ".ydr.xml")
    if not os.path.isfile(xml_path):
        return None
    root = ET.parse(xml_path).getroot()
    verts = []
    for vb in root.iter("VertexBuffer"):
        data = vb.find("Data")
        if data is None or data.text is None:
            continue
        for line in data.text.splitlines():
            t = line.split()
            if len(t) >= 3:
                try:
                    verts.append(Vector((float(t[0]), float(t[1]),
                                         float(t[2]))))
                except ValueError:
                    pass
    return verts or None


def eval_world_verts(names, dg):
    out = []
    for n in names:
        o = bpy.context.scene.objects.get(n)
        if o is None or o.type != 'MESH':
            continue
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        mw = ev.matrix_world
        out.extend(mw @ v.co for v in me.vertices)
        ev.to_mesh_clear()
    return out


def slerp_hemi(q0, q1, f):
    if q0.dot(q1) < 0.0:
        q1 = q1.copy()
        q1.negate()
    return q0.slerp(q1, f)


def read_ydr_xml_bbox_center(model_name):
    xml_path = os.path.join(RES_DIR, "stream", model_name + ".ydr.xml")
    if not os.path.isfile(xml_path):
        return None
    root = ET.parse(xml_path).getroot()
    bmin = root.find("BoundingBoxMin")
    bmax = root.find("BoundingBoxMax")
    if bmin is None or bmax is None:
        return None
    mn = Vector((float(bmin.get("x")), float(bmin.get("y")), float(bmin.get("z"))))
    mx = Vector((float(bmax.get("x")), float(bmax.get("y")), float(bmax.get("z"))))
    return 0.5 * (mn + mx)

print("\n=== PASS 2 & 3: .YDR INTERNAL OFFSET vs MANIFEST DOUBLE-ROTATION ===")
for part in man.get("parts", []):
    model = part["model"]
    c_ydr = read_ydr_xml_bbox_center(model)
    off = Vector(part.get("attach_offset", (0, 0, 0)))
    rot_deg = part.get("folded_rot") or part.get("initial_rot") or [0, 0, 0]
    R_man = Euler([math.radians(x) for x in rot_deg], 'YXZ').to_matrix()
    if c_ydr is not None:
        pos_no_rot = off + c_ydr
        pos_with_rot = off + (R_man @ c_ydr)
        lever_swing = (pos_with_rot - pos_no_rot).length
        print(f"  Part {model} ({part.get('kind')}):")
        print(f"    .ydr internal bbox center c_ydr = {tuple(round(x, 4) for x in c_ydr)} "
              f"(||c_ydr|| = {c_ydr.length:.4f}m <-- should be ~0 if pivot matched!)")
        print(f"    manifest attach_offset          = {tuple(round(x, 4) for x in off)}")
        print(f"    manifest folded/initial_rot     = {rot_deg} deg (YXZ)")
        print(f"    world pos WITHOUT extra rot     = {tuple(round(x, 4) for x in pos_no_rot)}")
        print(f"    world pos WITH manifest rot     = {tuple(round(x, 4) for x in pos_with_rot)}")
        print(f"    --> LEVER-ARM DISPLACEMENT      = {lever_swing:.4f}m")

# --- PASS 3b: MAX-VERTEX manifest reconstruction ---
# Ground truth = pre-canonicalization world verts of the source objects
# (evaluated at f_start, mapped through N+body_off). Canonical verts from
# the .ydr.xml. Metric: max_v ||(p_manifest + R_manifest @ v_canon) - v_src||.
# Indexed when vertex counts match (single-member parts keep vert order);
# chamfer (nearest-vertex) otherwise / as a scramble check.
print("\n=== PASS 3b: MAX-VERTEX MANIFEST RECONSTRUCTION AT f_start ===")
import numpy as np
for part in man.get("parts", []):
    model = part["model"]
    cverts = read_ydr_xml_verts(model)
    members = part.get("source_objects") or []
    true = normalize_src(eval_world_verts(members, dg))
    if not cverts or not true:
        continue
    off = Vector(part.get("attach_offset", (0, 0, 0)))
    rot_deg = part.get("folded_rot") or part.get("initial_rot") or [0, 0, 0]
    R_man = Euler([math.radians(x) for x in rot_deg], 'YXZ').to_matrix()
    step = max(1, len(cverts) // 800)
    pred = np.array([[*(off + R_man @ v)] for v in cverts[::step]])
    tv = np.array([[*v] for v in true])
    idx_err = None
    if len(cverts) == len(true):
        full_pred = np.array([[*(off + R_man @ v)] for v in cverts])
        idx_err = np.sqrt(((full_pred - tv) ** 2).sum(-1))
    # one-sided chamfer: dist from each predicted vert to nearest true vert
    d2 = ((pred[:, None, :] - tv[None, :, :]) ** 2).sum(-1)
    dn = np.sqrt(d2.min(1))
    extra = (f" idx_max={idx_err.max():.4f} idx_mean={idx_err.mean():.4f}"
             if idx_err is not None else "")
    print(f"  Part {model}: max_vertex_err={dn.max():.4f}m "
          f"mean={dn.mean():.4f}m (n={len(pred)}/{len(cverts)} canon, "
          f"{len(tv)} src){extra}")

# --- PASS 4: MID-POSE manifest lerp vs TRUE source frame_mid ---
# Endpoints match by construction; t=0.5 is where 2-pose interpolation
# diverges from the source animation (chord-vs-arc, staggered timing).
f_end = man.get("frame_window", {}).get("end_frame", f_start)
f_mid = (f_start + f_end) // 2
t_mid = (f_mid - f_start) / (f_end - f_start) if f_end > f_start else 0.0
if f_end > f_start:
    bpy.context.scene.frame_set(f_mid)
    bpy.context.view_layer.update()
    dg_mid = bpy.context.evaluated_depsgraph_get()
    print(
        f"\n=== PASS 4: MANIFEST-LERP vs TRUE SOURCE @ frame {f_mid} "
        f"(t={t_mid:.4f}) ===")
    for part in man.get("parts", []):
        dep_off = part.get("deployed_offset")
        dep_rot = part.get("deployed_rot")
        if not dep_off or not dep_rot:
            continue
        members = part.get("source_objects") or []
        true_mid = normalize_src(eval_world_verts(members, dg_mid))
        cverts = read_ydr_xml_verts(part["model"])
        if not true_mid or not cverts:
            continue
        off0 = Vector(part.get("attach_offset", (0, 0, 0)))
        off1 = Vector(dep_off)
        off_mid = off0 + (off1 - off0) * t_mid
        r0 = part.get("folded_rot") or part.get("initial_rot") or [0, 0, 0]
        q0 = Euler([math.radians(x) for x in r0], 'YXZ').to_quaternion()
        q1 = Euler([math.radians(x) for x in dep_rot],
                   'YXZ').to_quaternion()
        R_mid = slerp_hemi(q0, q1, t_mid).to_matrix()
        c_local = sum(cverts, Vector()) / len(cverts)
        pred = off_mid + R_mid @ c_local
        c_true = sum(true_mid, Vector()) / len(true_mid)
        delta = (pred - c_true).length
        flag = "  <-- EXCEEDS 0.03m (chord-vs-arc/staggered)" \
            if delta > 0.03 else ""
        print(f"  MIDPOSE_DELTA {part['model']} ({part.get('kind')}): "
              f"{delta:.4f}m{flag}")
