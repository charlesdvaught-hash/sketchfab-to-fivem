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
  [PASS 4] Interior probes: piecewise keyframe lerp+slerp (sequential
      hemisphere chaining) vs TRUE source evaluation at inter-stop
      midpoints + fixed sweep — knot points are trivially exact, so the
      probes sit between them; falls back to t=0.5 on 2-pose manifests.
"""
import bpy
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from mathutils import Euler, Matrix, Quaternion, Vector

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


# Carrier-relative frame: the manifest stores part trajectories in the
# body's f_start frame (W_rel = ref0 @ ref_k^-1 @ W_k, generalizing
# D_b_inv @ W1). PASS4 must relativize TRUE source verts the same way or
# mid-clip carrier motion reads as phantom part error.
_mc = sidecar.get("motion_config") or {}
_pm = _mc.get("part_map") or {}
_body_spec = _pm.get("_body") or []
forced_order = (list(_body_spec.get("meshes") or [])
                if isinstance(_body_spec, dict) else list(_body_spec))
ref_name = forced_order[0] if forced_order else \
    (body_names[0] if body_names else None)
ref_obj = (bpy.context.scene.objects.get(ref_name)
           if ref_name else None)


def _rigid_se3(M):
    """Strip scale/shear: carrier relativization must be strictly
    Euclidean or animated root squash injects phantom shear (F-04)."""
    return Matrix.LocRotScale(M.to_translation(),
                              M.to_quaternion().normalized(),
                              Vector((1.0, 1.0, 1.0)))


ref0_mw = (_rigid_se3(ref_obj.matrix_world.copy())
           if ref_obj is not None else None)


def carrier_rel(dg_now):
    """C_k: maps world-at-frame-k into the carrier's f_start frame."""
    if ref_obj is None:
        return Matrix.Identity(4)
    refk = _rigid_se3(ref_obj.evaluated_get(dg_now).matrix_world)
    return ref0_mw @ refk.inverted()


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
src_at_start = {}
member_start_verts = {}
for part in man.get("parts", []):
    model = part["model"]
    cverts = read_ydr_xml_verts(model)
    members = part.get("source_objects") or []
    true = normalize_src(eval_world_verts(members, dg))
    if len(members) > 1:
        member_start_verts[model] = {
            m: normalize_src(eval_world_verts([m], dg)) for m in members}
    if not cverts or not true:
        continue
    off = Vector(part.get("attach_offset", (0, 0, 0)))
    rot_deg = part.get("folded_rot") or part.get("initial_rot") or [0, 0, 0]
    R_man = Euler([math.radians(x) for x in rot_deg], 'YXZ').to_matrix()
    step = max(1, len(cverts) // 800)
    pred = np.array([[*(off + R_man @ v)] for v in cverts[::step]])
    tv = np.array([[*v] for v in true])
    src_at_start[model] = true
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

# --- PASS 4: INTERIOR PROBES — manifest piecewise lerp vs TRUE source ---
# Knot points are trivially exact (a baked stop reports 0.0000 by
# construction). Probe midpoints BETWEEN every adjacent keyframe pair plus
# a fixed 4-point sweep so inter-stop interpolation error can't hide.
# Falls back to the single t=0.5 probe for legacy 2-pose manifests.
def predict_pose(part, t):
    """Manifest-space (off, R, repose, S) at normalized t — piecewise
    keyframe lerp+slerp when 'keyframes' exist, else 2-pose hemisphere
    slerp. repose/S are only non-None on rest_rot parts: the emitted basis
    is q_pred = S @ ramp(s) with ramp=slerp(D4_inv,I,s), so the net re-pose
    baked into predicted verts vs raw source is repose = ramp @ D4 =
    slerp(I, D4, s). Truth-side comparison must apply the SAME re-pose:
    conjugate truth about off_p by S @ repose @ S^-1 (exact -- the
    right-side q_pred strip leaves a commutator term)."""
    q_D4 = None
    if part.get("rest_rot_deg"):
        q_D4 = Euler([math.radians(x) for x in part["rest_rot_deg"]],
                     'XYZ').to_quaternion()
    kfs = part.get("keyframes")
    if kfs:
        tks = [float(k["t"]) for k in kfs]
        ps = [Vector(k["pos"]) for k in kfs]
        if all(k.get("quat") for k in kfs):
            # prefer the emitted chained quat -- euler 'rot' can hop
            # branches on >180deg swing hinges and corrupt the slerp
            qs = [Quaternion(k["quat"]) for k in kfs]
        else:
            qs = [Euler([math.radians(x) for x in k["rot"]],
                        'YXZ').to_quaternion() for k in kfs]
        for i in range(1, len(qs)):
            if qs[i - 1].dot(qs[i]) < 0.0:  # sequential hemisphere chain
                qs[i].negate()
        hi = next((i for i in range(1, len(tks)) if tks[i] >= t),
                  len(tks) - 1)
        lo = max(0, hi - 1)
        a = 0.0 if tks[hi] <= tks[lo] else (t - tks[lo]) / (tks[hi] - tks[lo])
        q_p = qs[lo].slerp(qs[hi], a)
        repose = S = None
        if q_D4 is not None:
            svals = [float(k.get("repose", k["t"])) for k in kfs]
            s_t = svals[lo] + (svals[hi] - svals[lo]) * a
            q_ident = Euler((0.0, 0.0, 0.0)).to_quaternion()
            repose = q_ident.slerp(q_D4, s_t)          # slerp(I,D4,s)
            ramp = q_D4.inverted().slerp(q_ident, s_t)  # slerp(D4_inv,I,s)
            S = (q_p @ ramp.inverted()).to_matrix()
        return (ps[lo] + (ps[hi] - ps[lo]) * a,
                q_p.to_matrix(), repose, S)
    off0 = Vector(part.get("attach_offset", (0, 0, 0)))
    off1 = Vector(part.get("deployed_offset", off0) or off0)
    r0 = part.get("folded_rot") or part.get("initial_rot") or [0, 0, 0]
    r1 = part.get("deployed_rot") or part.get("folded_rot") \
        or part.get("initial_rot") or [0, 0, 0]
    q0 = Euler([math.radians(x) for x in r0], 'YXZ').to_quaternion()
    q1 = Euler([math.radians(x) for x in r1], 'YXZ').to_quaternion()
    q_p = slerp_hemi(q0, q1, t)
    repose = S = None
    if q_D4 is not None:
        q_ident = Euler((0.0, 0.0, 0.0)).to_quaternion()
        repose = q_ident.slerp(q_D4, t)
        ramp = q_D4.inverted().slerp(q_ident, t)
        S = (q_p @ ramp.inverted()).to_matrix()
    return off0 + (off1 - off0) * t, q_p.to_matrix(), repose, S


def repose_truth(off_p, repose, S, pts):
    """Apply the predicted part's net rest_rot re-pose to source-truth
    verts: rotate them about the pivot by S @ repose @ S^-1."""
    if repose is None or S is None:
        return pts
    q_net = S.to_quaternion() @ repose @ S.to_quaternion().inverted()
    M = q_net.to_matrix()
    return [off_p + M @ (p - off_p) for p in pts]


f_end = man.get("frame_window", {}).get("end_frame", f_start)
if f_end > f_start:
    span = f_end - f_start
    print("\n=== PASS 4: INTERIOR PROBES manifest-vs-TRUE-source ===")
    for part in man.get("parts", []):
        dep_off = part.get("deployed_offset")
        dep_rot = part.get("deployed_rot")
        if not dep_off or not dep_rot:
            continue
        kfs = part.get("keyframes") or []
        if len(kfs) >= 2:
            probes = {0.5 * (float(kfs[i]["t"]) + float(kfs[i + 1]["t"]))
                      for i in range(len(kfs) - 1)}
            probes |= {0.125, 0.375, 0.625, 0.875}
            stop_ts = {float(k["t"]) for k in kfs}
        else:
            probes = {0.5}
            stop_ts = set()
        frames = sorted({f_start + round(t * span) for t in probes})
        frames = [f for f in frames if f_start < f < f_end]
        # F-05a: drop probes that landed on a baked stop frame -- a knot
        # point reports ~0 by construction and would waste the budget.
        frames = [f for f in frames
                  if all(abs((f - f_start) / span - st) * span > 0.5
                         for st in stop_ts)]
        members = part.get("source_objects") or []
        cverts = read_ydr_xml_verts(part["model"])
        if not members or not cverts:
            continue
        c_local = sum(cverts, Vector()) / len(cverts)
        worst = None
        worst_member = 0.0
        # per-member canonical centroids: u_m = R_fold^-1 @ (v0 - off_p)
        # (folded basis reproduces v0 exactly -- PASS3b), so each member's
        # own predicted centroid is off_p + R_p @ u_m.
        member_uc = {}
        if len(members) > 1:
            r_fr = Euler([math.radians(x) for x in
                          (part.get("folded_rot")
                           or part.get("initial_rot") or [0, 0, 0])],
                         'YXZ').to_matrix().inverted()
            off0 = Vector(part.get("attach_offset", (0, 0, 0)))
            for mname in members:
                v0 = normalize_src(eval_world_verts([mname], dg))
                if v0:
                    member_uc[mname] = r_fr @ (
                        sum(v0, Vector()) / len(v0) - off0)
        for fr in frames:
            t_a = (fr - f_start) / span  # actual t at the integer frame
            bpy.context.scene.frame_set(fr)
            bpy.context.view_layer.update()
            dg_f = bpy.context.evaluated_depsgraph_get()
            Ck = carrier_rel(dg_f)
            true_f = normalize_src([Ck @ v
                                    for v in eval_world_verts(members,
                                                              dg_f)])
            if not true_f:
                continue
            off_p, R_p, repose, S = predict_pose(part, t_a)
            true_f = repose_truth(off_p, repose, S, true_f)
            pred = off_p + R_p @ c_local
            c_true = sum(true_f, Vector()) / len(true_f)
            delta = (pred - c_true).length
            flag = "  <-- EXCEEDS 0.03m" if delta > 0.03 else ""
            print(f"  PROBE {part['model']} t={t_a:.4f} (f{fr}): "
                  f"{delta:.4f}m{flag}")
            if member_uc:
                # per-member worst deviation: each member's own predicted
                # centroid vs its true centroid (repose-adjusted)
                dm = 0.0
                for mname, u_m in member_uc.items():
                    tv = normalize_src(
                        [Ck @ v for v in eval_world_verts([mname], dg_f)])
                    if tv:
                        tv = repose_truth(off_p, repose, S, tv)
                        pm = off_p + R_p @ u_m
                        dm = max(dm,
                                 (pm - sum(tv, Vector()) / len(tv)).length)
                worst_member = max(worst_member, dm)
            if worst is None or delta > worst[0]:
                worst = (delta, fr, t_a, c_true, pred)
        if worst is not None:
            delta, fr, t_a, c_true, pred = worst
            bpy.context.scene.frame_set(f_end)
            bpy.context.view_layer.update()
            dg_e = bpy.context.evaluated_depsgraph_get()
            Ce = carrier_rel(dg_e)
            true_e = normalize_src([Ce @ v
                                    for v in eval_world_verts(members,
                                                              dg_e)])
            true_fold = src_at_start.get(part["model"])
            if true_fold and true_e:
                cf = sum(true_fold, Vector()) / len(true_fold)
                ce = sum(true_e, Vector()) / len(true_e)
                print(f"      worst: {delta:.4f}m @ t={t_a:.4f} "
                      f"pred={tuple(round(x,3) for x in pred)} "
                      f"true={tuple(round(x,3) for x in c_true)}")
                if worst_member > 0.0:
                    print(f"      worst per-member centroid dev "
                          f"(interior probes): {worst_member:.4f}m")
                print(f"      |true_worst - folded|={(c_true-cf).length:.3f}m "
                      f"|true_worst - deployed|={(c_true-ce).length:.3f}m "
                      f"|folded - deployed|={(ce-cf).length:.3f}m")

# --- PASS 3c: DEPLOYED-POSE per-member residual ---
# PASS3b validates the f_start join (intra-member deviation is 0 there by
# construction); the deployed end is where non-rigid multi-member chains
# show their permanent error. Per-member: predicted pose vs that member's
# own true verts at f_end, with the rest_rot re-pose conjugated onto truth.
    print("\n=== PASS 3c: DEPLOYED-POSE PER-MEMBER RESIDUAL AT f_end ===")
    bpy.context.scene.frame_set(f_end)
    bpy.context.view_layer.update()
    dg_end = bpy.context.evaluated_depsgraph_get()
    Ce = carrier_rel(dg_end)
    for part in man.get("parts", []):
        members = part.get("source_objects") or []
        if len(members) < 2:
            continue
        dep_off = part.get("deployed_offset")
        dep_rot = part.get("deployed_rot")
        if not dep_off or not dep_rot:
            continue
        off_p, R_p, repose, S = predict_pose(part, 1.0)
        r_fr_inv = Euler([math.radians(x) for x in
                          (part.get("folded_rot")
                           or part.get("initial_rot") or [0, 0, 0])],
                         'YXZ').to_matrix().inverted()
        print(f"  Part {part['model']} ({len(members)} members):")
        for mname in members:
            tv = normalize_src(
                [Ce @ v for v in eval_world_verts([mname], dg_end)])
            if not tv:
                continue
            tv = repose_truth(off_p, repose, S, tv)
            v0 = member_start_verts.get(part["model"], {}).get(mname)
            if not v0:
                continue
            step = max(1, len(v0) // 400)
            pred = [off_p + R_p @ (r_fr_inv @ (v - off_p))
                    for v in v0[::step]]
            dn = max((pv - tvx).length
                     for pv, tvx in zip(pred, tv[::step]))
            print(f"    {mname}: max_vert_dev={dn:.4f}m "
                  f"(n={len(pred)} vs {len(tv[::step])})")
