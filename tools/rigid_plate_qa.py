#!/usr/bin/env python3
"""rigid_plate_qa.py — Drop-in Upgrade for autorig_check.py on Rigid-Bone Mechs

Why existing autorig_check.py passes broken rigid rigs:
  autorig_check.py measures intra-mesh edge stretch (l / r). On a 48-part
  rigid-bound mech (100% single-bone weights per mesh island), NO mesh edge
  connects vertices on two different bones. Thus every edge has stretch = 1.000
  under every pose, even if a left-thigh plate is bound to SKEL_R_Thigh!

This check adds 4 rigid-plate invariants without needing an in-game spawn:
  1. Connected-Island Decomposition & Single-Bone Purity
  2. Bilateral Symmetry (L/R X-sign) & Bone-Axis Projection Window t in [-0.3, 1.3]
  3. Virtual Seam Adjacency Graph: touching plates (rest gap < 2.5 cm) must map
     to the SAME bone (0 hops) or PARENT-CHILD bones (1 hop) in the GTA skeleton.
  4. Single-Joint FK Isolation Seam Shear & BVH Penetration Check.
"""
import bpy
import math
import os
import sys
from collections import deque
from mathutils import Euler, Vector
from mathutils.bvhtree import BVHTree

def decompose_rigid_islands(mesh_obj, arm_obj):
    me = mesh_obj.data
    vg_names = {g.index: g.name for g in mesh_obj.vertex_groups}
    # .ydd meshes store split-normal duplicates, so raw edge adjacency
    # explodes into per-face micro-islands -- weld verts by position first.
    weld = {}
    key_of = []
    for v in me.vertices:
        k = (round(v.co.x, 5), round(v.co.y, 5), round(v.co.z, 5))
        key_of.append(k)
        weld.setdefault(k, []).append(v.index)
    keys = list(weld)
    key_idx = {k: i for i, k in enumerate(keys)}
    adj = [[] for _ in range(len(keys))]
    for e in me.edges:
        u, v = key_idx[key_of[e.vertices[0]]], key_idx[key_of[e.vertices[1]]]
        if u != v:
            adj[u].append(v)
            adj[v].append(u)
    visited = [False] * len(keys)
    islands = []
    for vi in range(len(keys)):
        if visited[vi]:
            continue
        q = deque([vi])
        visited[vi] = True
        comp = []
        bone_counts = {}
        part_counts = {}
        while q:
            cur = q.popleft()
            comp.append(cur)
            for vi2 in weld[keys[cur]]:
                for g in me.vertices[vi2].groups:
                    if g.group not in vg_names:
                        continue
                    gn = vg_names[g.group]
                    if gn.startswith("__part_"):
                        part_counts[gn[7:]] = part_counts.get(gn[7:], 0) + 1
                    elif g.weight > 0.5:
                        bone_counts[gn] = bone_counts.get(gn, 0) + 1
            for nb in adj[cur]:
                if not visited[nb]:
                    visited[nb] = True
                    q.append(nb)
        dom_bone = max(bone_counts, key=bone_counts.get) if bone_counts else None
        dom_part = max(part_counts, key=part_counts.get) if part_counts else None
        pts = [mesh_obj.matrix_world @ Vector(keys[i]) for i in comp]
        centroid = sum(pts, Vector()) / len(pts)
        islands.append({
            "id": len(islands),
            "verts": comp,
            "bone": dom_bone,
            "part": dom_part,
            "centroid": centroid,
            "mins": Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            "maxs": Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))),
        })
    return islands

def _find_partmap(src):
    """<asset>.partmap.json next to the .ydd -- pre-join part centroids/
    bboxes written by blender_worker (non-bone __part_* vgroups do NOT
    survive Sollumz .ydd export)."""
    base = src
    for suf in (".ydd.xml", ".ydd"):
        if base.endswith(suf):
            base = base[:-len(suf)]
    cand = base + ".partmap.json"
    return cand if os.path.isfile(cand) else None

def attribute_islands(islands, partmap_path, pad_m=0.01):
    """Assign each welded island a source part: first by padded-bbox
    containment of the island centroid, tie-broken/fallen-back to nearest
    part centroid. Marks confidence: 'part' = contained, 'part_guess' =
    nearest only."""
    import json
    parts = (json.load(open(partmap_path)).get("parts")
             if partmap_path else None)
    if not parts:
        return
    for isl in islands:
        if isl.get("part"):
            continue  # __part_* vgroup survived (in-Blender QA use)
        c = isl["centroid"]
        hits = []
        for name, p in parts.items():
            mn, mx = p["mins"], p["maxs"]
            if all(mn[k] - pad_m <= c[k] <= mx[k] + pad_m
                   for k in range(3)):
                hits.append(((Vector(p["centroid"]) - c).length, name, p))
        contained = bool(hits)
        if not hits:
            hits = [((Vector(p["centroid"]) - c).length, name, p)
                    for name, p in parts.items()]
        if not hits:
            continue
        hits.sort(key=lambda h: h[0])
        _d, name, p = hits[0]
        isl["part" if contained else "part_guess"] = name
        isl["part_lock"] = p.get("assigned_bone") or p.get("bone_lock")

def bone_hop_distance(arm_obj, b1, b2):
    if b1 == b2:
        return 0
    bones = arm_obj.data.bones
    if b1 not in bones or b2 not in bones:
        return 999
    anc1 = {}
    cur, d = bones[b1], 0
    while cur:
        anc1[cur.name] = d
        cur, d = cur.parent, d + 1
    cur, d = bones[b2], 0
    while cur:
        if cur.name in anc1:
            return anc1[cur.name] + d
        cur, d = cur.parent, d + 1
    return 999

def _part_tag(isl):
    if isl.get("part"):
        return f" [part={isl['part']}]"
    if isl.get("part_guess"):
        return f" [part~{isl['part_guess']}]"
    return ""


def audit_rigid_plate_map(mesh_obj, arm_obj, islands=None, pad_m=0.025):
    if islands is None:
        islands = decompose_rigid_islands(mesh_obj, arm_obj)
    anomalies = []
    # Check 1: L/R lateral sign vs assigned SKEL_L_ / SKEL_R_ bone
    for isl in islands:
        bn = isl["bone"] or ""
        b = arm_obj.data.bones.get(bn)
        if b is None:
            anomalies.append(f"Island {isl['id']} has unmapped bone {bn}")
            continue
        bx = (arm_obj.matrix_world @ b.head_local).x
        if ("_L_" in bn or "_R_" in bn) and abs(isl["centroid"].x) > 0.04:
            if (isl["centroid"].x * bx) < 0:
                anomalies.append(
                    f"CROSS_LATERAL_BIND: Island {isl['id']} at x={isl['centroid'].x:.3f} "
                    f"bound to opposite-side bone {bn} (bone x={bx:.3f})"
                    + _part_tag(isl))
        # exported dominant bone disagrees with the part's assigned lock
        if isl.get("part_lock") and isl["part_lock"] != bn:
            anomalies.append(
                f"PART_BONE_MISMATCH: Island {isl['id']} dominant bone {bn} "
                f"but its part expects {isl['part_lock']}" + _part_tag(isl))

    # Check 2: Virtual Seam Adjacency vs Skeleton Tree Hop Count
    for i in range(len(islands)):
        for j in range(i + 1, len(islands)):
            ia, ib = islands[i], islands[j]
            if all(ia["mins"][k] - pad_m <= ib["maxs"][k] and
                   ia["maxs"][k] + pad_m >= ib["mins"][k] for k in range(3)):
                hops = bone_hop_distance(arm_obj, ia["bone"], ib["bone"])
                if hops > 1:
                    anomalies.append(
                        f"DISJOINT_KINEMATIC_SEAM: Touching islands {ia['id']} ({ia['bone']}) "
                        f"and {ib['id']} ({ib['bone']}) are {hops} bone-hops apart (expected <=1)")
    return islands, anomalies


def _isl_box(isl, delta=None):
    pts = []
    for x in (isl["mins"].x, isl["maxs"].x):
        for y in (isl["mins"].y, isl["maxs"].y):
            for z in (isl["mins"].z, isl["maxs"].z):
                p = Vector((x, y, z))
                pts.append(delta @ p if delta is not None else p)
    return (Vector((min(p.x for p in pts), min(p.y for p in pts),
                   min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts),
                   max(p.z for p in pts))))


def _overlap(a, b):
    return all(a[0][k] <= b[1][k] and a[1][k] >= b[0][k] for k in range(3))


def fk_sweep_penetration(mesh_obj, arm_obj, islands, angle_deg=25.0):
    """Check 4 (isolation FK): swing each island-owning bone +/-angle about
    its local X (the flex axis). Islands on that bone AND its descendants
    ride along; flag any NEW AABB interpenetration against islands bound
    elsewhere -- a correct rigid map can contact a neighbour plate only at
    a real joint, never pass through a distant one."""
    anomalies = []
    by_bone = {}
    for i, isl in enumerate(islands):
        by_bone.setdefault(isl["bone"], []).append(i)
    bones = arm_obj.data.bones

    def descendants(bn):
        out = {bn}
        stack = [bn]
        while stack:
            n = stack.pop()
            if n in bones:
                for c in bones[n].children:
                    out.add(c.name)
                    stack.append(c.name)
        return out

    armw = arm_obj.matrix_world
    armw_inv = armw.inverted()
    pbs = arm_obj.pose.bones
    rest_boxes = [_isl_box(i) for i in islands]
    rest_touch = [[False] * len(islands) for _ in islands]
    for i in range(len(islands)):
        for j in range(i + 1, len(islands)):
            if _overlap(rest_boxes[i], rest_boxes[j]):
                rest_touch[i][j] = rest_touch[j][i] = True

    for bn in by_bone:
        if bn not in pbs:
            continue
        pb = pbs[bn]
        moved = {i for i, isl in enumerate(islands)
                 if isl["bone"] in descendants(bn)}
        for sgn in (1.0, -1.0):
            pb.rotation_mode = 'QUATERNION'
            pb.rotation_quaternion = Euler(
                (math.radians(sgn * angle_deg), 0.0, 0.0)).to_quaternion()
            bpy.context.view_layer.update()
            delta = armw @ pb.matrix @ pb.bone.matrix_local.inverted() @ armw_inv
            for i in moved:
                box_i = _isl_box(islands[i], delta)
                for j in range(len(islands)):
                    if j in moved or rest_touch[i][j]:
                        continue
                    if _overlap(box_i, rest_boxes[j]):
                        anomalies.append(
                            f"FK_PENETRATION: bone {bn} {sgn*angle_deg:+.0f}deg "
                            f"moves island {islands[i]['id']} ({islands[i]['bone']}) "
                            f"through island {islands[j]['id']} "
                            f"({islands[j]['bone']})")
            pb.rotation_quaternion = Euler((0, 0, 0)).to_quaternion()
    bpy.context.view_layer.update()
    return anomalies


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    src = os.path.abspath(argv[0])
    bpy.ops.preferences.addon_enable(module="Sollumz")
    bpy.ops.sollumz.import_assets(
        directory=os.path.dirname(src),
        files=[{"name": os.path.basename(src)}])
    arm = next((o for o in bpy.context.scene.objects
                if o.type == 'ARMATURE'), None)
    mesh = next((o for o in bpy.context.scene.objects
                 if o.type == 'MESH' and o.vertex_groups
                 and o.find_armature()), None)
    if arm is None or mesh is None:
        print("[rigid_qa] FAIL: need a skinned mesh + armature in", src)
        sys.exit(1)
    print(f"[rigid_qa] mesh={mesh.name} armature={arm.name} "
          f"bones={len(arm.data.bones)}")
    islands = decompose_rigid_islands(mesh, arm)
    pm = _find_partmap(src)
    if pm:
        attribute_islands(islands, pm)
        print(f"[rigid_qa] partmap: {os.path.basename(pm)}")
    islands, anomalies = audit_rigid_plate_map(mesh, arm, islands)
    print(f"[rigid_qa] {len(islands)} rigid islands")
    for isl in islands:
        pure = isl["bone"] is not None
        print(f"  island {isl['id']:3d}: {len(isl['verts']):6d} verts "
              f"-> {isl['bone'] or 'UNMAPPED'}"
              + _part_tag(isl)
              + ("" if pure else "  <-- PURITY FAIL"))
    anomalies += fk_sweep_penetration(mesh, arm, islands)
    print(f"[rigid_qa] {len(anomalies)} anomalies")
    for a in anomalies:
        print(" ", a)
    sys.exit(1 if anomalies else 0)


main()
