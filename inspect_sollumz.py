# SPDX-License-Identifier: AGPL-3.0-only
"""Probe the installed Sollumz add-on and print a JSON capability report.

Run:  blender --background --factory-startup --python inspect_sollumz.py
      [-- --caps-out <toolchain_caps.json>]
The report is printed between SFP_REPORT_BEGIN / SFP_REPORT_END markers so the
orchestrator can parse it out of Blender's noisy stdout. With --caps-out the
same report is also written verbatim to the given file.
"""
import json
import os
import sys

import bpy

report = {"ok": True, "errors": []}

try:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="Sollumz")
except Exception as exc:  # pragma: no cover - runs inside blender
    report["ok"] = False
    report["errors"].append(f"addon_enable failed: {exc!r}")

report["blender_version"] = list(bpy.app.version)
report["blender_python"] = sys.version.split()[0]

# --- add-on version -------------------------------------------------------
try:
    import Sollumz

    report["sollumz_module"] = Sollumz.__file__
    report["sollumz_version"] = list(Sollumz.bl_info.get("version", []))
except Exception as exc:
    report["errors"].append(f"import Sollumz: {exc!r}")

# --- registered sollumz operators ------------------------------------------
try:
    ops = sorted(op for op in dir(bpy.ops.sollumz) if not op.startswith("_"))
    report["operators"] = ops
except Exception as exc:
    report["errors"].append(f"ops dir: {exc!r}")

# --- key property groups / enums -------------------------------------------
try:
    from Sollumz.sollumz_properties import SollumType, ArchetypeType, AssetType

    report["SollumType"] = [e.value for e in SollumType]
    report["ArchetypeType"] = [e.value for e in ArchetypeType]
    report["AssetType"] = [e.value for e in AssetType]
except Exception as exc:
    report["errors"].append(f"enums: {exc!r}")

# --- helper functions the worker wants -------------------------------------
helpers = {}
try:
    from Sollumz.tools import drawablehelper, boundhelper

    for name in ("convert_obj_to_drawable", "convert_objs_to_single_drawable",
                 "convert_obj_to_model", "center_drawable_to_models"):
        helpers[f"drawablehelper.{name}"] = hasattr(drawablehelper, name)
    for name in ("convert_obj_to_composite", "convert_objs_to_single_composite",
                 "convert_obj_to_geometry", "convert_obj_to_bvh", "apply_flag_preset"):
        helpers[f"boundhelper.{name}"] = hasattr(boundhelper, name)
except Exception as exc:
    report["errors"].append(f"helper import: {exc!r}")
try:
    from Sollumz.ydr import shader_materials

    helpers["shader_materials.create_shader"] = hasattr(shader_materials, "create_shader")
except Exception as exc:
    report["errors"].append(f"shader_materials import: {exc!r}")
try:
    from Sollumz.ybn.gta5.presets import flag as flag_presets

    helpers["DEFAULT_FLAG_PRESET_NAME"] = getattr(flag_presets, "DEFAULT_FLAG_PRESET_NAME", None)
except Exception as exc:
    report["errors"].append(f"flag presets import: {exc!r}")
report["helpers"] = helpers

# --- bound flag preset names ------------------------------------------------
try:
    import json as _json
    from pathlib import Path

    presets_path = Path(Sollumz.__file__).parent / "ybn" / "gta5" / "presets" / "flag_presets.json"
    pdata = _json.loads(presets_path.read_text())
    report["flag_presets"] = [p.get("name") for p in pdata.get("presets", [])]
except Exception as exc:
    report["flag_presets"] = []
    report["errors"].append(f"flag preset list: {exc!r}")

# --- scene-level toggles the convert/export ops read ------------------------
scene = bpy.context.scene
scene_props = {}
for prop in ("auto_create_embedded_col", "center_drawable_to_selection",
             "create_seperate_drawables", "sz_default_flag_preset_name",
             "create_archetype_type", "ytyp_index"):
    scene_props[prop] = getattr(scene, prop, "<missing>") if hasattr(scene, prop) else "<missing>"
    if not isinstance(scene_props[prop], (int, float, bool, str)):
        scene_props[prop] = str(scene_props[prop])
scene_props["ytyps_len"] = len(scene.ytyps) if hasattr(scene, "ytyps") else -1
report["scene_props"] = scene_props

# --- export op signature -----------------------------------------------------
try:
    rna = bpy.ops.sollumz.export_assets.get_rna_type()
    report["export_assets_props"] = sorted(p.identifier for p in rna.properties)
except Exception as exc:
    report["errors"].append(f"export_assets rna: {exc!r}")
try:
    rna = bpy.ops.sollumz.export_ytyp_io.get_rna_type()
    report["export_ytyp_io_props"] = sorted(p.identifier for p in rna.properties)
except Exception as exc:
    report["errors"].append(f"export_ytyp_io rna: {exc!r}")

# --- shader material node layout (what image nodes exist per .sps) -----------
shader_report = {}
try:
    from Sollumz.ydr.shader_materials import create_shader

    for sps in ("default.sps", "normal_spec.sps", "normal.sps", "spec.sps"):
        try:
            mat = create_shader(sps)
            nodes = []
            for n in mat.node_tree.nodes:
                if n.bl_idname == "ShaderNodeTexImage":
                    embedded = None
                    try:
                        embedded = bool(n.texture_properties.embedded)
                    except Exception:
                        pass
                    nodes.append({"node": n.name, "label": n.label,
                                  "param": getattr(n, "texture_name", None),
                                  "embedded": embedded})
            shader_report[sps] = nodes
            bpy.data.materials.remove(mat)
        except Exception as exc:
            shader_report[sps] = f"create failed: {exc!r}"
except Exception as exc:
    report["errors"].append(f"shader probe: {exc!r}")
report["shader_nodes"] = shader_report

# --- ytyp archetype write surface ---------------------------------------------
try:
    scene.ytyps.add()
    ytyp = scene.ytyps[0]
    from Sollumz.sollumz_properties import ArchetypeType as _AT

    arch = ytyp.new_archetype(_AT.BASE)
    arch_fields = {}
    for f in ("name", "asset", "asset_type", "asset_name", "texture_dictionary",
              "physics_dictionary", "drawable_dictionary", "lod_dist",
              "hd_texture_dist", "bb_min", "bb_max", "bs_center", "bs_radius"):
        arch_fields[f] = hasattr(arch, f)
    arch_fields["flags_total_prop"] = hasattr(arch.flags, "total")
    report["archetype_fields"] = arch_fields
    report["archetype_defaults"] = {
        "lod_dist": arch.lod_dist, "hd_texture_dist": arch.hd_texture_dist,
        "flags_total": arch.flags.total, "asset_type": arch.asset_type,
    }
except Exception as exc:
    report["errors"].append(f"archetype probe: {exc!r}")

# --- bundled dependencies (szio/pymateria) ----------------------------------
deps = {}
for mod_name in ("szio", "pymateria"):
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", None)
        if ver is None:
            try:
                import importlib.metadata as ilm
                ver = ilm.version(mod_name)
            except Exception:
                ver = None
        deps[mod_name] = {"present": True, "version": ver,
                          "path": getattr(mod, "__file__", None)}
    except Exception as exc:
        deps[mod_name] = {"present": False, "error": repr(exc)}
report["sollumz_deps"] = deps

# --- native provider matrix: per target x ext -> RSC version -----------------
# This is the authoritative generation discriminator: RSC7 magic is shared by
# Legacy(Gen8) and Enhanced(Gen9); only the header version field differs.
rsc_versions = {}
try:
    from szio.gta5.assets import get_providers

    for ext in (".ydr", ".ydd", ".yft", ".ytd", ".ytyp", ".ybn", ".yld"):
        for target, provider in get_providers().items():
            if target.format.name != "NATIVE":
                continue
            key = target.version.name  # GEN8 / GEN9
            try:
                ver = provider.get_supported_rsc_version(ext)
                rsc_versions.setdefault(key, {})[ext] = ver
            except Exception:
                rsc_versions.setdefault(key, {}).setdefault(ext, None)
        rsc_versions  # noqa
    report["rsc_versions"] = rsc_versions
    report["providers"] = sorted(
        f"{t.format.name}_{t.version.name}" for t in get_providers())
except Exception as exc:
    report["providers"] = []
    report["errors"].append(f"provider probe: {exc!r}")

# --- capability flags --------------------------------------------------------
caps = {}
try:
    from Sollumz.ydd import yddexport  # noqa: F401
    caps["drawable_dictionary_export"] = True
except Exception as exc:
    caps["drawable_dictionary_export"] = repr(exc)
try:
    import inspect as _inspect
    from Sollumz.ydr.ydrexport import create_drawable_asset
    caps["skinned_export"] = "armature_obj" in _inspect.signature(
        create_drawable_asset).parameters
except Exception as exc:
    caps["skinned_export"] = repr(exc)
try:
    from Sollumz.tools.drawablehelper import convert_obj_to_model  # noqa: F401
    from Sollumz.tools.blenderhelper import create_empty_object  # noqa: F401
    caps["drawable_helpers"] = True
except Exception as exc:
    caps["drawable_helpers"] = repr(exc)
# cloth physics: szio can serialize AssetClothDictionary but the add-on offers
# no verified authoring/export path for .yld -- probe for the pieces anyway.
try:
    from szio.gta5.native.adapters.cloth import (  # noqa: F401
        save_cloth_dictionary_to_native)
    caps["cloth_szio_saver"] = True
except Exception:
    caps["cloth_szio_saver"] = False
cloth_ops = [op for op in report.get("operators", []) if "cloth" in op]
caps["cloth_ops"] = cloth_ops
caps["cloth_export"] = False  # never claim support without an authoring path
# YMT / ped metadata writer: required for add-on apparel packs to register.
ymt_found = False
try:
    import pymateria.gta5 as _pm
    for _attr in ("MetaDataFile", "PedVariationInfo", "Ymt", "PedMeta"):
        ymt_found = ymt_found or hasattr(_pm, _attr)
except Exception:
    pass
caps["ymt_export"] = bool(ymt_found)
caps["embedded_bounds"] = bool(helpers.get(
    "boundhelper.convert_obj_to_composite"))
report["capabilities"] = caps

print("\nSFP_REPORT_BEGIN")
print(json.dumps(report, indent=2, default=str))
print("SFP_REPORT_END\n")

_argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if "--caps-out" in _argv:
    _out = _argv[_argv.index("--caps-out") + 1]
    os.makedirs(os.path.dirname(os.path.abspath(_out)), exist_ok=True)
    with open(_out, "w") as _fh:
        json.dump(report, _fh, indent=2, default=str)

sys.exit(0 if report["ok"] else 1)
