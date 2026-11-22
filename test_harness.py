# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end regression suite for the Sketchfab asset pipeline.

Generates fixtures in Blender (textured prop cube, multi-part drone, rigged
skinned cylinder), feeds them through orchestrator --file, and asserts:

  * happy paths: prop publish, ytd mode, snapshot_sequence, multi_part,
    ped_clothing (.ydd), enhanced/both target generations
  * quarantine paths: every gate + the new ERR_* codes (zips, sidecars,
    sniffing, ped gates, apparel, conflicts, motion)

Run:  python test_harness.py
Exit: 0 = all checks passed.
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
OUT_RES = os.path.join(BUILD, "resources", "[sketchfab_props]")
REGISTRY = os.path.join(BUILD, "asset_registry.json")
CAPS_FILE = os.path.join(BUILD, "toolchain_caps.json")
QUAR = os.path.join(ROOT, "quarantine")

sys.path.insert(0, ROOT)
from orchestrator import find_blender  # noqa: E402

GEN_SCRIPT = r"""
import bpy, sys
import numpy as np
out = sys.argv[sys.argv.index('--') + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 0.2))
cube = bpy.context.active_object
cube.name = 'test_cube'
mat = bpy.data.materials.new('testmat')
mat.use_nodes = True
bsdf = mat.node_tree.nodes['Principled BSDF']
tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
img = bpy.data.images.new('testdiff', width=256, height=256)
# noise texture: RSC7 pages compress uniform images to nothing, which makes
# the exported .ydr falsely look texture-less. Noise is incompressible.
rng = np.random.default_rng(42)
img.pixels = rng.random((256, 256, 4), dtype=np.float32).flatten().tolist()
tex.image = img
mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
cube.data.materials.append(mat)
# animation to be stripped (EC_02)
cube.keyframe_insert(data_path='location', frame=1)
cube.location.z = 2.0
cube.keyframe_insert(data_path='location', frame=20)
# junk objects to be removed (EC_03)
bpy.ops.object.camera_add(location=(3, -3, 2))
bpy.ops.object.empty_add(location=(0, 0, 0))
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
print('WROTE', out)
"""

# Multi-part drone: static body+antenna, one arm keyframed 0->90deg Z over
# frames 1-45. Exercises multi_part_prop_assembly + snapshot_sequence.
DRONE_GEN_SCRIPT = r"""
import bpy, sys, math
out = sys.argv[sys.argv.index('--') + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)

def cube(name, loc, scale):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return o

body = cube('drone_body', (0, 0, 0.15), (0.3, 0.3, 0.1))
antenna = cube('drone_antenna', (0, 0, 0.4), (0.02, 0.02, 0.15))
arm = cube('drone_arm', (0.3, 0, 0.2), (0.2, 0.05, 0.05))
# hinge = the arm's own object origin (0.3, 0, 0.2); keyed Z rot 0 -> 90 deg
arm.rotation_euler = (0, 0, 0)
arm.keyframe_insert(data_path='rotation_euler', frame=1)
arm.rotation_euler = (0, 0, math.radians(90))
arm.keyframe_insert(data_path='rotation_euler', frame=45)
# rotor = thin blade spinning 720 deg about Z through its own origin
blade = cube('drone_blade', (0, 0.35, 0.2), (0.25, 0.03, 0.02))
blade.rotation_euler = (0, 0, 0)
blade.keyframe_insert(data_path='rotation_euler', frame=1)
blade.rotation_euler = (0, 0, math.radians(720))
blade.keyframe_insert(data_path='rotation_euler', frame=45)

mat = bpy.data.materials.new('dronemat')
mat.use_nodes = True
bsdf = mat.node_tree.nodes['Principled BSDF']
tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
import numpy as np
img = bpy.data.images.new('dronediff', width=256, height=256)
img.pixels = np.random.default_rng(7).random(
    (256, 256, 4), dtype=np.float32).flatten().tolist()
tex.image = img
mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
body.data.materials.append(mat)
antenna.data.materials.append(mat)
arm.data.materials.append(mat)
blade.data.materials.append(mat)
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
print('WROTE', out)
"""

# Two clips on ONE object ('swing' translate-Y, 'spin' 720deg Z) exported as
# two glTF animations -> two muted NLA strips + one active on import.
# Exercises motion_config.keep_actions clip selection.
KACT_GEN_SCRIPT = r"""
import bpy, sys, math
out = sys.argv[sys.argv.index('--') + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0, 0, 0.15))
k = bpy.context.active_object
k.name = 'keeper'
ad = k.animation_data_create()
act = bpy.data.actions.new('swing')
ad.action = act
k.location.y = 0.0
k.keyframe_insert(data_path='location', frame=1)
k.location.y = 0.4
k.keyframe_insert(data_path='location', frame=45)
k.location.y = 0.0
tr = ad.nla_tracks.new()
tr.name = 'swing'
tr.strips.new('swing', 1, act)
ad.action = None
act2 = bpy.data.actions.new('spin')
ad.action = act2
k.rotation_euler = (0, 0, 0)
k.keyframe_insert(data_path='rotation_euler', frame=1)
k.rotation_euler = (0, 0, math.radians(720))
k.keyframe_insert(data_path='rotation_euler', frame=45)
k.rotation_euler = (0, 0, 0)
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(1.0, 0, 0.25))
bpy.context.active_object.name = 'keeper_body'
import numpy as np
mat = bpy.data.materials.new('keepermat')
mat.use_nodes = True
bsdf = mat.node_tree.nodes['Principled BSDF']
tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
img = bpy.data.images.new('keepdiff', width=256, height=256)
img.pixels = np.random.default_rng(13).random(
    (256, 256, 4), dtype=np.float32).flatten().tolist()
tex.image = img
mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
for o in bpy.data.objects:
    if o.type == 'MESH':
        o.data.materials.append(mat)
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
print('WROTE', out)
"""

# Rigged fixture: 2-bone armature + cylinder weighted bottom->spine, top->head.
# argv after '--': <out.glb> [noweights]
RIGGED_GEN_SCRIPT = r"""
import bpy, sys
argv = sys.argv[sys.argv.index('--') + 1:]
out = argv[0]
noweights = len(argv) > 1 and argv[1] == 'noweights'
bpy.ops.wm.read_factory_settings(use_empty=True)

bpy.ops.object.armature_add(location=(0, 0, 0))
arm = bpy.context.active_object
arm.name = 'rig'
bpy.ops.object.mode_set(mode='EDIT')
eb = arm.data.edit_bones[0]
eb.name = 'spine'
eb.head = (0, 0, 0)
eb.tail = (0, 0, 0.5)
b2 = arm.data.edit_bones.new('head')
b2.head = (0, 0, 0.5)
b2.tail = (0, 0, 1.0)
b2.parent = eb
bpy.ops.object.mode_set(mode='OBJECT')

bpy.ops.mesh.primitive_cylinder_add(radius=0.15, depth=1.0,
                                    location=(0, 0, 0.5))
m = bpy.context.active_object
m.name = 'torso'
if not noweights:
    vg1 = m.vertex_groups.new(name='spine')
    vg2 = m.vertex_groups.new(name='head')
    for v in m.data.vertices:
        (vg1 if v.co.z < 0 else vg2).add([v.index], 1.0, 'REPLACE')
mod = m.modifiers.new('arm', 'ARMATURE')
mod.object = arm
m.parent = arm
if noweights:
    # glTF drops unskinned armatures entirely; a tiny weighted helper keeps
    # the skin (and so the armature) alive while 'torso' stays unweighted
    bpy.ops.mesh.primitive_cube_add(size=0.05, location=(0.3, 0, 0.25))
    h = bpy.context.active_object
    h.name = 'weighted_helper'
    hvg = h.vertex_groups.new(name='spine')
    hvg.add(list(range(len(h.data.vertices))), 1.0, 'REPLACE')
    hmod = h.modifiers.new('arm', 'ARMATURE')
    hmod.object = arm
    h.parent = arm
    m.parent = arm

import numpy as np
mat = bpy.data.materials.new('clothmat')
mat.use_nodes = True
bsdf = mat.node_tree.nodes['Principled BSDF']
tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
img = bpy.data.images.new('clothdiff', width=256, height=256)
img.pixels = np.random.default_rng(11).random(
    (256, 256, 4), dtype=np.float32).flatten().tolist()
tex.image = img
mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
m.data.materials.append(mat)
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
print('WROTE', out)
"""


SYNTH_DRONE_GEN_SCRIPT = r"""
import bpy, sys
out = sys.argv[sys.argv.index('--') + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)

bpy.ops.object.armature_add()
arm = bpy.context.active_object
arm.name = "drone_arm"
bpy.ops.object.mode_set(mode='EDIT')
eb = arm.data.edit_bones[0]
eb.name = "panel"; eb.head = (0, 0, 0.5); eb.tail = (0, 0, 1.0)
rb = arm.data.edit_bones.new("rotor")
rb.head = (0.6, 0, 0.8); rb.tail = (0.6, 0, 1.0)
rb2 = arm.data.edit_bones.new("rotor_r")
rb2.head = (-0.6, 0, 0.8); rb2.tail = (-0.6, 0, 1.0)
bpy.ops.object.mode_set(mode='OBJECT')

mat = bpy.data.materials.new('drone_mat')
mat.use_nodes = True

def cube(name, loc, scale=0.1):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=scale,
                                        location=loc)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return o

# hull pitches -0.25 rad at f12 while panels pitch +0.25 rad -> the
# counter-tilt fit should recover k_pitch ~ -2.0 (0.5 relative / -0.25 hull)
hull = cube("hull", (0, 0, 0.4), 0.4)
pa = cube("panel_a", (-0.5, 0, 0.7), 0.12)
pb = cube("panel_b", (-0.5, 0.15, 0.7), 0.1)   # co-moves with panel_a
rot = cube("rotor_blade", (0.6, 0, 0.9), 0.05)
rot.scale.x = 6
rot2 = cube("rotor_blade_r", (-0.6, 0, 0.9), 0.05)
rot2.scale.x = 6

for o, bn in ((pa, "panel"), (pb, "panel"), (rot, "rotor"),
              (rot2, "rotor_r")):
    o.parent = arm
    o.parent_type = 'BONE'
    o.parent_bone = bn

arm.animation_data_create()
act = bpy.data.actions.new("fly")
arm.animation_data.action = act
scn = bpy.context.scene
scn.render.fps = 24
scn.frame_end = 24
pb1 = arm.pose.bones["panel"]
pb2 = arm.pose.bones["rotor"]
pb3 = arm.pose.bones["rotor_r"]
for f, (hrx, prx, rz) in ((1, (0, 0, 0)), (12, (-0.25, 0.25, 3.14)),
                         (24, (0, 0, 6.28))):
    scn.frame_set(f)
    hull.rotation_mode = 'XYZ'
    hull.rotation_euler = (hrx, 0, 0)
    hull.keyframe_insert("rotation_euler")
    pb1.rotation_mode = 'XYZ'
    pb1.rotation_euler = (prx, 0, 0)
    pb1.keyframe_insert("rotation_euler")
    pb2.rotation_mode = 'XYZ'
    pb2.rotation_euler = (0, 0, rz)
    pb2.keyframe_insert("rotation_euler")
    pb3.rotation_mode = 'XYZ'
    pb3.rotation_euler = (0, 0, -rz)
    pb3.keyframe_insert("rotation_euler")
scn.frame_set(1)
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB',
                          export_animations=True)
print('WROTE', out)
"""


def run(cmd, **kw):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    blender = find_blender()
    print("blender:", blender)
    import shutil as _sh

    CLEAN = ("sf_test_cube", "sf_drone_multi", "sf_drone_snap",
             "sf_collide", "sf_tiny", "sf_drone_ytd", "sf_rigped",
             "sf_enhanced", "sf_genboth", "sf_genboth_g9",
             "sf_drone_grp", "sf_drone_sspin", "sf_drone_spbad",
             "sf_kspin", "sf_kswing", "sf_knomatch", "sf_drone_excl")
    for name in CLEAN:
        _sh.rmtree(os.path.join(OUT_RES, name), ignore_errors=True)
    if os.path.isfile(REGISTRY):
        with open(REGISTRY) as f:
            reg = json.load(f)
        for name in CLEAN:
            reg.pop(name, None)
        with open(REGISTRY, "w") as f:
            json.dump(reg, f, indent=2)

    failures = []
    tmp = tempfile.mkdtemp(prefix="sfp_test_")

    # ---------------- fixture generation ---------------------------------
    glb = os.path.join(tmp, "test_cube.glb")
    gen = os.path.join(tmp, "gen_test_asset.py")
    with open(gen, "w") as fh:
        fh.write(GEN_SCRIPT)
    r = run([blender, "--background", "--factory-startup", "--python", gen,
             "--", glb])
    assert os.path.isfile(glb) and os.path.getsize(glb) > 100, \
        f"GLB gen failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"

    rigged_glb = os.path.join(tmp, "rigged.glb")
    rig_gen = os.path.join(tmp, "gen_rigged.py")
    with open(rig_gen, "w") as fh:
        fh.write(RIGGED_GEN_SCRIPT)
    r = run([blender, "--background", "--factory-startup", "--python",
             rig_gen, "--", rigged_glb])
    assert os.path.isfile(rigged_glb), f"rigged gen failed:\n{r.stdout[-1500:]}"

    rigged_nw = os.path.join(tmp, "rigged_nw.glb")
    r = run([blender, "--background", "--factory-startup", "--python",
             rig_gen, "--", rigged_nw, "noweights"])
    assert os.path.isfile(rigged_nw), f"rigged_nw gen failed:\n{r.stdout[-1500:]}"
    print("fixtures generated")

    # ---------------- helpers ---------------------------------------------
    def run_job(src, sidecar, dirname="job"):
        """Copy src + override.json into tmp/dirname, run orchestrator,
        return (res_dir_name_or_None, quarantine_reason_or_None)."""
        d = os.path.join(tmp, dirname)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            _sh.copy(src, dst)
        if sidecar is not None:
            with open(os.path.join(d, "override.json"), "w") as fh:
                if isinstance(sidecar, str):
                    fh.write(sidecar)      # raw (malformed) content
                else:
                    json.dump(sidecar, fh)
        r = run([sys.executable, os.path.join(ROOT, "orchestrator.py"),
                 "--file", dst])
        tail = r.stdout[-1200:]
        name = ("sf_" + (sidecar.get("asset_name") or
                         os.path.splitext(os.path.basename(src))[0])
                if isinstance(sidecar, dict) else
                "sf_" + os.path.splitext(os.path.basename(src))[0])
        res_dir = os.path.join(OUT_RES, name)
        qs = sorted(q for q in os.listdir(QUAR) if q.startswith(name + "_"))
        reason = None
        if qs:
            try:
                with open(os.path.join(QUAR, qs[-1], "failure_report.json")) \
                        as f:
                    reason = json.load(f).get("reason")
            except Exception:
                reason = "?"
        return (res_dir if os.path.isdir(res_dir) else None), \
            (reason or (None if os.path.isdir(res_dir)
                        else f"unpublished+unquarantined:{tail[-200:]}"))

    def expect_quar(tag, src, sidecar, want_reason, dirname):
        res, reason = run_job(src, sidecar, dirname)
        base = want_reason.split(":")[0]
        # quarantine verdict wins over a pre-existing resource dir: gate4
        # intentionally leaves the earlier same-name publish in place
        if reason and reason.startswith(base):
            print(f"{tag} OK: {reason}")
        elif res is not None:
            failures.append(f"{tag}: published but expected {want_reason}")
        else:
            failures.append(f"{tag}: expected {want_reason}, got {reason}")

    def rsc_version(path):
        with open(path, "rb") as f:
            hdr = f.read(8)
        return int.from_bytes(hdr[4:8], "little") if hdr[:4] == b"RSC7" else None

    caps = {}
    if os.path.isfile(CAPS_FILE):
        try:
            with open(CAPS_FILE) as f:
                caps = json.load(f)
        except Exception:
            caps = {}
    rscv = caps.get("rsc_versions") or {}

    # ============ HAPPY PATHS ==============================================
    # ---- base prop conversion --------------------------------------------
    res_dir, reason = run_job(glb, {}, "base")
    if not res_dir:
        failures.append(f"base cube not published: {reason}")
    else:
        ydr = os.path.join(res_dir, "stream", "sf_test_cube.ydr")
        ytyp = os.path.join(res_dir, "stream", "sf_test_cube.ytyp")
        ytyp_xml = ytyp + ".xml"
        manifest = os.path.join(res_dir, "fxmanifest.lua")
        report = os.path.join(res_dir, "build_report.json")
        if not os.path.isfile(ydr):
            failures.append("ydr missing")
        else:
            if rsc_version(ydr) is None:
                failures.append("ydr not RSC7")
            if os.path.getsize(ydr) <= 5000:
                failures.append(f"ydr suspiciously small: {os.path.getsize(ydr)}B")
            ydr_xml = ydr + ".xml"
            if os.path.isfile(ydr_xml):
                import xml.etree.ElementTree as ET
                if not any("textur" in el.tag.lower()
                           for el in ET.parse(ydr_xml).iter()):
                    failures.append("ydr.xml declares no texture elements")
        if not os.path.isfile(ytyp) or rsc_version(ytyp) is None:
            failures.append("ytyp missing/not RSC7")
        if not os.path.isfile(ytyp_xml):
            failures.append("ytyp.xml sidecar missing")
        else:
            with open(ytyp_xml, encoding="utf-8", errors="replace") as f:
                if "<name>sf_test_cube</name>" not in f.read():
                    failures.append("ytyp.xml missing archetype name")
        if not os.path.isfile(manifest):
            failures.append("fxmanifest missing")
        else:
            with open(manifest, encoding="utf-8") as f:
                mtxt = f.read()
            if "lua54" in mtxt:
                failures.append("fxmanifest emits deprecated lua54")
        if not os.path.isfile(report):
            failures.append("build_report missing")
        else:
            with open(report) as f:
                rep = json.load(f)
            if rep.get("status") != "SUCCESS":
                failures.append(f"build_report status: {rep.get('status')}")
            dims = rep["dimensions_meters"]
            if not all(0.02 <= d <= 80.0 for d in dims):
                failures.append(f"dims out of range: {dims}")
            if rep.get("runtime_validation") != "NOT_PERFORMED":
                failures.append("runtime_validation honesty field wrong")
            if not (rep.get("toolchain") or {}).get("sollumz_version"):
                failures.append("build_report missing toolchain.sollumz_version")
            insp = rep.get("inspect") or {}
            if insp.get("mesh_count", 0) < 1 or "animation" not in insp:
                failures.append("build_report inspect block incomplete")
            print("dims:", dims, "tris:", rep["final_triangles"],
                  "warnings:", rep["warnings"])
        with open(REGISTRY) as f:
            reg = json.load(f)
        if "sf_test_cube" not in reg:
            failures.append("registry entry missing")

    # ---- multi_part drone --------------------------------------------------
    drone_glb = os.path.join(tmp, "drone_src", "drone.glb")
    os.makedirs(os.path.dirname(drone_glb), exist_ok=True)
    drone_gen = os.path.join(tmp, "gen_drone.py")
    with open(drone_gen, "w") as fh:
        fh.write(DRONE_GEN_SCRIPT)
    r = run([blender, "--background", "--factory-startup", "--python",
             drone_gen, "--", drone_glb])
    assert os.path.isfile(drone_glb), f"drone gen failed:\n{r.stdout[-1500:]}"

    res, reason = run_job(drone_glb, {
        "asset_name": "drone_multi",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "assembly_config": {"transition_ms": 1200,
                                "ai_carrier_archetype": "frogger"}},
        "license": "CC-BY-4.0",
        "source_url": "https://sketchfab.com/test/drone",
        "attribution": "test_drone by harness"}, "mp")
    if not res:
        failures.append(f"multi_part not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        for expect in ("sf_drone_multi_body.ydr",
                       "sf_drone_multi_part01.ydr",
                       "sf_drone_multi_prop01.ydr"):
            p = os.path.join(sdir, expect)
            if not os.path.isfile(p):
                failures.append(f"multi_part missing {expect}")
            elif rsc_version(p) is None:
                failures.append(f"{expect} bad magic")
        aman = os.path.join(res, "assembly_manifest.lua")
        if not os.path.isfile(aman):
            failures.append("assembly_manifest.lua missing")
        else:
            with open(aman, encoding="utf-8") as f:
                am = f.read()
            if "deployed_rot" not in am or "attach_offset" not in am:
                failures.append("assembly manifest missing rot/offset fields")
            if "deployed_offset" not in am:
                failures.append("assembly manifest missing deployed_offset")
            if "manifest_only_requires_consumer" not in am:
                failures.append("assembly manifest missing runtime_status")
            import re as _re
            rots = _re.findall(r"deployed_rot.*?\{([^}]*)\}", am)
            if len(rots) != 1:
                failures.append(
                    f"expected exactly 1 hinge deployed_rot, got {len(rots)}")
            for grp in rots:
                vals = [float(x) for x in grp.split(",")]
                if len(vals) != 3:
                    failures.append(f"deployed_rot not 3 floats: {grp}")
                elif abs(vals[2] - 90.0) > 1.0:
                    failures.append(f"deployed_rot z ~90 expected: {vals}")
            # G2: rotor -> kind=spin, Z axis, ~65rpm (720deg/1.83s), no hinge
            if am.count('"kind"] = "spin"') != 1:
                failures.append("expected exactly 1 kind=spin part")
            if am.count('"kind"] = "hinge"') != 1:
                failures.append("expected exactly 1 kind=hinge part")
            ax = _re.search(r"spin_axis.*?\{([^}]*)\}", am)
            if not ax:
                failures.append("spin_axis missing")
            else:
                axyz = [float(x) for x in ax.group(1).split(",")]
                if abs(axyz[2]) < 0.99 or abs(axyz[0]) > 0.05:
                    failures.append(f"spin_axis not ~Z: {axyz}")
            rpm = _re.search(r"spin_rpm.*?= ([0-9.]+)", am)
            if not rpm or not (30.0 < float(rpm.group(1)) < 120.0):
                failures.append(f"spin_rpm implausible: "
                                f"{rpm.group(1) if rpm else 'missing'}")
        if os.path.isfile(os.path.join(sdir, "sf_drone_multi_part02.ydr")):
            failures.append("static antenna wrongly became part02")
        with open(os.path.join(res, "build_report.json")) as f:
            mp_rep = json.load(f)
        if mp_rep.get("license") != "CC-BY-4.0" or \
                "sketchfab.com" not in str(mp_rep.get("source_url")):
            failures.append("license/source_url passthrough missing")

    # ---- multi_part: grouped part_map + dict spin config -------------------
    res, reason = run_job(drone_glb, {
        "asset_name": "drone_grp",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "part_map": {
                "cluster": ["drone_arm", "drone_antenna"],
                "prop": {"meshes": ["drone_blade"], "kind": "spin",
                         "spin_axis": "auto", "spin_rpm": "auto"}}}}, "mpgrp")
    if not res:
        failures.append(f"mp group/dict not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        for expect in ("sf_drone_grp_cluster.ydr", "sf_drone_grp_prop.ydr"):
            if not os.path.isfile(os.path.join(sdir, expect)):
                failures.append(f"group/dict part_map missing {expect}")
        am = open(os.path.join(res, "assembly_manifest.lua")).read()
        if "drone_antenna" not in am or "drone_arm" not in am:
            failures.append("group part source_objects missing")

    # ---- multi_part: explicit static spin part ------------------------------
    res, reason = run_job(drone_glb, {
        "asset_name": "drone_sspin",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "part_map": {
                "prop": {"meshes": ["drone_antenna"], "kind": "spin",
                         "spin_axis": [0, 0, 1], "spin_rpm": 600}}}}, "mpspin")
    if not res:
        failures.append(f"static spin not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        if not os.path.isfile(os.path.join(sdir, "sf_drone_sspin_prop.ydr")):
            failures.append("static spin: sf_drone_sspin_prop.ydr missing")
        am = open(os.path.join(res, "assembly_manifest.lua")).read()
        if "spin_rpm\"] = 600" not in am:
            failures.append("static spin: configured rpm not in manifest")

    # ---- multi_part: static spin without axis -> unresolved ----------------
    res, reason = run_job(drone_glb, {
        "asset_name": "drone_spbad",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "part_map": {
                "prop": {"meshes": ["drone_antenna"], "kind": "spin",
                         "spin_rpm": 300}}}}, "mpsbad")
    if not reason or not reason.startswith("ERR_SPIN_AXIS_UNRESOLVED"):
        failures.append(f"expected ERR_SPIN_AXIS_UNRESOLVED, got {reason}")

    # ---- keep_actions clip selection ---------------------------------------
    keep_glb = os.path.join(tmp, "keep_src", "keep.glb")
    os.makedirs(os.path.dirname(keep_glb), exist_ok=True)
    keep_gen = os.path.join(tmp, "gen_keep.py")
    with open(keep_gen, "w") as fh:
        fh.write(KACT_GEN_SCRIPT)
    r = run([blender, "--background", "--factory-startup", "--python",
             keep_gen, "--", keep_glb])
    assert os.path.isfile(keep_glb), f"keep gen failed:\n{r.stdout[-1500:]}"

    res, reason = run_job(keep_glb, {
        "asset_name": "kspin",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "keep_actions": ["spin"]}}, "kspin")
    if not res:
        failures.append(f"keep_actions spin not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        if not os.path.isfile(os.path.join(sdir, "sf_kspin_prop01.ydr")):
            failures.append("keep spin: sf_kspin_prop01.ydr missing")
        am = open(os.path.join(res, "assembly_manifest.lua")).read()
        if '"kind"] = "spin"' not in am:
            failures.append("keep spin: no spin part in manifest")

    res, reason = run_job(keep_glb, {
        "asset_name": "kswing",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "keep_actions": ["swing"]}}, "kswing")
    if not res:
        failures.append(f"keep_actions swing not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        if not os.path.isfile(os.path.join(sdir, "sf_kswing_part01.ydr")):
            failures.append("keep swing: sf_kswing_part01.ydr missing")
        am = open(os.path.join(res, "assembly_manifest.lua")).read()
        if '"kind"] = "spin"' in am:
            failures.append("keep swing: spin part leaked in")

    res, reason = run_job(keep_glb, {
        "asset_name": "knomatch",
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "keep_actions": ["zzz_no_such_clip"]}}, "kno")
    if not reason or not reason.startswith("ERR_KEEP_ACTIONS_NO_MATCH"):
        failures.append(f"expected ERR_KEEP_ACTIONS_NO_MATCH, got {reason}")

    # ---- exclude_objects ----------------------------------------------------
    res, reason = run_job(drone_glb, {
        "asset_name": "drone_excl",
        "exclude_objects": ["drone_antenna", "drone_blade"],
        "motion_config": {
            "motion_mode": "multi_part_prop_assembly",
            "frame_window": {"start_frame": 1, "end_frame": 45}}}, "mpexcl")
    if not res:
        failures.append(f"exclude_objects not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        if os.path.isfile(os.path.join(sdir, "sf_drone_excl_prop01.ydr")):
            failures.append("excluded blade still became a prop part")

    # ---- rig_recipe: preview -> co-moving merge + multi-instance naming ----
    # synth drone: hull + 2 co-moving panels (thruster_tilt) + 2 rotors on
    # separate bones; hull -0.25rad vs panels +0.25rad -> fitted k_pitch -2.0
    synth_glb = os.path.join(tmp, "synth_src", "synth_drone.glb")
    os.makedirs(os.path.dirname(synth_glb), exist_ok=True)
    synth_gen = os.path.join(tmp, "gen_synth.py")
    with open(synth_gen, "w") as fh:
        fh.write(SYNTH_DRONE_GEN_SCRIPT)
    r = run([blender, "--background", "--factory-startup", "--python",
             synth_gen, "--", synth_glb])
    assert os.path.isfile(synth_glb), f"synth gen failed:\n{r.stdout[-1500:]}"

    prev_json = os.path.join(tmp, "synth_preview.json")
    r = run([blender, "--background", "--factory-startup", "--python",
             os.path.join(ROOT, "blender_worker.py"), "--",
             "--dump-preview", synth_glb, prev_json, "--asset-type", "prop"])
    if not os.path.isfile(prev_json):
        failures.append(f"dump-preview failed:\n{r.stdout[-800:]}")
    else:
        with open(prev_json) as f:
            prev = json.load(f)
        sa = prev.get("source_anim")
        if not sa or not sa.get("part_tracks"):
            failures.append("synth preview missing source_anim part_tracks")
        else:
            # non-zero motion sampling: at least one track must deviate from
            # its first sample (guards the matrix_world.copy() regression
            # where every sampled matrix aliased the live depsgraph buffer)
            moved = False
            for samp in sa["part_tracks"].values():
                if len(samp) > 1 and any(
                        max(abs(a - b) for a, b in zip(s, samp[0])) > 1e-4
                        for s in samp[1:]):
                    moved = True
                    break
            if not moved:
                failures.append("source_anim tracks all flat (matrix copy?)")

        rolemap = {"hull": "hull", "panel_a": "thruster_tilt",
                   "panel_b": "thruster_tilt", "rotor_blade": "rotor",
                   "rotor_blade_r": "rotor"}
        recipe = {"parts": [{"id": p["id"], "name": p["name"],
                             "role": rolemap.get(p["name"], "hull"),
                             "bone_lock": "", "pivot": p["pivot"],
                             **({"rest_rot": [0, 0, 90]}
                                if p["name"] == "rotor_blade" else {})}
                            for p in prev["parts"]],
                  "preview_transform": prev["preview_transform"],
                  "user_transform": {"rot_z_deg": 0.0, "scale": 1.0,
                                     "offset": [0, 0, 0]},
                  "anim_capture": {"mode": "drone_reactive_flight",
                                   "clip_name": "synth_flight",
                                   "rest_sec": 0.0, "in_sec": 0.0,
                                   "out_sec": 0.958, "sample_hz": 12}}
        res, reason = run_job(synth_glb, {
            "asset_name": "synthrig",
            "license": "CC0-1.0",
            "motion_config": {
                "motion_mode": "multi_part_prop_assembly"},
            "rig_recipe": recipe}, "synthrig")
        if not res:
            failures.append(f"rig_recipe compile not published: {reason}")
        else:
            sdir = os.path.join(res, "stream")
            ydrs = {f for f in os.listdir(sdir) if f.endswith(".ydr")}
            want = {"sf_synthrig_body.ydr", "sf_synthrig_thruster_tilt.ydr",
                    "sf_synthrig_rotor_0.ydr", "sf_synthrig_rotor_1.ydr"}
            missing = want - ydrs
            if missing:
                failures.append(f"rig_recipe missing ydrs {missing}; "
                                f"have {sorted(ydrs)}")
            # no bare-role collision names may exist alongside indexed ones
            for bad in ("sf_synthrig_rotor.ydr",
                        "sf_synthrig_rotor_01.ydr"):
                if bad in ydrs:
                    failures.append(f"collision-prone name emitted: {bad}")
            cfj = os.path.join(res, "client_flight.lua.json")
            if not os.path.isfile(cfj):
                failures.append("client_flight.lua.json missing")
            else:
                with open(cfj) as f:
                    cf = json.load(f)
                gains = cf.get("gains") or {}
                tt = gains.get("thruster_tilt") or {}
                if not tt.get("fitted"):
                    failures.append("thruster_tilt gains not fitted")
                elif abs(tt.get("k_pitch", 0) + 2.0) > 0.15:
                    failures.append(f"k_pitch ~ -2.0 expected, got "
                                    f"{tt.get('k_pitch')}")
                thr = cf.get("thrusters") or []
                models = {t.get("model") for t in thr}
                if len(thr) != 3 or len(models) != 3:
                    failures.append(f"expected 3 distinct thruster models, "
                                    f"got {sorted(models)}")
                pivots = {tuple(t.get("pivot") or ()) for t in thr}
                if len(pivots) != len(thr):
                    failures.append("thruster pivots not distinct per cluster")
            # rest_rot [0,0,90] on rotor_blade must be baked into the
            # exported verts: rotor_0's X/Y extents swap vs unrotated rotor_1
            def _ydr_extent(name):
                import xml.etree.ElementTree as ET
                root = ET.parse(os.path.join(
                    sdir, name + ".ydr.xml")).getroot()
                mn = mx = None
                for el in root.iter():
                    if el.tag == "BoundingBoxMin":
                        mn = [float(el.get(k)) for k in ("x", "y", "z")]
                    elif el.tag == "BoundingBoxMax":
                        mx = [float(el.get(k)) for k in ("x", "y", "z")]
                if mn is None or mx is None:
                    return None
                return [b - a for a, b in zip(mn, mx)]
            e0 = _ydr_extent("sf_synthrig_rotor_0")
            e1 = _ydr_extent("sf_synthrig_rotor_1")
            if e0 is None or e1 is None:
                failures.append("rotor ydr.xml bounds missing")
            elif not (e0[1] > 2.0 * e0[0] and e1[0] > 2.0 * e1[1]):
                failures.append(f"rest_rot not baked: rotor_0 ext {e0} "
                                f"rotor_1 ext {e1}")
            with open(os.path.join(res, "build_report.json")) as f:
                srep = json.load(f)
            kc = srep.get("kinematic_chain") or {}
            kparts = [t.get("part") for t in kc.get("thrusters", [])]
            if "rotor_0" not in kparts or "rotor_1" not in kparts:
                failures.append(f"kinematic_chain missing rotor_0/1: {kparts}")

    # ---- texture_mode='ytd' ------------------------------------------------
    res, reason = run_job(drone_glb, {"asset_name": "drone_ytd",
                                      "texture_mode": "ytd"}, "ytd")
    if not res:
        failures.append(f"ytd not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        ytds = [f for f in os.listdir(sdir) if f.endswith(".ytd")]
        if not ytds:
            failures.append("ytd mode: no .ytd emitted")
        else:
            p = os.path.join(sdir, ytds[0])
            if rsc_version(p) is None:
                failures.append(f"{ytds[0]} bad magic")
            if os.path.getsize(p) <= 1024:
                failures.append("ytd suspiciously small")

    # ---- snapshot_sequence -------------------------------------------------
    res, reason = run_job(drone_glb, {
        "asset_name": "drone_snap",
        "motion_config": {
            "motion_mode": "snapshot_sequence",
            "frame_window": {"start_frame": 1, "end_frame": 45},
            "snapshot_sequence": {"samples": [0.0, 0.5, 1.0],
                                  "sample_type": "ratio",
                                  "intermediate_hold_ms": 100}}}, "snap")
    if not res:
        failures.append(f"snapshot not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        for i in range(3):
            p = os.path.join(sdir, f"sf_drone_snap_snap{i:02d}.ydr")
            if not os.path.isfile(p):
                failures.append(f"snapshot missing snap{i:02d}")
            elif rsc_version(p) is None:
                failures.append(f"snap{i:02d} bad magic")
        sman = os.path.join(res, "sequence_manifest.lua")
        if not os.path.isfile(sman):
            failures.append("sequence_manifest.lua missing")
        else:
            with open(sman, encoding="utf-8") as f:
                sm = f.read()
            if sm.count("_snap") < 3 or "hold_ms" not in sm:
                failures.append("sequence manifest malformed")
            if "manifest_only_requires_consumer" not in sm:
                failures.append("sequence manifest missing runtime_status")

    # ---- ped_clothing happy path -------------------------------------------
    res, reason = run_job(rigged_glb, {
        "asset_name": "rigped",
        "asset_type": "ped",
        "collision_mode": "none",
        "clothing_fix": {"normal_push_meters": 0.002}}, "ped")
    if not res:
        failures.append(f"ped_clothing not published: {reason}")
    else:
        sdir = os.path.join(res, "stream")
        ydd = os.path.join(sdir, "sf_rigped.ydd")
        if not os.path.isfile(ydd):
            failures.append("ped: .ydd missing")
        elif rsc_version(ydd) is None:
            failures.append("ped: .ydd not RSC7")
        if os.path.isfile(os.path.join(sdir, "sf_rigped.ydr")):
            failures.append("ped: wrongly emitted a prop .ydr")
        if os.path.isfile(os.path.join(sdir, "sf_rigped.ytyp")):
            failures.append("ped: wrongly emitted a .ytyp")
        ydd_xml = ydd + ".xml"
        if not os.path.isfile(ydd_xml):
            failures.append("ped: .ydd.xml sidecar missing")
        else:
            import xml.etree.ElementTree as ET
            root = ET.parse(ydd_xml).getroot()
            if "drawabledictionary" not in root.tag.lower():
                failures.append("ped: .ydd.xml not a drawable dictionary")
            if not any("bone" in el.tag.lower() for el in root.iter()):
                failures.append("ped: .ydd.xml has no skeleton/bone elements")
        with open(os.path.join(res, "fxmanifest.lua"), encoding="utf-8") as f:
            mtxt = f.read()
        if "DLC_ITYP_REQUEST" in mtxt:
            failures.append("ped manifest wrongly requests .ytyp")
        with open(os.path.join(res, "build_report.json")) as f:
            rep = json.load(f)
        if rep.get("asset_type") != "ped":
            failures.append("ped: build_report asset_type != ped")
        if rep.get("runtime_status") != "downstream_consumer_required":
            failures.append("ped: runtime_status honesty field wrong")

    # ---- enhanced generation ----------------------------------------------
    res, reason = run_job(glb, {"asset_name": "enhanced",
                                "target_generation": "enhanced"}, "gen9")
    if not res:
        failures.append(f"enhanced not published: {reason}")
    else:
        ydr = os.path.join(res, "stream", "sf_enhanced.ydr")
        if not os.path.isfile(ydr):
            failures.append("enhanced: ydr missing")
        else:
            want = (rscv.get("GEN9") or {}).get(".ydr")
            got = rsc_version(ydr)
            if want is not None and got != want:
                failures.append(f"enhanced ydr rsc_version {got} != GEN9 {want}")
            elif got is None:
                failures.append("enhanced ydr not RSC7")
            else:
                print(f"enhanced ydr rsc_version={got} (expected {want})")

    # ---- both generations ---------------------------------------------------
    res, reason = run_job(glb, {"asset_name": "genboth",
                                "target_generation": "both"}, "genboth")
    g9 = os.path.join(OUT_RES, "sf_genboth_g9")
    if not res or not os.path.isdir(g9):
        failures.append(f"both-gen: expected 2 resources, "
                        f"res={res} reason={reason}")
    else:
        for path, gen_name in ((os.path.join(res, "stream", "sf_genboth.ydr"),
                                "GEN8"),
                               (os.path.join(g9, "stream", "sf_genboth.ydr"),
                                "GEN9")):
            if not os.path.isfile(path):
                failures.append(f"both-gen missing {gen_name} ydr at {path}")
            else:
                want = (rscv.get(gen_name) or {}).get(".ydr")
                got = rsc_version(path)
                if want is not None and got != want:
                    failures.append(
                        f"both-gen {gen_name} ydr version {got}!={want}")
        print("both-gen resources OK")

    # ============ QUARANTINE PATHS ===========================================
    # zip with no 3D content
    junk = os.path.join(tmp, "junkpack.zip")
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("readme.txt", "no models here")
    expect_quar("junkpack", junk, None, "ERR_NO_SUPPORTED_3D_FILE", "jq")

    # zip traversal
    trav = os.path.join(tmp, "trav.zip")
    with zipfile.ZipFile(trav, "w") as zf:
        zf.writestr("../evil.glb", "x")
        zf.writestr("ok.glb", b"glTF" + b"\x00" * 32)
    expect_quar("zip_traversal", trav, None, "ERR_ZIP_TRAVERSAL", "zt")

    # zip nested archive
    nest = os.path.join(tmp, "nest.zip")
    with zipfile.ZipFile(nest, "w") as zf:
        zf.writestr("inner.zip", "x")
        zf.writestr("model.glb", b"glTF" + b"\x00" * 32)
    expect_quar("zip_nested", nest, None, "ERR_ZIP_NESTED_ARCHIVE", "zn")

    # malformed sidecar (unique filename so the prefix can't hit sf_test_cube)
    sides = os.path.join(tmp, "sides_src", "sides.glb")
    os.makedirs(os.path.dirname(sides), exist_ok=True)
    _sh.copy(glb, sides)
    expect_quar("bad_sidecar", sides, "{not json",
                "ERR_MALFORMED_SIDECAR", "ms")

    # malformed source (extension lies about content)
    fake = os.path.join(tmp, "fake.glb")
    with open(fake, "w") as fh:
        fh.write("this is not a glb")
    expect_quar("bad_source", fake, None, "ERR_MALFORMED_SOURCE", "mf")

    # Gate 3 dims
    expect_quar("gate3", glb,
                {"asset_name": "tiny", "target_height_meters": 0.001},
                "ERR_GATE3_DIMS_OUT_OF_RANGE", "tiny")

    # Gate 4: same name, different source bytes
    run_job(glb, {"asset_name": "collide"}, "c1")
    expect_quar("gate4", drone_glb, {"asset_name": "collide"},
                "ERR_GATE4", "c2")

    # motion: too many samples (cap is 12)
    expect_quar("samples13", drone_glb, {
        "asset_name": "snapmany",
        "motion_config": {"motion_mode": "snapshot_sequence",
                          "snapshot_sequence": {
                              "samples": [i / 13 for i in range(14)]}}},
        "ERR_TOO_MANY_SAMPLES", "samp13")

    # motion: quarantined vehicle mode
    expect_quar("vehmode", glb, {
        "asset_name": "veh",
        "motion_config": {"motion_mode": "door_control"}},
        "ERR_MOTION_MODE_REQUIRES_VEHICLE_RIG", "veh")

    # unknown motion mode
    expect_quar("badmode", glb, {
        "asset_name": "badm",
        "motion_config": {"motion_mode": "helicopter"}},
        "ERR_UNKNOWN_MOTION_MODE", "badm")

    # unknown texture mode
    expect_quar("badtex", glb, {"asset_name": "badtex",
                                "texture_mode": "ktx"},
        "ERR_UNKNOWN_TEXTURE_MODE", "badtex")

    # ---- ped quarantine paths ------------------------------------------------
    expect_quar("ped_noarm", glb,
                {"asset_name": "pednoarm", "asset_type": "ped",
                 "collision_mode": "none"},
                "ERR_PED_NO_ARMATURE", "pna")

    expect_quar("ped_noweights", rigged_nw,
                {"asset_name": "pednw", "asset_type": "ped",
                 "collision_mode": "none"},
                "ERR_PED_NO_WEIGHTS", "pnw")

    expect_quar("ped_collision", rigged_glb,
                {"asset_name": "pedcol", "asset_type": "ped"},
                "ERR_COLLISION_MODE_UNSUPPORTED", "pcol")

    expect_quar("ped_cloth", rigged_glb,
                {"asset_name": "pedcloth", "asset_type": "ped",
                 "collision_mode": "none",
                 "clothing_fix": {"cloth": True}},
                "ERR_PED_CLOTH_PHYSICS_UNSUPPORTED", "pcl")

    expect_quar("ped_conflict", rigged_glb,
                {"asset_name": "pedconf", "asset_type": "ped",
                 "collision_mode": "none",
                 "motion_config": {"motion_mode": "snapshot_sequence"}},
                "ERR_ASSET_TYPE_CONFLICT", "pcf")

    expect_quar("bad_asset_type", glb,
                {"asset_name": "veh2", "asset_type": "vehicle"},
                "ERR_UNKNOWN_ASSET_TYPE", "bat")

    # apparel: valid config but ymt unsupported on this toolchain
    apparel_cfg = {"ped": "mp_m_freemode_01", "dlc": "sfp_test",
                   "components": [{"component": "jbib", "index": 0,
                                   "textures": 1}]}
    res, reason = run_job(rigged_glb, {
        "asset_name": "apparel", "asset_type": "ped",
        "collision_mode": "none", "apparel_pack": True,
        "apparel_config": apparel_cfg}, "app")
    if (caps.get("capabilities") or {}).get("ymt_export"):
        # toolchain learned ymt: expect a real apparel publish
        if res is None:
            failures.append(f"apparel (ymt-capable) not published: {reason}")
    else:
        if res is not None:
            failures.append("apparel published despite ymt unsupported")
        elif not (reason or "").startswith("ERR_PED_APPAREL_YMT_UNSUPPORTED"):
            failures.append(f"apparel: expected ERR_PED_APPAREL_YMT_UNSUPPORTED, "
                            f"got {reason}")
        else:
            print(f"apparel OK: {reason}")

    expect_quar("apparel_badcfg", rigged_glb,
                {"asset_name": "appbc", "asset_type": "ped",
                 "collision_mode": "none", "apparel_pack": True},
                "ERR_APPAREL_CONFIG_INVALID", "abc")

    expect_quar("apparel_on_prop", glb,
                {"asset_name": "approp", "apparel_pack": True,
                 "apparel_config": apparel_cfg},
                "ERR_ASSET_TYPE_CONFLICT", "aop")

    # generation quarantine paths
    expect_quar("bad_gen", glb,
                {"asset_name": "badgen", "target_generation": "gen10"},
                "ERR_TARGET_GENERATION_UNSUPPORTED", "bg")

    if failures:
        print("\nFAILED:", "; ".join(failures))
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
