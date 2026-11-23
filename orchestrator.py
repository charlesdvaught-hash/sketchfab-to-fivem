# SPDX-License-Identifier: AGPL-3.0-only
"""Sketchfab -> FiveM prop pipeline orchestrator. Runs OUTSIDE Blender.

Usage:
    python orchestrator.py --file <path>     # process one file/archive
    python orchestrator.py                   # process everything in ./inbox once
    python orchestrator.py --watch           # poll ./inbox forever

Pipeline dirs (per PIPELINE_SPEC.json) resolve relative to this script's dir:
    ./inbox  ./work  ./quarantine  ./build/resources/[sketchfab_props]
    ./build/asset_registry.json  ./build/toolchain_caps.json

Env: SFP_BLENDER_EXE overrides Blender discovery.
All production conversion is LOCAL (pinned Blender/Sollumz). No remote
conversion APIs, uploads, or fallbacks (network isolation requirement).
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(ROOT, "inbox")
WORK = os.path.join(ROOT, "work")
QUAR = os.path.join(ROOT, "quarantine")
BUILD = os.path.join(ROOT, "build")
OUT_RES = os.path.join(BUILD, "resources", "[sketchfab_props]")
REGISTRY = os.path.join(BUILD, "asset_registry.json")
CAPS_FILE = os.path.join(BUILD, "toolchain_caps.json")
PROBE = os.path.join(ROOT, "inspect_sollumz.py")

SUPPORTED = (".glb", ".gltf", ".fbx", ".obj", ".zip")
PRIORITY = [".glb", ".gltf", ".fbx", ".obj"]
ARCHIVE_EXTS = (".zip", ".rar", ".7z")
NAME_RE = re.compile(r"^[a-z0-9_]{3,48}$")
TIMEOUT_S = 120
MAX_DIM, MIN_DIM = 80.0, 0.02

# pinned production baseline -- drift is WARN, not auto-upgrade
BASELINE = {"blender": (5, 2, 1), "sollumz": (2, 9, 0),
            "orchestrator_python": (3, 12)}

SIDECAR_DEFAULTS = {
    "asset_name": None,
    "target_height_meters": None,
    "scale_multiplier": 1.0,
    "rotation_offset_deg": [0.0, 0.0, 0.0],
    "pivot_mode": "bottom_center",
    "split_multi_prop": False,
    "collision_mode": "convex_hull",
    "max_triangles": 35000,
    "max_texture_res": 2048,
    "motion_config": {},
    "rig_recipe": None,
    "texture_mode": "embed",
    "lod_dist": 150.0,
    "hd_texture_dist": 60.0,
    "archetype_flags": "32",
    "license": None,
    "source_url": None,
    "attribution": None,
    "asset_type": "prop",
    "exclude_objects": [],
    "require_weights": True,
    "clothing_fix": {},
    "target_generation": "legacy",
    "apparel_pack": False,
    "apparel_config": None,
    "max_zip_members": 512,
    "max_zip_extract_bytes": 536870912,
    "blender_timeout_s": 120,
}

FXMANIFEST_PROP = """fx_version 'cerulean'
game 'gta5'

description 'Auto-built Sketchfab prop: {name}'

data_file 'DLC_ITYP_REQUEST' 'stream/{ytyp_file}'
"""

FXMANIFEST_PED = """fx_version 'cerulean'
game 'gta5'

description 'Auto-built skinned drawable dictionary: {name}'
"""

FXMANIFEST_APPAREL = """fx_version 'cerulean'
game 'gta5'

description 'Auto-built apparel pack: {name}'

dependency '/policy:subdir_file_mapping'

files {{
  '{meta_name}'
}}

data_file 'SHOP_PED_APPAREL_META_FILE' '{meta_name}'
"""

SPAWN_SNIPPET = """local model = `{name}`
RequestModel(model)
while not HasModelLoaded(model) do Wait(0) end
local obj = CreateObject(model, x, y, z, true, true, false)
SetModelAsNoLongerNeeded(model)"""

# GTA ped component name -> ShopPedApparel eCompType (verified against real
# packs: jbib/teef/berd etc. in [assets]/mafiav2).
COMPONENT_TO_ECOMP = {
    "head": "PV_COMP_HEAD", "berd": "PV_COMP_BERD", "hair": "PV_COMP_HAIR",
    "uppr": "PV_COMP_UPPR", "lowr": "PV_COMP_LOWR", "hand": "PV_COMP_HAND",
    "feet": "PV_COMP_FEET", "teef": "PV_COMP_TEEF", "accs": "PV_COMP_ACCS",
    "task": "PV_COMP_TASK", "decl": "PV_COMP_DECL", "jbib": "PV_COMP_JBIB",
}

LUA_EXE = shutil.which("lua")


class ZipReject(Exception):
    """Raised with an ERR_* code when an archive fails safety validation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def log(msg):
    print(f"[orchestrator] {msg}", flush=True)


def find_blender():
    env = os.environ.get("SFP_BLENDER_EXE")
    if env and os.path.isfile(env):
        return env
    base = r"C:\Program Files\Blender Foundation"
    if os.path.isdir(base):
        vers = sorted((d for d in os.listdir(base) if d.startswith("Blender")),
                      reverse=True)
        for v in vers:
            exe = os.path.join(base, v, "blender.exe")
            if os.path.isfile(exe):
                return exe
    exe = shutil.which("blender")
    if exe:
        return exe
    raise RuntimeError("Blender not found; set SFP_BLENDER_EXE")


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    asset = f"sf_{slug}" if slug else "sf_prop"
    asset = asset[:48].rstrip("_")
    while len(asset) < 3:
        asset += "_"
    return asset


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sidecar(paths):
    """Merge sidecars -> (sidecar, err_code_or_None).

    A malformed sidecar is invalid input -- the job must quarantine with
    ERR_MALFORMED_SIDECAR, never silently continue on defaults.
    """
    side = dict(SIDECAR_DEFAULTS)
    err = None
    for p in paths:
        if p and os.path.isfile(p):
            try:
                with open(p) as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("sidecar must be a JSON object")
                side.update({k: v for k, v in data.items()
                             if k in SIDECAR_DEFAULTS})
                log(f"sidecar loaded: {p}")
            except Exception as exc:
                err = f"ERR_MALFORMED_SIDECAR:{os.path.basename(p)}:{exc}"
    if side["asset_name"]:
        candidate = slugify(side["asset_name"])
        if NAME_RE.match(candidate):
            side["asset_name"] = candidate
        else:
            log(f"WARN: sidecar asset_name rejected, deriving from filename")
            side["asset_name"] = None
    return side, err


def quarantine(job_dir, code, extra=None):
    dest = os.path.join(QUAR, os.path.basename(job_dir))
    os.makedirs(QUAR, exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.move(job_dir, dest)
    report = {"status": "QUARANTINED", "reason": code, "extra": extra or {},
              "time": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(dest, "failure_report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    log(f"QUARANTINED {os.path.basename(dest)}: {code}")
    return dest


# ---------------------------------------------------------------------------
# Toolchain capability probe (build/toolchain_caps.json)
# ---------------------------------------------------------------------------

def run_probe(blender):
    """Run inspect_sollumz.py inside Blender -> parse + persist caps report."""
    os.makedirs(BUILD, exist_ok=True)
    cmd = [blender, "--background", "--factory-startup", "--python", PROBE,
           "--", "--caps-out", CAPS_FILE]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    caps = None
    if os.path.isfile(CAPS_FILE):
        try:
            with open(CAPS_FILE) as fh:
                caps = json.load(fh)
        except Exception:
            caps = None
    if caps is None:
        m = re.search(r"SFP_REPORT_BEGIN\s*(.*?)SFP_REPORT_END",
                      proc.stdout + proc.stderr, re.S)
        if m:
            try:
                caps = json.loads(m.group(1))
                with open(CAPS_FILE, "w") as fh:
                    json.dump(caps, fh, indent=2)
            except Exception:
                caps = None
    return caps


def load_caps(blender):
    """Cached caps if present and parseable; otherwise re-probe.

    Delete build/toolchain_caps.json to force a re-probe (e.g. after a
    toolchain change).
    """
    if os.path.isfile(CAPS_FILE):
        try:
            with open(CAPS_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return run_probe(blender)


def texconv_identity():
    """texconv has no reliable --version; record path+sha256+size instead."""
    p = os.path.join(ROOT, "bin", "texconv.exe")
    if not os.path.isfile(p):
        return {"path": None, "present": False}
    return {"path": p, "present": True, "size": os.path.getsize(p),
            "sha256": sha256_of(p)}


def toolchain_block(caps, blender):
    """Recorded toolchain facts + drift warnings (never hardcode RSC versions)."""
    caps = caps or {}
    warns = []
    bv = caps.get("blender_version")
    if bv and tuple(bv[:3]) != BASELINE["blender"]:
        warns.append(f"WARN_TOOLCHAIN_VERSION_DRIFT:blender:{bv}")
    sv = caps.get("sollumz_version")
    if sv and tuple(sv[:3]) != BASELINE["sollumz"]:
        warns.append(f"WARN_TOOLCHAIN_VERSION_DRIFT:sollumz:{sv}")
    pv = platform.python_version_tuple()[:2]
    if tuple(int(x) for x in pv) != BASELINE["orchestrator_python"]:
        warns.append(
            f"WARN_TOOLCHAIN_VERSION_DRIFT:orchestrator_python:{platform.python_version()}")
    tc = {
        "blender_exe": blender,
        "blender_version": bv,
        "blender_python": caps.get("blender_python"),
        "orchestrator_python": platform.python_version(),
        "sollumz_version": sv,
        "sollumz_module": caps.get("sollumz_module"),
        "sollumz_deps": caps.get("sollumz_deps"),
        "texconv": texconv_identity(),
        "providers": caps.get("providers", []),
        "rsc_versions": caps.get("rsc_versions", {}),
        "capabilities": caps.get("capabilities", {}),
    }
    return tc, warns


def toolchain_fatal(caps):
    """ERR_TOOLCHAIN_INCOMPATIBLE-level failures (no usable exporter at all)."""
    if not caps or not caps.get("ok"):
        return "ERR_TOOLCHAIN_INCOMPATIBLE:probe_failed"
    providers = caps.get("providers") or []
    if "NATIVE_GEN8" not in providers:
        return "ERR_TOOLCHAIN_INCOMPATIBLE:no_gen8_provider"
    if "export_assets" not in (caps.get("operators") or []):
        return "ERR_TOOLCHAIN_INCOMPATIBLE:no_export_assets_op"
    return None


# ---------------------------------------------------------------------------
# Input validation (archives + content sniffing)
# ---------------------------------------------------------------------------

def safe_extract_zip(zip_path, raw_dir, max_members, max_bytes):
    """Validate EVERY member before extracting anything (untrusted input)."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ZipReject("ERR_BAD_ZIP")
    infos = zf.infolist()
    if len(infos) > max_members:
        raise ZipReject("ERR_ZIP_TOO_MANY_FILES", len(infos))
    total = 0
    raw_abs = os.path.abspath(raw_dir)
    for i in infos:
        name = i.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) \
                or ".." in parts:
            raise ZipReject("ERR_ZIP_TRAVERSAL", i.filename)
        if os.path.splitext(name)[1].lower() in ARCHIVE_EXTS:
            raise ZipReject("ERR_ZIP_NESTED_ARCHIVE", i.filename)
        total += i.file_size
        if total > max_bytes:
            raise ZipReject("ERR_ZIP_TOO_LARGE", total)
    for i in infos:
        rel = os.path.normpath(i.filename.replace("\\", "/"))
        dest = os.path.abspath(os.path.join(raw_abs, rel))
        if dest != raw_abs and not dest.startswith(raw_abs + os.sep):
            raise ZipReject("ERR_ZIP_TRAVERSAL", i.filename)
        if i.is_dir():
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(i) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)


def sniff_source(path):
    """Magic-byte check that content matches the claimed extension."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return "ERR_MALFORMED_SOURCE:unreadable"
    if ext == ".glb":
        if not head.startswith(b"glTF"):
            return "ERR_MALFORMED_SOURCE:glb_magic"
    elif ext == ".gltf":
        if not head.lstrip().startswith(b"{"):
            return "ERR_MALFORMED_SOURCE:gltf_not_json"
    elif ext == ".fbx":
        if not (head.startswith(b"Kaydara FBX Binary") or b"FBX" in head):
            return "ERR_MALFORMED_SOURCE:fbx_magic"
    elif ext == ".obj":
        try:
            head.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return "ERR_MALFORMED_SOURCE:obj_binary"
    return None


def rsc_header_version(path):
    """RSC7 magic is shared by Gen8/Gen9 -- the uint32 at offset 4 is the
    generation discriminator."""
    try:
        with open(path, "rb") as fh:
            hdr = fh.read(8)
    except OSError:
        return None
    if hdr[:4] != b"RSC7":
        return None
    return struct.unpack("<I", hdr[4:8])[0]


def pick_primary_file(raw_dir):
    """EC_01: recursive search, priority .glb > .gltf > .fbx > .obj, largest wins."""
    found = []
    for dirpath, _dirs, files in os.walk(raw_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in PRIORITY:
                found.append((PRIORITY.index(ext),
                              -os.path.getsize(os.path.join(dirpath, f)),
                              os.path.join(dirpath, f)))
    if not found:
        return None, False
    found.sort()
    multi = len([f for f in found if f[0] == found[0][0]]) > 1
    return found[0][2], multi


# ---------------------------------------------------------------------------
# Classification (explicit metadata only -- never guessed from geometry)
# ---------------------------------------------------------------------------

def classify_job(sidecar, caps):
    """Resolve (kind, gen, apparel) or return an ERR_* code string."""
    asset_type = sidecar.get("asset_type", "prop")
    if asset_type not in ("prop", "ped"):
        return f"ERR_UNKNOWN_ASSET_TYPE:{asset_type}"
    mc = sidecar.get("motion_config") or {}
    mode = mc.get("motion_mode") or "static"
    gen = sidecar.get("target_generation", "legacy")
    if gen not in ("legacy", "enhanced", "both"):
        return f"ERR_TARGET_GENERATION_UNSUPPORTED:{gen}"
    apparel = bool(sidecar.get("apparel_pack"))
    is_ped = asset_type == "ped" or mode == "ped_clothing"
    if asset_type == "ped" and mode not in ("static", "ped_clothing"):
        return f"ERR_ASSET_TYPE_CONFLICT:ped+{mode}"
    if apparel:
        if not is_ped:
            return "ERR_ASSET_TYPE_CONFLICT:apparel+prop"
        if gen == "both":
            return "ERR_TARGET_GENERATION_UNSUPPORTED:both_with_apparel"
        cfg = sidecar.get("apparel_config")
        err = validate_apparel_config(cfg)
        if err:
            return err
    if is_ped:
        caps_c = caps.get("capabilities") or {}
        if caps_c.get("drawable_dictionary_export") is not True \
                or caps_c.get("skinned_export") is not True:
            return "ERR_PED_CLOTH_EXPORT_UNSUPPORTED"
        if sidecar.get("collision_mode", "convex_hull") != "none":
            return ("ERR_COLLISION_MODE_UNSUPPORTED:"
                    + str(sidecar.get("collision_mode")))
        if (sidecar.get("clothing_fix") or {}).get("cloth"):
            return "ERR_PED_CLOTH_PHYSICS_UNSUPPORTED"
    if gen in ("enhanced", "both") \
            and "NATIVE_GEN9" not in (caps.get("providers") or []):
        return "ERR_TARGET_GENERATION_UNSUPPORTED:enhanced"
    kind = "ped" if is_ped else "prop"
    return (kind, gen, apparel)


def validate_apparel_config(cfg):
    if not isinstance(cfg, dict):
        return "ERR_APPAREL_CONFIG_INVALID:missing"
    ped = cfg.get("ped")
    if not isinstance(ped, str) or not re.fullmatch(r"[a-z0-9_]+", ped):
        return "ERR_APPAREL_CONFIG_INVALID:ped"
    dlc = cfg.get("dlc")
    if not isinstance(dlc, str) or not re.fullmatch(r"[a-z0-9_]+", dlc):
        return "ERR_APPAREL_CONFIG_INVALID:dlc"
    comps = cfg.get("components")
    if not isinstance(comps, list) or len(comps) != 1:
        return "ERR_APPAREL_CONFIG_INVALID:components_one_per_job"
    c = comps[0]
    if not isinstance(c, dict) or c.get("component") not in COMPONENT_TO_ECOMP:
        return "ERR_APPAREL_CONFIG_INVALID:component"
    try:
        int(c.get("index", 0))
        int(c.get("textures", 1))
    except (TypeError, ValueError):
        return "ERR_APPAREL_CONFIG_INVALID:index"
    return None


# ---------------------------------------------------------------------------
# Generated metadata (add-on apparel -- mirrors a verified working pack)
# ---------------------------------------------------------------------------

def joaat(s):
    """GTA joaat hash (case-insensitive, unsigned)."""
    h = 0
    for ch in s.encode("ascii", "replace").lower():
        h = (h + ch) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


SHOP_ITEM = """            <Item>
            <lockHash />
            <cost value="0"/>
            <textLabel />
            <uniqueNameHash>{uname}</uniqueNameHash>
            <eShopEnum>CLO_SHOP_NONE</eShopEnum>
\t\t\t<locate value="-99" />
\t\t\t<scriptSaveData value="0" />
\t\t\t<restrictionTags>
\t\t\t</restrictionTags>
            <forcedComponents />
            <variantComponents />
\t\t\t<drawableIndex value="{draw_idx}" />
\t\t\t<localDrawableIndex value="{draw_idx}" />
\t\t\t<eCompType>{ecomp}</eCompType>
\t\t\t<textureIndex value="0" />
\t\t\t<isInOutfit value="false" />
\t\t</Item>
"""


def gen_shop_meta(ped, dlc, components, creature=None):
    """ShopPedApparel meta mirroring the verified mafiav2 schema field-for-field."""
    full = f"{ped}_{dlc}"
    creature = creature or f"MP_CreatureMetadata_{dlc}"
    items = []
    for c in components:
        comp = c["component"]
        draw_idx = int(c.get("index", 0))
        sex = "M" if "_m_" in ped else "F"
        uname = f"CLO_SFP_{comp.upper()}_{draw_idx}_0_{sex}"
        items.append(SHOP_ITEM.format(
            uname=uname, draw_idx=draw_idx,
            ecomp=COMPONENT_TO_ECOMP[comp]))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<ShopPedApparel>\n"
        f"\t<pedName>{ped}</pedName>\n"
        f"\t<dlcName>{dlc}</dlcName>\n"
        f"\t<fullDlcName>{full}</fullDlcName>\n"
        "\t<eCharacter>SCR_CHAR_MULTIPLAYER</eCharacter>\n"
        f"\t<creatureMetaData>{creature}</creatureMetaData>\n"
        "\t<pedOutfits>\n\t</pedOutfits>\n"
        "\t<pedComponents>\n" + "".join(items) +
        "\t</pedComponents>\n"
        "\t<pedPropItems>\n\t</pedPropItems>\n"
        "</ShopPedApparel>\n")


def lua_syntax_check(path):
    """loadfile()-check a generated .lua file when a Lua runtime exists."""
    if LUA_EXE is None:
        return "WARN_LUA_SYNTAX_UNVERIFIED"
    r = subprocess.run([LUA_EXE, "-e",
                        f"assert(loadfile({path!r}))"],
                       capture_output=True, text=True, timeout=15)
    return None if r.returncode == 0 else f"ERR_LUA_SYNTAX:{r.stderr.strip()[-200:]}"


# ---------------------------------------------------------------------------
# Main job lifecycle
# ---------------------------------------------------------------------------

def process_source(input_path, blender, caps):
    """Full lifecycle for one inbox file. Returns True on publish."""
    input_path = os.path.abspath(input_path)
    if not os.path.isfile(input_path):
        log(f"skip missing: {input_path}")
        return False

    toolchain, tc_warns = toolchain_block(caps, blender)
    warnings = list(tc_warns)

    src_hash = sha256_of(input_path)
    stem = os.path.splitext(os.path.basename(input_path))[0]
    sidecar_path = os.path.join(os.path.dirname(input_path), "override.json")
    prelim, side_err = load_sidecar([sidecar_path])
    asset_name = prelim["asset_name"] or slugify(stem)
    job_id = f"{asset_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    job_dir = os.path.join(WORK, job_id)
    raw_dir = os.path.join(job_dir, "raw")
    stream_dir = os.path.join(job_dir, "stream")
    textures_dir = os.path.join(job_dir, "textures")
    os.makedirs(raw_dir, exist_ok=True)

    if side_err:
        quarantine(job_dir, side_err)
        return False

    # toolchain-level fatal: nothing can convert without a working exporter
    fatal = toolchain_fatal(caps)
    if fatal:
        quarantine(job_dir, fatal, {"toolchain": toolchain})
        return False

    if input_path.lower().endswith(".zip"):
        try:
            safe_extract_zip(input_path, raw_dir,
                             int(prelim["max_zip_members"]),
                             int(prelim["max_zip_extract_bytes"]))
        except ZipReject as zr:
            quarantine(job_dir, zr.code, {"member": zr.detail})
            return False
        inner_sidecar = os.path.join(raw_dir, "override.json")
        sidecar, side_err = load_sidecar([inner_sidecar, sidecar_path])
        if side_err:
            quarantine(job_dir, side_err)
            return False
        primary, multi = pick_primary_file(raw_dir)
        if multi:
            warnings.append("WARN_MULTIPLE_SCENE_FILES")
        if primary is None:
            quarantine(job_dir, "ERR_NO_SUPPORTED_3D_FILE")
            return False
        source_file = primary
    else:
        source_file = os.path.join(raw_dir, os.path.basename(input_path))
        shutil.copy2(input_path, source_file)
        sidecar = prelim

    bad = sniff_source(source_file)
    if bad:
        quarantine(job_dir, bad, {"file": os.path.basename(source_file)})
        return False

    if sidecar["asset_name"]:
        asset_name = sidecar["asset_name"]

    # classification: explicit sidecar metadata only
    cls = classify_job(sidecar, caps or {})
    if isinstance(cls, str):
        quarantine(job_dir, cls)
        return False
    kind, gen, apparel = cls
    timeout_s = int(sidecar.get("blender_timeout_s", 120) or 120)

    job = {"source_file": os.path.abspath(source_file), "asset_name": asset_name,
           "sidecar": sidecar, "job_dir": job_dir, "stream_dir": stream_dir,
           "textures_dir": textures_dir}
    with open(os.path.join(job_dir, "job.json"), "w") as fh:
        json.dump(job, fh, indent=2)

    worker = os.path.join(ROOT, "blender_worker.py")
    cmd = [blender, "--background", "--factory-startup",
           "--python-exit-code", "1", "--python", worker, "--", job_dir]
    log(f"blender job {job_id} ({os.path.basename(input_path)}) "
        f"[{kind}/{gen}{'/apparel' if apparel else ''}]")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, cwd=job_dir)
        with open(os.path.join(job_dir, "blender_stdout.log"), "w") as fh:
            fh.write(proc.stdout + "\n--- STDERR ---\n" + proc.stderr)
    except subprocess.TimeoutExpired:
        quarantine(job_dir, "ERR_BLENDER_TIMEOUT", {"timeout_s": timeout_s})
        return False

    result_path = os.path.join(job_dir, "worker_result.json")
    worker_result = {}
    if os.path.isfile(result_path):
        try:
            with open(result_path) as fh:
                worker_result = json.load(fh)
        except Exception:
            worker_result = {}
    if proc.returncode != 0 or worker_result.get("status") != "SUCCESS":
        werrs = worker_result.get("errors") or []
        reason = next((e for e in werrs
                      if isinstance(e, str) and e.startswith("ERR_")),
                      "ERR_WORKER_FAILED")
        quarantine(job_dir, reason,
                   {"exit_code": proc.returncode, "errors": werrs})
        return False
    warnings.extend(worker_result.get("warnings", []))

    # --- validation gates (run BEFORE any quarantine move) --------------------
    def find_under(base, filename):
        for dirpath, _dirs, files in os.walk(base):
            if filename in files:
                return os.path.join(dirpath, filename)
        return None

    expected_versions = (caps.get("rsc_versions") or {})

    def native_files(subdir):
        """(path, ext, gen_name) for each emitted binary under stream_dir."""
        base = os.path.join(stream_dir, subdir) if subdir else stream_dir
        if not os.path.isdir(base):
            return
        for dirpath, _dn, fs in os.walk(base):
            if subdir == "" and os.path.basename(dirpath) in ("gen8", "gen9"):
                continue
            for f in fs:
                ext = os.path.splitext(f)[1].lower()
                if ext in (".ydr", ".ydd", ".ytd", ".ytyp"):
                    yield os.path.join(dirpath, f)

    def gate1(subdir, gen_name):
        """Every emitted binary: exists, size floor, RSC7 magic, version field
        == probed expected for (ext, gen)."""
        files = list(native_files(subdir))
        if not files:
            return "ERR_EXPORT_ARTIFACT_MISSING:empty_stream"
        expect = expected_versions.get(gen_name) or {}
        for p in files:
            ext = os.path.splitext(p)[1].lower()
            floor = {".ydr": (256 if sidecar.get("texture_mode") == "ytd"
                              else 1024),
                     ".ytd": 1024, ".ydd": 1024, ".ytyp": 64}.get(ext, 1)
            ver = rsc_header_version(p)
            if ver is None or os.path.getsize(p) <= floor:
                return f"ERR_GATE1_RSC7_INVALID:{os.path.basename(p)}"
            want = expect.get(ext)
            if want is not None and ver != want:
                return (f"ERR_GATE1_RSC_VERSION_MISMATCH:"
                        f"{os.path.basename(p)}:{ver}!={want}")
        return None

    # gen8 tree = stream root (legacy/enhanced) or gen8/ subdir (both)
    if gen == "both":
        g1 = gate1("gen8", "GEN8") or gate1("gen9", "GEN9")
    else:
        g1 = gate1("", "GEN9" if gen == "enhanced" else "GEN8")
    if g1:
        quarantine(job_dir, g1)
        return False

    def gate1_bin(subdir, want_file):
        p = find_under(os.path.join(stream_dir, subdir)
                       if subdir else stream_dir, want_file)
        return p if p and os.path.isfile(p) else None

    import xml.etree.ElementTree as ET

    def names_in_xml(path):
        try:
            r = ET.parse(path).getroot()
        except ET.ParseError:
            return None, None
        return r, {el.text.strip() for el in r.iter("name") if el.text}

    def gate2(subdir, gen_name):
        """prop: ytyp RSC7 + .xml declares every .ydr basename (+collision
        declaration iff mode != none). ped: .ydd.xml dictionary declares
        every drawable name."""
        base = os.path.join(stream_dir, subdir) if subdir else stream_dir
        if kind == "prop":
            ytyp_bin = gate1_bin(subdir, f"{asset_name}.ytyp")
            ytyp_xml = gate1_bin(subdir, f"{asset_name}.ytyp.xml")
            if not ytyp_bin or rsc_header_version(ytyp_bin) is None:
                return "ERR_GATE2_YTYP_INVALID:missing_bin"
            if not ytyp_xml:
                return "ERR_GATE2_YTYP_INVALID:missing_xml"
            root, names = names_in_xml(ytyp_xml)
            if root is None:
                return "ERR_GATE2_YTYP_INVALID:parse"
            ydr_names = {os.path.splitext(os.path.basename(p))[0]
                         for p in native_files(subdir)
                         if p.endswith(".ydr")}
            if not ydr_names <= names:
                return ("ERR_GATE2_YTYP_INVALID:missing_archetypes:"
                        + ",".join(sorted(ydr_names - names)))
            # collision must be embedded iff a non-none mode was requested --
            # a Blender-side collision mesh alone never counts as generated.
            try:
                ydr_xml = gate1_bin(
                    subdir,
                    os.path.splitext(sorted(ydr_names)[0])[0] + ".ydr.xml")
                bound_ok = None
                if ydr_xml:
                    r2 = ET.parse(ydr_xml).getroot()
                    bound_ok = any("bound" in el.tag.lower()
                                   for el in r2.iter())
                if sidecar.get("collision_mode") != "none" and not bound_ok:
                    return "ERR_COLLISION_MODE_UNSUPPORTED:verify_failed"
            except ET.ParseError:
                return "ERR_COLLISION_MODE_UNSUPPORTED:verify_failed"
        else:  # ped
            ydd_xml = gate1_bin(subdir, f"{asset_name}.ydd.xml")
            if not ydd_xml:
                return "ERR_GATE2_YDD_INVALID:missing_xml"
            root, _names = names_in_xml(ydd_xml)
            if root is None:
                return "ERR_GATE2_YDD_INVALID:parse"
            if "drawabledictionary" not in root.tag.lower():
                return "ERR_GATE2_YDD_INVALID:not_a_dictionary"
            drawables = {el.text.strip() for el in root.iter()
                         if el.tag.lower() == "name" and el.text}
            # dictionary name + at least one contained drawable name
            if len(drawables) < 2:
                return "ERR_GATE2_YDD_INVALID:empty"
        return None

    g2 = gate2("gen8" if gen == "both" else "", "GEN8") or \
        (gen == "both" and gate2("gen9", "GEN9"))
    if g2:
        quarantine(job_dir, g2)
        return False

    def gate_uv(subdir):
        """A drawable that binds a Texture parameter must emit TexCoord0 in
        its vertex Layout -- Sollumz silently drops UVs when the mesh's UV
        layer isn't named TexCoord0, which renders the prop untextured/black
        in-game even though the TextureDictionary shipped."""
        base = os.path.join(stream_dir, subdir) if subdir else stream_dir
        if not os.path.isdir(base):
            return None
        for dirpath, _dn, fs in os.walk(base):
            if subdir == "" and os.path.basename(dirpath) in ("gen8", "gen9"):
                continue
            for f in fs:
                if not f.endswith(".ydr.xml"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    x = ET.parse(p).getroot()
                except ET.ParseError:
                    continue
                has_tex_param = any(
                    it.get("type") == "Texture"
                    for it in x.iter("Item"))
                if not has_tex_param:
                    continue
                has_uv = any(el.tag == "TexCoord0" for el in x.iter())
                if not has_uv:
                    return ("ERR_EXPORT_MISSING_UV:"
                            + os.path.basename(p))
        return None

    guv = gate_uv("gen8" if gen == "both" else "") or \
        (gen == "both" and gate_uv("gen9"))
    if guv:
        quarantine(job_dir, guv)
        return False

    dims = worker_result.get("dims") or [0, 0, 0]
    if not all(MIN_DIM <= d <= MAX_DIM for d in dims):
        quarantine(job_dir, "ERR_GATE3_DIMS_OUT_OF_RANGE", {"dims": dims})
        return False

    registry = {}
    if os.path.isfile(REGISTRY):
        with open(REGISTRY) as fh:
            registry = json.load(fh)
    existing = registry.get(asset_name)
    if existing and existing["source_sha256"] != src_hash:
        quarantine(job_dir, "ERR_GATE4_NAME_COLLISION",
                   {"registered_source": existing.get("source_file")})
        return False

    # Gate 4b: emitted archetype names must not exist under a DIFFERENT source
    # hash anywhere else (registry index + sibling .ytyp.xml scan).
    expected = set()
    for sub in (["gen8", "gen9"] if gen == "both" else [""]):
        expected |= {os.path.splitext(os.path.basename(p))[0]
                     for p in native_files(sub)}
    arch_conflicts = []
    for reg_name, reg_entry in registry.items():
        if reg_name == asset_name:
            continue
        hit = set(reg_entry.get("archetypes", [])) & expected
        if hit and reg_entry.get("source_sha256") != src_hash:
            arch_conflicts.append({"resource": reg_name,
                                   "archetypes": sorted(hit)})
    if kind == "prop" and os.path.isdir(OUT_RES):
        for res_name in sorted(os.listdir(OUT_RES)):
            if res_name == asset_name:
                continue
            sdir = os.path.join(OUT_RES, res_name, "stream")
            if not os.path.isdir(sdir):
                continue
            for f in os.listdir(sdir):
                if not f.endswith(".ytyp.xml"):
                    continue
                try:
                    r = ET.parse(os.path.join(sdir, f)).getroot()
                    hit = expected & {e.text.strip() for e in r.iter("name")
                                      if e.text}
                    if hit:
                        arch_conflicts.append({"resource": res_name,
                                               "archetypes": sorted(hit)})
                except ET.ParseError:
                    pass
    if arch_conflicts:
        quarantine(job_dir, "ERR_GATE4_ARCHETYPE_COLLISION",
                   {"conflicts": arch_conflicts})
        return False

    # apparel pack needs a .ymt the toolchain cannot emit -- the .ydd itself
    # converted fine and is preserved inside the quarantined job dir
    if apparel and not (caps.get("capabilities") or {}).get("ymt_export"):
        quarantine(job_dir, "ERR_PED_APPAREL_YMT_UNSUPPORTED",
                   {"note": "ydd converted; pack cannot register "
                            "components without a .ymt"})
        return False

    # --- staging: build the complete resource dir(s) BEFORE any publish ------
    staging = tempfile.mkdtemp(prefix="sfp_stage_", dir=WORK)
    shipped = {}          # res_name -> res_dir (under staging)
    lua_artifacts = []

    def stage_resource(res_name, stream_subdir, template, meta_name=None):
        res_dir = os.path.join(staging, res_name)
        sdir = os.path.join(res_dir, "stream")
        os.makedirs(sdir, exist_ok=True)
        src_base = os.path.join(stream_dir, stream_subdir) \
            if stream_subdir else stream_dir
        shipped_files = []
        for dirpath, _dn, fs in os.walk(src_base):
            if not stream_subdir and os.path.basename(dirpath) in ("gen8", "gen9"):
                continue
            for f in fs:
                src = os.path.join(dirpath, f)
                rel = os.path.relpath(src, src_base)
                dst = os.path.join(sdir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                shipped_files.append(rel.replace(os.sep, "/"))
        # the manifest must name the file szio actually wrote -- for
        # both-gen resources the .ytyp keeps the asset name, not <name>_g9
        ytyp_file = next((os.path.basename(f) for f in shipped_files
                          if f.endswith(".ytyp")), f"{asset_name}.ytyp")
        manifest = template.format(name=res_name, ytyp_file=ytyp_file,
                                   meta_name=meta_name or "")
        with open(os.path.join(res_dir, "fxmanifest.lua"), "w") as fh:
            fh.write(manifest)
        return res_dir, shipped_files

    # manifests (lua_out) ship at the resource root of every emitted resource
    lua_out = os.path.join(job_dir, "lua_out")
    lua_files = sorted(f for f in os.listdir(lua_out)
                       if f.endswith(".lua")) \
        if os.path.isdir(lua_out) else []
    for lf in lua_files:
        lua_artifacts.append(lf)

    apparel_cfg = (sidecar.get("apparel_config") or {}) if apparel else None
    meta_name = None
    if apparel:
        ped = apparel_cfg["ped"]
        dlc = apparel_cfg["dlc"]
        meta_name = f"{ped}_{ped}_{dlc}_shop.meta"

    res_names = ([asset_name, f"{asset_name}_g9"] if gen == "both"
                 else [asset_name])
    stream_trees = [""] if gen != "both" else ["gen8", "gen9"]
    reports = {}
    for res_name, tree in zip(res_names, stream_trees):
        if apparel:
            # apparel layout: stream/<fullDlc>/ with ^-component filenames +
            # shop meta at resource root
            c = apparel_cfg["components"][0]
            comp = c["component"]
            full_dlc = f"{apparel_cfg['ped']}_{apparel_cfg['dlc']}"
            res_dir = os.path.join(staging, res_name)
            comp_dir = os.path.join(res_dir, "stream", full_dlc)
            os.makedirs(comp_dir, exist_ok=True)
            comp_files = []
            src_base = os.path.join(stream_dir, tree) if tree else stream_dir
            for dirpath, _dn, fs in os.walk(src_base):
                if not tree and os.path.basename(dirpath) in ("gen8", "gen9"):
                    continue
                for f in fs:
                    if f.endswith(".xml"):
                        continue  # sidecars audit only, not shipped to apparel
                    ext = os.path.splitext(f)[1].lower()
                    if ext == ".ydd":
                        fn = f"{full_dlc}^{comp}_{int(c.get('index', 0)):03d}_u.ydd"
                    elif ext == ".ytd":
                        fn = f"{full_dlc}^{comp}_diff_{int(c.get('index', 0)):03d}_a_uni.ytd"
                    else:
                        continue
                    shutil.move(os.path.join(dirpath, f),
                                os.path.join(comp_dir, fn))
                    comp_files.append(fn)
            with open(os.path.join(res_dir, meta_name), "w") as fh:
                fh.write(gen_shop_meta(apparel_cfg["ped"], apparel_cfg["dlc"],
                                       apparel_cfg["components"]))
            manifest = FXMANIFEST_APPAREL.format(name=res_name,
                                                 meta_name=meta_name)
            with open(os.path.join(res_dir, "fxmanifest.lua"), "w") as fh:
                fh.write(manifest)
            shipped[res_name] = res_dir
            reports[res_name] = {"files": comp_files + [meta_name]}
        else:
            template = FXMANIFEST_PROP if kind == "prop" else FXMANIFEST_PED
            res_dir, shipped_files = stage_resource(res_name, tree, template)
            shipped[res_name] = res_dir
            reports[res_name] = {"files": shipped_files}

    # lua manifests land at every emitted resource's root -- and must also be
    # declared in files{} or client-side LoadResourceFile consumers (dronelab,
    # signal_units) never receive them (files{} sends, never executes)
    for res_name, res_dir in shipped.items():
        for lf in lua_files:
            shutil.copy2(os.path.join(lua_out, lf),
                         os.path.join(res_dir, lf))
        # .lua.json twins (machine-readable manifest data for the renderer)
        # ship undeclared -- build artifacts, not client files
        if os.path.isdir(lua_out):
            for jf in (f for f in os.listdir(lua_out)
                       if f.endswith(".lua.json")):
                shutil.copy2(os.path.join(lua_out, jf),
                             os.path.join(res_dir, jf))
        reports[res_name]["files"] += lua_artifacts
        decl = [lf for lf in lua_files if lf != "fxmanifest.lua"]
        if decl:
            with open(os.path.join(res_dir, "fxmanifest.lua"), "a",
                      encoding="utf-8") as fh:
                fh.write("\nfiles { %s }\n" % ", ".join(
                    "'%s'" % f for f in decl))

    # manifest gate: every data_file/files{} path must resolve; no lua54
    for res_name, res_dir in shipped.items():
        mp = os.path.join(res_dir, "fxmanifest.lua")
        with open(mp, encoding="utf-8") as fh:
            text = fh.read()
        if re.search(r"lua54\b", text):
            shutil.rmtree(staging, ignore_errors=True)
            quarantine(job_dir, "ERR_MANIFEST_UNRESOLVED:lua54")
            return False
        # every quoted resource path (data_file targets + files{} entries)
        # must resolve inside the resource; data-file TYPE names never end
        # in these extensions so they cannot false-positive
        refs = re.findall(
            r"'([^']+\.(?:meta|xml|ydd|ytd|ydr|ytyp|ymt|lua))'", text)
        unresolved = [u for u in refs
                      if not os.path.isfile(os.path.join(res_dir, u))]
        if unresolved:
            shutil.rmtree(staging, ignore_errors=True)
            quarantine(job_dir, "ERR_MANIFEST_UNRESOLVED",
                       {"missing": unresolved})
            return False

    # lua gate: generated .lua (fxmanifest + motion manifests) loadfile-checked
    lua_warn = []
    if LUA_EXE is None:
        lua_warn.append("WARN_LUA_SYNTAX_UNVERIFIED")
    for res_name, res_dir in shipped.items():
        for lf in ["fxmanifest.lua"] + lua_files:
            r = lua_syntax_check(os.path.join(res_dir, lf))
            if r and r.startswith("ERR_"):
                shutil.rmtree(staging, ignore_errors=True)
                quarantine(job_dir, r)
                return False
    warnings += lua_warn

    # --- build_report per resource (distinguishes every category) --------------
    now = datetime.datetime.now().isoformat(timespec="seconds")
    build_report = {
        "status": "SUCCESS", "asset_name": asset_name,
        "asset_type": kind, "source_file": os.path.basename(input_path),
        "source_sha256": src_hash,
        "dimensions_meters": dims, "final_triangles": worker_result.get("tris"),
        "collision_mode": (sidecar["collision_mode"] if kind == "prop"
                           else "none"),
        "target_generation": gen,
        "resources": sorted(reports),
        "generated_files": {k: v["files"] for k, v in reports.items()},
        "checks_passed": ["gate1_rsc", "gate2_structure", "gate3_dims",
                          "gate4_name", "gate4b_archetypes", "manifest_gate",
                          "lua_gate"],
        "checks_failed": [],
        "warnings": warnings,
        "skipped": worker_result.get("skipped", []),
        "unsupported": worker_result.get("unsupported", []),
        "external_assumptions": [
            "docs.fivem.net resource-manifest + data-files (verified)",
            "docs.fivem.net/natives AttachEntityToEntity rotationOrder",
            "github.com/Sollumz/Sollumz + installed 2.9.0 source",
        ],
        "toolchain": toolchain,
        "motion_mode": worker_result.get("motion_mode", "static"),
        "kinematic_chain": worker_result.get("kinematic_chain"),
        "texture_mode": worker_result.get("texture_mode", "embed"),
        "artifacts": lua_artifacts,
        "files_readback": worker_result.get("files", []),
        "inspect": worker_result.get("inspect", {}),
        "sollumz_version": worker_result.get("sollumz_version"),
        "license": sidecar.get("license"),
        "source_url": sidecar.get("source_url"),
        "attribution": sidecar.get("attribution"),
        "provenance": {"license": sidecar.get("license"),
                       "source_url": sidecar.get("source_url"),
                       "attribution": sidecar.get("attribution")},
        "runtime_status": ("downstream_consumer_required" if kind == "ped"
                           else ("manifest_only_requires_consumer"
                                 if lua_artifacts else "self_contained")),
        "runtime_validation": "NOT_PERFORMED",
        "unresolved_limitations": (
            ["enhanced_output_format_verified_not_runtime"]
            if gen in ("enhanced", "both") else []) + (
            ["ped_ydd_unverified_in_game"] if kind == "ped" else []),
        "spawn_snippet_lua": (SPAWN_SNIPPET.format(name=asset_name)
                              if kind == "prop" else None),
        "built_at": now,
    }
    for res_name, res_dir in shipped.items():
        rep = dict(build_report, asset_name=res_name,
                   resource_name=res_name)
        with open(os.path.join(res_dir, "build_report.json"), "w") as fh:
            json.dump(rep, fh, indent=2)

    # --- visual verification: re-import emitted .ydr.xml and render
    # diagnostic PNGs (iso/side/top per drawable + assembled composite when a
    # manifest twin ships). Advisory only -- report artifact, never a gate.
    render_script = os.path.join(ROOT, "render_check.py")
    render_warn = []
    render_ok = set()
    for res_name, res_dir in shipped.items():
        try:
            rproc = subprocess.run(
                [blender, "--background", "--factory-startup",
                 "--python-exit-code", "1", "--python", render_script,
                 "--", res_dir, res_dir],
                capture_output=True, text=True, timeout=180, cwd=res_dir)
            if rproc.returncode != 0:
                render_warn.append("WARN_RENDER_CHECK:" + res_name)
            else:
                render_ok.add(res_name)
            with open(os.path.join(res_dir, "render_check.log"), "w") as fh:
                fh.write(rproc.stdout + "\n--- STDERR ---\n" + rproc.stderr)
        except (subprocess.TimeoutExpired, OSError):
            render_warn.append("WARN_RENDER_CHECK:" + res_name)
    warnings += render_warn
    for res_name, res_dir in shipped.items():
        rp = os.path.join(res_dir, "build_report.json")
        with open(rp) as fh:
            rep = json.load(fh)
        # claim the check only when the render subprocess actually completed
        rep["checks_passed"].append("render_check" if res_name in render_ok
                                    else "render_check_failed")
        rep["warnings"] = warnings
        with open(rp, "w") as fh:
            json.dump(rep, fh, indent=2)

    # --- pivot-offset audit: part .ydr verts are pivot-canonicalised, so a
    # part's own bounding-box centre should sit near the drawable origin. A
    # large residual means the declared pivot and the vertex data disagree --
    # both the double-rotation and the missing body_off produced ~1.3 m
    # centres on the buster nacelles. Advisory warning, never a gate.
    for res_name, res_dir in shipped.items():
        manj = os.path.join(res_dir, "assembly_manifest.lua.json")
        if not os.path.isfile(manj):
            continue
        try:
            with open(manj) as fh:
                parts = (json.load(fh) or {}).get("parts") or []
        except (OSError, ValueError):
            continue
        for p in parts:
            yx = os.path.join(res_dir, "stream",
                              str(p.get("model", "")) + ".ydr.xml")
            if not os.path.isfile(yx):
                continue
            try:
                txt = open(yx, encoding="utf8", errors="replace").read(8192)
                bmin = re.search(r"BoundingBoxMin[^>]*", txt)
                bmax = re.search(r"BoundingBoxMax[^>]*", txt)
                if not (bmin and bmax):
                    continue
                def _v(tag):
                    return [float(m) for m in re.findall(
                        r'x="(-?[\d.]+)"\s*y="(-?[\d.]+)"\s*z="(-?[\d.]+)"',
                        tag)[0]]
                lo, hi = _v(bmin.group(0)), _v(bmax.group(0))
                d = sum(((a + b) / 2.0) ** 2 for a, b in zip(lo, hi)) ** 0.5
                if d > 0.15:
                    warnings.append("WARN_PART_PIVOT_OFFSET:%s:%s:%.3f" % (
                        res_name, p.get("model", "?"), d))
            except (OSError, ValueError, IndexError):
                continue
    if any(w.startswith("WARN_PART_PIVOT_OFFSET") for w in warnings):
        for res_name, res_dir in shipped.items():
            rp = os.path.join(res_dir, "build_report.json")
            if not os.path.isfile(rp):
                continue
            with open(rp) as fh:
                rep = json.load(fh)
            rep["warnings"] = warnings
            with open(rp, "w") as fh:
                json.dump(rep, fh, indent=2)

    # --- atomic publish: fully-built staging -> single rename per resource ----
    for res_name, res_dir in shipped.items():
        dest = os.path.join(OUT_RES, res_name)
        os.makedirs(OUT_RES, exist_ok=True)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.rename(res_dir, dest)

    registry[asset_name] = {
        "source_sha256": src_hash, "source_file": os.path.basename(input_path),
        "archetypes": sorted(expected),
        "resource_dir": os.path.join(OUT_RES, asset_name),
        "resources": [os.path.join(OUT_RES, n) for n in res_names],
        "asset_type": kind, "target_generation": gen,
        "apparel_pack": apparel,
        "runtime_validation": "NOT_PERFORMED",
        "license": sidecar.get("license"),
        "source_url": sidecar.get("source_url"),
        "attribution": sidecar.get("attribution"),
        "built_at": now,
    }
    os.makedirs(BUILD, exist_ok=True)
    with open(REGISTRY, "w") as fh:
        json.dump(registry, fh, indent=2)
    shutil.rmtree(job_dir, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    # consume the inbox source only after a successful publish
    if os.path.abspath(os.path.dirname(input_path)) == os.path.abspath(INBOX) \
            or input_path.startswith(os.path.abspath(INBOX) + os.sep):
        try:
            os.remove(input_path)
        except OSError:
            pass
    log(f"PUBLISHED {', '.join(res_names)} -> {OUT_RES}")
    return True


def stable(path, wait=1.0):
    try:
        s1 = os.path.getsize(path)
        time.sleep(wait)
        return os.path.isfile(path) and os.path.getsize(path) == s1
    except OSError:
        return False


def inbox_files():
    if not os.path.isdir(INBOX):
        return []
    return [os.path.join(INBOX, f) for f in sorted(os.listdir(INBOX))
            if os.path.splitext(f)[1].lower() in SUPPORTED
            and os.path.isfile(os.path.join(INBOX, f))]


def main():
    global TIMEOUT_S
    ap = argparse.ArgumentParser(description="Sketchfab->FiveM prop pipeline")
    ap.add_argument("--file", help="process a single file")
    ap.add_argument("--watch", action="store_true", help="poll ./inbox forever")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    args = ap.parse_args()
    TIMEOUT_S = args.timeout

    blender = find_blender()
    log(f"blender: {blender}")
    for d in (INBOX, WORK, QUAR, BUILD):
        os.makedirs(d, exist_ok=True)

    caps = load_caps(blender)
    if caps:
        log(f"toolchain: sollumz {caps.get('sollumz_version')} "
            f"providers {caps.get('providers')}")
    else:
        log("WARN: capability probe failed - jobs will quarantine "
            "ERR_TOOLCHAIN_INCOMPATIBLE")

    if args.file:
        ok = process_source(args.file, blender, caps)
        sys.exit(0 if ok else 1)

    if args.watch:
        log(f"watching {INBOX}")
        while True:
            for f in inbox_files():
                if stable(f):
                    process_source(f, blender, caps)
            time.sleep(2.0)

    for f in inbox_files():
        process_source(f, blender, caps)


if __name__ == "__main__":
    main()
