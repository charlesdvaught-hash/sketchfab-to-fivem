# SPDX-License-Identifier: AGPL-3.0-only
"""Post-export visual verifier: re-import emitted .ydr.xml drawables via
Sollumz and render diagnostic PNGs (iso/side/top). Composite mode poses a
multi-part assembly per its assembly_manifest.lua.json (folded + deployed).

Usage (invoked by orchestrator; also usable standalone):
  blender --background --factory-startup --python render_check.py -- \
      <resource_dir|ydr.xml> <out_dir>

Prints [MESH] UV/color counts (UVs=0 == the TexCoord0 regression) and writes
<model>_{iso,side,top}.png plus _assembly_{folded,deployed}.png composites.
Exit code is nonzero only on import failure -- findings are advisory.

Sollumz's reimport material graph doesn't display textures under EEVEE
headless, so import_ydr() rebuilds a clean Principled BSDF per imported
material (TEX_IMAGE + UVMap wired from the Sollumz graph) -- preview only,
never touches the export. Textures verified end-to-end post-sRGB fix.
Assembly composites render per-part pastel debug colors + axis markers,
folded/mid/deployed poses.
"""
import bpy
import colorsys
import json
import math
import os
import subprocess
import sys
import zlib
from mathutils import Euler, Vector

TEXCONV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "bin", "texconv.exe")

argv = sys.argv[sys.argv.index("--") + 1:]
SRC = os.path.abspath(argv[0])
OUT_DIR = os.path.abspath(argv[1])
os.makedirs(OUT_DIR, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="Sollumz")


def wipe():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for blocks in (bpy.data.meshes, bpy.data.materials, bpy.data.images):
        for b in list(blocks):
            if b.users == 0:
                blocks.remove(b)


def _generate_pastel_color(part_name):
    """Stable pastel rgba for a part name (crc32 -> hue)."""
    h = (zlib.crc32(part_name.encode("utf-8")) & 0xffffffff) / 0xffffffff
    r, g, b = colorsys.hsv_to_rgb(h, 0.8, 0.75)
    return (r, g, b, 1.0)


def _rebuild_principled_material(material, image=None, has_alpha_link=False,
                                 flat_color_rgba=None, uv_map_name=""):
    """Strip whatever graph Sollumz built and wire a clean
    TEX_IMAGE(diffuse)->Principled->Output, or a flat diagnostic color."""
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    out_node = nodes.new(type='ShaderNodeOutputMaterial')
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    links.new(bsdf.outputs['BSDF'], out_node.inputs['Surface'])
    if image is not None:
        tex = nodes.new(type='ShaderNodeTexImage')
        tex.image = image
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
        if has_alpha_link:
            links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
            material.blend_method = 'HASHED'
        if uv_map_name:
            uvn = nodes.new(type='ShaderNodeUVMap')
            uvn.uv_map = uv_map_name
            links.new(uvn.outputs['UV'], tex.inputs['Vector'])
    elif flat_color_rgba is not None:
        bsdf.inputs['Base Color'].default_value = flat_color_rgba
        bsdf.inputs['Roughness'].default_value = 0.8
        spec = (bsdf.inputs.get('Specular IOR Level')
                or bsdf.inputs.get('Specular'))
        if spec is not None:
            spec.default_value = 0.0


def get_body_root():
    root = bpy.data.objects.get("BodyRoot")
    if root is None:
        root = bpy.data.objects.new("BodyRoot", None)
        bpy.context.scene.collection.objects.link(root)
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)
    return root


def import_ydr(path):
    before_mats = set(bpy.data.materials)
    bpy.ops.sollumz.import_assets(
        directory=os.path.dirname(path) + os.sep,
        files=[{"name": os.path.basename(path)}])
    for o in list(bpy.data.objects):
        if o.name.startswith("Bound"):
            bpy.data.objects.remove(o, do_unlink=True)
    # Blender can't decode DXT5 DDS: convert to PNG via texconv and relink,
    # so TEXTURE shading shows the real texdict instead of black.
    cache = os.path.join(OUT_DIR, "_texcache")
    for img in bpy.data.images:
        fp = bpy.path.abspath(img.filepath)
        if not fp.lower().endswith(".dds") or not os.path.isfile(fp):
            continue
        stem = os.path.splitext(os.path.basename(fp))[0]
        png = os.path.join(cache, stem + ".png")
        if not os.path.isfile(png):
            os.makedirs(cache, exist_ok=True)
            try:
                subprocess.run([TEXCONV, "-ft", "png", "-o", cache, fp],
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
        if os.path.isfile(png):
            new_img = bpy.data.images.load(png, check_existing=True)
            for m in bpy.data.materials:
                if not m.node_tree:
                    continue
                for n in m.node_tree.nodes:
                    if n.type == 'TEX_IMAGE' and n.image is img:
                        n.image = new_img
    # Sollumz's reimport node graph renders dark even with valid textures --
    # rebuild every material as clean TEX_IMAGE(diffuse)->Principled->Output.
    # Diffuse nodes live INSIDE Sollumz NodeGroups; search recursively.
    def find_tex_image(nt):
        for n in nt.nodes:
            if n.type == 'TEX_IMAGE' and n.image:
                return n, nt
            if n.type == 'GROUP' and n.node_tree:
                r = find_tex_image(n.node_tree)
                if r[0] is not None:
                    return r
        return None, None

    mat_uv = {}
    for o in bpy.data.objects:
        if o.type != 'MESH' or not o.data.uv_layers:
            continue
        uvl = o.data.uv_layers
        uvname = (uvl.active.name if uvl.active else uvl[0].name)
        for slot in o.material_slots:
            if slot.material and slot.material not in mat_uv:
                mat_uv[slot.material] = uvname
    for m in bpy.data.materials:
        if m in before_mats or not m.node_tree:
            continue
        tn, tnt = find_tex_image(m.node_tree)
        tex_img = tn.image if tn else None
        if tex_img:
            try:
                tex_img.colorspace_settings.name = 'sRGB'
            except Exception:
                pass
        has_alpha = bool(tnt and any(
            lk.from_node is tn and lk.from_socket.name == 'Alpha'
            for lk in tnt.links))
        _rebuild_principled_material(m, image=tex_img,
                                     has_alpha_link=has_alpha,
                                     uv_map_name=mat_uv.get(m, ""))


def mesh_report():
    for o in bpy.data.objects:
        if o.type == 'MESH' and not o.name.startswith("Bound"):
            print(f"[MESH] {o.name}: UVs={len(o.data.uv_layers)} "
                  f"Colors={len(o.data.color_attributes)}")


def add_axis_markers():
    def marker(name, rot, color):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.015, depth=0.6)
        o = bpy.context.active_object
        o.name = name
        o.rotation_euler = rot
        v = Vector((0, 0, 0.3))
        v.rotate(o.rotation_euler)
        o.location = v
        m = bpy.data.materials.new(name + "_mat")
        m.use_nodes = True
        _rebuild_principled_material(m, flat_color_rgba=(*color, 1.0))
        o.data.materials.append(m)
    marker("AxisX", (0, math.pi / 2, 0), (1, 0, 0))       # +X right
    marker("AxisY", (-math.pi / 2, 0, 0), (0, 1, 0))      # +Y fwd
    marker("AxisZ", (0, 0, 0), (0, 0, 1))                 # +Z up


def scene_bounds():
    mn = Vector((1e18,) * 3)
    mx = Vector((-1e18,) * 3)
    for o in bpy.data.objects:
        if o.type != 'MESH' or o.name.startswith(("Axis", "Bound")):
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
    return (mn + mx) / 2, max((mx - mn).length, 0.01)


def render_views(prefix, sun_energy=4.0):
    scn = bpy.context.scene
    # EEVEE honors the Sollumz node graph (Workbench renders it black).
    # Backface-culling equivalent: two-sided off = hidden flipped faces.
    scn.render.engine = 'BLENDER_EEVEE'
    try:
        scn.eevee.taa_render_samples = 16
    except AttributeError:
        pass
    scn.render.resolution_x, scn.render.resolution_y = 960, 540
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
    sun.data.energy = sun_energy
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    bpy.context.collection.objects.link(sun)
    world = bpy.data.worlds.new("W")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = \
        (0.15, 0.15, 0.18, 1.0)
    scn.world = world
    center, size = scene_bounds()
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    bpy.context.collection.objects.link(cam)
    scn.camera = cam
    cam.data.lens = 40
    views = {
        "iso": Vector((1.6, -1.6, 1.2)),
        "side": Vector((2.2, 0.0, 0.15)),
        "top": Vector((0.0, -0.01, 2.5)),
    }
    for name, dirv in views.items():
        cam.location = center + dirv.normalized() * size * 1.6
        cam.rotation_euler = (center - cam.location).to_track_quat(
            '-Z', 'Y').to_euler()
        scn.render.filepath = os.path.join(
            OUT_DIR, f"{prefix}_{name}.png")
        bpy.ops.render.render(write_still=True)


def ydr_xmls(res_dir):
    # .ydd.xml too: ped drawables render through the same importer, and
    # silently matching nothing makes the check vacuous-pass
    return sorted(f for f in os.listdir(res_dir)
                  if f.endswith((".ydr.xml", ".ydd.xml")))


def ydr_path_for(res_dir, model):
    # szio writes <model>.ydr + <model>.ydr.xml into stream/
    for cand in (os.path.join(res_dir, "stream", model + ".ydr.xml"),
                 os.path.join(res_dir, model + ".ydr.xml")):
        if os.path.isfile(cand):
            return cand
    return None


if os.path.isdir(SRC):
    res_dir = SRC
    stream = os.path.join(res_dir, "stream")
    xmls = ydr_xmls(stream) if os.path.isdir(stream) else ydr_xmls(res_dir)
    if not xmls and not os.path.isfile(
            os.path.join(res_dir, "assembly_manifest.lua.json")):
        print("[render_check] FAIL no .ydr/.ydd.xml found in", res_dir)
        sys.exit(2)

    # per-drawable renders
    for xf in xmls:
        model = xf[:-len(".ydr.xml")]
        wipe()
        import_ydr(os.path.join(stream, xf) if os.path.isdir(stream)
                   else os.path.join(res_dir, xf))
        mesh_report()
        add_axis_markers()
        render_views(model)

    # composite assembly pose(s) from the manifest twin
    man_path = os.path.join(res_dir, "assembly_manifest.lua.json")
    if os.path.isfile(man_path):
        man = json.load(open(man_path))
        body_name = man.get("body")
        imported = {}

        def load_model(model):
            if model in imported:
                return imported[model]
            p = ydr_path_for(res_dir, model)
            if p is None:
                print(f"[render_check] WARN no ydr.xml for {model}")
                return []
            before = set(bpy.data.objects)
            import_ydr(p)
            objs = [o for o in bpy.data.objects
                    if o not in before and o.type == 'MESH'
                    and not o.name.startswith("Bound")]
            imported[model] = objs
            return objs

        def flat_mat(name, rgba):
            m = bpy.data.materials.new("diag_" + name)
            m.use_nodes = True
            _rebuild_principled_material(m, flat_color_rgba=rgba)
            return m

        def paint(objs, mat):
            for o in objs:
                if o.type == 'MESH':
                    o.data.materials.clear()
                    o.data.materials.append(mat)

        def pose(parts, offset_key, rot_key, colorize=False,
                 interpolate_factor=None):
            wipe()
            imported.clear()
            root = get_body_root()

            def place(objs, off, rot):
                for o in objs:
                    # order matters: parent + inverse BEFORE local transforms
                    o.parent = root
                    o.matrix_parent_inverse = \
                        root.matrix_world.inverted()
                    o.rotation_mode = 'YXZ'
                    o.location = Vector(off) if off else Vector((0, 0, 0))
                    if rot:
                        o.rotation_euler = rot if isinstance(rot, Euler) \
                            else tuple(math.radians(r) for r in rot)

            body_objs = load_model(body_name)
            place(body_objs, None, None)
            if colorize:
                paint(body_objs, flat_mat("body", (0.3, 0.3, 0.34, 1)))
            for part in parts:
                model = part.get("model")
                base_rot = part.get("folded_rot") or part.get("initial_rot")
                if part.get("kind") == "spin" or \
                        offset_key != "deployed_offset":
                    off = part.get("attach_offset")
                    rot = base_rot
                else:
                    off = part.get("deployed_offset",
                                   part.get("attach_offset"))
                    rot = part.get("deployed_rot") or base_rot
                if interpolate_factor is not None:
                    f = interpolate_factor
                    off0 = part.get("attach_offset") or (0, 0, 0)
                    off1 = part.get("deployed_offset", off0) or off0
                    off = [a + (b - a) * f for a, b in zip(off0, off1)]
                    r0 = [math.radians(x) for x in
                          (part.get("folded_rot") or part.get("initial_rot")
                           or (0, 0, 0))]
                    r1 = [math.radians(x) for x in
                          (part.get("deployed_rot")
                           or part.get("folded_rot")
                           or part.get("initial_rot") or (0, 0, 0))]
                    q0 = Euler(r0, 'YXZ').to_quaternion()
                    q1 = Euler(r1, 'YXZ').to_quaternion()
                    if q0.dot(q1) < 0.0:  # hemisphere continuity
                        q1.negate()
                    rot = q0.slerp(q1, f).to_euler('YXZ')
                if off is None:
                    continue
                objs = load_model(model)
                place(objs, off, rot)
                if colorize:
                    paint(objs, flat_mat(model,
                                         _generate_pastel_color(model)))
            add_axis_markers()
            return len(parts)

        parts = man.get("parts", [])
        n = pose(parts, "attach_offset", "folded_rot", colorize=True)
        if n:
            render_views("_assembly_folded", sun_energy=1.5)
        n = pose(parts, "deployed_offset", "deployed_rot", colorize=True)
        if n:
            render_views("_assembly_deployed", sun_energy=1.5)
        hinges = [p for p in parts
                  if p.get("kind") != "spin"
                  and p.get("deployed_rot") != p.get("folded_rot")]
        if hinges:
            n = pose(parts, "deployed_offset", "deployed_rot",
                     colorize=True, interpolate_factor=0.5)
            if n:
                render_views("_assembly_mid", sun_energy=1.5)
else:
    wipe()
    import_ydr(SRC)
    mesh_report()
    add_axis_markers()
    model = os.path.basename(SRC).replace(".ydr.xml", "")
    render_views(model)

print("[render_check] done")
