# SPDX-License-Identifier: AGPL-3.0-only
"""Blender-side module: evaluated world-space bake + preview_scene.json export
for the dashboard Rig & Animation Workbench (reference/RIG_WORKBENCH.md).

Runs INSIDE Blender (bpy available). Imported by blender_worker.py
--dump-preview; also usable standalone from spike scripts.
"""
import bpy
import json
import math
from mathutils import Vector, Matrix


# ---------------------------------------------------------------------------
# Evaluated depsgraph bake: extract every visible mesh exactly as the source
# renders it (armature poses, bone/node parenting, 0.01 CAD unit scales all
# resolved into world space). Never trust raw obj.data.vertices / rest poses.
# ---------------------------------------------------------------------------

def snapshot_evaluated_world_meshes(context):
    depsgraph = context.evaluated_depsgraph_get()
    parts_info = []
    baked_objects = []

    for obj in list(context.scene.objects):
        if obj.type != 'MESH' or obj.hide_viewport:
            continue

        arm_mod = next((m for m in obj.modifiers
                        if m.type == 'ARMATURE' and m.object), None)
        if arm_mod:
            bind_kind = "SKINNED:" + arm_mod.object.name
        elif obj.parent_type == 'BONE':
            bind_kind = "BONE_PARENT:%s:%s" % (
                obj.parent.name if obj.parent else "?",
                obj.parent_bone or "")
        elif obj.parent:
            bind_kind = "NODE_PARENT:" + obj.parent.name
        else:
            bind_kind = "ROOT_MESH"

        eval_obj = obj.evaluated_get(depsgraph)
        try:
            new_mesh = bpy.data.meshes.new_from_object(
                eval_obj, preserve_all_data_layers=True,
                depsgraph=depsgraph)
        except Exception:
            continue

        wm = eval_obj.matrix_world.copy()
        if wm.determinant() < 0:
            # negative scale turns the part inside-out: bake then fix winding
            new_mesh.transform(wm)
            new_mesh.flip_normals()
        else:
            new_mesh.transform(wm)

        if len(new_mesh.vertices) == 0:
            bpy.data.meshes.remove(new_mesh)
            continue

        xs = new_mesh.vertices[0].co.x
        mins = Vector((xs, new_mesh.vertices[0].co.y,
                       new_mesh.vertices[0].co.z))
        maxs = mins.copy()
        for v in new_mesh.vertices:
            c = v.co
            mins.x = min(mins.x, c.x); mins.y = min(mins.y, c.y)
            mins.z = min(mins.z, c.z)
            maxs.x = max(maxs.x, c.x); maxs.y = max(maxs.y, c.y)
            maxs.z = max(maxs.z, c.z)
        centroid = (mins + maxs) * 0.5
        span = (maxs - mins).length

        parts_info.append({
            "name": obj.name,
            "bind_kind": bind_kind,
            "verts": len(new_mesh.vertices),
            "polys": len(new_mesh.polygons),
            "centroid": [centroid.x, centroid.y, centroid.z],
            "mins": [mins.x, mins.y, mins.z],
            "maxs": [maxs.x, maxs.y, maxs.z],
            "span": span,
        })

        clean = bpy.data.objects.new("baked_" + obj.name, new_mesh)
        clean.matrix_world = Matrix.Identity(4)
        clean.vertex_groups.clear()  # purge toxic source vgroups (Law #3)
        context.collection.objects.link(clean)
        baked_objects.append(clean)

    return baked_objects, parts_info


# ---------------------------------------------------------------------------
# Island clustering: union-find over padded AABBs. Two characters in one
# Sketchfab scene (or cables spanning 20m) split into distinct islands.
# ---------------------------------------------------------------------------

def cluster_islands(parts_info):
    """2-pass padded-AABB islands. Pass 1 quarantines 'bridge' outliers
    (oversized spans, extreme wire aspect) into singleton islands so a 20m
    cable can't fuse two robots; pass 2 unions parts whose AABBs overlap
    when padded by 2% of scene height."""
    n = len(parts_info)
    if n == 0:
        return []
    spans = sorted(p["span"] for p in parts_info)
    med_span = spans[n // 2] or 1.0
    scene_h = max(p["maxs"][2] for p in parts_info) - \
        min(p["mins"][2] for p in parts_info)
    pad = max(0.02 * scene_h, 0.01 * med_span, 1e-4)

    def is_bridge(p):
        dims = sorted(max(p["maxs"][k] - p["mins"][k], 1e-9)
                      for k in range(3))
        if p["span"] > 2.5 * med_span and p["span"] > 1.0:
            return True
        return dims[2] / dims[1] > 25.0 and p["span"] > 1.0

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        if is_bridge(parts_info[i]):
            continue  # singleton island, never merged
        ai, bi = parts_info[i]["mins"], parts_info[i]["maxs"]
        for j in range(i + 1, n):
            if is_bridge(parts_info[j]):
                continue
            aj, bj = parts_info[j]["mins"], parts_info[j]["maxs"]
            if all(ai[k] - pad <= bj[k] and bi[k] + pad >= aj[k]
                   for k in range(3)):
                union(i, j)

    roots = {}
    out = []
    for i in range(n):
        r = find(i)
        if r not in roots:
            roots[r] = len(roots)
        out.append(roots[r])
    return out


# ---------------------------------------------------------------------------
# Role auto-guess: suggestions only -- the canvas dims trash suggestions,
# never auto-deletes.
# ---------------------------------------------------------------------------

def guess_roles(parts_info, islands, donor_bone_names, asset_type):
    n = len(parts_info)
    if n == 0:
        return []
    spans = sorted(p["span"] for p in parts_info)
    med_span = spans[n // 2] or 1.0
    outlier_span = max(0.5, 3.0 * med_span)

    roles = []
    for i, p in enumerate(parts_info):
        role = None
        if p["span"] > outlier_span:
            role = "trash"
        elif asset_type == "ped":
            role = ("rigid_bone" if p["bind_kind"].startswith("BONE_PARENT")
                    else "smooth_skin")
        else:
            role = "hull"
        roles.append({"role": role, "bone_lock": ""})
    return roles


# ---------------------------------------------------------------------------
# Normalization: fit the non-trash assembly to target height at origin.
# Compose order: v' = Rz(rot_z_deg) @ v * scale + offset
# ---------------------------------------------------------------------------

def compute_normalization(parts_info, roles, target_height):
    keep = [i for i, r in enumerate(roles) if r["role"] != "trash"]
    if not keep:
        keep = list(range(len(parts_info)))
    mins = [1e30] * 3
    maxs = [-1e30] * 3
    for i in keep:
        for a in range(3):
            mins[a] = min(mins[a], parts_info[i]["mins"][a])
            maxs[a] = max(maxs[a], parts_info[i]["maxs"][a])
    z_span = max(maxs[2] - mins[2], 1e-6)
    scale = (target_height / z_span) if target_height else 1.0
    offset = [
        -0.5 * (mins[0] + maxs[0]) * scale,
        -0.5 * (mins[1] + maxs[1]) * scale,
        -mins[2] * scale,
    ]
    return {"rot_z_deg": 0.0, "scale": scale, "offset": offset}


def apply_preview_transform(pt, t):
    rz = math.radians(t.get("rot_z_deg", 0.0))
    c, s = math.cos(rz), math.sin(rz)
    x, y, z = pt
    x, y = x * c - y * s, x * s + y * c
    k = t.get("scale", 1.0)
    off = t.get("offset", [0.0, 0.0, 0.0])
    return [x * k + off[0], y * k + off[1], z * k + off[2]]


# ---------------------------------------------------------------------------
# Colour: Principled base colour factor only -- never touch image pixels
# (decoding a 4K PNG just to pick a hex defeats the fast-preview point).
# ---------------------------------------------------------------------------

def part_color_hex(mesh, fallback_name):
    base = None
    for mat in mesh.materials:
        if mat is None:
            continue
        if mat.use_nodes:
            bsdf = next((n for n in mat.node_tree.nodes
                         if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
            if bsdf is not None:
                base = tuple(bsdf.inputs['Base Color'].default_value)[:3]
                break
        base = tuple(mat.diffuse_color)[:3]
        break
    if base is None or all(c < 0.02 for c in base):
        h = abs(hash(fallback_name)) & 0xFFFFFF
        base = (((h >> 16) & 0xFF) / 255.0,
                ((h >> 8) & 0xFF) / 255.0,
                (h & 0xFF) / 255.0)
    return "#{:02x}{:02x}{:02x}".format(
        int(base[0] * 255), int(base[1] * 255), int(base[2] * 255))


# ---------------------------------------------------------------------------
# Source animation discovery: <=24 rigid transform tracks per part (raw
# source objects, BEFORE the bake replaces them). Stored in normalized
# preview space as absolute [tx,ty,tz,rx,ry,rz] samples so the canvas can
# re-basis to any rest frame via M(t) @ M(rest)^-1.
# ---------------------------------------------------------------------------

class PartTransformSampler:
    """Per-part rigid transform tracker robust to armature-skinned parts.

    eval_obj.matrix_world is Identity for Armature-modifier-driven meshes,
    so rotation/translation is measured from a 3-vertex orthonormal triad
    picked to maximize triangle area, plus the triad centroid. Works the
    same whether the driver is skin, bone parent, node parent or constraint.
    """

    def __init__(self, objs):
        self.objs = list(objs)
        self.triads = {}   # name -> (i0,i1,i2)

    @staticmethod
    def _eval_world_verts(obj, dg):
        ev = obj.evaluated_get(dg)
        me = ev.to_mesh()
        mw = ev.matrix_world
        out = [mw @ v.co for v in me.vertices]
        ev.to_mesh_clear()
        return out

    @staticmethod
    def _pick_triad(verts):
        if len(verts) < 3:
            return (0, 0, min(1, len(verts) - 1))
        i0 = 0
        p0 = verts[i0]
        i1 = max(range(len(verts)),
                 key=lambda i: (verts[i] - p0).length_squared)
        d = verts[i1] - p0
        dl = d.length
        if dl < 1e-9:
            return (0, i1, 0)
        dn = d / dl
        i2 = max(range(len(verts)),
                 key=lambda i: ((verts[i] - p0) - dn *
                               (verts[i] - p0).dot(dn)).length_squared)
        return (i0, i1, i2)

    @staticmethod
    def triad_rotation(p0, p1, p2):
        x = p1 - p0
        if x.length < 1e-12:
            return Matrix.Identity(3)
        x = x.normalized()
        z = x.cross(p2 - p0)
        if z.length < 1e-12:
            return Matrix.Identity(3)
        z = z.normalized()
        y = z.cross(x).normalized()
        return Matrix((x, y, z)).transposed()

    def prepare(self, context):
        """Call at the reference frame: pick triads and record ref pose."""
        dg = context.evaluated_depsgraph_get()
        self.ref = {}
        for o in self.objs:
            try:
                verts = self._eval_world_verts(o, dg)
            except Exception:
                verts = []
            if len(verts) < 1:
                self.ref[o.name] = None
                continue
            t = self._pick_triad(verts)
            self.triads[o.name] = t
            p0, p1, p2 = verts[t[0]], verts[t[1]], verts[t[2]]
            self.ref[o.name] = {
                "R": self.triad_rotation(p0, p1, p2),
                "c": (p0 + p1 + p2) / 3.0,
                "rest_verts": (p0.copy(), p1.copy(), p2.copy()),
            }

    def sample(self, context):
        """Call at any frame: {name: (centroid, R_rel)} in world space,
        R_rel = R(f) @ R(ref)^T (exact delta vs prepare() frame)."""
        dg = context.evaluated_depsgraph_get()
        out = {}
        for o in self.objs:
            ref = self.ref.get(o.name)
            if ref is None:
                out[o.name] = None
                continue
            try:
                verts = self._eval_world_verts(o, dg)
            except Exception:
                out[o.name] = None
                continue
            t = self.triads[o.name]
            try:
                p0, p1, p2 = verts[t[0]], verts[t[1]], verts[t[2]]
            except IndexError:
                out[o.name] = None
                continue
            c = (p0 + p1 + p2) / 3.0
            R_rel = self.triad_rotation(p0, p1, p2) @ ref["R"].transposed()
            out[o.name] = (c, R_rel)
        return out


class DriverTransformSampler:
    """Fast rigid-track sampler: reads each part's *driving* matrix once per
    frame instead of re-evaluating its mesh. Skinned parts use their dominant
    vertex group's bone matrix (strided vgroup read on the SOURCE object --
    baked proxies have their groups purged); node/bone-parented parts use
    matrix_world; only shape-key/mesh-deform parts fall back to the triad
    vertex sampler."""

    # roles on a single rigid sub-assembly (rotor blades etc.) share a driver
    _TRIAD_KINDS = ('MESH_DEFORM', 'SIMPLE_DEFORM', 'LATTICE', 'SHRINKWRAP',
                    'SURFACE_DEFORM')

    def __init__(self, objs):
        self.objs = list(objs)
        self.drivers = {}          # name -> ('obj',) | ('bone', arm, bone) | ('triad',)
        self._triad = PartTransformSampler([])  # lazily populated
        self._triad_objs = []

    @staticmethod
    def _dominant_bone(obj, arm_obj):
        arm_bones = {b.name for b in arm_obj.data.bones}
        totals = {}
        verts = obj.data.vertices
        step = max(1, len(verts) // 64)
        vg = obj.vertex_groups
        for i in range(0, len(verts), step):
            for g in verts[i].groups:
                if g.weight < 0.05 or g.group >= len(vg):
                    continue
                bn = vg[g.group].name
                if bn in arm_bones:
                    totals[bn] = totals.get(bn, 0.0) + g.weight
        return max(totals, key=totals.get) if totals else None

    def prepare(self, context):
        dg = context.evaluated_depsgraph_get()
        self.ref = {}
        triad_objs = []
        for o in self.objs:
            arm_mod = next((m for m in o.modifiers
                            if m.type == 'ARMATURE' and m.object), None)
            deformed = any(m.type in self._TRIAD_KINDS
                           for m in o.modifiers) or \
                (o.data.shape_keys is not None and
                 len(o.data.shape_keys.key_blocks) > 1)
            if deformed:
                self.drivers[o.name] = ('triad',)
                triad_objs.append(o)
            elif arm_mod is not None:
                bn = self._dominant_bone(o, arm_mod.object)
                if bn:
                    self.drivers[o.name] = ('bone', arm_mod.object, bn)
                else:
                    self.drivers[o.name] = ('triad',)
                    triad_objs.append(o)
            else:
                self.drivers[o.name] = ('obj',)
        # ref centroid + ref matrix per part (one evaluated pass total)
        self._M0 = {}
        self._c0 = {}
        mesh_sampler = PartTransformSampler(self.objs)
        mesh_sampler.prepare(context)
        self._triad = PartTransformSampler(triad_objs)
        self._triad.ref = {o.name: mesh_sampler.ref.get(o.name)
                           for o in triad_objs}
        self._triad.triads = {o.name: mesh_sampler.triads[o.name]
                              for o in triad_objs
                              if o.name in mesh_sampler.triads}
        for o in self.objs:
            r = mesh_sampler.ref.get(o.name)
            if r is None:
                self._c0[o.name] = None
                continue
            self._c0[o.name] = r["c"].copy()
            m0 = self._driver_matrix(o, dg)
            # evaluated matrix_world is a live depsgraph buffer that mutates
            # on the next frame_set -- copy it or M0 == M every frame
            self._M0[o.name] = m0.copy() if m0 is not None else None

    def _driver_matrix(self, o, dg):
        d = self.drivers[o.name]
        if d[0] == 'bone':
            arm_eval = d[1].evaluated_get(dg)
            pb = arm_eval.pose.bones.get(d[2])
            if pb is None:
                return None
            return arm_eval.matrix_world @ pb.matrix
        if d[0] == 'obj':
            return o.evaluated_get(dg).matrix_world
        return None  # triad path handled separately

    def sample(self, context):
        """{name: (centroid, R_rel)} exactly like PartTransformSampler."""
        dg = context.evaluated_depsgraph_get()
        out = {}
        triad_sample = None
        for o in self.objs:
            if self._c0.get(o.name) is None:
                out[o.name] = None
                continue
            d = self.drivers[o.name]
            if d[0] == 'triad':
                if triad_sample is None:
                    triad_sample = self._triad.sample(context)
                out[o.name] = triad_sample.get(o.name)
                continue
            M = self._driver_matrix(o, dg)
            M0 = self._M0.get(o.name)
            if M is None or M0 is None:
                out[o.name] = None
                continue
            dM = M @ M0.inverted()
            out[o.name] = (dM @ self._c0[o.name],
                           dM.to_3x3())
        return out

    def driver_of(self, name):
        return self.drivers.get(name)


def _preview_mat(t):
    """The preview transform as a 4x4: T(off) @ Scale(s) @ Rz(rot)."""
    rz = math.radians(t.get("rot_z_deg", 0.0))
    s = t.get("scale", 1.0)
    off = Vector(t.get("offset", (0.0, 0.0, 0.0)))
    return Matrix.Translation(off) @ Matrix.Scale(s, 4) @ Matrix.Rotation(rz, 4, 'Z')


def sample_source_anim(context, src_objs_by_name, transform, max_samples=16):
    """Sample each named object's rigid world transform across the union
    animation range via driving matrices (no per-frame mesh evals).
    Returns source_anim dict or None when the scene has no animation."""
    scene = context.scene
    if not bpy.data.actions:
        return None  # unanimated scene: don't emit bogus flat tracks
    rng = None
    for a in bpy.data.actions:
        r = a.frame_range
        rng = r if rng is None else (min(rng[0], r[0]), max(rng[1], r[1]))
    f_start, f_end = int(rng[0]), int(rng[1])
    if f_end - f_start <= 1:
        return None

    fps = float(scene.render.fps or scene.render.fps_base or 30.0)
    n = max(2, min(max_samples, f_end - f_start + 1))
    frames = [f_start + (f_end - f_start) * i / (n - 1) for i in range(n)]
    dump_frame = scene.frame_current

    P = _preview_mat(transform)
    sampler = DriverTransformSampler(src_objs_by_name.values())
    sampler.prepare(context)   # reference = current (dump) frame
    rest = {}
    tracks = {name: [] for name in src_objs_by_name}
    for f in frames:
        scene.frame_set(int(round(f)))
        context.view_layer.update()
        snap = sampler.sample(context)
        for name in src_objs_by_name:
            s = snap.get(name)
            if s is None:
                tracks[name].append(None)
                continue
            c, R_rel = s
            cw = P @ c
            e = (P.to_3x3() @ R_rel).to_euler()
            if name not in rest:
                rest[name] = [round(cw.x, 4), round(cw.y, 4),
                              round(cw.z, 4)]
            tracks[name].append([round(cw.x, 4), round(cw.y, 4),
                                 round(cw.z, 4),
                                 round(e.x, 4), round(e.y, 4),
                                 round(e.z, 4)])
    scene.frame_set(dump_frame)
    context.view_layer.update()

    return {
        "fps": fps,
        "frame_start": f_start,
        "frame_end": f_end,
        "dump_frame": dump_frame,
        "duration_sec": round((f_end - f_start) / fps, 3),
        "sample_times": [round((f - f_start) / fps, 4) for f in frames],
        "rest_centroids": rest,
        "part_tracks": tracks,
    }


# ---------------------------------------------------------------------------
# Export: decimate a visual proxy, transform verts into normalized preview
# space, write compact JSON for the canvas workbench.
# ---------------------------------------------------------------------------

def export_preview_scene_json(context, baked_objects, parts_info,
                              donor_armature, out_json_path,
                              asset_type="prop", target_height=1.9,
                              target_total_tris=18000, src_objs=None):
    islands = cluster_islands(parts_info)

    donor_bones = []
    if donor_armature is not None and donor_armature.type == 'ARMATURE':
        donor_bones = [b.name for b in donor_armature.data.bones]
    roles = guess_roles(parts_info, islands, donor_bones, asset_type)
    transform = compute_normalization(parts_info, roles, target_height)

    total_polys = sum(p["polys"] for p in parts_info)
    depsgraph = context.evaluated_depsgraph_get()
    # never decimate small parts: <250-tri mechanical detail keeps its
    # silhouette (is this a hinge or a stray cable?) and skips ~70% of
    # modifier overhead. Budgets are shaved only among the big parts.
    FLOOR = 250
    big_polys = sum(p["polys"] for p in parts_info
                    if p["polys"] >= FLOOR)
    budgets = [p["polys"] for p in parts_info]
    if big_polys > target_total_tris:
        k = target_total_tris / big_polys
        budgets = [p["polys"] if p["polys"] < FLOOR
                   else max(12, int(p["polys"] * k))
                   for p in parts_info]

    parts_out = []
    for idx, (obj, info) in enumerate(zip(baked_objects, parts_info)):
        mesh = obj.data
        eval_obj = None
        mod = None
        if len(mesh.polygons) > budgets[idx]:
            mod = obj.modifiers.new("PrevDec", 'DECIMATE')
            mod.ratio = max(0.01, budgets[idx] / len(mesh.polygons))
            context.view_layer.update()
            eval_obj = obj.evaluated_get(depsgraph)
            mesh = eval_obj.to_mesh()

        mesh.calc_loop_triangles()
        # decimate leaves dead verts in the evaluated mesh: reindex so the
        # JSON only carries verts the faces actually reference
        remap = {}
        tris = []
        for t in mesh.loop_triangles:
            tri = []
            for vi in t.vertices:
                ni = remap.get(vi)
                if ni is None:
                    ni = len(remap)
                    remap[vi] = ni
                tri.append(ni)
            tris.append(tri)
        verts = [None] * len(remap)
        for vi, ni in remap.items():
            c = mesh.vertices[vi].co
            verts[ni] = [round(x, 3) for x in
                         apply_preview_transform(c, transform)]

        if verts:
            xs = [v[0] for v in verts]
            ys = [v[1] for v in verts]
            zs = [v[2] for v in verts]
            pivot = [round((min(xs) + max(xs)) * 0.5, 4),
                     round((min(ys) + max(ys)) * 0.5, 4),
                     round((min(zs) + max(zs)) * 0.5, 4)]
        else:
            pivot = [0.0, 0.0, 0.0]

        parts_out.append({
            "id": idx,
            "name": info["name"],
            "bind_kind": info["bind_kind"],
            "island_id": islands[idx],
            "role": roles[idx]["role"],
            "bone_lock": roles[idx]["bone_lock"],
            "pivot": pivot,
            "color": part_color_hex(obj.data, info["name"]),
            "v": verts,
            "f": tris,
        })

        if eval_obj is not None:
            eval_obj.to_mesh_clear()
            obj.modifiers.remove(mod)

    bones_out = []
    if donor_armature is not None and donor_armature.type == 'ARMATURE':
        mat = donor_armature.matrix_world
        for b in donor_armature.data.bones:
            h = apply_preview_transform(mat @ b.head_local, transform)
            t = apply_preview_transform(mat @ b.tail_local, transform)
            bones_out.append({
                "name": b.name,
                "parent": b.parent.name if b.parent else None,
                "head": [round(c, 4) for c in h],
                "tail": [round(c, 4) for c in t],
            })

    payload = {
        "asset_type": asset_type,
        "preview_transform": transform,
        "parts": parts_out,
        "bones": bones_out,
    }

    if src_objs:
        try:
            sa = sample_source_anim(
                context, {p["name"]: src_objs[p["name"]]
                          for p in parts_info if p["name"] in src_objs},
                transform)
        except Exception as exc:
            print("[preview] source_anim sampling failed:", exc)
            sa = None
        if sa is not None:
            # rekey tracks by part id for the canvas
            by_name = {p["name"]: p["id"] for p in parts_out}
            sa["part_tracks"] = {
                str(by_name[n]): tr
                for n, tr in sa["part_tracks"].items() if n in by_name}
            payload["source_anim"] = sa

    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return transform
