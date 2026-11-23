#!/usr/bin/env python3
"""diagnose_buster_trs.py — Decisive 3-Way Divergence Check (Cause 1/2 vs Cause 3)

Run inside Blender headless against the source .glb and published resource dir:
  blender --background --factory-startup --python diagnose_buster_trs.py -- \\
      <path/to/buster_drone.glb> <path/to/published_resource_dir>

Checks:
  [PASS 1: Cause 3 vs Cause 1] Compares raw glTF import (38/39 NLA strips muted,
      which is what --dump-preview sees!) vs post-resolve_clip_playback() active
      pose at f_start.
  [PASS 2: Cause 2 Origin Offset] Parses emitted stream/<part>.ydr.xml
      <BoundingBoxMin/Max> to measure each .ydr's internal mesh offset from (0,0,0)
      (detects missing +body_off on pivot_cfg).
  [PASS 3: Cause 2 Double-Rotation] Compares true normalized centroid at f_start
      against (attach_offset + c_ydr) [no extra rotation] vs
      (attach_offset + R_YXZ(folded_rot) @ c_ydr) [current render_check.py].
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

print("\\n=== PASS 2 & 3: .YDR INTERNAL OFFSET vs MANIFEST DOUBLE-ROTATION ===")
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
