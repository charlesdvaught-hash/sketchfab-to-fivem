# SPDX-License-Identifier: AGPL-3.0-only
# Headless QA for auto-rigged ped output (.ydd from ped_conform-style runs).
#
#   blender -b --factory-startup --python autorig_check.py -- <file.ydd> <out_dir>
#
# Verifies: SKEL_* bone set present, <=4 weight influences/vert, normalized
# weights, mesh/skeleton bounds overlap. Stress-poses the skeleton (arms-up,
# squat, spine twist) and reports numeric deformation metrics + renders.
# Writes <out_dir>/autorig_report.json and PNG renders. Exit 1 on hard failure.
import bpy
import json
import math
import os
import sys
from mathutils import Euler, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
YDD = os.path.abspath(argv[0])
OUT = os.path.abspath(argv[1])
os.makedirs(OUT, exist_ok=True)

report = {"ydd": YDD, "checks": {}, "metrics": {}, "pass": True}


def fail(key, msg):
    report["checks"][key] = "FAIL: " + msg
    report["pass"] = False


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="Sollumz")
bpy.ops.sollumz.import_assets(directory=os.path.dirname(YDD),
                              files=[{"name": os.path.basename(YDD)}])

arm = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE"), None)
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not arm:
    fail("armature", "no armature in imported .ydd")
    arm = None
if not meshes:
    fail("meshes", "no meshes in imported .ydd")

# --- skeleton: SKEL_* coverage ---------------------------------------------
if arm:
    bones = [b.name for b in arm.data.bones]
    skel = [b for b in bones if b.startswith("SKEL_")]
    report["checks"]["bones"] = f"{len(bones)} bones, {len(skel)} SKEL_*"
    core = ["SKEL_ROOT", "SKEL_Spine3", "SKEL_L_UpperArm", "SKEL_R_UpperArm",
            "SKEL_L_Thigh", "SKEL_R_Thigh", "SKEL_Head"]
    missing = [b for b in core if b not in bones]
    if missing:
        fail("core_bones", "missing " + ",".join(missing))
    if len(skel) < 20:
        fail("skel_coverage", f"only {len(skel)} SKEL_* bones")

# --- weights: <=4 influences, normalized ------------------------------------
max_inf, unweighted, off_sum = 0, 0, 0
total_verts = 0
for m in meshes:
    n = len(m.data.vertices)
    total_verts += n
    for v in m.data.vertices:
        gs = [g for g in v.groups if g.weight > 0]
        max_inf = max(max_inf, len(gs))
        if not gs:
            unweighted += 1
        elif abs(sum(g.weight for g in gs) - 1.0) > 0.01:
            off_sum += 1
report["metrics"]["verts"] = total_verts
report["metrics"]["unweighted"] = unweighted
report["metrics"]["max_influences"] = max_inf
report["metrics"]["non_normalized"] = off_sum
report["checks"]["weights"] = (f"max {max_inf} influences/vert, "
                               f"{unweighted} unweighted, {off_sum} off-sum")
if max_inf > 4:
    fail("influences", f"{max_inf} > GTA limit 4")
if total_verts and unweighted / total_verts > 0.01:
    fail("coverage", f"{unweighted}/{total_verts} verts unweighted")

# --- bounds overlap ----------------------------------------------------------
if arm and meshes:
    big = max(meshes, key=lambda m: len(m.data.vertices))
    bb = [big.matrix_world @ Vector(c) for c in big.bound_box]
    mz = (min(v.z for v in bb), max(v.z for v in bb))
    bz = [ (arm.matrix_world @ b.head_local).z for b in arm.data.bones ]
    bz = (min(bz), max(bz))
    overlap = not (bz[1] < mz[0] or bz[0] > mz[1])
    report["checks"]["bounds_overlap"] = (
        f"mesh z {mz[0]:.2f}..{mz[1]:.2f}, bones z {bz[0]:.2f}..{bz[1]:.2f}"
        + ("" if overlap else " — NO OVERLAP"))
    if not overlap:
        fail("bounds", "skeleton outside mesh bounds")

# --- stress poses ------------------------------------------------------------
POSES = {
    "armsup": [("SKEL_L_UpperArm", (0, 0, 1.4)), ("SKEL_R_UpperArm", (0, 0, -1.4)),
               ("SKEL_L_Forearm", (0, 0, 0.5)), ("SKEL_R_Forearm", (0, 0, -0.5))],
    "squat": [("SKEL_L_Thigh", (-1.4, 0, 0)), ("SKEL_R_Thigh", (-1.4, 0, 0)),
              ("SKEL_L_Calf", (1.8, 0, 0)), ("SKEL_R_Calf", (1.8, 0, 0)),
              ("SKEL_Spine0", (0.4, 0, 0))],
    "twist": [("SKEL_Spine2", (0, 0, 0.9)), ("SKEL_Spine3", (0, 0, 0.7)),
              ("SKEL_Neck_1", (0, 0, -0.6))],
}


def rest_edge_lengths(me):
    out = {}
    for e in me.edges:
        out[e.index] = (me.vertices[e.vertices[0]].co
                        - me.vertices[e.vertices[1]].co).length
    return out


def eval_mesh(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    return obj.evaluated_get(dg).to_mesh()


rest = {}
stretch = {}
if arm and meshes:
    big = max(meshes, key=lambda m: len(m.data.vertices))
    rest = rest_edge_lengths(big.data)
    for pname, bones_rot in POSES.items():
        for pb in arm.pose.bones:
            pb.rotation_mode = 'XYZ'
            pb.rotation_euler = Euler((0, 0, 0), 'XYZ')
        applied = 0
        for bn, rot in bones_rot:
            pb = arm.pose.bones.get(bn)
            if pb:
                pb.rotation_mode = 'XYZ'
                pb.rotation_euler = Euler(rot, 'XYZ')
                applied += 1
        bpy.context.view_layer.update()
        em = eval_mesh(big)
        mx = 0.0
        for e in em.edges:
            l = (em.vertices[e.vertices[0]].co
                 - em.vertices[e.vertices[1]].co).length
            r = rest.get(e.index, 0) or 1e-6
            mx = max(mx, l / r)
        stretch[pname] = round(mx, 3)
        # verts weighted to bones far away = dangling-risk outliers
        far = 0
        world_bones = {b.name: (arm.matrix_world @ b.head_local)
                       for b in arm.data.bones}
        vg = {g.index: g.name for g in big.vertex_groups}
        for v in big.data.vertices:
            if not v.groups:
                continue
            dom = max(v.groups, key=lambda g: g.weight)
            bn = vg.get(dom.group)
            if bn in world_bones and (v.co - world_bones[bn]).length > 0.8:
                far += 1
        report["metrics"][f"far_bone_verts_{pname}"] = far
        big.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
        if applied < len(bones_rot):
            report["checks"][f"pose_{pname}"] = (
                f"{applied}/{len(bones_rot)} bones posed")
report["metrics"]["max_edge_stretch"] = stretch
for p, v in stretch.items():
    if v > 6.0:
        fail(f"stretch_{p}", f"edge stretched {v}x under {p}")
    elif v > 3.0:
        report["checks"][f"stretch_{p}"] = f"WARN: {v}x under {p} (borderline)"

# --- renders -----------------------------------------------------------------
# Turntable: 8 azimuth angles per pose, framed to the *deformed* mesh bounds
# (bound_box is the rest bbox and does not track armature deformation).
# Also writes one .glb per pose for interactive rotation in a 3D viewer.
scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE'
scene.render.resolution_x = scene.render.resolution_y = 720
scene.render.filepath = OUT
scene.render.image_settings.file_format = 'PNG'
w = bpy.data.worlds.new("qa_world")
scene.world = w
if w.use_nodes:
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.35, 0.35, 0.4, 1.0)
    w.node_tree.nodes["Background"].inputs[1].default_value = 0.6
for name, energy, rot, ltype in (
        ("qa_key", 4.0, (0.8, 0.1, 0.7), 'SUN'),
        ("qa_fill", 2.0, (0.6, 0.1, -2.2), 'SUN'),
        ("qa_rim", 3.0, (-0.5, 0.3, 2.8), 'SUN')):
    li = bpy.data.objects.new(name, bpy.data.lights.new(name, ltype))
    li.data.energy = energy
    li.rotation_euler = Euler(rot, 'XYZ')
    scene.collection.objects.link(li)
cam = bpy.data.objects.new("qa_cam", bpy.data.cameras.new("qa_cam"))
cam.data.lens = 50
scene.collection.objects.link(cam)
scene.camera = cam


def deformed_bounds(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    em = obj.evaluated_get(dg).to_mesh()
    pts = [obj.matrix_world @ v.co for v in em.vertices]
    obj.evaluated_get(dg).to_mesh_clear()
    c = sum(pts, Vector()) / len(pts)
    r = max((p - c).length for p in pts) or 1
    return c, r


ANGLES = list(range(0, 360, 45))          # turntable, deg
ELEV = 0.35                                # slight downward view
EXPORT_GLB = True                          # per-pose .glb for free rotation


def export_glb_pose(obj, path):
    dg = bpy.context.evaluated_depsgraph_get()
    em = obj.evaluated_get(dg).to_mesh()
    me_copy = em.copy()
    tmp = bpy.data.objects.new("qa_pose_export", me_copy)
    tmp.matrix_world = obj.matrix_world.copy()
    bpy.context.scene.collection.objects.link(tmp)
    bpy.ops.object.select_all(action='DESELECT')
    tmp.select_set(True)
    bpy.ops.export_scene.gltf(filepath=path, use_selection=True,
                              export_yup=True, export_apply=False)
    bpy.data.objects.remove(tmp, do_unlink=True)
    bpy.data.meshes.remove(me_copy)
    obj.evaluated_get(dg).to_mesh_clear()


for pname, bones_rot in POSES.items():
    if arm:
        for pb in arm.pose.bones:
            pb.rotation_euler = Euler((0, 0, 0), 'XYZ')
        for bn, rot in bones_rot:
            pb = arm.pose.bones.get(bn)
            if pb:
                pb.rotation_mode = 'XYZ'
                pb.rotation_euler = Euler(rot, 'XYZ')
        bpy.context.view_layer.update()
    meshes_now = [o for o in scene.objects if o.type == 'MESH']
    if not meshes_now:
        continue
    big_now = max(meshes_now, key=lambda m: len(m.data.vertices))
    center, rad = deformed_bounds(big_now)
    for deg in ANGLES:
        a = math.radians(deg)
        off = Vector((math.sin(a) * math.cos(ELEV),
                      -math.cos(a) * math.cos(ELEV),
                      math.sin(ELEV)))
        cam.location = center + off * rad * 2.1
        d = center - cam.location
        cam.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()
        scene.render.filepath = os.path.join(
            OUT, f"qa_{pname}_{deg:03d}.png")
        bpy.ops.render.render(write_still=True)
    if EXPORT_GLB:
        export_glb_pose(big_now, os.path.join(OUT, f"qa_{pname}.glb"))

with open(os.path.join(OUT, "autorig_report.json"), "w") as fh:
    json.dump(report, fh, indent=1)
print("AUTORIG_CHECK", json.dumps(report["checks"], indent=1))
print("PASS" if report["pass"] else "FAIL")
sys.exit(0 if report["pass"] else 1)
