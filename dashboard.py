# SPDX-License-Identifier: AGPL-3.0-only
"""Sketchfab->FiveM pipeline dashboard. Zero-dependency local web UI.

Run:  python dashboard.py [--port 8420]

Layout (non-coder facing):
  left  -- "CONVERT A MODEL": friendly form -> override.json, plus
           "CONVERSIONS RUNNING" live job list
  right -- "READY PROPS" (published, spawn-name first) and
           "PROBLEMS" (quarantine, human-readable, dismissible)

All data comes from the filesystem the orchestrator owns; job execution
shells out to `orchestrator.py --file`.
"""
import argparse
import ast
import datetime
import re
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(ROOT, "inbox")
QUAR = os.path.join(ROOT, "quarantine")
BUILD = os.path.join(ROOT, "build")
OUT_RES = os.path.join(BUILD, "resources", "[sketchfab_props]")
REGISTRY = os.path.join(BUILD, "asset_registry.json")
DISMISSED = os.path.join(BUILD, "dashboard_dismissed.json")
ORCH = os.path.join(ROOT, "orchestrator.py")
WORKER = os.path.join(ROOT, "blender_worker.py")
WORK_DIR = os.path.join(ROOT, "work")
DONOR_CFG = os.path.join(ROOT, "donor", "donor.json")
PREVIEWS = {}   # job id -> {"json": path, "source": path}
SF_API = "https://api.sketchfab.com/v3"
SF_TOKEN_FILE = os.path.join(BUILD, "sketchfab_token.json")
SF_OAUTH_FILE = os.path.join(BUILD, "sketchfab_oauth.json")
SF_STATE = [""]   # pending oauth nonce


def _parse_fixture_names_from_harness():
    """Resources the test harness publishes are gate-test fixtures, not usable
    props. The harness keeps its own name list in the CLEAN tuple -- parse it
    (ast.literal_eval only) so fixtures are flagged without duplicating names."""
    path = os.path.join(ROOT, "test_harness.py")
    try:
        content = open(path, encoding="utf-8").read()
        m = re.search(r"CLEAN\s*=\s*(\([^)]*\))", content, re.DOTALL)
        if not m:
            print("[dash] WARN: CLEAN tuple not found in test_harness.py")
            return set()
        parsed = ast.literal_eval(m.group(1))
        if isinstance(parsed, tuple):
            return {str(n) for n in parsed}
        print("[dash] WARN: CLEAN did not parse to a tuple")
    except (OSError, SyntaxError, ValueError) as e:
        print(f"[dash] WARN: could not parse fixture names: {e}")
    return set()


FIXTURE_NAMES = _parse_fixture_names_from_harness()

JOBS = {}          # id -> {status, cmd, started, tail}
JOBS_LOCK = threading.Lock()
JOB_SEQ = [0]

# reason code -> (headline, suggestion). Codes may carry ':detail' suffixes;
# the map is matched longest-prefix-first.
REASONS = [
    ("ERR_GATE4_NAME_COLLISION",
     "Name already taken",
     "That prop name is registered to a different source file. Pick another name."),
    ("ERR_GATE4_ARCHETYPE_COLLISION",
     "Internal name clash",
     "A part or pose name inside this model collides with a published prop. Rename or remap parts."),
    ("ERR_GATE3_DIMS_OUT_OF_RANGE",
     "Wrong size",
     "Finished prop must be within [0.02, 80.0] m. Set 'make it this tall' on the form or rescale the model."),
    ("ERR_GATE2_YTYP_INVALID",
     "Bad archetype file",
     "The .ytyp describing the prop to the game didn't validate. See raw report."),
    ("ERR_GATE2_YDD_INVALID",
     "Bad clothing file",
     "The .ydd drawable dictionary didn't validate - raw report has the detail."),
    ("ERR_GATE1_YTD",
     "Texture dictionary missing",
     "Separate-texture mode was selected but no valid .ytd was written. Try embedded textures."),
    ("ERR_GATE1_YDR",
     "Export produced no model",
     "Blender ran but no valid drawable came out. Open the raw report for the worker error."),
    ("ERR_GATE1_RSC7_INVALID",
     "Bad output file",
     "An exported file failed the RSC7 format check. Raw report names the file."),
    ("ERR_GATE1_RSC_VERSION_MISMATCH",
     "Wrong game generation",
     "The exported file's version doesn't match the requested GTA generation. Toolchain limitation - see report."),
    ("ERR_TARGET_GENERATION_UNSUPPORTED",
     "Generation not supported",
     "That GTA generation can't be produced by the installed toolchain (or isn't allowed for this asset type)."),
    ("ERR_TOOLCHAIN_INCOMPATIBLE",
     "Converter not ready",
     "The Blender/Sollumz toolchain failed its capability probe. Check Blender is installed and Sollumz is enabled."),
    ("ERR_PED_NO_ARMATURE", "No skeleton",
     "Ped clothing needs a rigged model (armature + skinned mesh). This file has none."),
    ("ERR_PED_MULTI_ARMATURE", "Multiple skeletons",
     "Ped clothing supports exactly one armature. Combine the rigs in the source file."),
    ("ERR_PED_NO_WEIGHTS", "No skin weights",
     "The mesh isn't weighted to the skeleton. Paint skin weights in the source or set require_weights:false."),
    ("ERR_PED_SKELETON_MISMATCH", "Weight/bone mismatch",
     "A vertex group names a bone that doesn't exist in the skeleton. Rename it to match."),
    ("ERR_PED_TRANSFORM_MISMATCH", "Rig/mesh misaligned",
     "Exported skeleton rest positions don't line up with the mesh. Raw report has details."),
    ("ERR_PED_CLOTH_PHYSICS_UNSUPPORTED", "Cloth unsupported",
     "Cloth physics (.yld) can't be authored by this toolchain - bake it or drop it in the source."),
    ("ERR_PED_CLOTH_EXPORT_UNSUPPORTED", "Clothing export unavailable",
     "The installed Sollumz lacks verified drawable-dictionary/skinned export for this job."),
    ("ERR_PED_APPAREL_YMT_UNSUPPORTED", "YMT not writable",
     "A working apparel pack needs a .ymt this toolchain can't produce. The .ydd still converted."),
    ("ERR_APPAREL_CONFIG_INVALID", "Bad apparel config",
     "apparel_config needs ped, dlc, and exactly one component - see README for the schema."),
    ("ERR_ASSET_TYPE_CONFLICT", "Conflicting options",
     "The asset_type and motion_mode settings contradict each other. Fix the override."),
    ("ERR_UNKNOWN_ASSET_TYPE", "Bad option", "asset_type must be prop or ped."),
    ("ERR_COLLISION_MODE_UNSUPPORTED", "Collision not supported",
     "That collision mode can't be produced for this asset type (embedded bounds only - props can use 'none')."),
    ("ERR_ZIP_TRAVERSAL", "Unsafe archive",
     "The .zip contains paths that escape the work folder. Re-download from the source."),
    ("ERR_ZIP_NESTED_ARCHIVE", "Nested archive",
     "The .zip contains another archive - extract it yourself and drop the inner files."),
    ("ERR_ZIP_TOO_MANY_FILES", "Archive too big",
     "The .zip has more members than the safety cap allows."),
    ("ERR_ZIP_TOO_LARGE", "Archive too big",
     "The .zip expands beyond the safety cap."),
    ("ERR_MALFORMED_SIDECAR", "Bad override.json",
     "The override.json next to the file isn't valid JSON. Fix or delete it."),
    ("ERR_MALFORMED_SOURCE", "Corrupt model file",
     "The file's contents don't match its extension. Re-download it."),
    ("ERR_LUA_SYNTAX", "Bad generated script",
     "A generated .lua manifest failed a syntax check. Raw report has the error."),
    ("ERR_MANIFEST_UNRESOLVED",
     "Broken package",
     "The resource manifest points at files that weren't written. Raw report has the missing list."),
    ("ERR_ARMATURE_DEFORM_REQUIRES_SNAPSHOT_MODE",
     "Skeleton-driven motion",
     "This model moves via bones, not hinges. Use 'Animated (flipbook poses)' instead of moving parts."),
    ("ERR_MOTION_MODE_REQUIRES_VEHICLE_RIG",
     "Needs manual rigging",
     "That motion type needs a vehicle skeleton (.yft/.ycd) - the pipeline won't fake it headless. Try flipbook or moving parts."),
    ("ERR_TOO_MANY_SAMPLES", "Too many poses",
     "Snapshot mode is capped at 12 poses - cut the sample count."),
    ("ERR_FRAME_OUT_OF_BOUNDS", "Bad animation range",
     "A sampled frame is outside the animation's range. Check the timeline in the source file."),
    ("ERR_FRAME_WINDOW_EMPTY", "Bad animation range",
     "The configured frame window resolved to nothing. Check start/end frames."),
    ("ERR_NO_GEOMETRY_AT_FRAME", "Empty animation frame",
     "The model evaluated to no geometry at a sampled frame. Adjust the sample range."),
    ("ERR_UNMAPPED_PART", "Parts couldn't be mapped",
     "The part_map lists meshes that don't exist in the model. Check mesh names."),
    ("ERR_TOO_MANY_PARTS", "Too many moving parts",
     "More moving meshes than the cap allows without an explicit part_map."),
    ("ERR_NO_STATIC_BODY", "Nothing stays still",
     "Every mesh moves - a multi-part prop needs at least one static piece to anchor to."),
    ("ERR_SPIN_AXIS_UNRESOLVED", "Spin axis unknown",
     "A rotor tagged for spinning has no detectable or configured spin_axis. Set spin_axis in the part_map entry."),
    ("ERR_SPIN_RPM_UNRESOLVED", "Spin speed unknown",
     "A rotor tagged for spinning has no detectable or configured spin_rpm. Set spin_rpm in the part_map entry."),
    ("ERR_KEEP_ACTIONS_NO_MATCH", "No matching clips",
     "keep_actions matched nothing in the source. Check the clip names (the raw report lists what was found)."),
    ("ERR_PREFLIGHT_MISSING_TEXTURE", "Missing texture file",
     "A shader references an image that isn't on disk after conversion. Re-export the model with textures."),
    ("ERR_PREFLIGHT_NO_SHADER_MAT", "Unusable material",
     "A material couldn't be converted to a game shader. Check the source's material setup."),
    ("ERR_PREFLIGHT_NO_UV", "Missing UVs",
     "A mesh has no UV layer, so textures can't land on it. Unwrap it in Blender first."),
    ("ERR_PREFLIGHT_EMPTY_MESH", "Empty mesh",
     "A mesh in the model has no vertices/faces. Clean the source file."),
    ("ERR_PREFLIGHT_EMPTY_BOUNDS", "Degenerate bounds",
     "The drawable's bounding volume collapsed to nothing."),
    ("ERR_PREFLIGHT_BAD_NAME", "Bad prop name",
     "Names must be lowercase letters, numbers and underscores, 3-48 chars."),
    ("ERR_POLYCOUNT_EXCEEDS_HARD_CAP", "Too many polygons",
     "Model is over the triangle budget. Decimate it or pick a lighter source."),
    ("ERR_TEXTURE_MODE_YTD_FAILED", "Texture dictionary failed",
     "Building the separate .ytd failed. Retry with embedded textures."),
    ("ERR_UNSUPPORTED_EXT", "Unsupported file type",
     "Needs .glb, .gltf, .fbx or .obj."),
    ("ERR_NO_SUPPORTED_3D_FILE", "No usable model file",
     "The drop contained no .glb/.gltf/.fbx/.obj - re-download from Sketchfab."),
    ("ERR_BAD_ZIP", "Corrupt archive", "The .zip couldn't be unpacked. Re-download it."),
    ("ERR_BLENDER_TIMEOUT", "Conversion timed out",
     "Blender exceeded the time limit - try a smaller model."),
    ("ERR_NO_DRAWABLE_CREATED", "Export failed",
     "Sollumz produced no drawable object. Raw report has the addon log."),
    ("ERR_EXPORT_ARTIFACT_MISSING", "Export failed",
     "An expected output file never hit disk. Raw report has the addon log."),
    ("ERR_UNKNOWN_MOTION_MODE", "Bad option", "Unknown motion mode in override."),
    ("ERR_UNKNOWN_TEXTURE_MODE", "Bad option", "texture_mode must be embed or ytd."),
    ("ERR_TOO_MANY_SUBPROPS", "Too many sub-objects",
     "Static split produced more drawables than the cap. Join loose meshes in the source."),
    ("ERR_NO_MESH_AFTER_CLEANUP", "Nothing to convert",
     "No mesh survived import/cleanup - the file may be empty or camera/light only."),
    ("ERR_WORKER", "Conversion failed",
     "The Blender worker crashed before writing a report. Raw report has stderr."),
]


def human_reason(code):
    code = (code or "ERR_UNKNOWN").split(":")[0]
    for prefix, head, sug in REASONS:
        if code == prefix or code.startswith(prefix):
            return head, sug
    return "Conversion failed", "Something broke in the worker - open the raw report."


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SIGNAL // prop converter</title>
<style>
@layer reset, base, theme, components, utilities, cyberpunk-ui;
@layer reset, base, theme, components, utilities;@layer theme{:root{--cyber-cyan-300: #87eaf2;--cyber-cyan-400: #54d1db;--cyber-cyan-500: #00f0ff;--cyber-cyan-600: #00c4cc;--cyber-cyan-700: #0097a7;--cyber-cyan-900: #005d6a;--cyber-magenta-500: #ff2a6d;--cyber-magenta-700: #b31248;--cyber-yellow-500: #fcee0a;--cyber-yellow-700: #aca406;--cyber-green-400: #43ff83;--cyber-green-500: #05ffa1;--cyber-green-700: #03a969;--cyber-green-900: #015331;--cyber-void-100: #2a2d3a;--cyber-void-200: #1f2230;--cyber-void-300: #181a25;--cyber-void-400: #12141d;--cyber-void-500: #0d0e14;--cyber-void-700: #07080c;--cyber-void-800: #040508;--cyber-void-900: #010204;--cyber-chrome-100: #f0f1f5;--cyber-chrome-200: #d1d4de;--cyber-chrome-300: #b2b7c7;--cyber-chrome-400: #939ab0;--cyber-chrome-500: #747d99;--cyber-chrome-600: #5c647a;--color-bg-primary: var(--cyber-void-500);--color-bg-secondary: var(--cyber-void-400);--color-bg-tertiary: var(--cyber-void-300);--color-bg-elevated: var(--cyber-void-200);--color-text-primary: var(--cyber-chrome-100);--color-text-secondary: var(--cyber-chrome-300);--color-text-muted: var(--cyber-chrome-500);--color-border-default: var(--cyber-void-100);--glow-cyan: 0 0 20px var(--cyber-cyan-500), 0 0 40px color-mix(in srgb, var(--cyber-cyan-500) 50%, transparent), 0 0 80px color-mix(in srgb, var(--cyber-cyan-500) 25%, transparent);--glow-magenta: 0 0 20px var(--cyber-magenta-500), 0 0 40px color-mix(in srgb, var(--cyber-magenta-500) 50%, transparent);--glow-yellow: 0 0 20px var(--cyber-yellow-500), 0 0 40px color-mix(in srgb, var(--cyber-yellow-500) 50%, transparent);--glow-green: 0 0 20px var(--cyber-green-500), 0 0 40px color-mix(in srgb, var(--cyber-green-500) 50%, transparent);--glow-text-cyan: 0 0 10px var(--cyber-cyan-500), 0 0 20px color-mix(in srgb, var(--cyber-cyan-500) 60%, transparent);--font-display: "Rajdhani", "Orbitron", system-ui, sans-serif;--font-body: "Exo 2", system-ui, sans-serif;--font-mono: "JetBrains Mono", monospace;--text-xs: clamp(0.64rem, 0.59rem + 0.24vw, 0.75rem);--text-sm: clamp(0.8rem, 0.74rem + 0.32vw, 0.94rem);--text-base: clamp(1rem, 0.93rem + 0.37vw, 1.18rem);--text-lg: clamp(1.25rem, 1.16rem + 0.47vw, 1.47rem);--text-xl: clamp(1.56rem, 1.45rem + 0.59vw, 1.84rem);--text-2xl: clamp(1.95rem, 1.81rem + 0.74vw, 2.3rem);--text-3xl: clamp(2.44rem, 2.26rem + 0.92vw, 2.87rem);--text-4xl: clamp(3.05rem, 2.83rem + 1.15vw, 3.58rem);--text-5xl: clamp(3.81rem, 3.54rem + 1.44vw, 4.48rem);--tracking-wide: 0.05em;--tracking-wider: 0.1em;--space-3xs: clamp(0.25rem, 0.23rem + 0.09vw, 0.31rem);--space-2xs: clamp(0.5rem, 0.46rem + 0.18vw, 0.63rem);--space-xs: clamp(0.75rem, 0.7rem + 0.28vw, 0.94rem);--space-sm: clamp(1rem, 0.93rem + 0.37vw, 1.25rem);--space-md: clamp(1.5rem, 1.39rem + 0.56vw, 1.88rem);--space-lg: clamp(2rem, 1.86rem + 0.74vw, 2.5rem);--space-xl: clamp(3rem, 2.78rem + 1.11vw, 3.75rem);--space-2xl: clamp(4rem, 3.71rem + 1.48vw, 5rem);--space-3xl: clamp(6rem, 5.57rem + 2.22vw, 7.5rem);--radius-sm: 2px;--radius-md: 4px;--radius-lg: 8px;--radius-full: 9999px;--radius-cyber: 0 12px 0 12px;--border-thin: 1px;--border-medium: 2px;--border-thick: 3px;--ease-cyber: cubic-bezier(0.77, 0, 0.175, 1);--ease-bounce: cubic-bezier(0.68, -0.55, 0.265, 1.55);--ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);--duration-fast: 150ms;--duration-normal: 300ms;--duration-slow: 500ms;--transition-fast: var(--duration-fast) ease;--transition-base: var(--duration-normal) ease;--transition-slow: var(--duration-slow) ease;--z-sticky: 100;--z-modal: 1000;--noise-opacity: 0.03;--scanline-opacity: 0.04}}@layer reset{*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}html{font-size:16px;color-scheme:dark;-webkit-font-smoothing:antialiased;scroll-behavior:smooth}@media(prefers-reduced-motion: reduce){html{scroll-behavior:auto}}}@layer base{body{min-height:100dvh;overflow-x:hidden;font-family:var(--font-body);font-size:var(--text-base);line-height:1.6;color:var(--color-text-primary);background-color:var(--color-bg-primary)}code{padding:.125em .375em;border:1px solid var(--color-border-default);border-radius:var(--radius-sm);font-family:var(--font-mono);font-size:.9em;color:var(--cyber-cyan-400);background:var(--color-bg-tertiary)}}@layer base{h1,h2,h3,h4,h5,h6{font-family:var(--font-display);font-weight:700;line-height:1.1;letter-spacing:.02em;text-transform:uppercase}h1{font-size:var(--text-5xl)}h2{font-size:var(--text-4xl)}h3{font-size:var(--text-3xl)}h4{font-size:var(--text-2xl)}h5{font-size:var(--text-xl)}h6{font-size:var(--text-lg)}}@layer components{.cyber-btn{--btn-color: var(--cyber-cyan-500);--btn-bg: transparent;--btn-glow: var(--glow-cyan);position:relative;display:inline-flex;gap:var(--space-xs);align-items:center;justify-content:center;padding:var(--space-xs) var(--space-md);border:var(--border-medium) solid var(--btn-color);border-radius:var(--radius-cyber);overflow:hidden;font-family:var(--font-display);font-size:var(--text-sm);font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--btn-color);background:var(--btn-bg);transition:color var(--duration-fast) var(--ease-cyber),background-color var(--duration-fast) var(--ease-cyber),border-color var(--duration-fast) var(--ease-cyber),box-shadow var(--duration-fast) var(--ease-cyber);cursor:pointer;isolation:isolate}.cyber-btn::before{content:"";position:absolute;z-index:-1;background:var(--btn-color);transform:translateX(-101%);transition:transform var(--duration-normal) var(--ease-cyber);inset:0}.cyber-btn:hover::before,.cyber-btn:focus-visible::before{transform:translateX(0)}.cyber-btn:hover,.cyber-btn:focus-visible{color:var(--cyber-void-500);box-shadow:var(--btn-glow)}.cyber-btn::after{content:"";position:absolute;right:-2px;bottom:-2px;width:12px;height:12px;border:var(--border-medium) solid var(--btn-color);border-top:none;border-left:none;background:var(--color-bg-primary);clip-path:polygon(100% 0, 100% 100%, 0 100%)}.cyber-btn:focus-visible{outline:2px solid var(--btn-color);outline-offset:4px}.cyber-btn:disabled,.cyber-btn[disabled]{opacity:40%;cursor:not-allowed;pointer-events:none;filter:grayscale(50%)}@media(hover: none){.cyber-btn::before{display:none}.cyber-btn:hover{color:var(--btn-color);box-shadow:none}.cyber-btn:active{color:var(--cyber-void-500);background:var(--btn-color);box-shadow:var(--btn-glow)}}.cyber-btn--magenta{--btn-color: var(--cyber-magenta-500);--btn-glow: var(--glow-magenta)}.cyber-btn--yellow{--btn-color: var(--cyber-yellow-500);--btn-glow: var(--glow-yellow)}.cyber-btn--green{--btn-color: var(--cyber-green-500);--btn-glow: var(--glow-green)}.cyber-btn--ghost{border-style:dashed}.cyber-btn--filled{color:var(--cyber-void-500);background:var(--btn-color)}.cyber-btn--filled::before{background:var(--cyber-void-500)}.cyber-btn--filled:hover{color:var(--btn-color)}.cyber-btn--sm{padding:var(--space-3xs) var(--space-sm);font-size:var(--text-xs)}.cyber-btn--lg{padding:var(--space-sm) var(--space-lg);font-size:var(--text-lg)}@media(prefers-reduced-motion: reduce){.cyber-btn::before{transition:none}}}@layer components{.cyber-card{--card-accent: var(--cyber-cyan-500);position:relative;padding:var(--space-md);border:1px solid var(--color-border-default);background:var(--color-bg-secondary);clip-path:polygon(0 0, calc(100% - 20px) 0, 100% 20px, 100% 100%, 20px 100%, 0 calc(100% - 20px))}.cyber-card::before,.cyber-card::after{content:"";position:absolute;width:30px;height:30px;border:2px solid var(--card-accent);pointer-events:none}.cyber-card::before{top:-1px;left:-1px;border-right:none;border-bottom:none}.cyber-card::after{right:-1px;bottom:-1px;border-top:none;border-left:none}.cyber-card__header{display:flex;gap:var(--space-sm);align-items:center;margin-bottom:var(--space-md);padding-bottom:var(--space-sm);border-bottom:1px solid var(--color-border-default)}.cyber-card__icon{display:flex;align-items:center;justify-content:center;width:40px;height:40px;border:1px solid var(--card-accent);font-size:var(--text-lg);color:var(--card-accent);background:color-mix(in srgb, var(--card-accent) 15%, transparent)}.cyber-card__title{font-family:var(--font-display);font-size:var(--text-lg);letter-spacing:.05em;text-transform:uppercase;color:var(--card-accent)}.cyber-card__badge{margin-left:auto;padding:var(--space-3xs) var(--space-xs);font-family:var(--font-mono);font-size:var(--text-xs);font-weight:700;color:var(--cyber-void-500);background:var(--card-accent)}.cyber-card--interactive{transition:all var(--duration-normal);cursor:pointer}.cyber-card--interactive:hover{box-shadow:inset 0 0 30px color-mix(in srgb, var(--card-accent) 10%, transparent),0 0 30px color-mix(in srgb, var(--card-accent) 30%, transparent);transform:translateY(-2px)}.cyber-card--holo{background:linear-gradient(135deg, var(--color-bg-secondary) 0%, color-mix(in srgb, var(--cyber-cyan-500) 5%, var(--color-bg-secondary)) 25%, color-mix(in srgb, var(--cyber-magenta-500) 5%, var(--color-bg-secondary)) 50%, color-mix(in srgb, var(--cyber-green-500) 5%, var(--color-bg-secondary)) 75%, var(--color-bg-secondary) 100%);background-size:400% 400%;animation:holo-shift 8s ease infinite}.cyber-card--magenta{--card-accent: var(--cyber-magenta-500)}.cyber-card--yellow{--card-accent: var(--cyber-yellow-500)}.cyber-card--green{--card-accent: var(--cyber-green-500)}@keyframes holo-shift{0%,100%{background-position:0% 50%}25%{background-position:100% 50%}50%{background-position:100% 100%}75%{background-position:0% 100%}}@media(prefers-reduced-motion: reduce){.cyber-card--holo{animation:none}.cyber-card--interactive{transition:none}.cyber-card--interactive:hover{transform:none}}}@layer components{.cyber-input{--input-accent: var(--cyber-cyan-500);display:block;width:100%;padding:var(--space-xs) var(--space-sm);border:1px solid var(--color-border-default);border-left:3px solid var(--input-accent);font-family:var(--font-mono);font-size:var(--text-base);color:var(--color-text-primary);background:var(--color-bg-tertiary);transition:all var(--duration-fast);outline:none}.cyber-input::-moz-placeholder{color:var(--color-text-muted)}.cyber-input::placeholder{color:var(--color-text-muted)}.cyber-input:focus{border-color:var(--input-accent);box-shadow:0 0 0 3px color-mix(in srgb, var(--input-accent) 20%, transparent),inset 0 0 15px color-mix(in srgb, var(--input-accent) 10%, transparent)}.cyber-field{margin-bottom:var(--space-md)}.cyber-field__label{display:block;margin-bottom:var(--space-2xs);font-family:var(--font-display);font-size:var(--text-xs);font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--color-text-secondary)}.cyber-field__label::before{content:"> ";color:var(--cyber-cyan-500)}.cyber-textarea{display:block;width:100%;min-height:120px;padding:var(--space-sm);border:1px solid var(--color-border-default);border-left:3px solid var(--cyber-cyan-500);border-radius:var(--radius-sm);font-family:var(--font-mono);font-size:var(--text-sm);line-height:1.6;color:var(--color-text-primary);background:var(--color-bg-tertiary);transition:all var(--duration-fast);outline:none;resize:vertical}.cyber-textarea::-moz-placeholder{color:var(--color-text-muted)}.cyber-textarea::placeholder{color:var(--color-text-muted)}.cyber-textarea:focus{border-color:var(--cyber-cyan-500);box-shadow:0 0 0 3px color-mix(in srgb, var(--cyber-cyan-500) 20%, transparent),inset 0 0 15px color-mix(in srgb, var(--cyber-cyan-500) 10%, transparent)}.cyber-select{display:block;width:100%;padding:var(--space-xs) var(--space-lg) var(--space-xs) var(--space-sm);border:1px solid var(--color-border-default);border-left:3px solid var(--cyber-magenta-500);font-family:var(--font-display);font-size:var(--text-sm);font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--color-text-primary);background:var(--color-bg-tertiary);background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23ff2a6d' d='M2 4l4 4 4-4'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right var(--space-sm) center;transition:all var(--duration-fast);cursor:pointer;-webkit-appearance:none;-moz-appearance:none;appearance:none;outline:none}.cyber-select:focus{border-color:var(--cyber-magenta-500);box-shadow:0 0 0 3px color-mix(in srgb, var(--cyber-magenta-500) 20%, transparent),inset 0 0 15px color-mix(in srgb, var(--cyber-magenta-500) 10%, transparent)}.cyber-select option{padding:var(--space-xs);color:var(--color-text-primary);background:var(--color-bg-secondary)}.cyber-checkbox{display:inline-flex;gap:var(--space-xs);align-items:center;cursor:pointer;-webkit-user-select:none;-moz-user-select:none;user-select:none}.cyber-checkbox input{position:absolute;width:0;height:0;opacity:0%}.cyber-checkbox__box{position:relative;display:flex;align-items:center;justify-content:center;width:20px;height:20px;border:2px solid var(--cyber-cyan-500);background:var(--color-bg-tertiary);transition:all var(--duration-fast)}.cyber-checkbox__box::before{content:"";width:10px;height:10px;background:var(--cyber-cyan-500);transform:scale(0);transition:transform var(--duration-fast) var(--ease-bounce)}input:checked~.cyber-checkbox__box{box-shadow:0 0 10px var(--cyber-cyan-500)}input:checked~.cyber-checkbox__box::before{transform:scale(1)}.cyber-checkbox__label{font-family:var(--font-display);font-size:var(--text-sm);letter-spacing:.05em;text-transform:uppercase}}@layer components{.cyber-progress{--progress-value: 0%;--progress-color: var(--cyber-cyan-500);position:relative;height:10px;border:1px solid var(--color-border-default);overflow:hidden;background:var(--color-bg-tertiary)}.cyber-progress__bar{position:relative;width:var(--progress-value);height:100%;background:linear-gradient(90deg, var(--progress-color), color-mix(in srgb, var(--progress-color) 70%, white));box-shadow:0 0 15px var(--progress-color);transition:width var(--duration-slow) var(--ease-cyber)}.cyber-progress__bar::before{content:"";position:absolute;background:repeating-linear-gradient(-45deg, transparent, transparent 5px, rgba(255, 255, 255, 0.15) 5px, rgba(255, 255, 255, 0.15) 10px);animation:progress-stripes .5s linear infinite;inset:0}.cyber-progress__value{position:absolute;top:50%;right:var(--space-xs);z-index:1;font-family:var(--font-mono);font-size:var(--text-xs);color:var(--color-text-primary);transform:translateY(-50%);text-shadow:0 1px 2px var(--cyber-void-500)}.cyber-progress--magenta{--progress-color: var(--cyber-magenta-500)}.cyber-progress--yellow{--progress-color: var(--cyber-yellow-500)}.cyber-progress--green{--progress-color: var(--cyber-green-500)}.cyber-progress--sm{height:6px}.cyber-progress--sm .cyber-progress__value{display:none}.cyber-progress--lg{height:16px}.cyber-progress--lg .cyber-progress__value{font-size:var(--text-sm)}.cyber-progress--no-animation .cyber-progress__bar::before{animation:none}.cyber-progress--indeterminate .cyber-progress__bar{position:absolute;left:0;width:30%;animation:progress-indeterminate 1.5s ease-in-out infinite}.cyber-progress--segmented{display:flex;gap:2px;border:none;background:rgba(0,0,0,0)}.cyber-progress--segmented .cyber-progress__segment{flex:1;height:100%;border:var(--border-thin) solid var(--color-border-default);background:var(--color-bg-tertiary);transition:background var(--transition-fast)}.cyber-progress--segmented .cyber-progress__segment--active{border-color:var(--progress-color);background:var(--progress-color)}@keyframes progress-stripes{from{background-position:0 0}to{background-position:20px 0}}@keyframes progress-indeterminate{0%{transform:translateX(-100%)}100%{transform:translateX(433%)}}.cyber-progress-circle{--progress-value: 0;--progress-size: 80px;--progress-stroke: 6px;--progress-color: var(--cyber-cyan-500);position:relative;width:var(--progress-size);height:var(--progress-size)}.cyber-progress-circle__svg{width:100%;height:100%;transform:rotate(-90deg)}.cyber-progress-circle__track{fill:none;stroke:var(--color-bg-tertiary);stroke-width:var(--progress-stroke)}.cyber-progress-circle__bar{transition:stroke-dashoffset var(--transition-slow) var(--ease-out-expo);fill:none;filter:drop-shadow(0 0 4px var(--progress-color));stroke:var(--progress-color);stroke-dasharray:251.2;stroke-dashoffset:calc(251.2 - 251.2*var(--progress-value)/100);stroke-linecap:round;stroke-width:var(--progress-stroke)}.cyber-progress-circle__value{position:absolute;top:50%;left:50%;font-family:var(--font-mono);font-size:var(--text-sm);font-weight:700;color:var(--color-text-primary);transform:translate(-50%, -50%)}.cyber-progress-circle--magenta{--progress-color: var(--cyber-magenta-500)}.cyber-progress-circle--yellow{--progress-color: var(--cyber-yellow-500)}.cyber-progress-circle--green{--progress-color: var(--cyber-green-500)}}@layer components{.cyber-badge{display:inline-flex;gap:var(--space-3xs);align-items:center;padding:var(--space-3xs) var(--space-sm);border:var(--border-thin) solid var(--cyber-cyan-500);border-radius:var(--radius-full);font-family:var(--font-display);font-size:var(--text-xs);font-weight:700;letter-spacing:.05em;text-transform:uppercase;white-space:nowrap;color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 20%, transparent);transition:background var(--transition-fast),box-shadow var(--transition-fast)}.cyber-badge svg,.cyber-badge__icon{flex-shrink:0;width:.875em;height:.875em}.cyber-badge--magenta{border-color:var(--cyber-magenta-500);color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 20%, transparent)}.cyber-badge--yellow{border-color:var(--cyber-yellow-500);color:var(--cyber-yellow-500);background:color-mix(in srgb, var(--cyber-yellow-500) 20%, transparent)}.cyber-badge--green{border-color:var(--cyber-green-500);color:var(--cyber-green-500);background:color-mix(in srgb, var(--cyber-green-500) 20%, transparent)}.cyber-badge--chrome{border-color:var(--cyber-chrome-500);color:var(--cyber-chrome-300);background:color-mix(in srgb, var(--cyber-chrome-500) 20%, transparent)}.cyber-badge--filled{border-color:var(--cyber-cyan-500);color:var(--cyber-void-500);background:var(--cyber-cyan-500)}.cyber-badge--filled.cyber-badge--magenta{border-color:var(--cyber-magenta-500);background:var(--cyber-magenta-500)}.cyber-badge--filled.cyber-badge--yellow{border-color:var(--cyber-yellow-500);background:var(--cyber-yellow-500)}.cyber-badge--filled.cyber-badge--green{border-color:var(--cyber-green-500);background:var(--cyber-green-500)}.cyber-badge--ghost{border-color:rgba(0,0,0,0);background:rgba(0,0,0,0)}.cyber-badge--ghost:hover{background:color-mix(in srgb, currentcolor 10%, transparent)}.cyber-badge--pulse{animation:badge-pulse 2s ease-in-out infinite}.cyber-badge--sm{padding:2px var(--space-2xs);font-size:.625rem}.cyber-badge--lg{padding:var(--space-2xs) var(--space-sm);font-size:var(--text-sm)}.cyber-badge--counter{justify-content:center;min-width:1.5rem;height:1.5rem;padding:0;border-radius:50%}.cyber-badge--online{border-color:var(--cyber-green-500);color:var(--cyber-green-500);background:color-mix(in srgb, var(--cyber-green-500) 20%, transparent)}.cyber-badge--online::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--cyber-green-500);animation:badge-pulse 2s ease-in-out infinite}.cyber-badge--offline{border-color:var(--cyber-chrome-500);color:var(--cyber-chrome-500);background:color-mix(in srgb, var(--cyber-chrome-500) 20%, transparent)}.cyber-badge--offline::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--cyber-chrome-500)}.cyber-badge--busy{border-color:var(--cyber-magenta-500);color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 20%, transparent)}.cyber-badge--busy::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--cyber-magenta-500);animation:badge-pulse 1s ease-in-out infinite}@keyframes badge-pulse{0%,100%{box-shadow:0 0 0 0 currentcolor}50%{box-shadow:0 0 0 6px rgba(0,0,0,0)}}.cyber-badge-group{display:inline-flex;flex-wrap:wrap;gap:var(--space-2xs)}}@layer components{.cyber-alert{--alert-color: var(--cyber-cyan-500);position:relative;padding:var(--space-md);padding-left:var(--space-xl);border:var(--border-thin) solid var(--alert-color);border-left:4px solid var(--alert-color);background:color-mix(in srgb, var(--alert-color) 10%, var(--color-bg-secondary))}.cyber-alert::before{content:"!";position:absolute;top:50%;left:var(--space-sm);display:flex;align-items:center;justify-content:center;width:24px;height:24px;font-family:var(--font-display);font-size:var(--text-sm);font-weight:700;line-height:1;color:var(--cyber-void-500);background:var(--alert-color);transform:translateY(-50%)}.cyber-alert__title{margin-bottom:var(--space-2xs);font-family:var(--font-display);font-size:var(--text-sm);font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--alert-color)}.cyber-alert--warning{--alert-color: var(--cyber-yellow-500)}.cyber-alert--warning::before{content:"⚠"}.cyber-alert--error{--alert-color: var(--cyber-magenta-500)}.cyber-alert--error::before{content:"✕"}.cyber-alert--success{--alert-color: var(--cyber-green-500)}.cyber-alert--success::before{content:"✓"}}@layer components{.cyber-table{width:100%;font-family:var(--font-mono);font-size:var(--text-sm);border-collapse:collapse}.cyber-table thead{background:var(--color-bg-tertiary)}.cyber-table thead th{padding:var(--space-sm);border-bottom:2px solid var(--cyber-cyan-500);font-family:var(--font-display);font-weight:700;letter-spacing:.05em;text-align:left;text-transform:uppercase;color:var(--cyber-cyan-500)}.cyber-table thead th[data-sortable]{cursor:pointer;-webkit-user-select:none;-moz-user-select:none;user-select:none}.cyber-table thead th[data-sortable]::after{content:"";display:inline-block;width:0;height:0;margin-left:var(--space-xs);border-top:4px solid currentcolor;border-right:4px solid rgba(0,0,0,0);border-left:4px solid rgba(0,0,0,0);opacity:30%;transition:opacity var(--transition-fast);vertical-align:middle}.cyber-table thead th[data-sortable]:hover::after{opacity:70%}.cyber-table thead th[data-sortable][data-sort=asc]::after{border-top:none;border-bottom:4px solid currentcolor;opacity:100%}.cyber-table thead th[data-sortable][data-sort=desc]::after{opacity:100%}.cyber-table tbody tr{transition:background var(--duration-fast)}.cyber-table tbody tr:hover{background:color-mix(in srgb, var(--cyber-cyan-500) 5%, transparent)}.cyber-table tbody td{padding:var(--space-sm);border-bottom:1px solid var(--color-border-default)}.cyber-table tfoot{background:var(--color-bg-tertiary)}.cyber-table tfoot td{padding:var(--space-sm);border-top:1px solid var(--color-border-default);font-weight:600;color:var(--color-text-primary)}.cyber-table--striped tbody tr:nth-child(even){background:color-mix(in srgb, var(--cyber-void-500) 50%, transparent)}.cyber-table--striped tbody tr:nth-child(even):hover{background:color-mix(in srgb, var(--cyber-cyan-500) 8%, transparent)}.cyber-table--bordered th,.cyber-table--bordered td{border:var(--border-thin) solid var(--color-border-default)}.cyber-table--bordered thead th{border-bottom-color:var(--cyber-cyan-500);border-bottom-width:var(--border-medium)}.cyber-table--compact{font-size:var(--text-xs)}.cyber-table--compact thead th,.cyber-table--compact tbody td,.cyber-table--compact tfoot td{padding:var(--space-xs)}.cyber-table-wrapper{overflow-x:auto;-webkit-overflow-scrolling:touch}.cyber-table-wrapper::before,.cyber-table-wrapper::after{content:"";position:absolute;top:0;bottom:0;width:20px;opacity:0%;transition:opacity var(--transition-fast);pointer-events:none}.cyber-table-wrapper::before{left:0;background:linear-gradient(90deg, var(--color-bg-primary), transparent)}.cyber-table-wrapper::after{right:0;background:linear-gradient(-90deg, var(--color-bg-primary), transparent)}.cyber-table-wrapper[data-scroll-left]::before{opacity:100%}.cyber-table-wrapper[data-scroll-right]::after{opacity:100%}.cyber-table .cell-number{font-family:var(--font-mono);text-align:right}.cyber-table .cell-status{display:inline-flex;gap:var(--space-2xs);align-items:center}.cyber-table .cell-status::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--cyber-chrome-500)}.cyber-table .cell-status--active::before{background:var(--cyber-green-500);box-shadow:0 0 6px var(--cyber-green-500)}.cyber-table .cell-status--inactive::before{background:var(--cyber-chrome-600)}.cyber-table .cell-status--pending::before{background:var(--cyber-yellow-500);animation:status-pulse 1.5s ease-in-out infinite}.cyber-table .cell-status--error::before{background:var(--cyber-magenta-500)}.cyber-table .cell-actions{display:flex;gap:var(--space-2xs);justify-content:flex-end}.cyber-table tr.row-highlight{border-left:var(--border-thick) solid var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 10%, transparent)}.cyber-table tr.row-selected{background:color-mix(in srgb, var(--cyber-cyan-500) 15%, transparent)}@keyframes status-pulse{0%,100%{opacity:100%}50%{opacity:40%}}}@layer components{.cyber-nav{position:sticky;top:0;z-index:100;display:flex;gap:var(--space-md);align-items:center;padding:var(--space-sm) var(--space-lg);border-bottom:1px solid var(--color-border-default);background:var(--color-bg-secondary);backdrop-filter:blur(10px)}.cyber-nav__brand{display:flex;gap:var(--space-xs);align-items:center;font-family:Orbitron,var(--font-display);font-size:var(--text-xl);font-weight:800;letter-spacing:.15em;text-decoration:none;text-transform:uppercase;color:var(--cyber-cyan-500);text-shadow:var(--glow-text-cyan)}.cyber-nav__brand::before{content:"//";color:var(--cyber-magenta-500)}.cyber-nav__links{display:flex;gap:var(--space-2xs);margin-left:auto;list-style:none}.cyber-nav__link{position:relative;padding:var(--space-xs) var(--space-sm);overflow:hidden;font-family:var(--font-display);font-size:var(--text-sm);font-weight:600;letter-spacing:.05em;text-decoration:none;text-transform:uppercase;color:var(--color-text-secondary);transition:color var(--duration-fast)}.cyber-nav__link::before{content:"";position:absolute;bottom:0;left:0;width:100%;height:2px;background:var(--cyber-cyan-500);transform:translateX(-101%);transition:transform var(--duration-normal) var(--ease-cyber)}.cyber-nav__link:hover,.cyber-nav__link--active{color:var(--cyber-cyan-500)}.cyber-nav__link:hover::before,.cyber-nav__link--active::before{transform:translateX(0)}.cyber-status{display:flex;gap:var(--space-2xs);align-items:center;padding:var(--space-3xs) var(--space-xs);border:1px solid var(--cyber-green-500);font-family:var(--font-mono);font-size:var(--text-xs);color:var(--cyber-green-500);background:color-mix(in srgb, var(--cyber-green-500) 15%, transparent)}.cyber-status__dot{width:8px;height:8px;border-radius:50%;background:var(--cyber-green-500);animation:pulse-glow 2s infinite}@keyframes pulse-glow{0%,100%{box-shadow:0 0 0 0 var(--cyber-green-500)}50%{box-shadow:0 0 10px 3px var(--cyber-green-500)}}}@layer components{.cyber-tabs{display:flex;flex-wrap:wrap;gap:2px;padding:2px;background:var(--color-border-default)}.cyber-tabs__list{display:flex;flex-wrap:wrap;gap:2px;margin:0;padding:0;list-style:none}.cyber-tabs--scrollable{scrollbar-width:thin}.cyber-tabs--scrollable::-webkit-scrollbar{height:4px}.cyber-tabs--scrollable::-webkit-scrollbar-track{background:var(--color-bg-secondary)}.cyber-tabs--scrollable::-webkit-scrollbar-thumb{border-radius:2px;background:var(--cyber-chrome-600)}.cyber-tabs--scrollable{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}.cyber-tabs--scrollable .cyber-tabs__list{flex-wrap:nowrap}.cyber-tabs--scrollable .cyber-tab{flex:none;white-space:nowrap}@media(width <= 768px){.cyber-tabs{scrollbar-width:thin}.cyber-tabs::-webkit-scrollbar{height:4px}.cyber-tabs::-webkit-scrollbar-track{background:var(--color-bg-secondary)}.cyber-tabs::-webkit-scrollbar-thumb{border-radius:2px;background:var(--cyber-chrome-600)}.cyber-tabs{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}.cyber-tabs .cyber-tab{flex:none;white-space:nowrap}}.cyber-tabs__panels{padding:var(--space-md) 0}.cyber-tabs__panel{display:none;animation:tab-fade-in var(--transition-base) ease-out}.cyber-tabs__panel--active{display:block}.cyber-tabs--vertical{flex-direction:column;gap:var(--space-md);padding:0;background:rgba(0,0,0,0)}.cyber-tabs--vertical .cyber-tabs__list{flex-direction:column;gap:2px;padding:2px;background:var(--color-border-default)}.cyber-tabs--vertical .cyber-tab{flex:none}.cyber-tabs--vertical .cyber-tab--active::after{width:3px;height:100%;inset:0 0 0 auto}.cyber-tabs--vertical .cyber-tabs__panels{flex:1;padding:0}.cyber-tabs--pills{gap:var(--space-xs);padding:0;background:rgba(0,0,0,0)}.cyber-tabs--pills .cyber-tabs__list{gap:var(--space-xs);background:rgba(0,0,0,0)}.cyber-tabs--pills .cyber-tab{flex:none;border:var(--border-thin) solid var(--color-border-default);clip-path:polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px))}.cyber-tabs--pills .cyber-tab--active{border-color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 20%, transparent)}.cyber-tabs--pills .cyber-tab--active::after{display:none}.cyber-tabs--fullwidth .cyber-tabs__list{width:100%}.cyber-tabs--fullwidth .cyber-tab{flex:1 1 auto;justify-content:center;min-width:-moz-fit-content;min-width:fit-content}@media(width <= 768px){.cyber-tabs--fullwidth .cyber-tab{flex:none}}.cyber-tab{position:relative;display:inline-flex;flex:none;gap:var(--space-xs);align-items:center;justify-content:center;padding:var(--space-sm) var(--space-md);border:none;font-family:var(--font-display);font-size:var(--text-sm);font-weight:700;letter-spacing:.05em;text-decoration:none;text-transform:uppercase;white-space:nowrap;color:var(--color-text-secondary);background:var(--color-bg-secondary);transition:color var(--duration-fast),background-color var(--duration-fast);cursor:pointer}.cyber-tab:hover{color:var(--color-text-primary);background:var(--color-bg-tertiary)}.cyber-tab:focus-visible{outline:2px solid var(--cyber-cyan-500);outline-offset:-2px}@media(hover: none){.cyber-tab:hover{color:var(--color-text-secondary);background:var(--color-bg-secondary)}.cyber-tab:active{color:var(--color-text-primary);background:var(--color-bg-tertiary)}.cyber-tab--active:hover{color:var(--cyber-cyan-500);background:var(--color-bg-tertiary)}}.cyber-tab--active{color:var(--cyber-cyan-500);background:var(--color-bg-tertiary)}.cyber-tab--active::after{content:"";position:absolute;right:0;bottom:0;left:0;height:3px;background:var(--cyber-cyan-500);box-shadow:0 0 10px var(--cyber-cyan-500)}.cyber-tab:disabled,.cyber-tab--disabled{opacity:50%;cursor:not-allowed;pointer-events:none}.cyber-tab svg,.cyber-tab__icon{flex-shrink:0;width:1em;height:1em}.cyber-tab__badge{padding:2px 6px;border:var(--border-thin) solid var(--cyber-cyan-500);font-family:var(--font-mono);font-size:var(--text-xs);color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 20%, transparent)}.cyber-tab--magenta.cyber-tab--active{color:var(--cyber-magenta-500)}.cyber-tab--magenta.cyber-tab--active::after{background:var(--cyber-magenta-500);box-shadow:0 0 10px var(--cyber-magenta-500)}.cyber-tab--magenta .cyber-tab__badge{border-color:var(--cyber-magenta-500);color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 20%, transparent)}.cyber-tab--yellow.cyber-tab--active{color:var(--cyber-yellow-500)}.cyber-tab--yellow.cyber-tab--active::after{background:var(--cyber-yellow-500);box-shadow:0 0 10px var(--cyber-yellow-500)}.cyber-tab--green.cyber-tab--active{color:var(--cyber-green-500)}.cyber-tab--green.cyber-tab--active::after{background:var(--cyber-green-500);box-shadow:0 0 10px var(--cyber-green-500)}@keyframes tab-fade-in{from{opacity:0%;transform:translateY(4px)}to{opacity:100%;transform:translateY(0)}}}@layer components{.cyber-modal{--modal-accent: var(--cyber-cyan-500);position:fixed;z-index:var(--z-modal, 9000);display:flex;align-items:center;justify-content:center;padding:var(--space-md);background:color-mix(in srgb, var(--cyber-void-500) 80%, transparent);opacity:0%;visibility:hidden;transition:opacity var(--transition-base),visibility var(--transition-base);backdrop-filter:blur(8px);inset:0}.cyber-modal--open{opacity:100%;visibility:visible}.cyber-modal--open .cyber-modal__dialog{opacity:100%;transform:translateY(0) scale(1)}.cyber-modal__dialog{position:relative;display:flex;flex-direction:column;width:100%;max-width:500px;max-height:calc(100vh - var(--space-xl));border:var(--border-thin) solid var(--color-border-default);background:var(--color-bg-secondary);box-shadow:0 25px 50px -12px color-mix(in srgb, var(--cyber-void-900) 80%, transparent),0 0 20px color-mix(in srgb, var(--modal-accent) 30%, transparent),0 0 40px color-mix(in srgb, var(--modal-accent) 15%, transparent);opacity:0%;transform:translateY(-20px) scale(0.95);transition:opacity var(--transition-base) var(--ease-out-expo),transform var(--transition-base) var(--ease-out-expo);clip-path:polygon(0 0, calc(100% - 20px) 0, 100% 20px, 100% 100%, 20px 100%, 0 calc(100% - 20px))}.cyber-modal__dialog::before,.cyber-modal__dialog::after{content:"";position:absolute;z-index:1;width:30px;height:30px;border:2px solid var(--modal-accent);pointer-events:none}.cyber-modal__dialog::before{top:-1px;left:-1px;border-right:none;border-bottom:none}.cyber-modal__dialog::after{right:-1px;bottom:-1px;border-top:none;border-left:none}.cyber-modal__header{display:flex;gap:var(--space-sm);align-items:center;padding:var(--space-md);border-bottom:var(--border-thin) solid var(--color-border-default)}.cyber-modal__title{flex:1;margin:0;font-family:var(--font-display);font-size:var(--text-lg);font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--modal-accent);text-shadow:0 0 10px color-mix(in srgb, var(--modal-accent) 60%, transparent)}.cyber-modal__close{display:flex;align-items:center;justify-content:center;width:2rem;height:2rem;padding:0;border:var(--border-thin) solid var(--color-border-default);color:var(--color-text-secondary);background:rgba(0,0,0,0);transition:color var(--transition-fast),border-color var(--transition-fast),background var(--transition-fast);cursor:pointer}.cyber-modal__close:hover{border-color:var(--cyber-magenta-500);color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 10%, transparent)}.cyber-modal__close:focus-visible{outline:2px solid var(--cyber-cyan-500);outline-offset:2px}.cyber-modal__close svg{width:1rem;height:1rem}.cyber-modal__body{flex:1;padding:var(--space-md);overflow-y:auto;line-height:var(--leading-relaxed);color:var(--color-text-secondary)}.cyber-modal__footer{display:flex;gap:var(--space-sm);align-items:center;justify-content:flex-end;padding:var(--space-md);border-top:var(--border-thin) solid var(--color-border-default)}.cyber-modal--sm .cyber-modal__dialog{max-width:360px}.cyber-modal--lg .cyber-modal__dialog{max-width:720px}.cyber-modal--xl .cyber-modal__dialog{max-width:960px}.cyber-modal--fullscreen .cyber-modal__dialog{width:calc(100% - var(--space-lg));max-width:none;height:calc(100% - var(--space-lg));max-height:none}.cyber-modal--magenta{--modal-accent: var(--cyber-magenta-500)}.cyber-modal--yellow{--modal-accent: var(--cyber-yellow-500)}.cyber-modal--green{--modal-accent: var(--cyber-green-500)}.cyber-modal--slide-up .cyber-modal__dialog{transform:translateY(40px) scale(1)}.cyber-modal--slide-up.cyber-modal--open .cyber-modal__dialog{transform:translateY(0) scale(1)}.cyber-modal--zoom .cyber-modal__dialog{transform:scale(0.8)}.cyber-modal--zoom.cyber-modal--open .cyber-modal__dialog{transform:scale(1)}.cyber-modal-confirm{text-align:center}.cyber-modal-confirm .cyber-modal__body{padding:var(--space-lg) var(--space-md)}.cyber-modal-confirm__icon{display:flex;align-items:center;justify-content:center;width:4rem;height:4rem;margin:0 auto var(--space-md);border:var(--border-medium) solid var(--cyber-cyan-500);color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 15%, transparent);box-shadow:0 0 15px color-mix(in srgb, var(--cyber-cyan-500) 40%, transparent)}.cyber-modal-confirm__icon svg{width:2rem;height:2rem}.cyber-modal-confirm__icon--warning{border-color:var(--cyber-yellow-500);color:var(--cyber-yellow-500);background:color-mix(in srgb, var(--cyber-yellow-500) 15%, transparent);box-shadow:0 0 15px color-mix(in srgb, var(--cyber-yellow-500) 40%, transparent)}.cyber-modal-confirm__icon--error{border-color:var(--cyber-magenta-500);color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 15%, transparent);box-shadow:0 0 15px color-mix(in srgb, var(--cyber-magenta-500) 40%, transparent)}.cyber-modal-confirm__icon--success{border-color:var(--cyber-green-500);color:var(--cyber-green-500);background:color-mix(in srgb, var(--cyber-green-500) 15%, transparent);box-shadow:0 0 15px color-mix(in srgb, var(--cyber-green-500) 40%, transparent)}.cyber-modal-confirm__title{margin:0 0 var(--space-xs);font-family:var(--font-display);font-size:var(--text-xl);font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--color-text-primary)}.cyber-modal-confirm__message{margin:0;color:var(--color-text-secondary)}@media(prefers-reduced-motion: reduce){.cyber-modal{transition:none}.cyber-modal__dialog{transition:none}}}@layer components{.cyber-dropdown{position:relative;display:inline-block}.cyber-dropdown__trigger{display:inline-flex;gap:var(--space-xs);align-items:center;padding:var(--space-xs) var(--space-sm);border:var(--border-thin) solid var(--color-border-default);border-radius:var(--radius-sm);font-family:var(--font-display);font-size:var(--text-sm);font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--color-text-primary);background:var(--color-bg-tertiary);transition:color var(--duration-fast),background-color var(--duration-fast),border-color var(--duration-fast);cursor:pointer}.cyber-dropdown__trigger:hover{border-color:var(--cyber-cyan-500);background:var(--color-bg-elevated)}.cyber-dropdown__trigger:focus-visible{outline:2px solid var(--cyber-cyan-500);outline-offset:2px}.cyber-dropdown__trigger::after{content:"";width:0;height:0;border-top:5px solid currentcolor;border-right:4px solid rgba(0,0,0,0);border-left:4px solid rgba(0,0,0,0);transition:transform var(--duration-fast)}.cyber-dropdown__menu{position:absolute;top:100%;left:0;z-index:1050;min-width:180px;margin:var(--space-2xs) 0 0;padding:var(--space-2xs) 0;padding-left:0;border:var(--border-thin) solid var(--color-border-default);border-left:3px solid var(--cyber-cyan-500);border-radius:var(--radius-sm);background:var(--color-bg-secondary);box-shadow:0 10px 25px color-mix(in srgb, var(--cyber-void-900) 60%, transparent),0 0 15px color-mix(in srgb, var(--cyber-cyan-500) 15%, transparent);opacity:0%;visibility:hidden;transform:translateY(-10px);transition:opacity var(--duration-fast),visibility var(--duration-fast),transform var(--duration-fast);list-style:none;list-style-type:none}.cyber-dropdown__menu>li{margin:0;padding:0;list-style:none;list-style-type:none}.cyber-dropdown__item{display:block;width:100%;padding:var(--space-xs) var(--space-sm);border:none;font-family:var(--font-mono);font-size:var(--text-sm);text-align:left;color:var(--color-text-secondary);background:rgba(0,0,0,0);transition:color var(--duration-fast),background-color var(--duration-fast);cursor:pointer}.cyber-dropdown__item:hover{color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 10%, transparent)}.cyber-dropdown__item:focus-visible{color:var(--cyber-cyan-500);background:color-mix(in srgb, var(--cyber-cyan-500) 15%, transparent);outline:none}.cyber-dropdown__item--active{color:var(--cyber-cyan-500)}.cyber-dropdown__item--active::before{content:"> "}.cyber-dropdown__item--disabled{opacity:50%;cursor:not-allowed;pointer-events:none}.cyber-dropdown__divider{height:1px;margin:var(--space-2xs) 0;background:var(--color-border-default)}.cyber-dropdown--open .cyber-dropdown__trigger::after{transform:rotate(180deg)}.cyber-dropdown--open .cyber-dropdown__menu{opacity:100%;visibility:visible;transform:translateY(0)}.cyber-dropdown--css-only:focus-within .cyber-dropdown__trigger::after{transform:rotate(180deg)}.cyber-dropdown--css-only:focus-within .cyber-dropdown__menu{opacity:100%;visibility:visible;transform:translateY(0)}.cyber-dropdown--right .cyber-dropdown__menu{right:0;left:auto;border-right:3px solid var(--cyber-cyan-500);border-left:var(--border-thin) solid var(--color-border-default)}.cyber-dropdown--right.cyber-dropdown--magenta .cyber-dropdown__menu{border-right-color:var(--cyber-magenta-500)}.cyber-dropdown--up .cyber-dropdown__menu{top:auto;bottom:100%;margin-top:0;margin-bottom:var(--space-2xs);transform:translateY(10px)}.cyber-dropdown--up.cyber-dropdown--open .cyber-dropdown__menu,.cyber-dropdown--up.cyber-dropdown--css-only:focus-within .cyber-dropdown__menu{transform:translateY(0)}.cyber-dropdown--magenta .cyber-dropdown__menu{box-shadow:0 10px 25px color-mix(in srgb, var(--cyber-void-900) 60%, transparent),0 0 15px color-mix(in srgb, var(--cyber-magenta-500) 15%, transparent);border-left-color:var(--cyber-magenta-500)}.cyber-dropdown--magenta .cyber-dropdown__item:hover,.cyber-dropdown--magenta .cyber-dropdown__item:focus-visible{color:var(--cyber-magenta-500);background:color-mix(in srgb, var(--cyber-magenta-500) 10%, transparent)}.cyber-dropdown--magenta .cyber-dropdown__item--active{color:var(--cyber-magenta-500)}}@layer components{.cyber-terminal{border:1px solid var(--cyber-green-700);overflow:hidden;font-family:var(--font-mono);font-size:var(--text-sm);background:var(--color-bg-secondary)}.cyber-terminal__header{display:flex;gap:var(--space-xs);align-items:center;padding:var(--space-xs) var(--space-sm);border-bottom:1px solid var(--cyber-green-700);background:var(--cyber-green-900)}.cyber-terminal__dot{width:10px;height:10px;border-radius:50%}.cyber-terminal__dot--red{background:var(--cyber-magenta-500)}.cyber-terminal__dot--yellow{background:var(--cyber-yellow-500)}.cyber-terminal__dot--green{background:var(--cyber-green-500)}.cyber-terminal__title{margin-left:auto;font-size:var(--text-xs);letter-spacing:.1em;text-transform:uppercase;color:var(--cyber-green-500)}.cyber-terminal__body{padding:var(--space-md);line-height:1.8;color:var(--cyber-green-400)}.cyber-terminal__prompt{color:var(--cyber-cyan-500)}.cyber-terminal__cursor{display:inline-block;width:8px;height:1.1em;margin-left:2px;background:var(--cyber-green-500);transform:translateY(0.1em);animation:blink 1s step-end infinite;vertical-align:middle}.cyber-terminal__line{display:flex;gap:var(--space-xs);line-height:1.8;white-space:pre-wrap;word-break:break-word}.cyber-terminal__line--input{color:var(--color-text-primary)}.cyber-terminal__line--output{color:var(--color-text-secondary)}.cyber-terminal__line--error{color:var(--cyber-magenta-500)}.cyber-terminal__line--success{color:var(--cyber-green-500)}.cyber-terminal__line--warning{color:var(--cyber-yellow-500)}.cyber-terminal__line--info{color:var(--cyber-cyan-500)}.cyber-terminal__command{color:var(--color-text-primary)}.cyber-terminal__input{display:flex;gap:var(--space-xs);align-items:center;padding:var(--space-sm);border-top:1px solid var(--cyber-green-700);background:var(--color-bg-tertiary)}.cyber-terminal__input-field{flex:1;border:none;font-family:inherit;font-size:inherit;color:var(--color-text-primary);background:rgba(0,0,0,0);caret-color:var(--cyber-cyan-500);outline:none}.cyber-terminal__input-field::-moz-placeholder{color:var(--color-text-muted)}.cyber-terminal__input-field::placeholder{color:var(--color-text-muted)}.cyber-terminal .syntax-keyword{color:var(--cyber-magenta-500)}.cyber-terminal .syntax-string{color:var(--cyber-green-500)}.cyber-terminal .syntax-number{color:var(--cyber-yellow-500)}.cyber-terminal .syntax-comment{font-style:italic;color:var(--cyber-chrome-500)}.cyber-terminal .syntax-function{color:var(--cyber-cyan-500)}.cyber-terminal .syntax-variable{color:var(--cyber-chrome-200)}.cyber-terminal .syntax-operator{color:var(--cyber-chrome-400)}.cyber-terminal--sm{font-size:var(--text-xs)}.cyber-terminal--sm .cyber-terminal__body{min-height:100px;max-height:250px}.cyber-terminal--lg{font-size:var(--text-base)}.cyber-terminal--lg .cyber-terminal__body{min-height:300px;max-height:700px}.cyber-terminal--fullheight .cyber-terminal__body{max-height:none}.cyber-terminal--no-header .cyber-terminal__header{display:none}.cyber-terminal--rounded{border-radius:var(--radius-lg)}.cyber-terminal--rounded .cyber-terminal__header{border-radius:var(--radius-lg) var(--radius-lg) 0 0}.cyber-terminal--glow{box-shadow:0 0 20px color-mix(in srgb, var(--cyber-green-500) 20%, transparent),inset 0 0 60px color-mix(in srgb, var(--cyber-green-500) 5%, transparent)}@keyframes blink{50%{opacity:0%}}.cyber-terminal-typing{border-right:2px solid var(--cyber-cyan-500);overflow:hidden;white-space:nowrap;animation:typing 2s steps(30, end),blink 1s step-end infinite}@keyframes typing{from{width:0}to{width:100%}}.cyber-terminal-ascii{line-height:1.2;white-space:pre;color:var(--cyber-cyan-500);text-shadow:0 0 10px var(--cyber-cyan-500)}}@layer components{.cyber-spinner{--spinner-size: 48px;--spinner-color-primary: var(--cyber-cyan-500);--spinner-color-secondary: var(--cyber-magenta-500);--spinner-thickness: 3px;position:relative;display:inline-block;width:var(--spinner-size);height:var(--spinner-size)}.cyber-spinner::before,.cyber-spinner::after{content:"";position:absolute;border:var(--spinner-thickness) solid rgba(0,0,0,0);border-radius:50%;inset:0;will-change:transform}.cyber-spinner::before{animation:spin 1s linear infinite;border-top-color:var(--spinner-color-primary)}.cyber-spinner::after{animation:spin 1s linear infinite reverse;animation-delay:-0.5s;border-bottom-color:var(--spinner-color-secondary)}.cyber-spinner--xs{--spinner-size: 16px;--spinner-thickness: 2px}.cyber-spinner--sm{--spinner-size: 24px;--spinner-thickness: 2px}.cyber-spinner--md{--spinner-size: 48px;--spinner-thickness: 3px}.cyber-spinner--lg{--spinner-size: 64px;--spinner-thickness: 4px}.cyber-spinner--xl{--spinner-size: 96px;--spinner-thickness: 5px}.cyber-spinner--magenta{--spinner-color-primary: var(--cyber-magenta-500);--spinner-color-secondary: var(--cyber-cyan-500)}.cyber-spinner--yellow{--spinner-color-primary: var(--cyber-yellow-500);--spinner-color-secondary: var(--cyber-cyan-500)}.cyber-spinner--green{--spinner-color-primary: var(--cyber-green-500);--spinner-color-secondary: var(--cyber-cyan-500)}.cyber-spinner--chrome{--spinner-color-primary: var(--cyber-chrome-400);--spinner-color-secondary: var(--cyber-chrome-600)}.cyber-spinner--single::after{display:none}.cyber-spinner--dotted::before{border-width:calc(var(--spinner-thickness) + 1px);border-style:dotted}.cyber-spinner--dotted::after{border-width:calc(var(--spinner-thickness) + 1px);border-style:dotted}.cyber-spinner--glow::before{filter:drop-shadow(0 0 4px var(--spinner-color-primary))}.cyber-spinner--glow::after{filter:drop-shadow(0 0 4px var(--spinner-color-secondary))}.cyber-spinner--pulse{animation:spinner-pulse 1.5s ease-in-out infinite}@keyframes spin{to{transform:rotate(360deg)}}@keyframes spinner-pulse{0%,100%{opacity:100%;transform:scale(1)}50%{opacity:60%;transform:scale(0.9)}}.cyber-spinner-dots{--dot-size: 8px;--dot-color: var(--cyber-cyan-500);display:inline-flex;gap:4px;align-items:center}.cyber-spinner-dots span{width:var(--dot-size);height:var(--dot-size);border-radius:50%;background:var(--dot-color);animation:spinner-dots-bounce 1.4s ease-in-out infinite}.cyber-spinner-dots span:nth-child(1){animation-delay:0s}.cyber-spinner-dots span:nth-child(2){animation-delay:.16s}.cyber-spinner-dots span:nth-child(3){animation-delay:.32s}.cyber-spinner-dots--magenta{--dot-color: var(--cyber-magenta-500)}.cyber-spinner-dots--yellow{--dot-color: var(--cyber-yellow-500)}.cyber-spinner-dots--green{--dot-color: var(--cyber-green-500)}@keyframes spinner-dots-bounce{0%,80%,100%{opacity:50%;transform:scale(0.6)}40%{opacity:100%;transform:scale(1)}}.cyber-spinner-bars{--bar-width: 4px;--bar-height: 24px;--bar-color: var(--cyber-cyan-500);display:inline-flex;gap:3px;align-items:center}.cyber-spinner-bars span{width:var(--bar-width);height:var(--bar-height);background:var(--bar-color);animation:spinner-bars-scale 1.2s ease-in-out infinite}.cyber-spinner-bars span:nth-child(1){animation-delay:0s}.cyber-spinner-bars span:nth-child(2){animation-delay:0.1s}.cyber-spinner-bars span:nth-child(3){animation-delay:0.2s}.cyber-spinner-bars span:nth-child(4){animation-delay:0.3s}.cyber-spinner-bars span:nth-child(5){animation-delay:0.4s}.cyber-spinner-bars--magenta{--bar-color: var(--cyber-magenta-500)}.cyber-spinner-bars--yellow{--bar-color: var(--cyber-yellow-500)}.cyber-spinner-bars--green{--bar-color: var(--cyber-green-500)}@keyframes spinner-bars-scale{0%,40%,100%{transform:scaleY(0.4)}20%{transform:scaleY(1)}}@media(prefers-reduced-motion: reduce){.cyber-spinner,.cyber-spinner::before,.cyber-spinner::after{animation:none}.cyber-spinner-dots span,.cyber-spinner-bars span{animation:none}}.cyber-spinner-overlay{position:absolute;z-index:var(--z-modal);display:flex;flex-direction:column;gap:var(--space-sm);align-items:center;justify-content:center;background:color-mix(in srgb, var(--cyber-void-500) 90%, transparent);backdrop-filter:blur(4px);inset:0}.cyber-spinner-overlay__text{font-family:var(--font-mono);font-size:var(--text-sm);letter-spacing:var(--tracking-wider);text-transform:uppercase;color:var(--color-text-secondary)}}@layer components{.cyber-skeleton{--skeleton-base: var(--cyber-void-400);--skeleton-shine: color-mix(in srgb, var(--cyber-cyan-500) 15%, var(--cyber-void-200));--skeleton-glow: color-mix(in srgb, var(--cyber-cyan-500) 8%, transparent);position:relative;display:block;border-radius:var(--radius-sm);overflow:hidden;background:var(--skeleton-base)}.cyber-skeleton::after{content:"";position:absolute;background:linear-gradient(90deg, transparent 0%, var(--skeleton-glow) 25%, var(--skeleton-shine) 50%, var(--skeleton-glow) 75%, transparent 100%);transform:translateX(-100%);animation:skeleton-shimmer 2s cubic-bezier(0.4, 0, 0.2, 1) infinite;inset:0}.cyber-skeleton--text{width:100%;height:1em;border-radius:var(--radius-sm)}.cyber-skeleton--title{width:60%;height:1.5em;border-radius:var(--radius-sm)}.cyber-skeleton--avatar{flex-shrink:0;width:48px;height:48px;border-radius:50%}.cyber-skeleton--avatar-sm{flex-shrink:0;width:32px;height:32px;border-radius:50%}.cyber-skeleton--avatar-lg{flex-shrink:0;width:64px;height:64px;border-radius:50%}.cyber-skeleton--thumbnail{width:100%;border-radius:var(--radius-md);aspect-ratio:16/9}.cyber-skeleton--square{width:100%;border-radius:var(--radius-md);aspect-ratio:1}.cyber-skeleton--button{width:120px;height:2.5rem;border-radius:0;clip-path:polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px))}.cyber-skeleton--badge{display:inline-block;width:60px;height:1.25rem;border-radius:0}.cyber-skeleton--cyan{--skeleton-base: color-mix(in srgb, var(--cyber-cyan-500) 8%, var(--cyber-void-400));--skeleton-shine: color-mix(in srgb, var(--cyber-cyan-500) 25%, var(--cyber-void-200));--skeleton-glow: color-mix(in srgb, var(--cyber-cyan-500) 12%, transparent)}.cyber-skeleton--magenta{--skeleton-base: color-mix(in srgb, var(--cyber-magenta-500) 8%, var(--cyber-void-400));--skeleton-shine: color-mix(in srgb, var(--cyber-magenta-500) 25%, var(--cyber-void-200));--skeleton-glow: color-mix(in srgb, var(--cyber-magenta-500) 12%, transparent)}.cyber-skeleton--yellow{--skeleton-base: color-mix(in srgb, var(--cyber-yellow-500) 8%, var(--cyber-void-400));--skeleton-shine: color-mix(in srgb, var(--cyber-yellow-500) 25%, var(--cyber-void-200));--skeleton-glow: color-mix(in srgb, var(--cyber-yellow-500) 12%, transparent)}.cyber-skeleton--green{--skeleton-base: color-mix(in srgb, var(--cyber-green-500) 8%, var(--cyber-void-400));--skeleton-shine: color-mix(in srgb, var(--cyber-green-500) 25%, var(--cyber-void-200));--skeleton-glow: color-mix(in srgb, var(--cyber-green-500) 12%, transparent)}.cyber-skeleton--pulse{animation:skeleton-pulse 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite}.cyber-skeleton--pulse::after{display:none}.cyber-skeleton--wave::after{background:linear-gradient(90deg, transparent 0%, var(--skeleton-glow) 15%, color-mix(in srgb, var(--skeleton-shine) 70%, transparent) 35%, var(--skeleton-shine) 50%, color-mix(in srgb, var(--skeleton-shine) 70%, transparent) 65%, var(--skeleton-glow) 85%, transparent 100%);animation:skeleton-wave 2.5s cubic-bezier(0.4, 0, 0.2, 1) infinite}.cyber-skeleton--glow{box-shadow:0 0 8px var(--skeleton-glow);animation:skeleton-glow 2s ease-in-out infinite}.cyber-skeleton--glow::after{background:linear-gradient(90deg, transparent 0%, var(--skeleton-shine) 50%, transparent 100%);animation:skeleton-shimmer 2s cubic-bezier(0.4, 0, 0.2, 1) infinite}.cyber-skeleton--no-animation::after{display:none}@keyframes skeleton-shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}@keyframes skeleton-pulse{0%,100%{opacity:100%}50%{opacity:60%}}@keyframes skeleton-wave{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}@keyframes skeleton-glow{0%,100%{box-shadow:0 0 8px var(--skeleton-glow)}50%{box-shadow:0 0 16px var(--skeleton-glow),0 0 24px var(--skeleton-glow)}}@media(prefers-reduced-motion: reduce){.cyber-skeleton::after{background:var(--skeleton-shine);opacity:50%;transform:none;animation:none}.cyber-skeleton--pulse,.cyber-skeleton--glow{animation:none}}.cyber-skeleton-card{display:flex;flex-direction:column;gap:var(--space-sm);padding:var(--space-md);border:var(--border-thin) solid var(--color-border-default);background:var(--color-bg-secondary)}.cyber-skeleton-card__header{display:flex;gap:var(--space-sm);align-items:center}.cyber-skeleton-card__body{display:flex;flex-direction:column;gap:var(--space-xs)}.cyber-skeleton-list{display:flex;flex-direction:column;gap:var(--space-sm)}.cyber-skeleton-list__item{display:flex;gap:var(--space-sm);align-items:center;padding:var(--space-sm);border:var(--border-thin) solid var(--color-border-default);background:var(--color-bg-secondary)}.cyber-skeleton-list__content{display:flex;flex:1;flex-direction:column;gap:var(--space-2xs)}.cyber-skeleton-table{width:100%;border:var(--border-thin) solid var(--color-border-default);background:var(--color-bg-secondary);border-collapse:collapse}.cyber-skeleton-table__row{display:flex;gap:var(--space-md);padding:var(--space-sm) var(--space-md);border-bottom:var(--border-thin) solid var(--color-border-default)}.cyber-skeleton-table__row:last-child{border-bottom:none}.cyber-skeleton-table__row--header{background:var(--color-bg-tertiary)}.cyber-skeleton-table__cell{flex:1}.cyber-skeleton-profile{display:flex;gap:var(--space-md);align-items:flex-start}.cyber-skeleton-profile__info{display:flex;flex:1;flex-direction:column;gap:var(--space-xs)}}@layer components{.cyber-noise{position:relative}.cyber-noise::before{content:"";position:absolute;z-index:10;border-radius:inherit;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");opacity:15%;pointer-events:none;inset:0}.cyber-noise--animated::before{animation:cyber-noise-shift .5s steps(10) infinite}@keyframes cyber-noise-shift{0%,100%{transform:translate(0, 0)}10%{transform:translate(-5%, -10%)}20%{transform:translate(-15%, 5%)}30%{transform:translate(7%, -25%)}40%{transform:translate(-5%, 25%)}50%{transform:translate(-15%, 10%)}60%{transform:translate(15%, 0%)}70%{transform:translate(0%, 15%)}80%{transform:translate(3%, 35%)}90%{transform:translate(-10%, 10%)}}@media(prefers-reduced-motion: reduce){.cyber-noise--animated{animation:none}}}@layer components{.cyber-scanlines{position:relative}.cyber-scanlines::after{content:"";position:absolute;z-index:10;border-radius:inherit;background:repeating-linear-gradient(0deg, transparent 0, transparent 2px, rgba(0, 0, 0, 0.5) 2px, rgba(0, 0, 0, 0.5) 4px);pointer-events:none;inset:0}.cyber-scanlines--fine::after{background:repeating-linear-gradient(0deg, transparent, transparent 1px, rgba(0, 0, 0, 0.2) 1px, rgba(0, 0, 0, 0.2) 2px)}.cyber-scanlines--heavy::after{background:repeating-linear-gradient(0deg, transparent, transparent 3px, rgba(0, 0, 0, 0.4) 3px, rgba(0, 0, 0, 0.4) 6px)}.cyber-scanlines--flicker::after{animation:cyber-scanline-flicker .15s infinite}.cyber-scanlines--scroll::after{animation:cyber-scanline-scroll 8s linear infinite}@keyframes cyber-scanline-flicker{0%,100%{opacity:var(--scanline-opacity)}50%{opacity:calc(var(--scanline-opacity)*98%)}}@keyframes cyber-scanline-scroll{0%{background-position:0 0}100%{background-position:0 100%}}@media(prefers-reduced-motion: reduce){.cyber-scanlines--flicker,.cyber-scanlines--scroll{animation:none}}}@layer components{.cyber-heading{position:relative;display:inline-block;color:var(--cyber-cyan-500);text-shadow:0 0 10px var(--cyber-cyan-500),0 0 20px color-mix(in srgb, var(--cyber-cyan-500) 60%, transparent)}.cyber-heading::before,.cyber-heading::after{content:attr(data-text);position:absolute;top:0;left:0;width:100%;height:100%;opacity:80%;will-change:transform,clip-path}.cyber-heading::before{z-index:-1;color:var(--cyber-magenta-500);animation:glitch-1 8s infinite linear alternate-reverse}.cyber-heading::after{z-index:-1;color:var(--cyber-cyan-300);animation:glitch-2 6s infinite linear alternate}@keyframes glitch-1{0%,100%{transform:translate(-2px, 0);clip-path:inset(0 0 95% 0)}10%{transform:translate(2px, 0);clip-path:inset(30% 0 40% 0)}20%{transform:translate(-1px, 0);clip-path:inset(70% 0 10% 0)}30%{transform:translate(1px, 0);clip-path:inset(10% 0 60% 0)}40%{transform:translate(-2px, 0);clip-path:inset(80% 0 5% 0)}50%{transform:translate(2px, 0);clip-path:inset(20% 0 55% 0)}60%{transform:translate(-1px, 0);clip-path:inset(50% 0 30% 0)}70%{transform:translate(1px, 0);clip-path:inset(5% 0 85% 0)}80%{transform:translate(-2px, 0);clip-path:inset(45% 0 35% 0)}90%{transform:translate(2px, 0);clip-path:inset(85% 0 5% 0)}}@keyframes glitch-2{0%,100%{transform:translate(2px, 0);clip-path:inset(95% 0 0 0)}10%{transform:translate(-2px, 0);clip-path:inset(40% 0 30% 0)}20%{transform:translate(1px, 0);clip-path:inset(10% 0 70% 0)}30%{transform:translate(-1px, 0);clip-path:inset(60% 0 10% 0)}40%{transform:translate(2px, 0);clip-path:inset(5% 0 80% 0)}50%{transform:translate(-2px, 0);clip-path:inset(55% 0 20% 0)}60%{transform:translate(1px, 0);clip-path:inset(30% 0 50% 0)}70%{transform:translate(-1px, 0);clip-path:inset(85% 0 5% 0)}80%{transform:translate(2px, 0);clip-path:inset(35% 0 45% 0)}90%{transform:translate(-2px, 0);clip-path:inset(5% 0 85% 0)}}@media(prefers-reduced-motion: reduce){.cyber-heading::before,.cyber-heading::after{opacity:0%;transform:none;animation:none;clip-path:none}}.cyber-glitch{--glitch-color-1: var(--cyber-cyan-500, #00f0ff);--glitch-color-2: var(--cyber-magenta-500, #ff2a6d);--glitch-offset: 2px;--glitch-duration: 3s;position:relative}.cyber-glitch::before,.cyber-glitch::after{content:attr(data-text);position:absolute;overflow:hidden;color:inherit;background:inherit;pointer-events:none;inset:0;will-change:transform,clip-path}.cyber-glitch::before{color:var(--glitch-color-1);animation:cyber-glitch-1 var(--glitch-duration) infinite linear alternate-reverse;clip-path:polygon(0 0, 100% 0, 100% 45%, 0 45%);mix-blend-mode:screen}.cyber-glitch::after{color:var(--glitch-color-2);animation:cyber-glitch-2 var(--glitch-duration) infinite linear alternate-reverse;clip-path:polygon(0 55%, 100% 55%, 100% 100%, 0 100%);mix-blend-mode:screen}.cyber-glitch--intense{--glitch-offset: 4px;--glitch-duration: 1.5s}.cyber-glitch--intense::before{animation:cyber-glitch-intense-1 var(--glitch-duration) infinite}.cyber-glitch--intense::after{animation:cyber-glitch-intense-2 var(--glitch-duration) infinite}.cyber-glitch--hover::before,.cyber-glitch--hover::after{opacity:0%;animation:none}.cyber-glitch--hover:hover::before,.cyber-glitch--hover:hover::after{opacity:100%}.cyber-glitch--hover:hover::before{animation:cyber-glitch-1 .3s infinite linear alternate-reverse}.cyber-glitch--hover:hover::after{animation:cyber-glitch-2 .3s infinite linear alternate-reverse}.cyber-glitch--subtle{--glitch-offset: 1px}.cyber-glitch--subtle::before,.cyber-glitch--subtle::after{opacity:70%}@keyframes cyber-glitch-1{0%,100%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 45%, 0 45%)}20%{transform:translate(calc(var(--glitch-offset) * -1), 0);clip-path:polygon(0 15%, 100% 15%, 100% 30%, 0 30%)}40%{transform:translate(var(--glitch-offset), 0);clip-path:polygon(0 5%, 100% 5%, 100% 20%, 0 20%)}60%{transform:translate(calc(var(--glitch-offset) * -0.5), 0);clip-path:polygon(0 35%, 100% 35%, 100% 50%, 0 50%)}80%{transform:translate(var(--glitch-offset), 0);clip-path:polygon(0 0, 100% 0, 100% 10%, 0 10%)}}@keyframes cyber-glitch-2{0%,100%{transform:translate(0);clip-path:polygon(0 55%, 100% 55%, 100% 100%, 0 100%)}20%{transform:translate(var(--glitch-offset), 0);clip-path:polygon(0 70%, 100% 70%, 100% 85%, 0 85%)}40%{transform:translate(calc(var(--glitch-offset) * -1), 0);clip-path:polygon(0 80%, 100% 80%, 100% 95%, 0 95%)}60%{transform:translate(calc(var(--glitch-offset) * 0.5), 0);clip-path:polygon(0 60%, 100% 60%, 100% 75%, 0 75%)}80%{transform:translate(calc(var(--glitch-offset) * -1), 0);clip-path:polygon(0 90%, 100% 90%, 100% 100%, 0 100%)}}@keyframes cyber-glitch-intense-1{0%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}5%{transform:translate(var(--glitch-offset), calc(var(--glitch-offset) * -1));clip-path:polygon(0 5%, 100% 5%, 100% 10%, 0 10%)}10%{transform:translate(calc(var(--glitch-offset) * -2), 0);clip-path:polygon(0 20%, 100% 20%, 100% 25%, 0 25%)}15%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}50%{transform:translate(calc(var(--glitch-offset) * -1), var(--glitch-offset));clip-path:polygon(0 40%, 100% 40%, 100% 50%, 0 50%)}55%{transform:translate(var(--glitch-offset), 0);clip-path:polygon(0 60%, 100% 60%, 100% 70%, 0 70%)}60%,100%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}}@keyframes cyber-glitch-intense-2{0%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}7%{transform:translate(calc(var(--glitch-offset) * -1), var(--glitch-offset));clip-path:polygon(0 75%, 100% 75%, 100% 80%, 0 80%)}12%{transform:translate(var(--glitch-offset), 0);clip-path:polygon(0 85%, 100% 85%, 100% 95%, 0 95%)}17%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}45%{transform:translate(var(--glitch-offset), calc(var(--glitch-offset) * -1));clip-path:polygon(0 30%, 100% 30%, 100% 35%, 0 35%)}52%{transform:translate(calc(var(--glitch-offset) * -2), 0);clip-path:polygon(0 15%, 100% 15%, 100% 25%, 0 25%)}58%,100%{transform:translate(0);clip-path:polygon(0 0, 100% 0, 100% 100%, 0 100%)}}@media(prefers-reduced-motion: reduce){.cyber-glitch::before,.cyber-glitch::after{opacity:0%;transform:none;animation:none;clip-path:none}}}@layer components{.cyber-neon-border{position:relative;isolation:isolate}.cyber-neon-border::before{content:"";position:absolute;z-index:-1;border-radius:inherit;background:linear-gradient(90deg, var(--cyber-cyan-500, #00f0ff), var(--cyber-magenta-500, #ff2a6d), var(--cyber-yellow-500, #fcee0a), var(--cyber-green-500, #05ffa1), var(--cyber-cyan-500, #00f0ff));background-size:400% 100%;animation:neon-flow 4s linear infinite;filter:blur(15px);inset:0}.cyber-neon-border::after{content:"";position:absolute;z-index:-1;border-radius:inherit;background:var(--color-bg-secondary, #0d1117);inset:0}.cyber-neon-border--magenta::before{background:linear-gradient(90deg, var(--cyber-magenta-500, #ff2a6d), var(--cyber-cyan-500, #00f0ff), var(--cyber-magenta-500, #ff2a6d), var(--cyber-yellow-500, #fcee0a), var(--cyber-magenta-500, #ff2a6d))}.cyber-neon-border--cyan::before{background:linear-gradient(90deg, var(--cyber-cyan-400, #33f3ff), var(--cyber-cyan-600, #00c4cc), var(--cyber-cyan-500, #00f0ff), var(--cyber-cyan-400, #33f3ff))}.cyber-neon-border--yellow::before{background:linear-gradient(90deg, var(--cyber-yellow-500, #fcee0a), var(--cyber-green-500, #05ffa1), var(--cyber-yellow-500, #fcee0a), var(--cyber-cyan-500, #00f0ff), var(--cyber-yellow-500, #fcee0a))}.cyber-neon-border--green::before{background:linear-gradient(90deg, var(--cyber-green-500, #05ffa1), var(--cyber-cyan-500, #00f0ff), var(--cyber-green-500, #05ffa1), var(--cyber-yellow-500, #fcee0a), var(--cyber-green-500, #05ffa1))}@keyframes neon-flow{0%{background-position:0% 50%}100%{background-position:400% 50%}}@media(prefers-reduced-motion: reduce){.cyber-neon-border::before{background-position:0% 50%;animation:none}}}@layer components{.cyber-datastream{position:relative}.cyber-datastream::before{content:"";position:absolute;z-index:1;border-radius:inherit;background:linear-gradient(180deg, transparent 0%, color-mix(in srgb, var(--cyber-cyan-500) 30%, transparent) 50%, transparent 100%);background-size:100% 200%;animation:datastream 3s linear infinite;pointer-events:none;inset:0}@keyframes datastream{0%{background-position:0% 0%}100%{background-position:0% 200%}}@media(prefers-reduced-motion: reduce){.cyber-datastream::before{animation:none}}}@layer utilities{.cyber-hidden{display:none}.cyber-block{display:block}.cyber-inline{display:inline}.cyber-inline-block{display:inline-block}.cyber-flex{display:flex}.cyber-inline-flex{display:inline-flex}.cyber-grid{display:grid}}@layer utilities{.cyber-flex-col{flex-direction:column}.cyber-flex-row{flex-direction:row}.cyber-flex-wrap{flex-wrap:wrap}.cyber-flex-nowrap{flex-wrap:nowrap}.cyber-items-start{align-items:flex-start}.cyber-items-center{align-items:center}.cyber-items-end{align-items:flex-end}.cyber-items-stretch{align-items:stretch}.cyber-justify-start{justify-content:flex-start}.cyber-justify-center{justify-content:center}.cyber-justify-end{justify-content:flex-end}.cyber-justify-between{justify-content:space-between}.cyber-justify-around{justify-content:space-around}.cyber-gap-xs{gap:var(--space-xs)}.cyber-gap-sm{gap:var(--space-sm)}.cyber-gap-md{gap:var(--space-md)}.cyber-gap-lg{gap:var(--space-lg)}.cyber-gap-xl{gap:var(--space-xl)}.cyber-flex-1{flex:1 1 0%}.cyber-flex-auto{flex:1 1 auto}.cyber-flex-none{flex:none}}@layer utilities{.cyber-grid--2{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 280px), 1fr));gap:var(--space-md)}@media(width >= 640px){.cyber-grid--2{grid-template-columns:repeat(2, 1fr)}}.cyber-grid--3{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 250px), 1fr));gap:var(--space-md)}@media(width >= 768px){.cyber-grid--3{grid-template-columns:repeat(3, 1fr)}}.cyber-grid--4{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 220px), 1fr));gap:var(--space-md)}@media(width >= 1024px){.cyber-grid--4{grid-template-columns:repeat(4, 1fr)}}.cyber-grid--auto-sm{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 150px), 1fr));gap:var(--space-md)}.cyber-grid--auto-md{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 250px), 1fr));gap:var(--space-md)}.cyber-grid--auto-lg{display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 350px), 1fr));gap:var(--space-md)}.cyber-col-span-2{grid-column:span 2}.cyber-col-span-3{grid-column:span 3}.cyber-col-span-full{grid-column:1/-1}}@layer utilities{.cyber-m-0{margin:0}.cyber-m-auto{margin:auto}.cyber-m-xs{margin:var(--space-xs)}.cyber-m-sm{margin:var(--space-sm)}.cyber-m-md{margin:var(--space-md)}.cyber-m-lg{margin:var(--space-lg)}.cyber-mx-auto{margin-inline:auto}.cyber-mx-sm{margin-inline:var(--space-sm)}.cyber-mx-md{margin-inline:var(--space-md)}.cyber-mx-lg{margin-inline:var(--space-lg)}.cyber-my-sm{margin-block:var(--space-sm)}.cyber-my-md{margin-block:var(--space-md)}.cyber-my-lg{margin-block:var(--space-lg)}.cyber-my-xl{margin-block:var(--space-xl)}.cyber-mt-0{margin-top:0}.cyber-mt-xs{margin-top:var(--space-xs)}.cyber-mt-sm{margin-top:var(--space-sm)}.cyber-mt-md{margin-top:var(--space-md)}.cyber-mt-lg{margin-top:var(--space-lg)}.cyber-mt-xl{margin-top:var(--space-xl)}.cyber-mb-0{margin-bottom:0}.cyber-mb-xs{margin-bottom:var(--space-xs)}.cyber-mb-sm{margin-bottom:var(--space-sm)}.cyber-mb-md{margin-bottom:var(--space-md)}.cyber-mb-lg{margin-bottom:var(--space-lg)}.cyber-mb-xl{margin-bottom:var(--space-xl)}.cyber-p-0{padding:0}.cyber-p-xs{padding:var(--space-xs)}.cyber-p-sm{padding:var(--space-sm)}.cyber-p-md{padding:var(--space-md)}.cyber-p-lg{padding:var(--space-lg)}.cyber-p-xl{padding:var(--space-xl)}.cyber-px-0{padding-inline:0}.cyber-px-sm{padding-inline:var(--space-sm)}.cyber-px-md{padding-inline:var(--space-md)}.cyber-px-lg{padding-inline:var(--space-lg)}.cyber-py-0{padding-block:0}.cyber-py-sm{padding-block:var(--space-sm)}.cyber-py-md{padding-block:var(--space-md)}.cyber-py-lg{padding-block:var(--space-lg)}.cyber-py-xl{padding-block:var(--space-xl)}}@layer utilities{.cyber-text-left{text-align:left}.cyber-text-center{text-align:center}.cyber-text-right{text-align:right}.cyber-text-primary{color:var(--color-text-primary)}.cyber-text-secondary{color:var(--color-text-secondary)}.cyber-text-muted{color:var(--color-text-muted)}.cyber-text-cyan{color:var(--cyber-cyan-500)}.cyber-text-magenta{color:var(--cyber-magenta-500)}.cyber-text-yellow{color:var(--cyber-yellow-500)}.cyber-text-green{color:var(--cyber-green-500)}.cyber-text-glow{color:var(--cyber-cyan-500);text-shadow:var(--glow-text-cyan)}.cyber-text-glow--magenta{color:var(--cyber-magenta-500);text-shadow:var(--glow-text-magenta)}.cyber-text-glow--yellow{color:var(--cyber-yellow-500);text-shadow:var(--glow-text-yellow)}.cyber-text-glow--green{color:var(--cyber-green-500);text-shadow:var(--glow-text-green)}.cyber-text-glow-cyan{color:var(--cyber-cyan-500);text-shadow:var(--glow-text-cyan)}.cyber-text-glow-magenta{color:var(--cyber-magenta-500);text-shadow:var(--glow-text-magenta)}.cyber-text-glow-yellow{color:var(--cyber-yellow-500);text-shadow:var(--glow-text-yellow)}.cyber-text-glow-green{color:var(--cyber-green-500);text-shadow:var(--glow-text-green)}.cyber-font-normal{font-weight:400}.cyber-font-medium{font-weight:500}.cyber-font-semibold{font-weight:600}.cyber-font-bold{font-weight:700}.cyber-font-display{font-family:var(--font-display)}.cyber-font-body{font-family:var(--font-body)}.cyber-font-mono{font-family:var(--font-mono)}.cyber-uppercase{letter-spacing:var(--tracking-wider);text-transform:uppercase}.cyber-lowercase{text-transform:lowercase}.cyber-capitalize{text-transform:capitalize}.cyber-truncate{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.cyber-line-clamp-2{display:-webkit-box;overflow:hidden;-webkit-box-orient:vertical;-webkit-line-clamp:2}.cyber-line-clamp-3{display:-webkit-box;overflow:hidden;-webkit-box-orient:vertical;-webkit-line-clamp:3}}@layer utilities{.cyber-sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;overflow:hidden;white-space:nowrap;clip:rect(0, 0, 0, 0)}.cyber-sr-only-focusable:focus,.cyber-sr-only-focusable:focus-within{position:static;width:auto;height:auto;margin:inherit;padding:inherit;overflow:visible;white-space:normal;clip:auto}.cyber-not-sr{speak:none}.cyber-focus-visible:focus-visible{outline:2px solid var(--cyber-cyan-500);outline-offset:2px}@media(prefers-reduced-motion: reduce){.cyber-motion-safe{transition:none !important;animation:none !important}}@media(prefers-reduced-motion: no-preference){.cyber-motion-reduce{transition:none !important;animation:none !important}}}@layer utilities{@keyframes cyber-pulse{0%,100%{opacity:100%}50%{opacity:50%}}@keyframes cyber-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}@keyframes cyber-flicker-in{0%{opacity:0%}10%{opacity:60%}20%{opacity:20%}30%{opacity:80%}40%{opacity:40%}50%{opacity:100%}60%{opacity:70%}70%{opacity:100%}80%{opacity:90%}100%{opacity:100%}}@keyframes cyber-glow-pulse{0%,100%{box-shadow:0 0 5px currentcolor;filter:brightness(1)}50%{box-shadow:0 0 20px currentcolor,0 0 40px currentcolor;filter:brightness(1.2)}}@keyframes cyber-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}@keyframes cyber-ping{75%,100%{opacity:0%;transform:scale(2)}}@keyframes cyber-bounce{0%,100%{transform:translateY(-25%);animation-timing-function:cubic-bezier(0.8, 0, 1, 1)}50%{transform:translateY(0);animation-timing-function:cubic-bezier(0, 0, 0.2, 1)}}.cyber-animate-pulse{animation:cyber-pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite}.cyber-animate-float{animation:cyber-float 3s ease-in-out infinite}.cyber-boot{animation:cyber-flicker-in .8s ease-out forwards}.cyber-animate-glow{animation:cyber-glow-pulse 2s ease-in-out infinite}.cyber-animate-spin{animation:cyber-spin 1s linear infinite}.cyber-animate-ping{animation:cyber-ping 1s cubic-bezier(0, 0, 0.2, 1) infinite}.cyber-animate-bounce{animation:cyber-bounce 1s infinite}.cyber-animate-none{animation:none}.cyber-animate-paused{animation-play-state:paused}.cyber-animate-running{animation-play-state:running}.cyber-delay-100{animation-delay:100ms}.cyber-delay-200{animation-delay:200ms}.cyber-delay-300{animation-delay:300ms}.cyber-delay-500{animation-delay:500ms}.cyber-delay-1000{animation-delay:1000ms}.cyber-duration-fast{animation-duration:150ms}.cyber-duration-normal{animation-duration:300ms}.cyber-duration-slow{animation-duration:500ms}.cyber-duration-slower{animation-duration:1000ms}.cyber-transition{transition-duration:var(--transition-base);transition-property:color,background-color,border-color,box-shadow,transform,opacity}.cyber-transition-fast{transition-duration:var(--transition-fast)}.cyber-transition-slow{transition-duration:var(--transition-slow)}.cyber-transition-none{transition:none}@media(prefers-reduced-motion: reduce){.cyber-animate-pulse,.cyber-animate-float,.cyber-boot,.cyber-animate-glow,.cyber-animate-spin,.cyber-animate-ping,.cyber-animate-bounce{animation:none}.cyber-transition{transition:none}}}@layer utilities{@keyframes cyber-icon-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}@keyframes cyber-icon-pulse{0%,100%{opacity:100%;transform:scale(1)}50%{opacity:60%;transform:scale(0.95)}}@keyframes cyber-icon-glitch{0%,100%{transform:translate(0);filter:none}10%{transform:translate(-2px, 1px);filter:drop-shadow(2px 0 var(--cyber-cyan-500)) drop-shadow(-2px 0 var(--cyber-magenta-500))}20%{transform:translate(2px, -1px);filter:drop-shadow(-1px 0 var(--cyber-cyan-500)) drop-shadow(1px 0 var(--cyber-magenta-500))}30%{transform:translate(0);filter:none}70%{transform:translate(1px, 1px);filter:drop-shadow(-2px 0 var(--cyber-cyan-500)) drop-shadow(2px 0 var(--cyber-magenta-500))}80%{transform:translate(-1px, -1px);filter:drop-shadow(1px 0 var(--cyber-cyan-500)) drop-shadow(-1px 0 var(--cyber-magenta-500))}90%{transform:translate(0);filter:none}}.cyber-icon--sm{width:1rem;height:1rem}.cyber-icon--md{width:1.5rem;height:1.5rem}.cyber-icon--lg{width:2rem;height:2rem}.cyber-icon--xl{width:3rem;height:3rem}.cyber-icon--cyan{color:var(--cyber-cyan-500)}.cyber-icon--magenta{color:var(--cyber-magenta-500)}.cyber-icon--yellow{color:var(--cyber-yellow-500)}.cyber-icon--green{color:var(--cyber-green-500)}.cyber-icon--spin{animation:cyber-icon-spin 1s linear infinite}.cyber-icon--pulse{animation:cyber-icon-pulse 2s ease-in-out infinite}.cyber-icon--glitch{animation:cyber-icon-glitch 3s infinite}.cyber-icon--inline{display:inline-flex;align-items:center;vertical-align:middle}@media(prefers-reduced-motion: reduce){.cyber-icon--spin,.cyber-icon--pulse,.cyber-icon--glitch{animation:none}}}

/*# sourceMappingURL=cybercore.min.css.map */
/*! @laddtnov/cyberpunk-ui | MIT | github.com/laddtnov/cyberpunk-ui */
/* Generated by scripts/build-bundle.js — edit the parts, not this file. */

/* ── tokens.css ────────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

:root {
  /* ── Neon accents ─────────────────────────────────────────── */
  --cy-neon-cyan:   #00f2ff;
  --cy-neon-pink:   #ff00ff;
  --cy-neon-purple: #9d00ff;

  /* Brass gold, the Deus Ex register rather than the neon one: a warmer,
     lower-saturation accent for text that should read as machined rather
     than lit. Deliberately not --cy-warning, which is a pale yellow with a
     meaning attached — this one carries none, and mixing them would make a
     decorative heading look like a caution. */
  --cy-neon-gold:   #d4af37;

  /* ── Surfaces ─────────────────────────────────────────────── */
  --cy-bg:      #0a0a0c;
  --cy-surface: #151518;

  /* ── Text ─────────────────────────────────────────────────── */
  --cy-text:    #d1d1d1;
  --cy-heading: #ffffff;

  /* ── Scrim ───────────────────────────────────────────────────
     The modal backdrop, and deliberately NOT derived from --cy-bg: a
     scrim's job is to push the page behind it away, so it stays dark in
     both themes. Tying it to --cy-bg made the light theme paint a
     near-white wash over a near-white page, which separated nothing.

     It is a finished colour rather than a hue plus an -rgb twin because
     nothing fades it further — the alpha is the point. */
  --cy-backdrop: rgba(10, 10, 12, 0.72);

  /* ── Type ─────────────────────────────────────────────────────
     Load these fonts yourself (Google Fonts / self-hosted); the kit
     degrades to system fonts if they're absent. */
  --cy-font-display: 'Orbitron', system-ui, sans-serif;
  --cy-font-body:    'Rajdhani', system-ui, sans-serif;
  --cy-font-mono:    'Share Tech Mono', ui-monospace, monospace;

  /* The terminal's own stack, and the only place the kit asks for a Nerd
     Font. Those are patched builds of ordinary monospaces carrying an extra
     few thousand glyphs — powerline separators, file-type and branch icons —
     which is exactly what makes a fake terminal look like a real one.

     They are named first and never shipped. The patched TTFs run to tens of
     megabytes against a kit measured in kilobytes, so this token asks for what
     a developer machine very often already has and falls through to
     --cy-font-mono when it does not. Nothing breaks without them; the icons
     simply are not there.

     Kept separate from --cy-font-mono on purpose: Hack is widely installed,
     and putting it first in the shared stack would quietly repaint every mono
     surface in the kit — nav, badges, inputs, breadcrumb — on most developer
     machines, which is not a font choice this kit should make for you. */
  --cy-font-terminal: 'Hack Nerd Font Mono', 'JetBrainsMono Nerd Font',
                      'FiraCode Nerd Font', var(--cy-font-mono);

  /* ── Motion ───────────────────────────────────────────────── */
  --cy-ease: cubic-bezier(0.22, 1, 0.36, 1);

  /* ── Geometry ─────────────────────────────────────────────── */
  --cy-radius-sm: 2px;
  --cy-radius:    4px;
  --cy-radius-lg: 8px;
  --cy-border-width: 1px;

  /* ── Spacing (4px-based scale) ────────────────────────────── */
  --cy-space-xs: 0.25rem;
  --cy-space-sm: 0.5rem;
  --cy-space-md: 0.75rem;
  --cy-space-lg: 1rem;
  --cy-space-xl: 1.5rem;

  /* ── Focus (shared by every interactive element) ──────────── */
  --cy-focus-width:  2px;
  --cy-focus-offset: 2px;
  --cy-focus-color:  var(--cy-neon-cyan);

  /* ── Status ────────────────────────────────────────────────
     --cy-info aliases the cyan on purpose: its channels are already
     published as --cy-cyan-rgb, so there is one source of truth. */
  --cy-success: #52ff9a;
  --cy-warning: #ffc857;
  --cy-danger:  #ff4d4d;
  --cy-info:    var(--cy-neon-cyan);

  --cy-disabled-opacity: 0.45;
}

/* ── Light theme ──────────────────────────────────────────────
   The bright neon hues fail WCAG contrast on light backgrounds
   (cyan is ~1.2:1 on white), so cyan/pink are dimmed here. Purple
   already passes (~5.5:1) and is left untouched. Toggle by setting
   data-theme="light" on the <html> element. */
:root[data-theme="light"] {
  /* #008099 measured 4.07 against --cy-bg — below the 4.5 AA floor for
     normal text. #00707f measures 5.10. */
  --cy-neon-cyan: #00707f;
  --cy-neon-pink: #b3009e;
  /* #d4af37 measures 1.85 on the light background — invisible. This is the
     same hue taken down until it clears the text floor: 5.66. */
  --cy-neon-gold: #7d5800;

  --cy-bg:      #f0f0f5;
  --cy-surface: #ffffff;
  --cy-text:    #3a3a45;
  --cy-heading: #16161d;

  /* Neon status hues fail contrast on a light background. */
  --cy-success: #0a7d43;
  --cy-warning: #8a5a00;
  --cy-danger:  #c2110f;

}

/* ── Light theme, chosen by the operating system ──────────────────
   The block above only fires when something sets data-theme="light" — a
   script, or server-rendered markup. That leaves a visitor whose OS is in
   light mode looking at neon-on-black, which is the one audience the light
   palette exists for.

   Opt in with data-theme="auto" on <html>, which is static markup, not a
   script — the thing this block exists to avoid needing.

   It is opt-in rather than automatic on purpose. `prefers-color-scheme: light`
   matches when a user has expressed no preference at all, not only when they
   have chosen light, so keying this off the absence of data-theme would have
   flipped the kit to light for most visitors and silently restyled every site
   already using it. A neon kit defaults to neon; the light palette is there
   for people who ask, by attribute or by operating system.

   The values are duplicated rather than shared, because a media query cannot
   join a selector list — there is no way to say "this block, and also under
   that condition". check-conventions.js compares the two declaration by
   declaration and fails when they drift, so the cost of duplicating is paid
   once in a checker rather than every time someone edits a colour. */
@media (prefers-color-scheme: light) {
  :root[data-theme="auto"] {

    /* #008099 measured 4.07 against --cy-bg — below the 4.5 AA floor for
       normal text. #00707f measures 5.10. */
    --cy-neon-cyan: #00707f;
    --cy-neon-pink: #b3009e;
    /* #d4af37 measures 1.85 on the light background — invisible. This is the
       same hue taken down until it clears the text floor: 5.66. */
    --cy-neon-gold: #7d5800;

    --cy-bg:      #f0f0f5;
    --cy-surface: #ffffff;
    --cy-text:    #3a3a45;
    --cy-heading: #16161d;

    /* Neon status hues fail contrast on a light background. */
    --cy-success: #0a7d43;
    --cy-warning: #8a5a00;
    --cy-danger:  #c2110f;

  }
}

/* ── Increased contrast ───────────────────────────────────────────
   `prefers-contrast: more` is a request to make everything easier to see,
   and most of the answer belongs here rather than in the components: raising
   a token reaches every rule that consumes it, which is the entire reason
   the substrate exists.

   Body text goes to the extremes of each theme, borders and focus rings get
   thicker, and disabled controls become more legible — "disabled" is still
   communicated by the cursor, the styling and the semantics, so dimming it
   to near-invisible was never carrying the meaning on its own.

   Individual borders in the component files use their own alpha values and
   cannot be lifted from here; each file raises its own alongside this.

   These values are strictly higher-contrast than the base ones they replace,
   so `scripts/check-contrast.js` — which reads the first `:root` block per
   theme — stays authoritative for the floors. */
@media (prefers-contrast: more) {
  :root {
    --cy-text: #ffffff;
    --cy-border-width: 2px;
    --cy-focus-width: 3px;
    --cy-disabled-opacity: 0.7;
  }

  :root[data-theme="light"] {
    --cy-text: #16161d;
  }
}

}

/* ── effects.css ───────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* ── 1. Box glow ──────────────────────────────────────────────── */
.cy-glow         { box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent), 0 0 30px color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent); }
.cy-glow--pink   { box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-pink) 50%, transparent), 0 0 30px color-mix(in srgb, var(--cy-neon-pink) 25%, transparent); }
.cy-glow--purple { box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-purple) 50%, transparent), 0 0 30px color-mix(in srgb, var(--cy-neon-purple) 25%, transparent); }
.cy-glow--gold   { box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-gold) 50%, transparent), 0 0 30px color-mix(in srgb, var(--cy-neon-gold) 25%, transparent); }

/* ── 2. Text glow ─────────────────────────────────────────────── */
.cy-text-glow       { text-shadow: 0 0 10px var(--cy-neon-cyan), 0 0 24px color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent); }
.cy-text-glow--pink { text-shadow: 0 0 10px var(--cy-neon-pink), 0 0 24px color-mix(in srgb, var(--cy-neon-pink) 50%, transparent); }

/* Gold reads differently from the neon pair and the numbers say why: it is a
   warm hue at a lower saturation, so the same 10px halo that makes cyan look
   lit makes gold look smeared. The near shadow is tightened and the far one
   dimmed — the effect is a machined edge catching light rather than a tube
   glowing, which is the register this colour belongs to. */
.cy-text-glow--gold { text-shadow: 0 0 6px color-mix(in srgb, var(--cy-neon-gold) 90%, transparent), 0 0 18px color-mix(in srgb, var(--cy-neon-gold) 35%, transparent); }

/* ── 3. Glitch text (RGB split) ───────────────────────────────── */
.cy-glitch {
  color: var(--cy-neon-cyan);
  text-shadow: 0 0 24px var(--cy-neon-cyan), 0 0 60px color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent);
  animation: cy-glitch 2.5s infinite;
}

@keyframes cy-glitch {
  0%, 90%, 100% { text-shadow: 0 0 24px var(--cy-neon-cyan), 0 0 60px color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent); }
  92% { text-shadow: -3px 0 var(--cy-neon-pink), 3px 0 var(--cy-neon-purple); }
  94% { text-shadow: 0 0 24px var(--cy-neon-cyan), 0 0 60px color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent); }
  96% { text-shadow:  3px 0 var(--cy-neon-pink), -3px 0 var(--cy-neon-purple); }
}

/* ── 4. Scanlines overlay ─────────────────────────────────────────
   Apply to any element; a CRT scanline veil is painted on top via
   ::after without intercepting clicks. */
.cy-scanlines { position: relative; }
.cy-scanlines::after {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
  background: repeating-linear-gradient(
    to bottom,
    rgba(0, 0, 0, 0)    0,
    rgba(0, 0, 0, 0)    2px,
    rgba(0, 0, 0, 0.18) 3px,
    rgba(0, 0, 0, 0)    4px
  );
}

/* ── 5. Animated grid backdrop ────────────────────────────────────
   Animates via background-position so it can sit on any element
   without hijacking its transform. */
.cy-grid-bg {
  background-image:
    linear-gradient(color-mix(in srgb, var(--cy-neon-cyan) 10%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--cy-neon-cyan) 10%, transparent) 1px, transparent 1px);
  background-size: 50px 50px;
  animation: cy-grid-move 20s linear infinite;
}

@keyframes cy-grid-move {
  0%   { background-position: 0 0; }
  100% { background-position: 0 50px; }
}

/* ── 6. Blinking terminal cursor ──────────────────────────────────
   Renders a blinking "_" after the element. */
.cy-cursor {
  display: inline-block;
  color: var(--cy-neon-cyan);
  animation: cy-cursor-blink 1s step-end infinite;
}
.cy-cursor::after { content: "_"; }

@keyframes cy-cursor-blink {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0; }
}

/* ── WCAG: honour reduced-motion ──────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .cy-glitch,
  .cy-grid-bg,
  .cy-cursor { animation: none; }
}

/* ── Forced colours ───────────────────────────────────────────────
   Windows High Contrast Mode and friends replace the palette with the
   user's own. This file is where the kit gives up the most, and it should:
   every effect here is decoration, and decoration is exactly what a user
   asking for forced colours is asking to be rid of.

   The removals are explicit rather than left to the UA. `text-shadow` is
   not forced by the feature, so a cyan glow can survive underneath forced
   text and muddy it; `background-image` is not forced either, so the grid
   and the scanline veil would keep painting over content whose colours
   have changed underneath them. The scanline overlay is the worst of these
   — it is a translucent black wash sitting on top of the text, which is a
   direct contrast reduction applied to someone who has asked for more. */
@media (forced-colors: active) {
  .cy-glow,
  .cy-glow--pink,
  .cy-glow--purple,
  .cy-glow--gold { box-shadow: none; }

  .cy-text-glow,
  .cy-text-glow--pink,
  .cy-text-glow--gold,
  .cy-glitch { text-shadow: none; }

  /* The RGB split is drawn entirely in text-shadow, so with the shadow
     gone the animation has nothing left to move. */
  .cy-glitch { animation: none; }

  .cy-scanlines::after { background: none; }

  .cy-grid-bg {
    background-image: none;
    animation: none;
  }
}

}

/* ── components.css ────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* ── Neon button ──────────────────────────────────────────────── */
.cy-btn {
  display: inline-block;
  font-family: var(--cy-font-mono);
  font-size: 0.95rem;
  letter-spacing: 0.1em;
  padding: var(--cy-space-sm) var(--cy-space-xl);
  color: var(--cy-neon-cyan);
  background: transparent;
  border: var(--cy-border-width) solid var(--cy-neon-cyan);
  border-radius: var(--cy-radius);
  cursor: pointer;
  text-decoration: none;
  transition: color 0.25s, background 0.25s, box-shadow 0.25s, transform 0.2s;
}

.cy-btn:hover,
.cy-btn:focus-visible {
  color: var(--cy-bg);
  background: var(--cy-neon-cyan);
  box-shadow: 0 0 14px var(--cy-neon-cyan), 0 0 30px color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent);
  transform: translateY(-1px);
}

.cy-btn:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

.cy-btn--pink {
  color: var(--cy-neon-pink);
  border-color: var(--cy-neon-pink);
}

.cy-btn--pink:hover,
.cy-btn--pink:focus-visible {
  background: var(--cy-neon-pink);
  box-shadow: 0 0 14px var(--cy-neon-pink), 0 0 30px color-mix(in srgb, var(--cy-neon-pink) 50%, transparent);
}

/* ── Holo card (glassmorphism + edge glow) ────────────────────── */
.cy-card {
  background: color-mix(in srgb, var(--cy-neon-cyan) 3%, transparent);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent);
  border-radius: var(--cy-radius-lg);
  padding: var(--cy-space-xl);
  color: var(--cy-text);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 0 24px color-mix(in srgb, var(--cy-neon-cyan) 12%, transparent);
  transition: border-color 0.3s, box-shadow 0.3s, transform 0.3s;
}

.cy-card:hover {
  border-color: color-mix(in srgb, var(--cy-neon-cyan) 60%, transparent);
  box-shadow: 0 0 32px color-mix(in srgb, var(--cy-neon-cyan) 30%, transparent);
  transform: translateY(-2px);
}

/* ── WCAG: honour reduced-motion ──────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .cy-btn,
  .cy-card { transition: none; }
  .cy-btn:hover,
  .cy-card:hover { transform: none; }
}

/* ── Button variants ──────────────────────────────────────────── */

/* Secondary: a quieter outline. Deliberately not an alias of --pink,
   which stays a distinct brand accent. */
.cy-btn--secondary {
  color: var(--cy-text);
  border-color: var(--cy-text);
}

.cy-btn--secondary:hover,
.cy-btn--secondary:focus-visible {
  color: var(--cy-bg);
  background: var(--cy-text);
  box-shadow: none;
}

.cy-btn--danger {
  color: var(--cy-danger);
  border-color: var(--cy-danger);
}

.cy-btn--danger:hover,
.cy-btn--danger:focus-visible {
  color: var(--cy-bg);
  background: var(--cy-danger);
  box-shadow: 0 0 14px var(--cy-danger),
              0 0 30px color-mix(in srgb, var(--cy-danger) 50%, transparent);
}

/* ── Button sizes ─────────────────────────────────────────────── */
.cy-btn--sm {
  font-size: 0.8rem;
  padding: var(--cy-space-xs) var(--cy-space-md);
}

.cy-btn--lg {
  font-size: 1.05rem;
  padding: var(--cy-space-md) var(--cy-space-xl);
}

/* ── Disabled ─────────────────────────────────────────────────── */
.cy-btn:disabled,
.cy-btn[aria-disabled="true"] {
  opacity: var(--cy-disabled-opacity);
  cursor: not-allowed;
}

.cy-btn:disabled:hover,
.cy-btn:disabled:focus-visible,
.cy-btn[aria-disabled="true"]:hover,
.cy-btn[aria-disabled="true"]:focus-visible {
  color: inherit;
  background: transparent;
  box-shadow: none;
  transform: none;
}

/* Native :disabled elements cannot receive focus, so removing the outline
   here is safe. [aria-disabled="true"] elements deliberately remain in the
   tab order (see forms.css/README for the pattern), so their outline must
   stay — otherwise a keyboard user can focus a control with no visible
   indicator, violating WCAG 2.4.7. */
.cy-btn:disabled:hover,
.cy-btn:disabled:focus-visible {
  outline: none;
}

/* ── Keycap ───────────────────────────────────────────────────────
   On <kbd>, which already means "the user presses this". All this adds is
   looking pressable.

   Sized in `em` rather than from the spacing scale, because a keycap belongs
   to the sentence it sits in: at 0.85em it tracks the surrounding type instead
   of standing at a fixed size inside a heading and inside body text alike.
   `min-inline-size` is what keeps a single letter from collapsing to a sliver
   while `⌘` and `Esc` stay their own width. */
.cy-kbd {
  display: inline-block;
  min-inline-size: 1.6em;
  padding: 0.1em 0.45em;
  font-family: var(--cy-font-mono);
  font-size: 0.85em;
  line-height: 1.5;
  text-align: center;
  white-space: nowrap;
  color: var(--cy-neon-cyan);
  background: color-mix(in srgb, var(--cy-neon-cyan) 8%, var(--cy-surface));
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent);
  border-radius: var(--cy-radius-sm);
  /* The only shadow here is the cap's depth, not a glow: a hard inset rim on
     the block end reads as a physical edge. Logical, so it stays at the bottom
     in vertical writing modes rather than flipping to a side. */
  box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--cy-neon-cyan) 20%, transparent);
}

/* ── Readout (term / value pairs) ─────────────────────────────────
   On <dl>. A HUD stat block: gold mono labels in a column that sizes itself
   to the longest one, values aligned against them.

   Two columns assigned explicitly rather than left to auto-placement, so a
   term with several <dd> values stacks them in the value column instead of
   pushing the second one under the label. */
.cy-dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--cy-space-sm) var(--cy-space-lg);
  /* The label is smaller than the value, so the default stretch leaves the
     two sitting at different heights in the same row. Baseline alignment is
     what makes a term read as belonging to its value. */
  align-items: baseline;
  margin: 0;
  color: var(--cy-text);
}

.cy-dl > dt {
  grid-column: 1;
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cy-neon-gold);
}

.cy-dl > dd {
  grid-column: 2;
  /* The UA puts 40px on the inline start of every <dd>, and the grid column
     is doing that job here. Logical, so RTL is not a special case. */
  margin-inline-start: 0;
  margin-block: 0;
}

/* ── Figure ───────────────────────────────────────────────────────
   On <figure>. The UA margin is `1em 40px`, which indents a figure out of
   alignment with everything around it; the frame replaces it.

   <svg> and <video> are listed beside <img> because a diagram and a clip are
   the same kind of content here, and leaving them out would mean a figure that
   frames a picture but lets a video overflow it. */
.cy-figure {
  margin: 0 0 var(--cy-space-lg);
  padding: var(--cy-space-sm);
  background: color-mix(in srgb, var(--cy-neon-cyan) 3%, transparent);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent);
  border-radius: var(--cy-radius);
}

.cy-figure > img,
.cy-figure > svg,
.cy-figure > video {
  display: block;
  max-inline-size: 100%;
  block-size: auto;
  border-radius: var(--cy-radius-sm);
}

.cy-figure > figcaption {
  padding: var(--cy-space-sm) var(--cy-space-xs) 0;
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  color: color-mix(in srgb, var(--cy-text) 75%, transparent);
}

/* ── Quote ────────────────────────────────────────────────────────
   On <blockquote>. An accent rule on the inline start, no decorative glyph:
   a `"` from ::before is content a screen reader may or may not announce, and
   a quotation already announces itself.

   The rule is a multiple of --cy-border-width rather than a flat 3px, so
   `prefers-contrast: more` thickens it along with every other border in the
   kit instead of leaving this one behind. */
.cy-quote {
  margin: 0 0 var(--cy-space-lg);
  padding: var(--cy-space-md) var(--cy-space-lg);
  border-inline-start: calc(var(--cy-border-width) * 3) solid var(--cy-neon-pink);
  background: color-mix(in srgb, var(--cy-neon-pink) 5%, transparent);
  color: var(--cy-text);
  font-size: 1.05rem;
}

/* A <blockquote> wraps block content, so its last paragraph carries a bottom
   margin that would otherwise show as uneven padding. */
.cy-quote > :last-child {
  margin-block-end: 0;
}

.cy-quote cite {
  display: block;
  margin-block-start: var(--cy-space-sm);
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  /* <cite> is italic by default. Mono and italic together read as a mistake
     rather than as an attribution. */
  font-style: normal;
  letter-spacing: 0.1em;
  color: var(--cy-neon-pink);
}

/* ── Increased contrast ───────────────────────────────────────── */
@media (prefers-contrast: more) {
  .cy-btn,
  .cy-card,
  .cy-kbd,
  .cy-figure { border-color: var(--cy-neon-cyan); }

  /* Both are deliberately dimmed below body text at rest — a caption and a
     label are secondary. `prefers-contrast: more` is the request that says
     stop doing that. */
  .cy-figure > figcaption { color: var(--cy-text); }
  .cy-dl > dt { color: var(--cy-heading); }
}

/* ── Forced colours ───────────────────────────────────────────────
   Both components carry a real border, so the UA forces it to the user's
   text colour and the shape survives on its own. What does not survive is
   the glow, and `.cy-btn:disabled` leans on opacity — which forced colours
   does not touch — so the system's own disabled colour is named instead.
   GrayText is the one the platform already uses for this, which is the
   point: in forced colours the user's conventions win, not ours. */
@media (forced-colors: active) {
  .cy-btn,
  .cy-card,
  .cy-kbd { box-shadow: none; }

  .cy-btn:disabled {
    color: GrayText;
    border-color: GrayText;
    opacity: 1;
  }

  /* The keycap and the figure both carry a real border, so the UA forces it to
     the user's text colour and the shape survives on its own. The quote's
     accent rule is forced the same way, which is the whole of its shape.
     What does not survive is a tinted fill: forced colours replaces the
     background with the user's, and these mixes would otherwise fight it. */
  .cy-figure,
  .cy-quote { background: none; }

  /* Two places the kit dims text with a mix or an accent hue. Forced colours
     has one text colour and the user picked it. */
  .cy-figure > figcaption,
  .cy-dl > dt,
  .cy-quote cite { color: CanvasText; }
}

}

/* ── containers.css ────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* Opt-in classes only. Styling bare `details {}` or `dialog {}` would hijack
   every disclosure and modal on a consumer's page the moment they import the
   kit.

   All three components here are built on a native element, so the behaviour
   ships with the browser rather than with the kit: <details> brings its own
   keyboard handling and open state, <dialog> brings focus trapping, Esc and
   the top layer. No JavaScript ships, ever. */

/* ── Accordion ────────────────────────────────────────────────────
   <details class="cy-accordion">
     <summary>Heading</summary>
     <div class="cy-accordion__body">…</div>
   </details>

   The summary is styled through the class rather than given one of its own:
   `.cy-accordion > summary` is still opt-in, since it cannot match without
   the wrapper, and it keeps the consumer's markup contract to one class.
   The body needs its own class because there is no way to select "everything
   that is not the summary" that stays readable. */
.cy-accordion {
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 35%, transparent);
  border-radius: var(--cy-radius);
  margin-bottom: var(--cy-space-sm);
  overflow: hidden;
}

.cy-accordion > summary {
  display: flex;
  align-items: center;
  gap: var(--cy-space-sm);
  padding: var(--cy-space-sm) var(--cy-space-md);
  font-family: var(--cy-font-mono);
  font-size: 0.85rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--cy-neon-cyan);
  cursor: pointer;
  transition: background 0.2s, color 0.2s;

  /* Two removals for one marker. `display: flex` already drops the
     ::marker in engines that follow the spec, list-style covers the rest,
     and WebKit needs its own pseudo-element hidden explicitly. */
  list-style: none;
}

.cy-accordion > summary::-webkit-details-marker { display: none; }

.cy-accordion > summary:hover {
  background: color-mix(in srgb, var(--cy-neon-cyan) 6%, transparent);
}

/* The ring is not negotiable — a disclosure is keyboard-operated by
   definition, and the summary is the thing that takes the focus. */
.cy-accordion > summary:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: calc(var(--cy-focus-offset) * -1);
}

/* Same rotated-border technique as the checkbox tick in forms.css, so the
   two read as one family. `margin-left: auto` pins it to the trailing edge
   whatever the heading length. */
.cy-accordion > summary::after {
  content: "";
  width: 0.4rem;
  height: 0.4rem;
  flex-shrink: 0;
  margin-inline-start: auto;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  rotate: 45deg;
  transition: rotate 0.2s var(--cy-ease);
}

.cy-accordion[open] > summary::after { rotate: 225deg; }

.cy-accordion[open] > summary {
  border-bottom: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 20%, transparent);
}

.cy-accordion__body {
  padding: var(--cy-space-md);
  color: var(--cy-text);
}

/* ── Modal ────────────────────────────────────────────────────────
   <dialog class="cy-modal">…</dialog>

   Open it with showModal(), not show(). Only showModal() puts the dialog in
   the top layer, traps focus, closes on Esc and renders ::backdrop at all —
   show() gives a non-modal box with none of that, and no backdrop to style.
   That call is the consumer's; the kit only paints the result. */
.cy-modal {
  padding: var(--cy-space-lg);
  max-width: min(90vw, 32rem);
  background: var(--cy-surface);
  color: var(--cy-text);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent);
  border-radius: var(--cy-radius-lg);
  box-shadow: 0 0 30px color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent);
}

/* The fallback is not belt-and-braces: ::backdrop has historically not
   inherited custom properties from the page, so var(--cy-backdrop) can fail
   to resolve here even though it is defined on :root. Without a fallback that
   would leave the backdrop transparent and the page fully legible behind an
   open modal. The literal repeats --cy-backdrop's value; keep them in step. */
.cy-modal::backdrop {
  background: var(--cy-backdrop, rgba(10, 10, 12, 0.72));
  backdrop-filter: blur(6px);
}

/* ── Terminal / code window ───────────────────────────────────────
   <div class="cy-terminal">
     <div class="cy-terminal__bar">~/ops</div>
     <pre>…</pre>
   </div>

   The bar is optional; a terminal with no chrome is a valid code block.
   Combine with .cy-scanlines and .cy-cursor from effects.css for the full
   CRT treatment — they are kept separate so the frame can be used plain. */
.cy-terminal {
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 35%, transparent);
  border-radius: var(--cy-radius);
  box-shadow: 0 0 18px color-mix(in srgb, var(--cy-neon-cyan) 12%, transparent);
  overflow: hidden;
}

.cy-terminal__bar {
  display: flex;
  align-items: center;
  gap: var(--cy-space-sm);
  padding: var(--cy-space-xs) var(--cy-space-md);
  background: color-mix(in srgb, var(--cy-neon-cyan) 8%, transparent);
  border-bottom: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent);
  font-family: var(--cy-font-terminal);
  font-size: 0.7rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--cy-neon-cyan);
}

/* Scoped to the frame, so a bare <pre> elsewhere on the page is untouched. */
.cy-terminal > pre {
  margin: 0;
  padding: var(--cy-space-md);
  overflow-x: auto;
  font-family: var(--cy-font-terminal);
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--cy-text);
}

/* ── Toggle-tip ───────────────────────────────────────────────────
   <button class="cy-btn cy-btn--sm" popovertarget="tip">?</button>
   <div class="cy-popover" popover id="tip">
     Visible to other operatives.
   </div>

   A tooltip you can actually reach. The kit declined tooltips for four
   versions because the CSS-only version is hover-only: keyboard users never
   see it, touch users never see it, and a screen reader is never told it
   exists. None of that is fixable with :hover.

   The Popover API fixes it by making this a *toggle*-tip instead — the
   trigger is a real button, so it is focusable, activatable by Enter and
   Space, and announced with its expanded state. The browser handles
   light-dismiss, Escape, and the top layer. No JavaScript, which is what took
   so long: `popover` only became Baseline in January 2025.

   It is not a hover tooltip and will not become one here. That needs interest
   invokers, which are Chrome-only at the time of writing — and a hover-only
   affordance is the thing this component exists to avoid.

   Positioning: the browser centres a popover in the viewport, which is a fine
   place for a short explanation and works everywhere. Where CSS anchor
   positioning is supported it is pinned above its trigger instead. That is an
   enhancement rather than the design, because anchor positioning is still
   Chrome-only. */
.cy-popover {
  margin: 0;
  max-width: 32ch;
  font-family: var(--cy-font-mono);
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--cy-text);
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 45%, transparent);
  border-radius: var(--cy-radius);
  padding: var(--cy-space-md) var(--cy-space-lg);
  box-shadow: 0 0 20px color-mix(in srgb, var(--cy-neon-cyan) 20%, transparent);
}

.cy-popover:popover-open {
  animation: cy-popover-in 0.15s var(--cy-ease);
}

@keyframes cy-popover-in {
  from { opacity: 0; transform: translateY(0.25rem); }
  to   { opacity: 1; transform: translateY(0); }
}

/* Pinned to its trigger where the engine can do it, centred everywhere else.
   position-area needs both halves of the anchor feature, so both are tested. */
@supports (anchor-name: --x) and (position-area: top) {
  /* Scoped to .cy-btn rather than to [popovertarget] alone. A bare attribute
     selector would claim every popover trigger on a consumer's page, which is
     the one thing this kit never does — and the convention checker caught it
     here rather than in review. The trigger is a button either way. */
  .cy-btn[popovertarget] { anchor-name: --cy-popover-anchor; }

  .cy-popover {
    position-anchor: --cy-popover-anchor;
    position-area: top;
    margin-bottom: var(--cy-space-sm);
    position-try-fallbacks: flip-block;
  }
}

/* ── Reduced motion ───────────────────────────────────────────────
   The chevron still turns — it reports the open state, so freezing it would
   remove information rather than decoration. Only the tween goes. */
@media (prefers-reduced-motion: reduce) {
  .cy-accordion > summary,
  .cy-accordion > summary::after { transition: none; }
  .cy-popover:popover-open { animation: none; }
  .cy-modal::backdrop { backdrop-filter: none; }
}

/* ── Increased contrast ───────────────────────────────────────── */
@media (prefers-contrast: more) {
  .cy-accordion,
  .cy-modal,
  .cy-terminal { border-color: var(--cy-neon-cyan); }

  .cy-accordion[open] > summary,
  .cy-terminal__bar { border-bottom-color: var(--cy-neon-cyan); }
}

/* ── Forced colours ───────────────────────────────────────────────
   All three keep their frames, since a forced border-color is still a
   border. The chevron survives too — it is drawn in `currentColor`, so it
   follows the summary's forced text colour without being told to.

   The backdrop is the exception worth spelling out. It is the only thing
   separating an open modal from the page behind it, and it is built from a
   translucent fill plus a blur. Forced colours does not reliably keep
   either, and a modal that cannot be told apart from the page underneath is
   a worse failure than a modal that looks plain. It is pinned to the
   system's own window background so the separation is never in doubt, and
   the blur — decoration, and expensive — goes. */
@media (forced-colors: active) {
  .cy-modal,
  .cy-popover,
  .cy-terminal { box-shadow: none; }

  /* The border is what separates a popover from the page once the glow and
     the surface tint are gone, so it is pinned rather than left translucent. */
  .cy-popover {
    background: Canvas;
    border-color: CanvasText;
  }

  .cy-modal::backdrop {
    background: Canvas;
    backdrop-filter: none;
    opacity: 0.85;
  }
}

}

/* ── navigation.css ────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* Opt-in classes only. Neither component here gets behaviour from its element
   the way <details> and <dialog> do — a <nav> is a landmark, not a widget, so
   these are styling plus a markup contract rather than paint over a machine.
   What the elements do give is semantics, and the contract exists to keep it:
   a breadcrumb that is not a list announces as a run of loose links.

   There is deliberately no --active modifier on either component. The current
   item is marked with aria-current, which the kit styles directly. A class
   would let you build a nav whose active item looks current and announces as
   ordinary — the exact shape of failure the rest of the kit is written to
   avoid. Using the attribute means the styling cannot drift from the
   announcement, because they are the same declaration. */

/* ── Nav bar ──────────────────────────────────────────────────────
   <nav class="cy-nav" aria-label="Main">
     <a class="cy-nav__brand" href="/">&gt;_ site</a>
     <a href="/docs">Docs</a>
     <a href="/api" aria-current="page">API</a>
   </nav>

   Links are styled through the wrapper rather than given a class each, which
   keeps the markup contract to one class on the <nav> itself. */
.cy-nav {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--cy-space-sm) var(--cy-space-lg);
  padding: var(--cy-space-sm) var(--cy-space-lg);
  background: var(--cy-surface);
  border-bottom: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 30%, transparent);
  font-family: var(--cy-font-mono);
}

.cy-nav a {
  color: var(--cy-text);
  font-size: 0.85rem;
  letter-spacing: 0.08em;
  text-decoration: none;
  padding: var(--cy-space-xs) 0;
  border-bottom: 2px solid transparent;
  transition: color 0.2s, border-color 0.2s;
}

.cy-nav a:hover {
  color: var(--cy-neon-cyan);
  border-bottom-color: color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent);
}

.cy-nav a:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

/* The current page. Styled off the attribute that announces it, never off a
   class — see the note at the top of this file. */
.cy-nav a[aria-current] {
  color: var(--cy-neon-cyan);
  border-bottom-color: var(--cy-neon-cyan);
}

.cy-nav__brand {
  margin-inline-end: auto;
  color: var(--cy-neon-cyan);
  font-size: 0.95rem;
  letter-spacing: 0.1em;
}

/* ── Breadcrumb ───────────────────────────────────────────────────
   <nav aria-label="Breadcrumb">
     <ol class="cy-breadcrumb">
       <li><a href="/">Home</a></li>
       <li><a href="/sector">Sector</a></li>
       <li><a href="/sector/7" aria-current="page">Node 7</a></li>
     </ol>
   </nav>

   The <ol> is not decorative: it is what makes this a trail rather than a row
   of unrelated links, and it is what tells a screen reader how many steps
   there are and which one this is. The wrapping <nav aria-label="Breadcrumb">
   is what lets someone jump to it. Neither can be supplied by CSS. */
.cy-breadcrumb {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--cy-space-xs) var(--cy-space-sm);
  margin: 0;
  padding: 0;
  list-style: none;
  font-family: var(--cy-font-mono);
  font-size: 0.8rem;
}

.cy-breadcrumb li {
  display: flex;
  align-items: center;
  gap: var(--cy-space-sm);
}

/* The separator is decoration and must not be read out. The `/ ""` half of
   this declaration is the alternative text for generated content: it says the
   glyph has no spoken equivalent. Engines that do not support the syntax fall
   back to ignoring generated content or reading the slash — which is why the
   separator is a character with an obvious reading rather than an icon. */
.cy-breadcrumb li + li::before {
  content: "/" / "";
  color: var(--cy-text);
  /* 0.75 is the lowest opacity that clears 4.5:1 against --cy-bg in both
     themes — 7.48:1 dark, 4.92:1 light — which is the same value and the same
     reasoning as .cy-hint in forms.css. The separator is decorative and marked
     as such above, so 1.4.3 does not strictly bind it; it is held to the text
     floor anyway, because a trail whose separators fade out is harder to read
     as a trail, and "technically exempt" is a poor reason to ship something
     dim. At 0.5 it measured 3.88:1 dark and 2.62:1 light. */
  opacity: 0.75;
}

.cy-breadcrumb a {
  color: var(--cy-text);
  text-decoration: none;
  transition: color 0.2s;
}

.cy-breadcrumb a:hover { color: var(--cy-neon-cyan); }

.cy-breadcrumb a:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

/* The last crumb is usually the current page. Marked with aria-current so it
   is announced as such; styled from the same attribute so the two cannot
   disagree. It stays a link rather than becoming plain text, so the trail
   remains navigable when the page is reached from elsewhere. */
.cy-breadcrumb a[aria-current] {
  color: var(--cy-neon-cyan);
  cursor: default;
}

/* ── Sidebar ──────────────────────────────────────────────────────
   <nav class="cy-sidebar" aria-labelledby="sectors">
     <h2 class="cy-sidebar__title" id="sectors">Sectors</h2>
     <ul>
       <li><a href="/grid">Grid</a></li>
       <li><a href="/node" aria-current="page">Node</a></li>
     </ul>
   </nav>

   The <ul> is required, and for the same reason the breadcrumb's <ol> is: it
   is what makes a screen reader say "list, 5 items" and count the way through
   them. A stack of bare links in a column looks identical and announces as a
   run of loose links.

   The title should be a heading, not a <span>. It is a real section heading in
   the document, so it belongs in the heading outline that people use to skip
   around a page — and pairing it with aria-labelledby names the landmark
   without repeating the text. A <span> styles the same and announces nothing;
   that is the whole difference, and it is not visible.

   The sidebar sets no width. Where a sidebar sits and how wide it is are
   layout decisions belonging to the page, not to the component — a kit that
   picks 16rem here is a kit you fight in a grid. It fills whatever column it
   is given. Nor is it sticky or scrollable by default: making it a scroll
   container would oblige it to be keyboard-reachable and labelled (the same
   contract .cy-table-scroll carries), and that is a real cost to impose on
   every sidebar for the sake of the tall ones. */
.cy-sidebar {
  display: flex;
  flex-direction: column;
  gap: var(--cy-space-sm);
  padding: var(--cy-space-lg);
  background: var(--cy-surface);
  border-inline-start: 2px solid color-mix(in srgb, var(--cy-neon-cyan) 30%, transparent);
  font-family: var(--cy-font-mono);
}

.cy-sidebar__title {
  margin: 0 0 var(--cy-space-xs);
  color: var(--cy-neon-cyan);
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}

.cy-sidebar ul {
  display: flex;
  flex-direction: column;
  gap: var(--cy-space-xs);
  margin: 0;
  padding: 0;
  list-style: none;
}

.cy-sidebar a {
  display: block;
  color: var(--cy-text);
  font-size: 0.85rem;
  letter-spacing: 0.06em;
  text-decoration: none;
  padding: var(--cy-space-xs) var(--cy-space-sm);
  border-inline-start: 2px solid transparent;
  border-start-end-radius: var(--cy-radius);
  border-end-end-radius: var(--cy-radius);
  transition: color 0.2s, border-color 0.2s, background 0.2s;
}

.cy-sidebar a:hover {
  color: var(--cy-neon-cyan);
  border-inline-start-color: color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent);
  background: color-mix(in srgb, var(--cy-neon-cyan) 6%, transparent);
}

.cy-sidebar a:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

/* Same rule as the nav above: the current item is styled from the attribute
   that announces it. A marker on the left edge rather than an underline,
   because in a vertical stack an underline reads as a separator between items
   rather than as a mark on one. */
.cy-sidebar a[aria-current] {
  color: var(--cy-neon-cyan);
  border-inline-start-color: var(--cy-neon-cyan);
  background: color-mix(in srgb, var(--cy-neon-cyan) 10%, transparent);
}

/* ── Reduced motion ───────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .cy-nav a,
  .cy-sidebar a,
  .cy-breadcrumb a { transition: none; }
}

/* ── Increased contrast ───────────────────────────────────────── */
@media (prefers-contrast: more) {
  .cy-nav { border-bottom-color: var(--cy-neon-cyan); }
  .cy-sidebar { border-inline-start-color: var(--cy-neon-cyan); }
  .cy-breadcrumb li + li::before { opacity: 1; }
}

/* ── Forced colours ───────────────────────────────────────────────
   The current item is the thing to protect. It is distinguished by colour
   plus an underline in the nav, and by colour alone in the breadcrumb — and
   forced colours collapses every link to one value, so the breadcrumb's
   current crumb would become indistinguishable from its ancestors. Both are
   given the system's own link/text pair, and the breadcrumb's current item
   gains weight so it does not rely on hue at all. */
@media (forced-colors: active) {
  .cy-nav a[aria-current] { border-bottom-color: LinkText; }

  /* The sidebar's current item leans on three signals, and forced colours
     takes two of them: the cyan goes to the one link colour, and the faint
     tint behind it goes to the page background. The left marker is all that
     survives, so it is pinned to a system colour and the label is given weight
     — two signals again, neither of them hue. */
  .cy-sidebar a[aria-current] {
    border-inline-start-color: LinkText;
    font-weight: 700;
  }

  .cy-breadcrumb a[aria-current] {
    color: CanvasText;
    font-weight: 700;
  }
}

}

/* ── table.css ─────────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* Opt-in classes only. The cells are styled through the wrapper — `.cy-table
   th`, `.cy-table td` — so a table elsewhere on the page is untouched and the
   markup contract stays one class.

   A data table is the component where the markup carries the most meaning and
   CSS can supply none of it. `scope` on every header is what lets a screen
   reader say "Sector: Night City" instead of reading a wall of values; a
   <caption> is what names the table; <thead> is what makes a header row a
   header row rather than a bold first line. All three are the consumer's, all
   three are invisible when missing, and the table looks identical either way.
   See docs/components/table.md. */

.cy-table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--cy-font-mono);
  font-size: 0.85rem;
  color: var(--cy-text);
}

.cy-table caption {
  padding-bottom: var(--cy-space-sm);
  color: var(--cy-neon-cyan);
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  text-align: start;
}

.cy-table th,
.cy-table td {
  padding: var(--cy-space-sm) var(--cy-space-md);
  text-align: start;
  border-bottom: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 15%, transparent);
}

.cy-table thead th {
  color: var(--cy-neon-cyan);
  font-size: 0.75rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  border-bottom-color: color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent);
}

/* A row header — <th scope="row"> — is the label for its row, so it reads as
   heading-ish rather than as another value. */
.cy-table tbody th {
  color: var(--cy-heading);
  font-weight: 400;
}

.cy-table tbody tr {
  transition: background 0.2s;
}

.cy-table tbody tr:hover {
  background: color-mix(in srgb, var(--cy-neon-cyan) 6%, transparent);
}

/* ── Variants ─────────────────────────────────────────────────── */

/* Zebra striping is decoration, not information: it helps the eye track a row
   across a wide table and means nothing on its own. */
.cy-table--striped tbody tr:nth-child(even) {
  background: color-mix(in srgb, var(--cy-neon-cyan) 3%, transparent);
}

.cy-table--compact th,
.cy-table--compact td {
  padding: var(--cy-space-xs) var(--cy-space-sm);
}

/* ── Scroll container ─────────────────────────────────────────────
   Put the table in a section with an accessible name and tabindex="0":

     section.cy-table-scroll[tabindex="0"][aria-label="Sector readings"]
       > table.cy-table

   A wide table has to scroll somewhere, and a scrollable box that only
   responds to a mouse fails WCAG 2.1.1 — there is content the keyboard user
   cannot reach. `tabindex="0"` is what fixes that, and a focusable thing needs
   a name, which is what the label supplies.

   Chrome and Firefox now focus overflowing scrollers on their own — measured
   in Chrome 148, which focuses the box once it actually overflows and not
   while it fits. Safari does not, so the attribute is kept rather than left to
   the engine. A linter flagging tabindex on a non-interactive element is
   applying a good rule to its documented exception.

   Use a section rather than a div with role="region": a named section already
   carries that role, and the kit prefers the element over the ARIA attribute
   everywhere else. Note the name is what makes the mapping happen — an
   unnamed section is not a region.

   A plain utility rather than a `__element`, because the table is its child
   rather than the other way round, and because a table which fits needs no
   wrapper at all. */
.cy-table-scroll {
  overflow-x: auto;
  /* The focus ring belongs on the scroll box: when it is focusable, it is the
     thing the user has landed on. */
  border-radius: var(--cy-radius);
}

.cy-table-scroll:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

/* ── Reduced motion ───────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .cy-table tbody tr { transition: none; }
}

/* ── Increased contrast ───────────────────────────────────────── */
@media (prefers-contrast: more) {
  .cy-table th,
  .cy-table td { border-bottom-color: color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent); }
  .cy-table thead th { border-bottom-color: var(--cy-neon-cyan); }
  .cy-table--striped tbody tr:nth-child(even) { background: color-mix(in srgb, var(--cy-neon-cyan) 10%, transparent); }
}

/* ── Forced colours ───────────────────────────────────────────────
   Row separation is the whole readability of a table, and it is carried here
   by translucent borders that forced colours flattens to one value — which is
   fine, since a border is geometry and survives. The striping does not: a
   background tint is rewritten to the page background, so striped and
   unstriped rows become identical. It is dropped rather than left to fail
   silently, and the borders do the work instead. */
@media (forced-colors: active) {
  .cy-table--striped tbody tr:nth-child(even) { background: Canvas; }

  .cy-table tbody tr:hover { background: Canvas; }

  .cy-table th,
  .cy-table td { border-bottom-color: CanvasText; }
}

}

/* ── forms.css ─────────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* Opt-in classes only. Styling bare `input {}` would hijack every input on a
   consumer's page the moment they import the kit. */

/* ── Field wrapper ────────────────────────────────────────────── */
.cy-field {
  display: flex;
  flex-direction: column;
  gap: var(--cy-space-xs);
  margin-bottom: var(--cy-space-lg);
}

.cy-label {
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cy-neon-cyan);
}

/* ── Fieldset ─────────────────────────────────────────────────────
   <fieldset class="cy-fieldset">
     <legend>Uplink mode</legend>
     <label><input class="cy-radio" type="radio" name="mode"> Primary</label>
     <label><input class="cy-radio" type="radio" name="mode"> Backup</label>
   </fieldset>

   The kit has shipped .cy-radio since 0.2.0 with nothing to group it, and a
   radio group is the one control that is broken without a group label: each
   button announces its own text and nothing says what is being chosen. A
   <div> wrapper and a heading do not fix it, because the association is what
   carries — only <fieldset> + <legend> makes a screen reader read the legend
   before the first option.

   <legend> is styled scoped to the wrapper, the same contract .cy-accordion
   uses for <summary>: one class on the container, native markup inside. The
   legend is deliberately the same mono-uppercase treatment as .cy-label, so a
   group label and a field label read as the same rank of thing. */
.cy-fieldset {
  min-inline-size: 0;
  margin: 0 0 var(--cy-space-lg);
  padding: var(--cy-space-md) var(--cy-space-lg) var(--cy-space-lg);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 35%, transparent);
  border-radius: var(--cy-radius);
}

/* min-inline-size above is not decoration. A <fieldset> has a UA
   `min-inline-size: min-content`, which stops it shrinking inside a flex or
   grid column and makes long legends blow out the layout. */
.cy-fieldset > legend {
  padding: 0 var(--cy-space-sm);
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cy-neon-cyan);
}

/* ── Text-like controls ───────────────────────────────────────── */
.cy-input {
  font-family: var(--cy-font-mono);
  font-size: 0.95rem;
  color: var(--cy-text);
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 35%, transparent);
  border-radius: var(--cy-radius);
  padding: var(--cy-space-sm) var(--cy-space-md);
  width: 100%;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.cy-input::placeholder {
  color: var(--cy-text);
  /* 0.7 is the lowest opacity that clears 4.5:1 in both themes against
     --cy-surface (the input's background): 6.40:1 dark, 4.57:1 light. */
  opacity: 0.7;
}

.cy-input:hover {
  border-color: color-mix(in srgb, var(--cy-neon-cyan) 60%, transparent);
}

.cy-input:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
  border-color: var(--cy-neon-cyan);
  box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-cyan) 30%, transparent);
}

/* Native dropdown arrows cannot be styled consistently, so draw our own.
   `mask-image` was tried so the arrow could take `background-color:
   var(--cy-neon-cyan)` directly, but a <select> has no wrapper element to
   confine it to: masking the select clips its ENTIRE painted box (fill,
   border, and text go with it, leaving only the arrow silhouette) because
   mask is applied to the whole element, not to one background layer. With
   no wrapper markup available (a bare `<select class="cy-input">` is the
   only markup contract this kit makes), the arrow has to stay a background
   image with its colour baked in — so it is baked in per theme instead of
   once, keyed off the same [data-theme="light"] switch tokens.css uses, and
   kept in sync with --cy-neon-cyan's value in each theme below. */
select.cy-input {
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath fill='%2300f2ff' d='M0 0h12L6 8z'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right var(--cy-space-md) center;
  background-size: 10px;
  padding-inline-end: var(--cy-space-xl);
}

/* The one place in the kit that still needs a physical direction. Everything
   else mirrors on its own through logical properties, but `background-position`
   has no inline-axis keyword — there is no `inset-inline-end` for a background
   image — so a right-to-left page needs the arrow moved explicitly. Matching on
   [dir="rtl"] rather than :dir(rtl) because the attribute is what RTL pages
   already set, and it works in every engine rather than Chrome 120 and up. */
[dir="rtl"] select.cy-input {
  background-position: left var(--cy-space-md) center;
  padding-inline-end: var(--cy-space-md);
  padding-inline-start: var(--cy-space-xl);
}

/* Keep in sync with --cy-neon-cyan's light-theme value in tokens.css. */
:root[data-theme="light"] select.cy-input {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath fill='%2300707f' d='M0 0h12L6 8z'/%3E%3C/svg%3E");
}

textarea.cy-input {
  min-height: 6rem;
  resize: vertical;
}

/* ── Sizes ────────────────────────────────────────────────────── */
.cy-input--sm { font-size: 0.8rem;  padding: var(--cy-space-xs) var(--cy-space-sm); }
.cy-input--lg { font-size: 1.05rem; padding: var(--cy-space-md) var(--cy-space-lg); }

/* ── Validation ───────────────────────────────────────────────────
   :user-invalid, not :invalid — :invalid matches empty required fields
   before the user has typed anything, so forms load pre-shouting in red.
   [aria-invalid="true"] lets JS validation drive the state explicitly. */

/* This rule intentionally sits AFTER .cy-input:focus-visible (line 44).
   Both have equal specificity (0,2,0), so source order is what makes the
   invalid state win while focused. The focus outline remains visible, so
   the field is never left without a focus affordance. Do not reorder. */
.cy-input:user-invalid,
.cy-input[aria-invalid="true"] {
  border-color: var(--cy-danger);
  box-shadow: 0 0 10px color-mix(in srgb, var(--cy-danger) 25%, transparent);
}

.cy-error {
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  color: var(--cy-danger);
}

.cy-hint {
  font-family: var(--cy-font-mono);
  font-size: 0.75rem;
  color: var(--cy-text);
  /* 0.75 is the lowest opacity that clears 4.5:1 in both themes against
     --cy-bg (hint text sits on the page background): 7.48:1 dark, 4.86:1
     light. */
  opacity: 0.75;
}

/* ── Checkbox and radio ───────────────────────────────────────────
   appearance:none on the real input, NOT the hidden-input + styled-span
   pattern: the input stays the focusable, screen-reader-announced element
   and we simply paint it. */
.cy-checkbox,
.cy-radio {
  appearance: none;
  width: 1.15rem;
  height: 1.15rem;
  flex-shrink: 0;
  margin: 0;
  display: inline-grid;
  place-content: center;
  /* An inline-grid box derives its baseline from its in-flow content rather
     than its border box. The checkbox's ::after tick (a rotated box with a
     negative margin-top) and the radio's ::after dot (a plain circle) are
     different content, so the two controls compute different baselines and
     land at different heights when default `vertical-align: baseline` is
     used. Aligning both to the line box's middle sidesteps the content-driven
     baseline entirely and keeps them level regardless of ::after shape — do
     not remove this as redundant. */
  vertical-align: middle;
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 50%, transparent);
  cursor: pointer;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.cy-checkbox { border-radius: var(--cy-radius-sm); }

/* The tick is drawn in the page background colour because a checked checkbox
   fills solid (see :checked below) — the tick is knocked out of the fill.
   It is only ever visible when checked, since it is scale(0) otherwise.

   The tick is also deliberately small relative to its box: any larger and it
   spans nearly the full square, reading heavier than the radio's dot so the
   two controls look mismatched side by side. */
.cy-checkbox::after {
  content: "";
  width: 0.45rem;
  height: 0.22rem;
  border-left: 2px solid var(--cy-bg);
  border-bottom: 2px solid var(--cy-bg);
  rotate: -45deg;
  margin-top: -0.1rem;
  transform: scale(0);
  transition: transform 0.15s var(--cy-ease);
}

/* The radio's dot is a background-image, NOT an ::after like the checkbox's
   tick.

   The original reason given for this was wrong, and is corrected here so it
   does not get "fixed" back: it claimed Gecko cannot generate pseudo-elements
   on replaced elements such as <input>. That rule does not apply to these
   controls — `appearance: none` makes the input non-replaced, and Firefox
   renders the checkbox's ::after tick above without trouble (verified in
   Firefox on macOS, 0.2.1).

   The change is kept anyway. A background-image needs no generated box at
   all, which is one less rendering assumption on a control whose only state
   signal is that dot — the checkbox at least fills solid when checked, so it
   still reads as selected if its tick were ever to vanish. Animating
   background-size keeps the same grow-in feel the ::after had. */
.cy-radio {
  border-radius: 50%;
  background-image: radial-gradient(
    circle at center,
    var(--cy-neon-cyan) 0 49%,
    transparent 50%
  );
  background-repeat: no-repeat;
  background-position: center;
  background-size: 0 0;
  transition: border-color 0.2s, box-shadow 0.2s, background-size 0.15s var(--cy-ease);
}

.cy-checkbox:checked,
.cy-radio:checked {
  border-color: var(--cy-neon-cyan);
  box-shadow: 0 0 10px color-mix(in srgb, var(--cy-neon-cyan) 45%, transparent);
}

/* A checked checkbox fills solid, matching how a checked radio fills with a
   solid dot. Both controls then signal "selected" the same way — a filled
   shape — instead of the checkbox being line-art beside a solid radio, which
   reads as mismatched when the two sit on the same row. The tick is knocked
   out in --cy-bg, so it stays legible in both themes. */
.cy-checkbox:checked {
  background: var(--cy-neon-cyan);
}

.cy-checkbox:checked::after {
  transform: scale(1);
}

.cy-radio:checked {
  background-size: 0.55rem 0.55rem;
}

.cy-checkbox:focus-visible,
.cy-radio:focus-visible {
  outline: var(--cy-focus-width) solid var(--cy-focus-color);
  outline-offset: var(--cy-focus-offset);
}

/* ── Disabled ─────────────────────────────────────────────────── */
.cy-input:disabled,
.cy-checkbox:disabled,
.cy-radio:disabled {
  opacity: var(--cy-disabled-opacity);
  cursor: not-allowed;
}

/* ── Reduced motion ───────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .cy-input,
  .cy-checkbox,
  .cy-radio,
  .cy-checkbox::after { transition: none; }
}

/* ── Increased contrast ───────────────────────────────────────────
   The hint and the placeholder are dimmed to the lowest opacity that still
   clears 4.5:1 (see the comments on each). Clearing the floor is the
   minimum, not the goal, so when more contrast is asked for they go to
   full strength and stop being the quietest text on the page. */
@media (prefers-contrast: more) {
  .cy-fieldset { border-color: var(--cy-neon-cyan); }

  .cy-input,
  .cy-checkbox,
  .cy-radio { border-color: var(--cy-neon-cyan); }

  .cy-hint,
  .cy-input::placeholder { opacity: 1; }
}

/* ── Forced colours ───────────────────────────────────────────────
   The checked states are the whole problem here. A checked checkbox fills
   with cyan and knocks its tick out in --cy-bg; a checked radio paints its
   dot as a background-image. Forced colours rewrites `background-color`
   but not `background-image`, and it rewrites the tick's border-color to
   the same text colour it gives the fill — so left alone, the checkbox
   becomes a solid block with an invisible tick, and the radio keeps a cyan
   dot the user asked not to see.

   Naming the system's own selected pair fixes both: Highlight is what the
   platform fills a chosen control with, HighlightText is what it draws on
   top. The kit stops choosing, which is the entire contract of this mode. */
@media (forced-colors: active) {
  .cy-input,
  .cy-checkbox,
  .cy-radio { box-shadow: none; }

  .cy-fieldset { border-color: CanvasText; }

  .cy-checkbox:checked,
  .cy-radio:checked {
    background: Highlight;
    border-color: Highlight;
  }

  .cy-checkbox:checked::after {
    border-color: HighlightText;
  }

  /* The dot is a background-image, which forced colours leaves alone, so it
     is dropped: a checked radio becomes a solid Highlight circle instead.
     Deliberately not an inset box-shadow standing in for the dot — shadows
     are unreliable in this mode, and a state signal must not depend on one.
     Filled-versus-outlined carries it, and the round shape still tells the
     two controls apart. */
  .cy-radio:checked { background-image: none; }

  .cy-input:disabled,
  .cy-checkbox:disabled,
  .cy-radio:disabled {
    color: GrayText;
    border-color: GrayText;
    opacity: 1;
  }
}

}

/* ── feedback.css ──────────────────────────────────────────── */
/* Everything below is in the `cyberpunk-ui` cascade layer. Any rule you write
   outside a layer beats it regardless of specificity, so overriding the kit
   never needs !important and never needs a longer selector than the one you
   would write anyway. The body is not re-indented: a layer wrapper is not a
   nesting level worth 1,600 lines of churn. */
@layer cyberpunk-ui {

/* ── Visually-hidden utility ──────────────────────────────────────
   Lives here because .cy-spinner requires a text label to be
   accessible, and a spinner without one is the common mistake. */
.cy-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ── Alerts ───────────────────────────────────────────────────────
   Status colour on a 3px inline-start border plus a faint tint of the
   same hue, mixed from the status token itself. */
.cy-alert {
  display: block;
  font-family: var(--cy-font-mono);
  font-size: 0.9rem;
  line-height: 1.6;
  color: var(--cy-text);
  padding: var(--cy-space-md) var(--cy-space-lg);
  border-radius: var(--cy-radius);
  border-inline-start: 3px solid var(--cy-info);
  background: color-mix(in srgb, var(--cy-neon-cyan) 8%, transparent);
}

.cy-alert--info    { border-inline-start-color: var(--cy-info);    background: color-mix(in srgb, var(--cy-neon-cyan) 8%, transparent); }
.cy-alert--success { border-inline-start-color: var(--cy-success); background: color-mix(in srgb, var(--cy-success) 8%, transparent); }
.cy-alert--warning { border-inline-start-color: var(--cy-warning); background: color-mix(in srgb, var(--cy-warning) 8%, transparent); }
.cy-alert--danger  { border-inline-start-color: var(--cy-danger);  background: color-mix(in srgb, var(--cy-danger) 8%, transparent); }

/* ── Toasts ───────────────────────────────────────────────────────
   Styling only — no JavaScript ships. Consumers append and remove
   nodes; see the README snippet. pointer-events on the container is
   none so it never blocks clicks on the page beneath it. */
.cy-toast-container {
  position: fixed;
  top: var(--cy-space-lg);
  inset-inline-end: var(--cy-space-lg);
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: var(--cy-space-sm);
  pointer-events: none;
}

.cy-toast-container--bottom {
  top: auto;
  bottom: var(--cy-space-lg);
}

.cy-toast {
  display: block;
  pointer-events: auto;
  min-width: 240px;
  max-width: 360px;
  font-family: var(--cy-font-mono);
  font-size: 0.85rem;
  color: var(--cy-text);
  background: var(--cy-surface);
  border: var(--cy-border-width) solid color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent);
  border-inline-start: 3px solid var(--cy-info);
  border-radius: var(--cy-radius);
  padding: var(--cy-space-md) var(--cy-space-lg);
  box-shadow: 0 0 24px color-mix(in srgb, var(--cy-neon-cyan) 18%, transparent);
  animation: cy-toast-in 0.25s var(--cy-ease);
}

.cy-toast--success { border-inline-start-color: var(--cy-success); }
.cy-toast--warning { border-inline-start-color: var(--cy-warning); }
.cy-toast--danger  { border-inline-start-color: var(--cy-danger); }

@keyframes cy-toast-in {
  from { opacity: 0; transform: translateX(1rem); }
  to   { opacity: 1; transform: translateX(0); }
}

/* ── Badges ───────────────────────────────────────────────────── */
.cy-badge {
  display: inline-block;
  font-family: var(--cy-font-mono);
  font-size: 0.7rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 0.15rem var(--cy-space-sm);
  border-radius: var(--cy-radius-lg);
  color: var(--cy-bg);
  background: var(--cy-info);
}

.cy-badge--success { background: var(--cy-success); }
.cy-badge--warning { background: var(--cy-warning); }
.cy-badge--danger  { background: var(--cy-danger); }

.cy-badge--outline {
  color: var(--cy-info);
  background: transparent;
  border: var(--cy-border-width) solid currentColor;
}

.cy-badge--outline.cy-badge--success { color: var(--cy-success); }
.cy-badge--outline.cy-badge--warning { color: var(--cy-warning); }
.cy-badge--outline.cy-badge--danger  { color: var(--cy-danger); }

/* ── Spinner ──────────────────────────────────────────────────── */
.cy-spinner {
  display: inline-block;
  width: 1.5rem;
  height: 1.5rem;
  border: 2px solid color-mix(in srgb, var(--cy-neon-cyan) 25%, transparent);
  border-top-color: var(--cy-neon-cyan);
  border-radius: 50%;
  animation: cy-spin 0.8s linear infinite,
             cy-spin-glow 1.6s ease-in-out infinite;
}

@keyframes cy-spin {
  to { transform: rotate(360deg); }
}

@keyframes cy-spin-glow {
  0%, 100% { box-shadow: 0 0 6px color-mix(in srgb, var(--cy-neon-cyan) 40%, transparent); }
  50%      { box-shadow: 0 0 16px color-mix(in srgb, var(--cy-neon-cyan) 80%, transparent); }
}

/* ── Progress ─────────────────────────────────────────────────── */
.cy-progress {
  width: 100%;
  height: 6px;
  background: color-mix(in srgb, var(--cy-neon-cyan) 15%, transparent);
  border-radius: var(--cy-radius-lg);
  overflow: hidden;
}

/* `.cy-progress__fill` was removed in 0.5.0. It let a plain div act as a
   progress bar, and it cost more than it returned: a bare div announces
   nothing, so correct use required a progressbar role, three aria-value
   attributes and a label — four things the kit cannot enforce and that were
   easy to omit, leaving a bar that looked finished and told a screen reader
   nothing. Along the way it drew a Sonar rule, read as a rendering duplicate
   when shown beside the native bar, and quietly drifted out of visual sync
   with it.

   The native element below does all of it correctly and for free. Migration
   is in CHANGELOG 0.5.0 for anyone who still needs the div. */

/* Native <progress class="cy-progress" value max>. Vendor pseudo-elements
   cannot share a selector list — an invalid one drops the whole rule — so
   each lives on its own. */
progress.cy-progress {
  appearance: none;
  -webkit-appearance: none;
  border: none;
}

progress.cy-progress::-webkit-progress-bar {
  background: color-mix(in srgb, var(--cy-neon-cyan) 15%, transparent);
  border-radius: var(--cy-radius-lg);
}

/* No transition on either pseudo-element below, deliberately. A native
   <progress> takes its bar length from the element's own value attribute,
   and neither engine transitions the resulting box. A transition here does
   nothing except suggest it works. */
progress.cy-progress::-webkit-progress-value {
  background: linear-gradient(90deg, var(--cy-neon-cyan), var(--cy-neon-pink));
  box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-cyan) 60%, transparent);
  border-radius: var(--cy-radius-lg);
}

progress.cy-progress::-moz-progress-bar {
  background: linear-gradient(90deg, var(--cy-neon-cyan), var(--cy-neon-pink));
  box-shadow: 0 0 12px color-mix(in srgb, var(--cy-neon-cyan) 60%, transparent);
  border-radius: var(--cy-radius-lg);
}

/* ── Meter ────────────────────────────────────────────────────────
   <meter class="cy-meter" value="0.9" low="0.3" high="0.7" optimum="1">90%</meter>

   The gauge to .cy-progress's task bar, and the distinction is the element's,
   not the kit's: <progress> says "this job is 60% done", <meter> says "this
   quantity sits here in its range" — power, integrity, disk, signal. Using a
   progress bar for a level is the common misuse, and it tells a screen reader
   something untrue.

   What makes it worth styling is that the browser already knows which of
   three states the value is in, from low/high/optimum, and exposes each as
   its own pseudo-element. No class, no JavaScript, no author logic: set the
   attributes and the colour follows the number.

   Vendor pseudo-elements cannot share a selector list — an invalid one drops
   the whole rule — so each lives on its own, exactly as .cy-progress does. */
.cy-meter {
  appearance: none;
  -webkit-appearance: none;
  width: 100%;
  height: 6px;
  border: none;
  /* Firefox paints the element itself rather than a bar pseudo-element, so
     the track colour is set here and ::-webkit-meter-bar repeats it below. */
  background: color-mix(in srgb, var(--cy-neon-cyan) 15%, transparent);
  border-radius: var(--cy-radius-lg);
  overflow: hidden;
}

/* Chromium wraps the bar in an inner element whose UA box does not stretch to
   the element's height. `display: flex` is what makes it stretch; `block` does
   not, and neither does setting a height on the bar or on the value
   pseudo-elements. Measured: with `block` the coloured bar paints across
   exactly the top half of the track at any height — 24 of 48 device rows at
   24px, 6 of 12 at the shipped 6px — and with `flex` it fills all 48. */
.cy-meter::-webkit-meter-inner-element {
  display: flex;
  height: 100%;
}

.cy-meter::-webkit-meter-bar {
  height: 100%;
  background: color-mix(in srgb, var(--cy-neon-cyan) 15%, transparent);
  border-radius: var(--cy-radius-lg);
}

/* The three states, in the browser's own vocabulary: inside the good band,
   one step out, two steps out. Mapped to the kit's status colours so a meter
   reads the same way as an alert does. */
.cy-meter::-webkit-meter-optimum-value {
  background: var(--cy-success);
  border-radius: var(--cy-radius-lg);
}

.cy-meter::-webkit-meter-suboptimum-value {
  background: var(--cy-warning);
  border-radius: var(--cy-radius-lg);
}

.cy-meter::-webkit-meter-even-less-good-value {
  background: var(--cy-danger);
  border-radius: var(--cy-radius-lg);
}

/* Firefox exposes one bar pseudo-element for every state, so it cannot colour
   them apart. It gets the optimum colour, and the value stays readable
   because <meter> requires fallback text anyway. */
.cy-meter::-moz-meter-bar {
  background: var(--cy-success);
  border-radius: var(--cy-radius-lg);
}

/* ── Reduced motion ───────────────────────────────────────────────
   The spinner KEEPS rotating: a frozen spinner reads as broken and it
   is essential feedback, not decoration. Slow it and drop the
   decorative glow pulse instead. */
@media (prefers-reduced-motion: reduce) {
  .cy-spinner { animation: cy-spin 2s linear infinite; }
  .cy-toast   { animation: cy-fade-in 0.2s ease; }
}

@keyframes cy-fade-in {
  from { opacity: 0; }
  to   { opacity: 1; }
}

/* ── Increased contrast ───────────────────────────────────────── */
@media (prefers-contrast: more) {
  .cy-toast,
  .cy-badge { border-color: var(--cy-neon-cyan); }

  .cy-spinner { border-color: color-mix(in srgb, var(--cy-neon-cyan) 60%, transparent); border-top-color: var(--cy-neon-cyan); }

  /* The alert tints sit at 0.08 so the hue reads without competing with the
     text on top. Doubling that keeps the variants distinguishable from each
     other, which is the only job the fill has. */
  .cy-alert            { background: color-mix(in srgb, var(--cy-neon-cyan) 16%, transparent); }
  .cy-alert--info      { background: color-mix(in srgb, var(--cy-neon-cyan) 16%, transparent); }
  .cy-alert--success   { background: color-mix(in srgb, var(--cy-success) 16%, transparent); }
  .cy-alert--warning   { background: color-mix(in srgb, var(--cy-warning) 16%, transparent); }
  .cy-alert--danger    { background: color-mix(in srgb, var(--cy-danger) 16%, transparent); }
}

/* ── Forced colours ───────────────────────────────────────────────
   The spinner is the one component here that loses its meaning outright.
   Its whole animation reads because three borders are faint and the fourth
   is bright — and forced colours rewrites every border-color to the same
   value, which turns a spinning arc into a static ring. Nothing is broken,
   nothing looks wrong, and it silently stops saying "working". Naming two
   different system colours restores the contrast the rotation depends on.

   The progress fill has the mirror-image problem: it is a gradient, so
   forced colours leaves it alone entirely and it would keep its cyan-to-pink
   while everything around it changed. Both the native bar and the div path
   are pinned to the system's Highlight instead.

   Badges are filled shapes with no border, so once the fill is forced to the
   page background they would dissolve into it. They get an outline. */
@media (forced-colors: active) {
  .cy-toast,
  .cy-alert,
  .cy-badge { box-shadow: none; }

  .cy-spinner {
    border-color: GrayText;
    border-top-color: CanvasText;
    animation: cy-spin 0.8s linear infinite;
  }

  .cy-badge { border: var(--cy-border-width) solid CanvasText; }

  .cy-progress,
  .cy-meter {
    background: Canvas;
    border: var(--cy-border-width) solid CanvasText;
  }

  /* Forced colours leaves all three meter states one colour, so the number
     is carried by the bar's length and by the element's own fallback text. */
  .cy-meter::-webkit-meter-bar { background: Canvas; }
  .cy-meter::-webkit-meter-optimum-value,
  .cy-meter::-webkit-meter-suboptimum-value,
  .cy-meter::-webkit-meter-even-less-good-value,
  .cy-meter::-moz-meter-bar { background: Highlight; }

  progress.cy-progress::-webkit-progress-value,
  progress.cy-progress::-moz-progress-bar {
    background-image: none;
    background-color: Highlight;
  }

  progress.cy-progress::-webkit-progress-bar { background: Canvas; }
}

}


/* ── custom overrides (un-layered: always beats kit layers) ── */
:root{
 --bg:#05070a; --s1:#080b10; --s2:#0b1017; --s3:#101620; --s4:#141a26;
 --fg:#eceef2; --fgm:rgba(236,238,242,.64); --fgs:rgba(236,238,242,.38);
 --hair:rgba(236,238,242,.05); --bord:rgba(236,238,242,.08); --bstr:rgba(255,255,255,.16);
 --cyan:#00d9e8; --viol:#a78bfa; --grn:#34d399; --amb:#f5a524; --rose:#fb5a7e;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,'JetBrains Mono','Cascadia Code',Consolas,monospace;
 min-height:100vh;background-image:repeating-linear-gradient(0deg,transparent 0 2px,rgba(255,255,255,.012) 2px 4px);
 margin:0}
.hidden{display:none!important}

/* pinned header telemetry */
.ph{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:14px;padding:16px 28px;
 border-bottom:1px solid var(--bord);background:linear-gradient(180deg,rgba(5,7,10,.97),rgba(5,7,10,.92));
 backdrop-filter:blur(6px);flex-wrap:wrap}
.ph h1{font-size:15px;letter-spacing:.18em;font-weight:600;white-space:nowrap;margin:0}
.ph h1 .sig{color:var(--cyan);text-shadow:0 0 12px rgba(0,217,232,.35)}
.chip{font-size:10px;letter-spacing:.12em;padding:3px 9px;border:1px solid var(--bstr);border-radius:2px;color:var(--fgm);white-space:nowrap}
.chip.ok{color:var(--grn);border-color:rgba(52,211,153,.4);box-shadow:0 0 0 1px rgba(52,211,153,.15),0 0 12px rgba(52,211,153,.12)}
.chip.run{color:var(--cyan);border-color:rgba(0,217,232,.45);animation:chipulse 1.4s ease-in-out infinite}
.chip.amber{color:var(--amb);border-color:rgba(245,165,36,.45);box-shadow:0 0 0 1px rgba(245,165,36,.12)}
.chip.warn{color:var(--rose);border-color:rgba(251,90,126,.4)}
@keyframes chipulse{50%{box-shadow:0 0 14px rgba(0,217,232,.45);opacity:.75}}
@media(prefers-reduced-motion:reduce){.chip.run{animation:none}}
#st_prob{cursor:pointer;font:inherit}

/* pipeline strip */
.pipe{display:flex;align-items:center;gap:14px;padding:8px 24px;border-bottom:1px solid var(--hair);background:var(--s1)}
.pipe progress{flex:1;max-width:420px}
.pipe output{font-size:10px;letter-spacing:.1em;color:var(--fgs);text-transform:uppercase}
.pipe output b{color:var(--fgm);font-weight:500}
.pipe .run{color:var(--amb)}

/* tab nav */
.tabs{display:flex;gap:4px;padding:0 24px;border-bottom:1px solid var(--bord);background:var(--s1)}
.tabbtn{background:transparent;border:1px solid transparent;border-bottom:none;color:var(--fgs);
 font:inherit;font-size:13px;letter-spacing:.16em;text-transform:uppercase;padding:13px 26px;cursor:pointer;
 border-radius:3px 3px 0 0;transition:all .15s}
.tabbtn:hover{color:var(--fg)}
.tabbtn[aria-selected="true"]{color:var(--cyan);border-color:var(--bord);background:rgba(0,217,232,.08);
 box-shadow:inset 0 1px 0 var(--cyan);text-shadow:0 0 10px rgba(0,217,232,.4)}

main{max-width:1450px;margin:0 auto;padding:16px 24px}
.tab-panel{animation:fadein .2s ease}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){.tab-panel{animation:none}}

/* problems rail */
#problems-rail{position:fixed;top:0;right:0;bottom:0;width:min(400px,92vw);z-index:200;
 background:var(--s1);border-left:1px solid rgba(251,90,126,.25);
 box-shadow:-18px 0 40px -12px rgba(0,0,0,.7);overflow-y:auto;
 transform:translateX(102%);transition:transform .25s ease}
#problems-rail.open{transform:translateX(0)}
.rail-head{position:sticky;top:0;display:flex;align-items:baseline;gap:10px;padding:14px 16px;
 background:var(--s1);border-bottom:1px solid var(--bord);z-index:1}
.rail-head .t{font-size:11px;letter-spacing:.2em;color:var(--rose);text-transform:uppercase}
.rail-head .sp{margin-left:auto}
.rail-x{background:none;border:none;color:var(--fgs);font-size:22px;cursor:pointer;line-height:1;padding:0 2px}
.rail-x:hover{color:var(--rose)}
@media(prefers-reduced-motion:reduce){#problems-rail{transition:none}}

/* generic card fallback for non-kit containers */
.card{background:var(--s1);border:1px solid var(--hair);border-radius:4px;position:relative;margin-bottom:16px;
 transition:border-color .15s,box-shadow .15s}
.card::before{content:"";position:absolute;top:-1px;left:-1px;width:10px;height:10px;border-top:1px solid var(--cyan);border-left:1px solid var(--cyan);opacity:0;transition:opacity .15s}
.card:hover{border-color:var(--bord);box-shadow:0 0 24px -8px rgba(0,217,232,.18)}
.card:hover::before{opacity:.7}
.card h2{font-size:10px;letter-spacing:.2em;color:var(--fgs);padding:10px 14px;border-bottom:1px solid var(--hair);text-transform:uppercase;margin:0}
.card h2 .n{float:right;color:var(--fgm)}
.card .body{padding:14px}

/* form section grouping (spacing scale) */
:root{--sp1:4px;--sp2:8px;--sp3:12px;--sp4:16px;--sp5:24px}
.formsec{margin-top:var(--sp5);padding-top:var(--sp2);border-top:1px dashed var(--hair)}
.formsec:first-of-type{margin-top:var(--sp2);border-top:none;padding-top:0}
.formsec-label{font-size:9px;font-weight:600;letter-spacing:.22em;color:rgba(0,217,232,.55);text-transform:uppercase;margin-bottom:var(--sp2)}
fieldset.cy-fieldset{border:1px solid var(--hair);border-radius:3px;padding:var(--sp2) var(--sp3) var(--sp3);margin:var(--sp2) 0 0}
fieldset.cy-fieldset>legend{font-size:9px;letter-spacing:.16em;color:var(--fgs);text-transform:uppercase;padding:0 4px}
fieldset.cy-fieldset .opt label.radio{margin:var(--sp1) 0 0}
label{display:block;font-size:10px;letter-spacing:.14em;color:var(--fgs);text-transform:uppercase;margin:var(--sp3) 0 var(--sp1)}
input[type=text],input[type=number],input[type=password],input[type=search],textarea,select{width:100%;background:var(--s2);border:1px solid var(--bord);color:var(--fg);
 padding:7px 9px;border-radius:3px;font:inherit;font-size:12px}
input:focus,textarea:focus,select:focus{outline:none;border-color:rgba(0,217,232,.5);box-shadow:0 0 0 1px rgba(0,217,232,.15),0 0 12px rgba(0,217,232,.1)}
textarea{min-height:90px;resize:vertical;font-size:10px}
input[type=radio],input[type=checkbox]{accent-color:var(--cyan)}
input.cy-radio,input.cy-checkbox{margin:0}
.opt label.radio .cy-radio,.opt label.radio .cy-checkbox{flex-shrink:0}

.btn{display:inline-block;background:transparent;color:var(--cyan);border:1px solid rgba(0,217,232,.4);
 padding:7px 14px;border-radius:3px;cursor:pointer;font:inherit;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
 box-shadow:0 0 0 1px rgba(0,217,232,.12),0 0 14px rgba(0,217,232,.08);transition:all .15s}
.btn:hover{background:rgba(0,217,232,.08);box-shadow:0 0 0 1px rgba(0,217,232,.2),0 0 20px rgba(0,217,232,.15)}
.btn.big{width:100%;padding:11px;font-size:13px;letter-spacing:.22em;color:#041014;background:var(--cyan);
 border-color:var(--cyan);font-weight:700;text-shadow:none;box-shadow:0 0 18px rgba(0,217,232,.3)}
.btn.big:hover{background:#4ee8f3}
.btn.mini{padding:2px 8px;font-size:9px;letter-spacing:.1em}
.btn.go{color:#041014;background:var(--cyan);border-color:var(--cyan);font-weight:700;text-shadow:none}
.btn.go:hover{background:#4ee8f3}
.btn.rose{color:var(--rose);border-color:rgba(251,90,126,.35);box-shadow:none}
.btn.rose:hover{background:rgba(251,90,126,.08);box-shadow:0 0 0 1px rgba(251,90,126,.2)}

.drop{position:relative;overflow:hidden;border:1px dashed rgba(0,217,232,.35);border-radius:4px;padding:22px 14px;text-align:center;cursor:pointer;
 color:var(--fgm);font-size:11px;transition:all .15s;background:var(--s2)}
.drop:hover,.drop.over{border-color:var(--cyan);background:rgba(0,217,232,.05);color:var(--fg)}
.drop.over{box-shadow:0 0 22px -4px rgba(0,217,232,.35)}
.drop.over::after{content:"";position:absolute;inset:0;pointer-events:none;
 background:repeating-linear-gradient(to bottom,transparent 0 2px,rgba(0,0,0,.18) 3px,transparent 4px);
 animation:droppulse 1.2s ease-in-out infinite}
@keyframes droppulse{50%{opacity:.4}}
@media(prefers-reduced-motion:reduce){.drop.over::after{animation:none}}
.drop .fn{color:var(--cyan);display:block;margin-top:4px;font-size:10px;word-break:break-all}
.drop .chg{display:none;font-size:9px;letter-spacing:.12em;color:var(--fgs);margin-top:6px;text-transform:uppercase}
.drop.picked{border-style:solid;border-color:rgba(52,211,153,.45);text-align:left;padding:12px 14px}
.drop.picked .chg{display:inline-block}
.drop.picked .chg:hover{color:var(--cyan)}
.row{display:flex;gap:8px;margin-top:10px;align-items:center}
.hint{font-size:9px;color:var(--fgs);margin-top:4px;letter-spacing:.04em}
details{margin-top:6px}
summary{cursor:pointer;font-size:9px;letter-spacing:.14em;color:var(--fgs);text-transform:uppercase;list-style:none}
summary::before{content:"\25B8 ";color:var(--cyan)}
details[open] summary::before{content:"\25BE "}
.opt{display:flex;gap:14px;flex-wrap:wrap}
.opt label.radio{display:flex;align-items:center;gap:6px;margin:6px 0 0;font-size:11px;letter-spacing:.04em;
 color:var(--fgm);text-transform:none;cursor:pointer}
.tag{display:inline-block;font-size:9px;padding:1px 6px;border-radius:2px;letter-spacing:.1em;border:1px solid var(--bstr);color:var(--fgm)}
.tag.pub{color:var(--grn);border-color:rgba(52,211,153,.35)}
.tag.vio{color:var(--viol);border-color:rgba(167,139,250,.35)}
.tag.amb{color:var(--amb);border-color:rgba(245,165,36,.35)}
.tag.qar{color:var(--rose);border-color:rgba(251,90,126,.35)}
.tag.run{color:var(--amb);border-color:rgba(245,165,36,.35);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.4}}
.item{border-bottom:1px solid var(--hair);padding:10px 14px}
.item:last-child{border-bottom:none}
.item .top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.item .nm{color:var(--cyan);font-size:13px;cursor:pointer;letter-spacing:.04em}
.item .nm:hover{text-shadow:0 0 10px rgba(0,217,232,.5)}
.item .sub{font-size:10px;color:var(--fgs);margin-top:2px}
.item .sub b{color:var(--fgm);font-weight:500}
.item .acts{margin-left:auto;display:flex;gap:6px;align-items:center}
.kv{font-size:10px;color:var(--fgm);background:var(--s2);padding:8px 10px;margin-top:6px;border-left:2px solid rgba(0,217,232,.4);white-space:pre-wrap;word-break:break-all}
.kv.err{border-left-color:rgba(251,90,126,.5)}
.prob{border:1px solid rgba(251,90,126,.18);border-radius:4px;margin:10px 14px;background:var(--s2);padding:10px 12px}
.prob .head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.prob .h{color:var(--rose);font-size:12px;letter-spacing:.06em;font-weight:600}
.prob .sug{font-size:10px;color:var(--fgm);margin-top:4px}
.prob .job{font-size:9px;color:var(--fgs)}
.prob .acts{margin-left:auto;display:flex;gap:6px}
.prob.done{opacity:.38}
.prob.done .h{color:var(--fgs)}
.empty{color:var(--fgs);font-size:11px;padding:18px 12px;text-align:center;letter-spacing:.1em}
.sec{display:flex;align-items:baseline;gap:10px;padding:0 0 8px}
.sec .t{font-size:11px;letter-spacing:.2em;color:var(--fgm);text-transform:uppercase}
.sec .hint2,.rail-head .hint2{font-size:9px;color:var(--fgs);margin-left:auto;cursor:pointer;letter-spacing:.1em}
.sec .hint2:hover,.rail-head .hint2:hover{color:var(--cyan)}
.rail-head .hint2{margin-left:0}
.mono{color:var(--cyan)}
.dim{color:var(--fgs)}
a.plain{color:var(--fgs);text-decoration:none}
a.plain:hover{color:var(--cyan)}
.fxgrp{margin:10px 14px;border-top:1px dashed var(--bord)}
.fxgrp .item{opacity:.72}
.thumb{cursor:zoom-in}

/* discover layout */
.disc-hero{margin-bottom:16px}
.disc-grid{display:grid;grid-template-columns:minmax(0,42fr) minmax(0,58fr);gap:16px;align-items:start}
@media(max-width:1100px){.disc-grid{grid-template-columns:1fr}}

/* terminal job log */
#joblog{background:#04060a;font-family:ui-monospace,'JetBrains Mono','Cascadia Code',Consolas,monospace;
 max-height:62vh;overflow-y:auto;position:relative}
#joblog .jobtime{color:var(--fgs);font-size:9px;letter-spacing:.08em}
.termcard{background:#04060a}
.termcard::after{content:"";position:absolute;inset:0;pointer-events:none;z-index:1;
 background:repeating-linear-gradient(to bottom,transparent 0 2px,rgba(0,0,0,.16) 3px,transparent 4px)}
.termcard h2,.termcard #joblog{position:relative;z-index:2}

/* library */
.view-switch{display:flex;gap:6px;margin-left:auto}
.view-switch .btn[aria-pressed="true"]{background:rgba(0,217,232,.1);box-shadow:0 0 0 1px rgba(0,217,232,.25),0 0 16px rgba(0,217,232,.18)}
.libtable{width:100%;border-collapse:collapse;font-size:11px}
.libtable th{font-size:9px;letter-spacing:.18em;color:var(--fgs);text-transform:uppercase;text-align:left;padding:8px 12px;border-bottom:1px solid var(--bord)}
.libtable td{padding:8px 12px;border-bottom:1px solid var(--hair);vertical-align:middle}
.libtable tr:hover td{background:rgba(0,217,232,.03)}
.libtable-wrap{max-height:70vh;overflow-y:auto}
.render-grid-6{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:8px}
.render-grid-6.wrap{flex-wrap:wrap;display:flex}
.render-frame{background:#6b6f76;border:1px solid color-mix(in srgb,var(--cyan) 35%,transparent);padding:2px}
.render-frame img{display:block;width:100%;height:auto;filter:none!important;background:#6b6f76}
.render-frame .cap{display:block;font-size:9px;color:var(--fgs);background:var(--s1);text-align:center;padding:2px 0}
.libgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.libgrid .item{border:1px solid var(--bord);border-radius:4px;background:var(--s1);padding:10px;margin:0}
.libgrid .item:hover{border-color:rgba(0,217,232,.35)}
.asset-display-container img{filter:none!important}

/*__FIXES__*/

/* ── Layout & Polish Fixes (Discover, Library, Settings, Problems Rail) ── */
.row > input[type=text],
.row > input[type=number],
.row > input[type=password],
.row > input[type=search],
.row > select{flex:1 1 auto;min-width:0;width:auto}
.row > .btn,.row > span{flex-shrink:0}
.ph #clock{margin-left:auto}
.chip.amber{background:rgba(245,165,36,.12);color:#ffbe55;border-color:rgba(245,165,36,.5)}
.chip.amber:hover{background:rgba(245,165,36,.2);box-shadow:0 0 14px rgba(245,165,36,.25)}
.pipe progress[value="4"][max="4"]{opacity:.38;filter:saturate(.5);transition:opacity .25s,filter .25s}
.disc-grid > .termcard{position:sticky;top:68px;display:flex;flex-direction:column;min-height:480px}
.disc-grid > .termcard h2{display:flex;align-items:center;gap:8px}
.disc-grid > .termcard h2 #copylog{margin-left:auto}
#joblog{flex:1;display:flex;flex-direction:column}
#joblog > .empty{margin:auto}
#panel-settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,640px));gap:16px;align-items:start}
#panel-settings .card{margin-bottom:0}
#panel-library > .sec{padding:6px 2px 12px;align-items:center}
#readylist .item{transition:background .12s}
#readylist .item:hover{background:rgba(0,217,232,.025)}
#problems-rail{top:53px;border-top:1px solid var(--bord)}
.prob .head{display:grid;grid-template-columns:1fr auto;align-items:center;gap:4px 8px}
.prob .acts{grid-column:2;grid-row:1 / span 2}
#panel-rig .body > .row:last-child{display:grid;grid-template-columns:minmax(380px,56fr) minmax(320px,44fr);gap:16px;align-items:start;margin-top:12px}
@media (max-width:1024px){#panel-rig .body > .row:last-child{grid-template-columns:1fr}}
#rig_canvas{width:100%;aspect-ratio:4 / 3;min-height:420px;background:radial-gradient(circle at 50% 45%,#101824 0%,#06090e 100%);border:1px solid var(--bord);border-radius:4px;display:block;cursor:crosshair}
#rig_pivot_row input[type=number],#rig_animbar input[type=number]{max-width:84px}
#rig_scrub{flex:1;accent-color:var(--cyan)}
#rig_animbar{background:var(--s2);border:1px solid var(--hair);border-radius:3px;padding:8px 10px;margin-top:10px}
#rig_animbar > .row:first-child{margin-top:0}
#rig_parts{margin-top:12px;max-height:320px;overflow-y:auto;border:1px solid var(--hair);border-radius:3px;background:var(--s2)}

/* ══ 2026 PRECISION TELEMETRY OVERHAUL ══ */
body.prec main{max-width:1900px;margin:0 auto;padding:12px 16px}
body.prec .ph{padding:10px 16px;background:#06080c;border-bottom:1px solid rgba(255,255,255,.1)}
body.prec .ph #clock{margin-left:auto;font-variant-numeric:tabular-nums;border-color:transparent;background:rgba(255,255,255,.03)}
body.prec .tabs{padding:0 16px;background:#06080c;gap:1px}
body.prec .tabbtn{border-radius:0;padding:12px 22px;font-size:12.5px;letter-spacing:.16em;border-right:1px solid var(--hair)}
body.prec .tabbtn[aria-selected="true"]{background:var(--s2);box-shadow:inset 0 2px 0 var(--cyan);text-shadow:none}
body.prec .pipe{padding:6px 16px;background:#040609;border-bottom:1px solid var(--bord)}
body.prec .pipe progress.cy-progress{appearance:none;-webkit-appearance:none;height:6px;max-width:280px;background:#0d1219;border:1px solid rgba(255,255,255,.12);border-radius:0;box-shadow:none}
body.prec .pipe progress.cy-progress::-webkit-progress-bar{background:#0d1219}
body.prec .pipe progress.cy-progress::-webkit-progress-value{background:repeating-linear-gradient(90deg,var(--cyan) 0 64px,#040609 64px 68px);box-shadow:none}
body.prec .pipe progress[value="4"][max="4"]::-webkit-progress-value{background:repeating-linear-gradient(90deg,rgba(52,211,153,.45) 0 64px,#040609 64px 68px)}
body.prec fieldset.cy-fieldset{border:none;padding:0;margin:10px 0 0}
body.prec fieldset.cy-fieldset > legend{padding:0;margin-bottom:6px;font-size:9px;letter-spacing:.14em}
body.prec fieldset.cy-fieldset .opt{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;background:#05070a;padding:3px;border:1px solid var(--bord);border-radius:2px}
body.prec fieldset.cy-fieldset .opt label.radio{margin:0;padding:6px 8px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;justify-content:center;text-align:center;border-radius:1px;background:var(--s1);border:1px solid transparent;color:var(--fgs);transition:all .12s ease}
body.prec fieldset.cy-fieldset .opt label.radio:hover{color:var(--fg);background:var(--s3)}
body.prec fieldset.cy-fieldset .opt label.radio{position:relative}
body.prec fieldset.cy-fieldset .opt label.radio input.cy-radio{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;cursor:pointer}
body.prec fieldset.cy-fieldset .opt label.radio:has(input.cy-radio:focus-visible){outline:2px solid var(--cyan);outline-offset:1px}
body.prec fieldset.cy-fieldset .opt label.radio:has(input.cy-radio:checked){background:rgba(0,217,232,.12);border-color:rgba(0,217,232,.55);color:var(--cyan);font-weight:600;box-shadow:inset 0 0 12px rgba(0,217,232,.08)}
body.prec .disc-hero .opt{gap:6px;margin-top:6px}
body.prec .disc-hero .opt label.radio{margin:0;padding:3px 9px;background:var(--s2);border:1px solid var(--bord);border-radius:2px;font-size:10px;text-transform:uppercase;letter-spacing:.08em}
body.prec .disc-hero .opt label.radio{position:relative}
body.prec .disc-hero .opt label.radio input.cy-checkbox{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;cursor:pointer}
body.prec .disc-hero .opt label.radio:has(input.cy-checkbox:focus-visible){outline:2px solid var(--cyan);outline-offset:1px}
body.prec .disc-hero .opt label.radio:has(input.cy-checkbox:checked){background:rgba(0,217,232,.12);border-color:rgba(0,217,232,.5);color:var(--cyan)}
body.prec .card{border-radius:0;border-color:var(--bord);background:#070a0f}
body.prec .card::after{content:"";position:absolute;bottom:-1px;right:-1px;width:8px;height:8px;border-bottom:1px solid rgba(255,255,255,.22);border-right:1px solid rgba(255,255,255,.22);pointer-events:none}
body.prec .card h2{background:rgba(255,255,255,.02);padding:8px 12px;font-weight:600;color:var(--fgm);display:flex;align-items:center;gap:8px}
body.prec .card h2 #copylog, body.prec .card h2 .n{margin-left:auto}
body.prec input[type=text], body.prec input[type=number], body.prec input[type=password], body.prec input[type=search], body.prec textarea, body.prec select{border-radius:1px;background:#040609;border-color:rgba(255,255,255,.1);padding:6px 8px;font-size:11px}
body.prec .btn{border-radius:1px}
body.prec .disc-grid{grid-template-columns:440px 1fr;gap:12px}
body.prec .disc-grid > .termcard{position:sticky;top:56px;height:calc(100vh - 76px);display:flex;flex-direction:column;margin-bottom:0}
body.prec #joblog{flex:1;max-height:none}

/* ── chrome-industrial reskin (replaces neon palette inside workbench theme) ── */
body.prec{
 --bg:#090a0c;--s1:#0e1114;--s2:#13171b;--s3:#181d23;--s4:#1f252c;
 --hair:rgba(200,214,228,.07);--bord:rgba(200,214,228,.13);--bstr:rgba(200,214,228,.22);
 --cyan:#8fb4d1;--viol:#9aa7b8;--grn:#7fb598;--amb:#e0a83f;--rose:#d08a8a;
 background:var(--bg);color:var(--fg)
}
body.prec .ph,body.prec .tabs{background:linear-gradient(180deg,#15181c,#0e1114)}
body.prec .tabbtn[aria-selected="true"]{background:var(--s3);box-shadow:inset 0 2px 0 var(--cyan)}
body.prec .pipe{background:#0b0d10}
body.prec .card{background:var(--s1);box-shadow:inset 0 1px 0 rgba(200,214,228,.05)}
body.prec .card h2{background:rgba(200,214,228,.04);border-bottom:1px solid var(--hair)}
body.prec input[type=text],body.prec input[type=number],body.prec input[type=password],
body.prec input[type=search],body.prec textarea,body.prec select{background:#0b0d10;border-color:var(--bord)}
body.prec input:focus,body.prec select:focus,body.prec textarea:focus{border-color:var(--cyan);box-shadow:0 0 0 1px rgba(143,180,209,.35);outline:none}
body.prec .btn{background:linear-gradient(180deg,#1b2026,#13171b);border-color:var(--bstr);color:var(--fgm)}
body.prec .btn:hover{color:var(--fg);border-color:var(--cyan)}
body.prec .btn.go{background:linear-gradient(180deg,#c3d2df,#93a9bd);border-color:#93a9bd;color:#0a0d10}
body.prec .chip{background:rgba(200,214,228,.05);border-color:var(--bord)}
body.prec fieldset.cy-fieldset .opt label.radio:has(input.cy-radio:checked),
body.prec .disc-hero .opt label.radio:has(input.cy-checkbox:checked){
 background:rgba(143,180,209,.14);border-color:rgba(143,180,209,.55);color:var(--cyan);
 box-shadow:inset 0 0 10px rgba(143,180,209,.08)}
body.prec fieldset.cy-fieldset .opt label.radio:has(input:focus-visible),
body.prec .disc-hero .opt label.radio:has(input:focus-visible){outline-color:var(--cyan)}
body.prec .pipe progress.cy-progress::-webkit-progress-value{
 background:repeating-linear-gradient(90deg,var(--cyan) 0 64px,#0b0d10 64px 68px)}
body.prec .termcard{background:#07090b}
body.prec #drop{border-color:rgba(200,214,228,.18)}
body.prec .sig{text-shadow:none;color:var(--fg)}

/* ── PRECISION BRUTALISM pass (Lattice / TE / Slate / Linear idiom) ──
   mono type, hairline structure, one signal accent, colour = state only */
body.prec{
 --cyan:#ff5a1f;               /* signal orange — the one non-state accent */
 --font-mono:"Cascadia Mono","JetBrains Mono",Consolas,"Courier New",monospace;
 font-family:var(--font-mono);font-variant-numeric:tabular-nums
}
body.prec h1,body.prec h2,body.prec .tabbtn,body.prec .btn,body.prec .chip,
body.prec legend,body.prec .formsec-label,body.prec label,body.prec .hint{
 font-family:var(--font-mono)}
/* zero radius — machined, not rounded */
body.prec .btn,body.prec .chip,body.prec input,body.prec select,body.prec textarea,
body.prec .tabbtn,body.prec #drop,body.prec .prob,body.prec .item,
body.prec #joblog,body.prec fieldset.cy-fieldset .opt,
body.prec fieldset.cy-fieldset .opt label.radio,
body.prec .disc-hero .opt label.radio{border-radius:0}
/* engraved section markers: small accent square before every legend/section label */
body.prec fieldset.cy-fieldset > legend::before,
body.prec .formsec-label::before{
 content:"";display:inline-block;width:5px;height:5px;background:var(--cyan);
 margin-right:7px;vertical-align:2px}
/* registration marks: top-left + bottom-right corner ticks on every card */
body.prec .card::before{
 content:"";position:absolute;top:-1px;left:-1px;width:8px;height:8px;
 border-top:1px solid rgba(200,214,228,.25);border-left:1px solid rgba(200,214,228,.25);
 pointer-events:none}
/* accent recolour: checked cells, focus rings, progress segments */
body.prec fieldset.cy-fieldset .opt label.radio:has(input.cy-radio:checked),
body.prec .disc-hero .opt label.radio:has(input.cy-checkbox:checked){
 background:rgba(255,90,31,.13);border-color:rgba(255,90,31,.6);color:var(--cyan);
 box-shadow:inset 0 0 10px rgba(255,90,31,.07)}
body.prec input:focus,body.prec select:focus,body.prec textarea:focus{
 border-color:var(--cyan);box-shadow:0 0 0 1px rgba(255,90,31,.35)}
body.prec .pipe progress.cy-progress::-webkit-progress-value{
 background:repeating-linear-gradient(90deg,var(--cyan) 0 64px,#0b0d10 64px 68px)}
body.prec #rig_scrub{accent-color:var(--cyan)}
/* inverted paper primary button — TE/Linear move */
body.prec .btn.go{background:#e8ecf0;border-color:#e8ecf0;color:#0b0d10}
body.prec .btn.go:hover{background:#fff;border-color:#fff;color:#000}
/* instrument chips: tiny status square before label */
body.prec .chip{display:inline-flex;align-items:center;gap:6px}
body.prec .chip::before{content:"";width:5px;height:5px;background:currentColor;flex-shrink:0}
/* square scrollbars */
body.prec ::-webkit-scrollbar{width:8px;height:8px}
body.prec ::-webkit-scrollbar-track{background:var(--s1)}
body.prec ::-webkit-scrollbar-thumb{background:var(--s4);border:1px solid var(--bord)}
body.prec ::-webkit-scrollbar-thumb:hover{background:var(--cyan)}

/* ── brushed-metal sheen pass ── */
body.prec .ph{
 background:linear-gradient(180deg,#23282e 0%,#171a1f 45%,#101316 100%);
 border-bottom:1px solid rgba(200,214,228,.2);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.09),inset 0 -1px 0 rgba(0,0,0,.5)}
body.prec .tabs{
 background:linear-gradient(180deg,#1a1e23 0%,#121519 100%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.06)}
body.prec .tabbtn[aria-selected="true"]{
 background:linear-gradient(180deg,#22272d,#181c21)}
body.prec .card{
 background:linear-gradient(180deg,#13171b 0%,#0e1114 70px);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.07),0 1px 0 rgba(0,0,0,.4)}
body.prec .card h2{
 background:linear-gradient(180deg,rgba(200,214,228,.08),rgba(200,214,228,.02));
 box-shadow:inset 0 -1px 0 var(--hair)}
body.prec .btn{
 background:linear-gradient(180deg,#2a3037 0%,#1c2026 50%,#161a1f 100%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.1),0 1px 2px rgba(0,0,0,.5)}
body.prec .btn:active{box-shadow:inset 0 1px 3px rgba(0,0,0,.6)}
body.prec .btn.go{
 background:linear-gradient(180deg,#f4f7fa 0%,#ccd5de 55%,#b3bfca 100%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.9),0 1px 3px rgba(0,0,0,.5)}
body.prec input[type=text],body.prec input[type=number],body.prec input[type=password],
body.prec input[type=search],body.prec textarea,body.prec select{
 background:linear-gradient(180deg,#07090b,#0c0f12);
 box-shadow:inset 0 1px 3px rgba(0,0,0,.55),inset 0 -1px 0 rgba(200,214,228,.05)}
body.prec fieldset.cy-fieldset .opt{
 background:linear-gradient(180deg,#0a0c0f,#10141a);
 box-shadow:inset 0 1px 3px rgba(0,0,0,.5)}
body.prec fieldset.cy-fieldset .opt label.radio{
 background:linear-gradient(180deg,#1a1f25,#14181d);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
body.prec .disc-hero .opt label.radio{
 background:linear-gradient(180deg,#1c2127,#15191e);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
body.prec .chip{
 background:linear-gradient(180deg,rgba(200,214,228,.09),rgba(200,214,228,.03));
 box-shadow:inset 0 1px 0 rgba(255,255,255,.07)}
body.prec #drop{
 background:linear-gradient(180deg,#0b0e11,#0e1216);
 box-shadow:inset 0 1px 4px rgba(0,0,0,.4)}
body.prec .pipe progress.cy-progress{
 background:linear-gradient(180deg,#0a0c0e,#0f1318);
 box-shadow:inset 0 1px 2px rgba(0,0,0,.6)}
body.prec .termcard{box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 0 0 1px rgba(0,0,0,.3)}

/* ── machined bezel pass — frame-gradient + inset face technique,
     adapted from alyshukry/chrome-button (MIT) to square controls ── */
body.prec .btn{
 border:1px solid transparent;
 background:linear-gradient(180deg,#2b3138 0%,#1d2127 50%,#181c21 100%) padding-box,
            linear-gradient(180deg,#e6ebf1 0%,#5f6770 45%,#b9c0c9 55%,#33383f 100%) border-box;
 box-shadow:0 1px 3px rgba(0,0,0,.55)}
body.prec .btn:active{
 background:linear-gradient(180deg,#14181c,#1d2127) padding-box,
            linear-gradient(180deg,#4a525c,#6a727c) border-box;
 box-shadow:inset 0 1px 3px rgba(0,0,0,.6)}
body.prec .btn.go{
 background:linear-gradient(180deg,#f6f8fb 0%,#d2dae2 55%,#b8c3cd 100%) padding-box,
            linear-gradient(180deg,#ffffff 0%,#8a939e 50%,#e8edf2 55%,#6a727c 100%) border-box;
 color:#0b0d10;box-shadow:0 1px 4px rgba(0,0,0,.6)}
body.prec .btn.go:hover{background-color:#fff;color:#000}
body.prec fieldset.cy-fieldset .opt{
 border:1px solid transparent;
 background:linear-gradient(180deg,#0a0c0f,#10141a) padding-box,
            linear-gradient(180deg,#7d858f 0%,#2b3037 50%,#5f6770 100%) border-box;
 box-shadow:inset 0 1px 3px rgba(0,0,0,.5)}
body.prec fieldset.cy-fieldset .opt label.radio{
 border:1px solid transparent;
 background:linear-gradient(180deg,#20262c,#161a1f) padding-box,
            linear-gradient(180deg,#6a727c,#3a4048 55%,#565d66) border-box}
body.prec fieldset.cy-fieldset .opt label.radio:has(input.cy-radio:checked){
 background:linear-gradient(180deg,rgba(255,90,31,.22),rgba(255,90,31,.08)) padding-box,
            linear-gradient(180deg,#ffb08a,#ff5a1f 55%,#8a3010) border-box;
 color:#ffb899;box-shadow:inset 0 0 10px rgba(255,90,31,.1)}
body.prec .chip{
 border:1px solid transparent;
 background:linear-gradient(180deg,rgba(200,214,228,.07),rgba(200,214,228,.03)) padding-box,
            linear-gradient(180deg,#565d66,#2b3037) border-box}
body.prec .disc-hero .opt label.radio{
 border:1px solid transparent;
 background:linear-gradient(180deg,#1e2329,#15191e) padding-box,
            linear-gradient(180deg,#5f6770,#33383f 55%,#4a525c) border-box}
body.prec .disc-hero .opt label.radio:has(input.cy-checkbox:checked){
 background:linear-gradient(180deg,rgba(255,90,31,.2),rgba(255,90,31,.07)) padding-box,
            linear-gradient(180deg,#ffb08a,#ff5a1f 55%,#8a3010) border-box;
 color:#ffb899}

/* render dialog */
#lbox{border:1px solid var(--bord);background:#0a0d12;padding:0;max-width:94vw;max-height:92vh}
#lbox::backdrop{background:rgba(4,8,10,.88)}
.lbx{position:relative;max-width:94vw;text-align:center;padding:16px}
.lbx img{max-width:88vw;max-height:78vh;border:1px solid var(--bord);background:#6b6f76;display:block;margin:0 auto;filter:none!important}
.lbx .cap{color:var(--fgm);font-size:11px;margin-top:8px;word-break:break-all}
.lbx .x{position:absolute;top:4px;right:10px;color:var(--fgs);font-size:28px;cursor:pointer;line-height:1;background:none;border:none;font:inherit}
.lbx .x:hover{color:var(--rose)}
.lbx .nt{display:inline-block;color:var(--fgs);font-size:12px;margin-top:4px;text-decoration:none;letter-spacing:.1em}
.lbx .nt:hover{color:var(--cyan)}
</style></head><body>

<header class="ph">
 <h1><span class="sig cy-text-glow">SIGNAL</span> // PROP CONVERTER</h1>
 <span class="chip ok" id="st_status">SYSTEM: &hellip;</span>
 <span class="chip" id="st_ready"></span>
 <button class="chip amber" id="st_prob" aria-expanded="false" aria-controls="problems-rail" role="alert" aria-live="polite"></button>
 <span class="chip" id="clock" style="margin-left:auto">&mdash;</span>
</header>

<div class="pipe">
 <progress id="pipeline-progress" class="cy-progress" max="4" value="4"></progress>
 <output id="pipeline-status" for="pipeline-progress" role="status">
  IDLE &mdash; <b>1. fetch GLB &rarr; 2. decimate &amp; collision &rarr; 3. .ydr/.ytyp compile &rarr; 4. 6-view render</b>
 </output>
</div>

<nav class="tabs" role="tablist" aria-label="Primary sections">
 <button class="tabbtn" role="tab" id="tab-discover" aria-selected="true" aria-controls="panel-discover">Discover &amp; Convert</button>
 <button class="tabbtn" role="tab" id="tab-library" aria-selected="false" aria-controls="panel-library">Asset Library</button>
 <button class="tabbtn" role="tab" id="tab-rig" aria-selected="false" aria-controls="panel-rig">Rig Workbench</button>
 <button class="tabbtn" role="tab" id="tab-settings" aria-selected="false" aria-controls="panel-settings">Settings &amp; Tools</button>
</nav>

<aside id="problems-rail" aria-label="Quarantined jobs">
 <div class="rail-head"><span class="t">Problems</span>
  <span class="sp"></span>
  <span class="hint2" id="qtoggle" style="cursor:pointer">show all</span>
  <button class="rail-x" onclick="toggleProblemsRail()" aria-label="Close problems panel">&times;</button>
 </div>
 <div id="problist"><div class="empty">NO PROBLEMS &mdash; CLEAN RUNS ONLY</div></div>
</aside>

<main>
 <!-- TAB 1: DISCOVER & CONVERT -->
 <section id="panel-discover" role="tabpanel" aria-labelledby="tab-discover" class="tab-panel">
  <div class="card disc-hero">
   <h2>find on sketchfab</h2>
   <div class="body">
    <div class="row" style="margin-top:0">
     <input type="search" id="sf_q" placeholder="search downloadable models &mdash; e.g. drone, mech, signage"
            style="flex:1" onkeydown="if(event.key==='Enter')sfSearch()">
     <button class="btn mini go" onclick="sfSearch()">search</button>
    </div>
    <div class="opt">
     <label class="radio"><input type="checkbox" class="cy-checkbox" id="sf_anim">animated only</label>
     <label class="radio"><input type="checkbox" class="cy-checkbox" id="sf_rig">rigged only</label>
    </div>
    <div class="opt">
     <label class="radio"><input type="checkbox" class="sflic cy-checkbox" value="by" checked>CC BY</label>
     <label class="radio"><input type="checkbox" class="sflic cy-checkbox" value="by-sa" checked>CC BY-SA</label>
     <label class="radio"><input type="checkbox" class="sflic cy-checkbox" value="by-nd" checked>CC BY-ND</label>
     <label class="radio"><input type="checkbox" class="sflic cy-checkbox" value="cc0" checked>CC0</label>
     <label class="radio"><input type="checkbox" class="sflic cy-checkbox" value="free-st" checked>Free Standard</label>
    </div>
    <div class="hint">unchecking every license = show everything, including noncommercial</div>
    <div id="sf_res"><div class="empty">SEARCH SKETCHFAB &mdash; RESULTS APPEAR HERE</div></div>
    <div id="sf_sel" style="display:none"></div>
    <div class="row" id="sf_go_row" style="display:none">
     <button class="btn big" id="sfbtn" onclick="sfGo()">&#9656; DOWNLOAD + CONVERT</button>
     <button class="btn mini" onclick="sfSearch('more')" style="margin-left:auto">more results</button>
    </div>
    <div class="hint">form options below apply (kind, size, collision&hellip;). license + source auto-fill.
     only <b>downloadable</b> models are listed &mdash; that's the api's rule, not ours.</div>
   </div>
  </div>
  <div class="disc-grid">
   <div class="card" id="dropcard">
    <h2>convert a model</h2>
    <div class="body">
     <div class="drop" id="drop" onclick="document.getElementById('upl').click()">
      DROP A FILE HERE<br><span class="dim" style="font-size:9px">or click to browse &mdash; .glb .gltf .fbx .obj .zip</span>
      <span class="fn" id="dropfn"></span>
      <span class="chg" id="dropchg">&#8635; change file</span>
     </div>
     <input type="file" id="upl" style="display:none" onchange="pickedFile(this.files[0])">
     <div class="formsec"><div class="formsec-label">prop identity</div>
      <label>prop name <span style="color:var(--cyan)">&larr; this is the spawn name</span></label>
      <input type="text" id="f_name" placeholder="my_cool_prop (blank = auto)">
      <fieldset class="cy-fieldset"><legend>what kind of prop?</legend>
       <div class="opt">
        <label class="radio"><input type="radio" class="cy-radio" name="kind" value="static" checked>still prop</label>
        <label class="radio"><input type="radio" class="cy-radio" name="kind" value="snapshot" onchange="kindChanged()">animated (flipbook poses)</label>
        <label class="radio"><input type="radio" class="cy-radio" name="kind" value="multi" onchange="kindChanged()">moving parts (hinges)</label>
        <label class="radio"><input type="radio" class="cy-radio" name="kind" value="ped" onchange="kindChanged()">ped clothing (rigged .ydd)</label>
       </div>
      </fieldset>
      <div id="posesrow" style="display:none">
       <label>how many poses? <span class="dim">(2-12, sampled across the animation)</span></label>
       <input type="number" id="f_poses" value="4" min="2" max="12">
      </div>
      <div id="pedrow" style="display:none">
       <label>clothing offset (m) <span class="dim">&mdash; pushes mesh out along normals, stops clipping</span></label>
       <input type="number" id="f_push" step="0.001" placeholder="e.g. 0.003">
       <div class="opt">
        <label class="radio"><input type="checkbox" class="cy-checkbox" id="f_weights" checked>require skin weights (uncheck to allow unrigged meshes)</label>
       </div>
       <div class="hint">collision is always none for ped clothing; apparel packs need apparel_config in advanced override</div>
      </div>
     </div>
     <div class="formsec"><div class="formsec-label">scale &amp; collision</div>
      <label>make it this tall (m) <span class="dim">&mdash; optional, rescales the model</span></label>
      <input type="number" id="f_height" step="0.1" placeholder="e.g. 1.8">
      <label>collision</label>
      <select id="f_col">
       <option value="convex_hull" selected>smart shape (convex hull) &mdash; default</option>
       <option value="box">simple box</option>
       <option value="mesh_decimated">detailed mesh (slower)</option>
       <option value="none">no collision (decorative)</option>
      </select>
     </div>
     <div class="formsec"><div class="formsec-label">textures &amp; generation</div>
      <fieldset class="cy-fieldset"><legend>textures</legend>
       <div class="opt">
        <label class="radio"><input type="radio" class="cy-radio" name="tex" value="embed" checked>baked into the prop &mdash; recommended</label>
        <label class="radio"><input type="radio" class="cy-radio" name="tex" value="ytd">separate .ytd file</label>
       </div>
      </fieldset>
      <label>gta generation</label>
      <select id="f_gen">
       <option value="legacy" selected>legacy (gen8) &mdash; default, what most servers run</option>
       <option value="enhanced">enhanced (gen9)</option>
       <option value="both">both &mdash; publishes a second resource named _g9</option>
      </select>
     </div>
     <div class="formsec"><div class="formsec-label">attribution</div>
      <label>license / where's it from <span class="dim">&mdash; recorded in the build report</span></label>
      <input type="text" id="f_lic" placeholder="e.g. CC-BY-4.0">
      <input type="text" id="f_url" placeholder="sketchfab url" style="margin-top:4px">
     </div>
     <details><summary>advanced &mdash; raw override.json</summary>
      <textarea id="f_adv" placeholder='{"motion_config":{"samples":[0,0.5,1]},"lod_dist":300}'></textarea>
      <div class="hint">anything here merges OVER the fields above &middot; moving-parts extras: part_map {"label":["mesh","mesh"] or {"meshes":[...],"kind":"spin","spin_axis":[0,0,1],"spin_rpm":900} or "_body":[carrier meshes]} &middot; keep_actions &middot; exclude_objects &middot; assembly_config {max_parts, spin_min_deg, spin_tail_min_dpf, spin_max_travel_m, part_collision} &mdash; see MOTION_SPEC.md</div>
     </details>
     <div class="row"><button class="btn big" id="convertbtn" onclick="convert()">&#9656; CONVERT</button></div>
     <div class="hint" style="margin-top:8px">already on disk? <a class="plain" href="#" onclick="showPath();return false">run a local path</a> &middot; <a class="plain" href="#" onclick="sweep();return false">sweep inbox</a> (<span id="inboxn">0</span> waiting)</div>
     <div id="pathbox" style="display:none;margin-top:8px">
      <input type="text" id="f_path" placeholder="C:\\path\\to\\model.glb">
      <div class="row"><button class="btn mini" onclick="runPath()">run it</button>
       <span class="hint">form options above apply; file is copied into the inbox</span></div>
     </div>
    </div>
   </div>
   <div class="card cy-terminal termcard">
    <h2>conversions<span class="n" id="jobsn"></span>
     <button class="btn mini" id="copylog" onclick="copyLog()" style="float:right">copy</button></h2>
    <div id="joblog"><div class="empty">IDLE &mdash; NOTHING RUNNING</div></div>
   </div>
  </div>
 </section>

 <!-- TAB 2: ASSET LIBRARY -->
 <section id="panel-library" role="tabpanel" aria-labelledby="tab-library" class="tab-panel hidden">
  <div class="sec"><span class="t">ready to use</span>
   <span class="dim" style="font-size:9px">click a name to copy the spawn name</span>
   <div class="view-switch" role="group" aria-label="Library view mode">
    <button class="btn mini" data-view="details" aria-pressed="true">list w/ details</button>
    <button class="btn mini" data-view="preview" aria-pressed="false">list w/ preview</button>
    <button class="btn mini" data-view="grid" aria-pressed="false">grid</button>
   </div>
  </div>
  <div class="card" id="readycard"><div id="readylist" class="asset-display-container"><div class="empty">NOTHING PUBLISHED YET &mdash; CONVERT SOMETHING</div></div></div>
 </section>

 <!-- TAB 3: SETTINGS & TOOLS -->
 <section id="panel-settings" role="tabpanel" aria-labelledby="tab-settings" class="tab-panel hidden">
  <div class="card">
   <h2>appearance</h2>
   <div class="body">
    <fieldset class="cy-fieldset" style="margin-top:0">
     <legend>interface theme</legend>
     <div class="opt">
      <label class="radio"><input class="cy-radio" type="radio" name="uitheme" value="classic" onchange="setTheme('classic')"> classic console</label>
      <label class="radio"><input class="cy-radio" type="radio" name="uitheme" value="prec" onchange="setTheme('prec')"> precision workbench</label>
     </div>
    </fieldset>
    <div class="hint">classic = neon kit styling. workbench = flat inspector look with segmented controls. saved locally.</div>
   </div>
  </div>
  <div class="card">
   <h2>sketchfab auth</h2>
   <div class="body">
    <div class="row" style="margin-top:0">
     <span id="sf_status" class="dim" style="font-size:10px">not logged in</span>
     <button class="btn mini" id="sf_login" style="display:none" onclick="sfLogin()">connect sketchfab</button>
     <button class="btn mini rose" id="sf_logout" style="display:none" onclick="sfLogout()">disconnect</button>
    </div>
    <div class="row" id="sf_cidrow" style="display:none">
     <input type="text" id="sf_cid" placeholder="oauth client id &mdash; register an app at sketchfab.com/developers/oauth" style="flex:1">
     <button class="btn mini" onclick="sfSaveCid()">save</button>
    </div>
    <div class="row" id="sf_tokrow" style="display:none">
     <input type="password" id="sf_tok" placeholder="or paste API token (settings &rarr; password &amp; api)" style="flex:1">
     <button class="btn mini" onclick="sfSaveToken()">save token</button>
    </div>
    <div class="hint">token/oauth lets the search tab list your downloadable models. nothing leaves this machine except calls to sketchfab's api.</div>
   </div>
  </div>
  <div class="card">
   <h2>inbox</h2>
   <div class="body">
    <div class="row" style="margin-top:0">
     <span class="dim" style="font-size:10px"><span id="inboxn2">0</span> file(s) waiting in inbox/</span>
     <button class="btn mini" onclick="sweep()">sweep inbox</button>
    </div>
    <div class="hint">drop .glb/.gltf/.fbx/.obj/.zip (+ optional override.json) into the inbox folder and sweep to batch-convert.</div>
   </div>
  </div>
 </section>

 <!-- TAB 4: RIG WORKBENCH -->
 <section id="panel-rig" role="tabpanel" aria-labelledby="tab-rig" class="tab-panel hidden">
  <div class="card">
   <h2>rig &amp; animation workbench</h2>
   <div class="body">
    <div class="row" style="margin-top:0">
     <select id="rig_file" style="flex:1"></select>
     <select id="rig_atype">
      <option value="prop">prop / multi-part</option>
      <option value="ped">ped (GTA skeleton)</option>
     </select>
     <button class="btn mini go" id="rig_prevbtn" onclick="rigPreview()">&#9656; PREVIEW &amp; RIG</button>
    </div>
    <div class="hint" id="rig_status">pick an inbox file and dump an interactive part preview. no textures are decoded &mdash; this is the fast pass.</div>
    <div class="row" style="align-items:flex-start;margin-top:8px">
     <canvas id="rig_canvas" width="760" height="540"
             style="background:#14171c;border:1px solid var(--color-border-default);cursor:crosshair"></canvas>
     <div style="flex:1;min-width:240px">
      <div id="rig_sel" class="hint">no selection &mdash; click a part in the viewport, shift+drag for box select, or use the list.</div>
      <div class="row" id="rig_role_row" style="display:none;flex-wrap:wrap">
       <select id="rig_role" onchange="rigApplyRole()">
        <option value="smooth_skin">smooth skin</option>
        <option value="rigid_bone">rigid lock</option>
        <option value="hull">hull / chassis</option>
        <option value="turret">turret (yaw)</option>
        <option value="barrel">barrel (pitch)</option>
        <option value="rotor">rotor (spin)</option>
        <option value="gimbal">gimbal</option>
        <option value="trash">trash</option>
       </select>
       <select id="rig_bone" style="max-width:160px" onchange="rigApplyRole()"></select>
       <button class="btn mini rose" onclick="rigTrashSel()">trash</button>
       <button class="btn mini" onclick="rigSelectIsland()">select island</button>
      </div>
      <div class="row" id="rig_pivot_row" style="display:none">
       <span class="dim" style="font-size:10px">pivot</span>
       <input id="rig_px" type="number" step="0.01" style="width:70px" onchange="rigPivotSet()">
       <input id="rig_py" type="number" step="0.01" style="width:70px" onchange="rigPivotSet()">
       <input id="rig_pz" type="number" step="0.01" style="width:70px" onchange="rigPivotSet()">
       <span class="dim" style="font-size:10px">rest rot&deg;</span>
       <input id="rig_rrx" type="number" step="1" style="width:58px" onchange="rigRestRotSet()" title="rest rotation X (deg, baked about pivot)">
       <input id="rig_rry" type="number" step="1" style="width:58px" onchange="rigRestRotSet()" title="rest rotation Y (deg, baked about pivot)">
       <input id="rig_rrz" type="number" step="1" style="width:58px" onchange="rigRestRotSet()" title="rest rotation Z (deg, baked about pivot)">
      </div>
      <div class="row" style="flex-wrap:wrap">
       <span class="dim" style="font-size:10px">anim:</span>
       <button class="btn mini" data-anim="none" onclick="rigAnim('none')">off</button>
       <button class="btn mini" data-anim="source_clip" onclick="rigAnim('source_clip')" id="rig_srcclip_btn" style="display:none">source clip</button>
       <button class="btn mini" data-anim="tank_aim" onclick="rigAnim('tank_aim')">tank aim</button>
       <button class="btn mini" data-anim="drone_spin" onclick="rigAnim('drone_spin')">drone spin</button>
       <button class="btn mini" data-anim="ped_walk" onclick="rigAnim('ped_walk')">walk</button>
       <button class="btn mini" data-anim="ped_squat" onclick="rigAnim('ped_squat')">squat</button>
       <button class="btn mini" id="rig_skel" onclick="rigToggleSkel()">skeleton</button>
      </div>
      <div id="rig_animbar" style="display:none;margin-top:6px">
       <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center">
        <button class="btn mini" id="rig_playbtn" onclick="rigPlayPause()">&#9654;</button>
        <input type="range" id="rig_scrub" min="0" max="100" value="0" style="flex:1;min-width:160px" oninput="rigScrub()">
        <span class="dim" id="rig_tlabel" style="font-size:10px;min-width:70px">0.00s</span>
       </div>
       <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px">
        <span class="dim" style="font-size:10px">in</span><input id="rig_in" type="number" onchange="rigSyncAnimInputs()" step="0.1" value="0" style="width:64px">
        <span class="dim" style="font-size:10px">out</span><input id="rig_out" type="number" onchange="rigSyncAnimInputs()" step="0.1" value="0" style="width:64px">
        <button class="btn mini" onclick="rigUseRest()">use current time as rest pose</button>
       </div>
       <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px">
        <span class="dim" style="font-size:10px">capture:</span>
        <select id="rig_capmode" onchange="rigSyncAnimInputs()">
         <option value="none">none (rig only)</option>
         <option value="clip_slice">clip_slice (in&rarr;out animation)</option>
         <option value="drone_reactive_flight">drone_reactive_flight</option>
        </select>
        <input id="rig_clipname" type="text" onchange="rigSyncAnimInputs()" placeholder="clip_name" style="width:110px">
        <span class="dim" style="font-size:10px">hz</span><input id="rig_hz" type="number" onchange="rigSyncAnimInputs()" value="20" min="5" max="60" style="width:52px">
       </div>
      </div>
      <div class="row">
       <button class="btn mini" onclick="rigRefit()">re-fit to active parts</button>
       <button class="btn mini go" onclick="rigCompile()">&#9656; COMPILE TO RAGE</button>
      </div>
      <div id="rig_parts" style="max-height:250px;overflow:auto;font-size:10px;margin-top:6px"></div>
     </div>
    </div>
   </div>
  </div>
 </section>
</main>

<dialog id="lbox">
 <div class="lbx"><button class="x" onclick="hideLbox()" aria-label="Close">&times;</button>
  <img id="lbx_img"><div class="cap" id="lbx_cap"></div>
  <a class="nt" id="lbx_tab" target="_blank">open in new tab &#8599;</a>
 </div>
</dialog>

<script>
const $=id=>document.getElementById(id);
let STATE={published:[],quarantined:[],jobs:[],inbox:[]};
let showAllQ=false, picked=null;
let activeTab=localStorage.getItem('sig_tab')||'discover';
let viewMode=localStorage.getItem('sig_view')||'preview';
async function api(u,opt){const r=await fetch(u,opt);return r.json();}
function lbox(src,cap){$('lbx_img').src=src;$('lbx_cap').textContent=cap;$('lbx_tab').href=src;
 $('lbox').showModal();}
function hideLbox(){$('lbox').close();$('lbx_img').src='';}
$('lbox').addEventListener('click',e=>{if(e.target===$('lbox'))hideLbox();});
$('lbox').addEventListener('close',()=>{$('lbx_img').src='';});
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function ago(iso){if(!iso)return'';const t=new Date(iso.replace(' ','T'));if(isNaN(t))return iso;
 const s=(Date.now()-t.getTime())/1000;
 if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+'m ago';
 if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
const KIND={null:'STATIC','':'STATIC',static:'STATIC',snapshot_sequence:'FLIPBOOK',multi_part_prop_assembly:'MOVING PARTS',ped_clothing:'PED CLOTH'};
const KINDTAG={STATIC:'',FLIPBOOK:'vio','MOVING PARTS':'vio','PED CLOTH':'amb'};
function dims(r){return r.dims?r.dims.map(x=>x.toFixed(2)).join(' \u00D7 ')+' m':'\u2014';}

function activateTab(name){
 activeTab=name;localStorage.setItem('sig_tab',name);
 document.querySelectorAll('[role="tab"]').forEach(t=>t.setAttribute('aria-selected',String(t.id==='tab-'+name)));
 document.querySelectorAll('[role="tabpanel"]').forEach(p=>p.classList.toggle('hidden',p.id!=='panel-'+name));
}
document.querySelectorAll('[role="tab"]').forEach(t=>t.addEventListener('click',()=>activateTab(t.id.replace('tab-',''))));
function toggleProblemsRail(){
 const r=$('problems-rail'),open=!r.classList.contains('open');
 r.classList.toggle('open',open);$('st_prob').setAttribute('aria-expanded',String(open));
}
$('st_prob').addEventListener('click',toggleProblemsRail);
function setTheme(t){
 document.body.classList.toggle('prec',t==='prec');
 localStorage.setItem('sig_theme',t);
 const r=document.querySelector('input[name=uitheme][value='+t+']');
 if(r)r.checked=true;
}
setTheme(localStorage.getItem('sig_theme')||'prec');
window.addEventListener('storage',e=>{if(e.key==='sig_sf_auth')refresh();});
function setView(v){
 viewMode=v;localStorage.setItem('sig_view',v);
 document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===v)));
 const rl=$('readylist');if(rl)rl.dataset.sig='';render();
}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));

function pickedFile(f){if(!f)return;picked=f;$('drop').classList.add('picked');
 $('dropfn').textContent='\u00BB '+f.name+' ('+(f.size/1048576).toFixed(1)+' MB)';}
function kindChanged(){
 const k=document.querySelector('input[name=kind]:checked').value;
 $('posesrow').style.display=k==='snapshot'?'':'none';
 $('pedrow').style.display=k==='ped'?'':'none';
}
document.querySelectorAll('input[name=kind]').forEach(r=>r.addEventListener('change',kindChanged));
const dc=$('dropcard');
dc.addEventListener('dragover',e=>{e.preventDefault();$('drop').classList.add('over');});
dc.addEventListener('dragleave',()=>$('drop').classList.remove('over'));
dc.addEventListener('drop',e=>{e.preventDefault();$('drop').classList.remove('over');
 if(e.dataTransfer.files[0])pickedFile(e.dataTransfer.files[0]);});

function buildOverride(){
 const o={};
 const nm=$('f_name').value.trim().toLowerCase().replace(/[^a-z0-9_]/g,'_');
 if(nm)o.asset_name=nm;
 const h=parseFloat($('f_height').value); if(h>0)o.target_height_meters=h;
 const col=$('f_col').value; if(col!=='convex_hull')o.collision_mode=col;
 const kind=document.querySelector('input[name=kind]:checked').value;
 if(kind==='snapshot'){const n=Math.min(12,Math.max(2,parseInt($('f_poses').value)||4));
  o.motion_config={motion_mode:'snapshot_sequence',samples:Array.from({length:n},(_,i)=>+(i/(n-1)).toFixed(3))};}
 if(kind==='multi')o.motion_config={motion_mode:'multi_part_prop_assembly'};
 if(kind==='ped'){o.asset_type='ped';o.motion_config={motion_mode:'ped_clothing'};o.collision_mode='none';
  const p=parseFloat($('f_push').value);if(p)o.clothing_fix={normal_push_meters:p};
  if(!$('f_weights').checked)o.require_weights=false;}
 const gen=$('f_gen').value;if(gen!=='legacy')o.target_generation=gen;
 if(document.querySelector('input[name=tex]:checked').value==='ytd')o.texture_mode='ytd';
 const lic=$('f_lic').value.trim(); if(lic)o.license=lic;
 const url=$('f_url').value.trim(); if(url)o.source_url=url;
 const adv=$('f_adv').value.trim();
 if(adv){const a=JSON.parse(adv);for(const k in a)o[k]=a[k];}
 return o;
}
async function convert(){
 if(!picked){alert('drop a model file first');return;}
 let ov; try{ov=buildOverride();}catch(e){alert('advanced override is not valid JSON');return;}
 const fd=new FormData();fd.append('file',picked);fd.append('override',JSON.stringify(ov));
 $('convertbtn').textContent='\u25B8 RUNNING\u2026';$('convertbtn').disabled=true;
 const r=await api('/api/upload?filename='+encodeURIComponent(picked.name),{method:'POST',body:fd});
 $('convertbtn').textContent='\u25B8 CONVERT';$('convertbtn').disabled=false;
 if(r.error)alert(r.error); refresh();
}
function showPath(){$('pathbox').style.display=$('pathbox').style.display==='none'?'':'none';}
async function runPath(){
 const p=$('f_path').value.trim();if(!p)return;
 let ov; try{ov=buildOverride();}catch(e){alert('advanced override is not valid JSON');return;}
 const r=await api('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({path:p,override:ov})});
 if(r.error)alert(r.error); refresh();
}
async function sweep(){await api('/api/sweep',{method:'POST'});refresh();}
async function dismiss(job){await api('/api/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({job})});refresh();}
async function copyNm(el,name){
 try{await navigator.clipboard.writeText(name);const o=el.textContent;el.textContent=name+' \u2713 copied';
 setTimeout(()=>el.textContent=o,900);}catch(e){}
}
function toggleAll(){showAllQ=!showAllQ;$('qtoggle').textContent=showAllQ?'show recent':'show all';render();}
$('qtoggle').addEventListener('click',toggleAll);

let SF={next:null,sel:null,results:[]};
async function sfSearch(more){
 const q=$('sf_q').value.trim();
 const lics=[...document.querySelectorAll('.sflic:checked')].map(c=>c.value);
 const r=await api('/api/sf/search?q='+encodeURIComponent(q)+(more&&SF.next?'&cursor='+SF.next:'')
  +($('sf_anim').checked?'&animated=1':'')+($('sf_rig').checked?'&rigged=1':'')
  +(lics.length?'&lic='+lics.join(','):''));
 if(r.error){$('sf_res').innerHTML='<div class="empty">'+esc(r.error)+'</div>';return;}
 SF.next=r.next; SF.results=more?SF.results.concat(r.results):r.results;
 $('sf_res').innerHTML=SF.results.length?SF.results.map((m,i)=>`<div class="item" style="cursor:pointer" onclick="sfPick(${i})">
  <div class="top"><img src="${esc(m.thumb)}" style="width:44px;height:28px;object-fit:cover;border:1px solid var(--fgs)" onerror="this.style.display='none'">
   <span class="nm" style="font-size:11px">${esc(m.name)}</span>
   <span class="sub">by ${esc(m.author)} \u00B7 ${esc(m.license)}${m.animations?` \u00B7 ${m.animations} anim`:''} \u00B7 ${Math.round(m.faces/1000)}k faces</span></div></div>`).join('')
  :'<div class="empty">NO RESULTS</div>';
 $('sf_go_row').style.display=SF.next||SF.sel?'':'none';
}
function sfSuggest(m){
 const hay=(m.name+' '+m.categories.join(' ')).toLowerCase();
 if(/people|character|human|pedestrian|clothing/.test(hay))return'ped';
 if(m.animations>0)return /drone|robot|mech|vehicle|rotor|propeller|prop\b/.test(hay)?'multi':'snapshot';
 return'static';
}
async function sfPick(i){
 const m=SF.results[i];
 const d=await api('/api/sf/model?uid='+m.uid);
 if(d.error){alert(d.error);return;}
 SF.sel=d;
 const slug=(d.name||'model').toLowerCase().replace(/[^a-z0-9_]+/g,'_').replace(/^_+|_+$/g,'');
 $('f_name').value='sf_'+slug.slice(0,45);
 $('f_lic').value=d.license;$('f_url').value=d.viewer_url;
 const k=sfSuggest(d);
 document.querySelector(`input[name=kind][value="${k}"]`).checked=true;kindChanged();
 $('sf_sel').style.display='';$('sf_sel').innerHTML=`<div class="item"><div class="top">
  <span class="nm">${esc(d.name)}</span><span class="tag vio">${KIND[k==='ped'?'ped_clothing':k==='snapshot'?'snapshot_sequence':k==='multi'?'multi_part_prop_assembly':'']}</span>
  <span class="sub">guessed kind \u2014 change below if wrong</span></div>
  ${d.description?`<div class="kv">${esc(d.description.slice(0,200))}</div>`:''}</div>
  <iframe src="https://sketchfab.com/models/${esc(d.uid)}/embed"
   style="width:100%;height:260px;border:1px solid var(--fgs);margin-top:6px"
   allow="autoplay; fullscreen; xr-spatial-tracking"></iframe>`;
 $('sf_go_row').style.display='';
}
async function sfGo(){
 if(!SF.sel)return;
 let ov; try{ov=buildOverride();}catch(e){alert('advanced override is not valid JSON');return;}
 $('sfbtn').textContent='\u25B8 FETCHING\u2026';$('sfbtn').disabled=true;
 const r=await api('/api/sf/download',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({uid:SF.sel.uid,override:ov,meta:SF.sel})});
 $('sfbtn').textContent='\u25B8 DOWNLOAD + CONVERT';$('sfbtn').disabled=false;
 if(r.error)alert(r.error);refresh();
}
async function sfSaveToken(){
 const t=$('sf_tok').value.trim();
 await api('/api/sf/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t})});
 $('sf_tok').value='';refresh();
}
async function sfSaveCid(){
 const c=$('sf_cid').value.trim();
 const r=await api('/api/sf/client_id',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:c})});
 $('sf_cid').value='';refresh();
}
async function sfLogin(){
 const r=await api('/api/sf/oauth/start');
 if(r.error){alert(r.error+'\n\nRegister an app first: sketchfab.com/developers/oauth \u2014 grant type: implicit, redirect: http://127.0.0.1:8420/sf_oauth_cb');return;}
 const w=560,h=720,l=Math.max(0,(screen.width-w)/2),t=Math.max(0,(screen.height-h)/2);
 const pop=window.open(r.url,'sf_oauth','popup=yes,width='+w+',height='+h+',left='+l+',top='+t);
 if(!pop)window.open(r.url,'_blank');
}
async function sfLogout(){
 await api('/api/sf/oauth/disconnect',{method:'POST'});refresh();
}

const VIEWBTN={details:'details',preview:'preview',grid:'grid'};
function propMeta(r){
 const k=r.asset_type==='ped'?'PED CLOTH':(KIND[r.motion]||'STATIC');
 const gentag=r.target_generation==='enhanced'?'GEN9':(r.name.endsWith('_g9')?'GEN9':'GEN8');
 const tags=`<span class="tag ${KINDTAG[k]||''}">${k}</span><span class="tag">${gentag}</span>${r.texture_mode==='ytd'?'<span class="tag amb">YTD</span>':''}${r.test?'<span class="tag qar">TEST</span>':''}`;
 return {k,gentag,tags};
}
function propRenders(r,h){
 if(!(r.renders&&(r.renders.assembly.length||r.renders.parts.length)))return'';
 const lbl=f=>esc(f.replace(/^_assembly_/,'').replace(/\.png$/,'').replace(/_/g,' '));
 const im=f=>{const u=`/img?res=${esc(r.name)}&f=${esc(f)}`;
  return `<div class="render-frame"><img class="thumb" src="${u}" loading="lazy" title="${esc(f)} \u2014 click to enlarge"
   onclick="lbox('${u}','${esc(r.name)} / ${esc(f)}')" style="height:${h}px;object-fit:cover">
   <span class="cap">${lbl(f)}</span></div>`;};
 const grid=`<div class="render-grid-6">${r.renders.assembly.map(f=>im(f)).join('')}</div>`;
 const prt=r.renders.parts.length&&h>95
  ?`<details style="margin-top:6px"><summary>individual parts (${r.renders.parts.length})</summary>
   <div class="render-grid-6" style="margin-top:6px">${r.renders.parts.map(f=>im(f)).join('')}</div></details>`:'';
 return grid+prt;
}
function propTech(r){
 return `<details><summary>technical details</summary><div class="kv">`+
 `spawn name   ${esc(r.name)}\nsource file  ${esc(r.source||'\u2014')}\npolygons     ${esc(r.tris??'\u2014')}\n`+
 `runtime check ${esc(r.runtime_validation||'NOT_PERFORMED')} (never spawned in-game yet)\n`+
 `license      ${esc(r.license||'\u2014')}\nresource     build/resources/[sketchfab_props]/${esc(r.name)}/\n`+
 `             ensure it on the server to load</div>`+
 (r.renders&&(r.renders.assembly.length||r.renders.parts.length)?propRenders(r,170)
 :'<div class="dim" style="margin-top:6px">no renders for this build</div>')+
 `</details>`;
}
function renderReady(s){
 const rl=$('readylist');
 const rsig=viewMode+'|'+JSON.stringify(s.published.map(r=>[r.name,r.built_at,r.test,
  (r.renders&&r.renders.assembly||[]).length,(r.renders&&r.renders.parts||[]).length]));
 if(rl.dataset.sig===rsig)return;
 rl.dataset.sig=rsig;
 const real=s.published.filter(r=>!r.test), fx=s.published.filter(r=>r.test);
 if(!real.length&&!fx.length){rl.innerHTML='<div class="empty">NOTHING PUBLISHED YET \u2014 CONVERT SOMETHING</div>';return;}
 let html='';
 if(viewMode==='details'){
  const row=r=>{const m=propMeta(r);
   return `<tr><td><span class="nm" style="cursor:pointer" onclick="copyNm(this,'${esc(r.name)}')" title="copy spawn name">${esc(r.name)}</span></td>
   <td>${m.tags}</td><td>${dims(r)}</td><td>${esc(r.tris??'\u2014')}</td>
   <td>${esc(r.runtime_validation||'NOT_PERFORMED')}</td><td class="dim">${ago(r.built_at)}</td></tr>`;};
  html='<div class="libtable-wrap" tabindex="0"><table class="libtable"><thead><tr>'+
   '<th>Spawn Name</th><th>Tags</th><th>Dimensions</th><th>Polygons</th><th>Runtime Check</th><th>Built</th></tr></thead><tbody>'+
   real.map(row).join('')+'</tbody></table></div>'+
   (fx.length?`<details class="fxgrp"><summary>test fixtures (${fx.length}) \u2014 pipeline gate tests, not usable props</summary>
    <div class="libtable-wrap"><table class="libtable"><tbody>`+fx.map(row).join('')+'</tbody></table></div></details>':'');
 }else if(viewMode==='grid'){
  const card=r=>{const m=propMeta(r);
   return `<div class="item"><div class="top"><span class="nm" onclick="copyNm(this,'${esc(r.name)}')" title="copy spawn name">${esc(r.name)}</span>${m.tags}</div>
   ${propRenders(r,95)||'<div class="dim" style="margin-top:6px">no renders</div>'}</div>`;};
  html='<div class="libgrid">'+real.map(card).join('')+'</div>'+
   (fx.length?`<details class="fxgrp"><summary>test fixtures (${fx.length}) \u2014 pipeline gate tests, not usable props</summary>
    <div class="libgrid">`+fx.map(card).join('')+'</div></details>':'');
 }else{
  const card=r=>{const m=propMeta(r);
   return `<div class="item"><div class="top">
    <span class="nm" onclick="copyNm(this,'${esc(r.name)}')" title="copy spawn name">${esc(r.name)}</span>
    ${m.tags}
    <span class="sub">size <b>${dims(r)}</b> \u00B7 ${ago(r.built_at)}</span></div>
    ${propTech(r)}</div>`;};
  html=(real.length?real.map(card).join(''):'<div class="empty">NOTHING PUBLISHED YET \u2014 CONVERT SOMETHING</div>')
   +(fx.length?`<details class="fxgrp"><summary>test fixtures (${fx.length}) \u2014 pipeline gate tests, not usable props</summary>`+
    fx.map(card).join('')+`</details>`:'');
 }
 rl.innerHTML=html;
}
function updateProgress(){
 const run=STATE.jobs.filter(j=>j.status==='running');
 const p=$('pipeline-progress'),o=$('pipeline-status');
 if(run.length){p.removeAttribute('value');
  o.innerHTML='<span class="run">RUNNING: '+esc(run.map(j=>j.label).join(', '))+'</span> \u2014 1. fetch GLB \u2192 2. decimate &amp; collision \u2192 3. .ydr/.ytyp compile \u2192 4. 6-view render';
 }else{p.setAttribute('value','4');p.setAttribute('max','4');
  o.textContent='PIPELINE \u2014 IDLE';}
}
function copyLog(){
 const t=[...document.querySelectorAll('#joblog .item')].map(i=>i.innerText).join('\n');
 const txt=t||'IDLE \u2014 NOTHING RUNNING';
 try{navigator.clipboard.writeText(txt);$('copylog').textContent='copied';
  setTimeout(()=>$('copylog').textContent='copy',900);}catch(e){}
}
function render(){
 const s=STATE;
 const run=s.jobs.filter(j=>j.status==='running');
 $('st_status').textContent=run.length?('RUNNING: '+run.length+' conversion'+(run.length>1?'s':'')):'SYSTEM: OPERATIONAL';
 $('st_status').className='chip '+(run.length?'run':'ok');
 const lastPub=s.published[0], lastQ=s.quarantined.find(q=>!q.dismissed);
 const nQ=s.quarantined.filter(q=>!q.dismissed).length;
 $('st_ready').textContent=lastPub?('LAST READY: '+lastPub.name+' \u00B7 '+ago(lastPub.built_at)):'NOTHING PUBLISHED YET';
 $('st_prob').textContent=lastQ?('LAST PROBLEM: '+lastQ.headline.toLowerCase()+' \u00B7 '+ago(lastQ.time)+(nQ>1?' \u00D7'+nQ:'')):'NO PROBLEMS';
 $('st_prob').style.display=lastQ?'':'none';
 $('clock').textContent=new Date().toLocaleTimeString();
 $('inboxn').textContent=s.inbox.length;$('inboxn2').textContent=s.inbox.length;
 const sf=s.sf||{};
 $('sf_status').textContent=sf.connected
  ?('logged in as '+(sf.username||'?')+' \u00B7 via '+(sf.method==='Bearer'?'oauth':'api token'))
  :'not logged in';
 $('sf_login').style.display=(!sf.connected&&sf.has_client_id)?'':'none';
 $('sf_logout').style.display=(sf.connected&&sf.method==='Bearer')?'':'none';
 $('sf_cidrow').style.display=(!sf.connected&&!sf.has_client_id)?'':'none';
 $('sf_tokrow').style.display=sf.connected?'none':'';
 renderReady(s);
 const pl=$('problist');
 const qs=s.quarantined.filter(q=>showAllQ||!q.dismissed);
 const psig=showAllQ+'|'+JSON.stringify(qs.map(q=>[q.job,q.dismissed,!!q.extra]));
 if(pl.dataset.sig!==psig){
 const vis=showAllQ?qs:qs.slice(0,8);
 pl.innerHTML=!qs.length?'<div class="empty">NO PROBLEMS \u2014 CLEAN RUNS ONLY</div>'
  :vis.map(q=>`<div class="prob${q.dismissed?' done':''}">
   <div class="head"><span class="h">${esc(q.headline)}</span>
    <span class="job">${esc(q.job)} \u00B7 ${ago(q.time)}</span>
    <span class="acts">${q.dismissed?'':'<button class="btn mini rose" onclick="dismiss(\''+esc(q.job)+'\')">dismiss</button>'}</span></div>
   <div class="sug">${esc(q.suggestion)}</div>
   ${q.extra?`<details><summary>raw report</summary><div class="kv err">${esc(JSON.stringify({reason:q.reason,...q.extra},null,1))}</div></details>`:''}
  </div>`).join('')+
  (!showAllQ&&qs.length>8?`<div class="empty" style="padding:8px">\u2026and ${qs.length-8} more \u2014 click "show all"</div>`:'');
 pl.dataset.sig=psig;
 }
 const jl=$('joblog');
 $('jobsn').textContent=run.length?run.length+' RUNNING':'';
 const jsig=JSON.stringify(s.jobs.map(j=>[j.label,j.status,(j.tail||'').length]));
 if(jl.dataset.sig!==jsig){
 const atBottom=jl.scrollHeight-jl.scrollTop-jl.clientHeight<40;
 jl.innerHTML=!s.jobs.length?'<div class="empty">IDLE \u2014 NOTHING RUNNING</div>'
  :s.jobs.map(j=>`<div class="item"><div class="top">
  <span class="tag ${j.status==='running'?'run':(j.status==='failed'?'qar':'pub')}">${j.status.toUpperCase()}</span>
  <span class="mono" style="font-size:11px">${esc(j.label)}</span>
  <span class="jobtime" style="margin-left:auto">${esc(j.started)}</span></div>
  ${j.tail?`<details><summary>log</summary><div class="kv">${esc(j.tail)}</div></details>`:''}</div>`).join('');
 jl.dataset.sig=jsig;
 if(atBottom)jl.scrollTop=jl.scrollHeight;
 }
 // rig workbench: rebuild the file select ONLY when the inbox signature
 // changes, so the 3s poll never clobbers a mid-rig selection
 if(typeof rigLoadInbox==='function'){
  rigLoadInbox(JSON.stringify(s.inbox||[]));
  rigPoll();
 }
 updateProgress();
}
async function refresh(){STATE=await api('/api/state');render();}
document.addEventListener('DOMContentLoaded',()=>{
 activateTab(activeTab);
 document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===viewMode)));
});
activateTab(activeTab);
// ==== Rig & Animation Workbench (zero-dependency canvas rig preview) ====
const RigWorkbench=(()=>{
 let scene=null,canvas=null,ctx=null;
 let cam={yaw:0.6,pitch:0.35,dist:3.2,target:[0,0,0.9]};
 let selectedPartIds=new Set();
 let animMode="none",mouseAim={yaw:0,pitch:0},showSkel=true;
 let screenTris=[],marquee=null;
 let userTransform={rot_z_deg:0,scale:1,offset:[0,0,0]};
 // source_anim state: per-part rest basis {restC:[3], restR:[9]}, scrubbed
 // time, and the user's rest_sec pick (seconds in source-anim time)
 let anim={playing:true,t:0,restSec:null};

 function init(canvasEl,sceneData){
  canvas=canvasEl;ctx=canvas.getContext("2d");scene=sceneData;
  anim={playing:true,t:0,restSec:null};
  const sa=scene.source_anim;
  if(sa){
   for(const p of scene.parts){
    p.track=sa.part_tracks[String(p.id)]||null;
    p.restC=(sa.rest_centroids||{})[p.name]||p.pivot.slice();
    p.restR=[1,0,0,0,1,0,0,0,1];
   }
  }
  autoBindNearestBones();wireEvents();requestAnimationFrame(loop);
 }
 // 3x3 helpers (row-major [a,b,c,d,e,f,g,h,i])
 function m3Euler(ex,ey,ez){
  const cx=Math.cos(ex),sx=Math.sin(ex),cy=Math.cos(ey),sy=Math.sin(ey),cz=Math.cos(ez),sz=Math.sin(ez);
  // Rz*Ry*Rx
  return[cy*cz, sx*sy*cz-cx*sz, cx*sy*cz+sx*sz,
         cy*sz, sx*sy*sz+cx*cz, cx*sy*sz-sx*cz,
         -sy,   sx*cy,          cx*cy];
 }
 function m3T(m){return[m[0],m[3],m[6],m[1],m[4],m[7],m[2],m[5],m[8]];}
 function m3Mul(a,b){
  const o=new Array(9);
  for(let r=0;r<3;r++)for(let c=0;c<3;c++)
   o[r*3+c]=a[r*3]*b[c]+a[r*3+1]*b[3+c]+a[r*3+2]*b[6+c];
  return o;
 }
 function m3Apply(m,v){return[m[0]*v[0]+m[1]*v[1]+m[2]*v[2],
                              m[3]*v[0]+m[4]*v[1]+m[5]*v[2],
                              m[6]*v[0]+m[7]*v[1]+m[8]*v[2]];}
 // interpolate a part's source-anim track to seconds -> {c:[3], R:[9]}
 // track entries are [cx,cy,cz, rx,ry,rz] with rotation = delta vs the
 // dump-frame rest basis (triad-measured, so skinned parts track right)
 function sampleTrack(p,sec){
  const sa=scene.source_anim;if(!p.track||!sa)return null;
  const st=sa.sample_times;
  let i=0;while(i<st.length-1&&st[i+1]<sec)i++;
  const i1=Math.min(i+1,st.length-1);
  const a=p.track[i],b=p.track[i1];
  if(!a)return b?{c:b.slice(0,3),R:m3Euler(b[3],b[4],b[5])}:null;
  if(!b)return{c:a.slice(0,3),R:m3Euler(a[3],a[4],a[5])};
  const span=Math.max(st[i1]-st[i],1e-6),k=(sec-st[i])/span;
  const c=[a[0]+(b[0]-a[0])*k,a[1]+(b[1]-a[1])*k,a[2]+(b[2]-a[2])*k];
  const R=m3Euler(a[3]+(b[3]-a[3])*k,a[4]+(b[4]-a[4])*k,a[5]+(b[5]-a[5])*k);
  return{c,R};
 }
 function autoBindNearestBones(){
  if(!scene||!scene.bones||!scene.bones.length)return;
  const mids=scene.bones.map(b=>[(b.head[0]+b.tail[0])/2,(b.head[1]+b.tail[1])/2,(b.head[2]+b.tail[2])/2]);
  for(const p of scene.parts){
   p.vBone=p.v.map(v=>{
    let best=0,bd=1e18;
    mids.forEach((m,i)=>{const d=(v[0]-m[0])**2+(v[1]-m[1])**2+(v[2]-m[2])**2;if(d<bd){bd=d;best=i;}});
    return best;});
  }
 }
 function rotAround(p,piv,rx,ry,rz){
  let x=p[0]-piv[0],y=p[1]-piv[1],z=p[2]-piv[2];
  if(rx){const c=Math.cos(rx),s=Math.sin(rx);[y,z]=[y*c-z*s,y*s+z*c];}
  if(ry){const c=Math.cos(ry),s=Math.sin(ry);[x,z]=[x*c+z*s,-x*s+z*c];}
  if(rz){const c=Math.cos(rz),s=Math.sin(rz);[x,y]=[x*c-y*s,x*s+y*c];}
  return[x+piv[0],y+piv[1],z+piv[2]];
 }
 // FK: per-frame local rotations, then walk each bone's parent chain so a
 // calf vertex inherits the animated thigh transform (shin stays welded).
 function updateBoneTransforms(t){
  const bones=scene.bones||[];const byName=new Map(bones.map((b,i)=>[b.name,i]));
  const phase=animMode==="ped_walk"?Math.sin(t*4.0):Math.abs(Math.sin(t*2.0));
  for(const b of bones){
   const n=b.name.toLowerCase();let rx=0;
   if(n.includes("l_thigh"))rx=phase*0.55;
   else if(n.includes("l_calf"))rx=-Math.abs(phase)*0.75;
   else if(n.includes("r_thigh"))rx=-phase*0.55;
   else if(n.includes("r_calf"))rx=-Math.abs(phase)*0.75;
   else if(n.includes("l_upperarm")||n.includes("l_clavicle"))rx=-phase*0.45;
   else if(n.includes("r_upperarm")||n.includes("r_clavicle"))rx=phase*0.45;
   b.localRx=rx;
  }
  return function evalByBone(v,bi){
   let pt=v,cur=bones[bi];
   while(cur){
    if(cur.localRx)pt=rotAround(pt,cur.head,cur.localRx,0,0);
    cur=cur.parent?bones[byName.get(cur.parent)]:null;
   }
   return pt;};
 }
 function evalVertex(part,vIdx,t,evalByBone){
  const v=part.v[vIdx];
  if(part.role==="trash"||animMode==="none")return v;
  if(animMode==="source_clip"&&part.track){
   // v' = R(t)·Rrestᵀ·(v_rebased − crest) + c(t)
   const s=sampleTrack(part,anim.t);if(!s)return v;
   const rel=m3Mul(s.R,m3T(part.restR));
   const d=[v[0]-part.restC[0],v[1]-part.restC[1],v[2]-part.restC[2]];
   const w=m3Apply(rel,d);
   return[w[0]+s.c[0],w[1]+s.c[1],w[2]+s.c[2]];
  }
  if(animMode==="tank_aim"){
   const tp=scene.parts.find(p=>p.role==="turret");
   const tpiv=tp?tp.pivot:[0,0,0];
   if(part.role==="turret")return rotAround(v,tpiv,0,0,mouseAim.yaw);
   if(part.role==="barrel"){
    const pitched=rotAround(v,part.pivot,mouseAim.pitch,0,0);
    return rotAround(pitched,tpiv,0,0,mouseAim.yaw);}
   return v;
  }
  if(animMode==="drone_spin"){
   if(part.role==="rotor")return rotAround(v,part.pivot,0,0,t*18.0);
   if(part.role==="gimbal")return rotAround(v,part.pivot,Math.sin(t*2)*0.4,0,Math.cos(t)*0.5);
   return v;
  }
  if((animMode==="ped_walk"||animMode==="ped_squat")&&part.vBone){
   const bi=part.bone_lock_idx!==undefined?part.bone_lock_idx:part.vBone[vIdx];
   if(scene.bones[bi])return evalByBone(v,bi);
  }
  return v;
 }
 function project(pt){
  let x=pt[0]-cam.target[0],y=pt[1]-cam.target[1],z=pt[2]-cam.target[2];
  const cy=Math.cos(cam.yaw),sy=Math.sin(cam.yaw);
  let x1=x*cy-y*sy,y1=x*sy+y*cy;
  const cp=Math.cos(cam.pitch),sp=Math.sin(cam.pitch);
  let z2=z*cp-y1*sp,y2=z*sp+y1*cp+cam.dist;
  if(y2<0.1)y2=0.1;
  const fov=canvas.height*0.85;
  return[canvas.width*0.5+(x1/y2)*fov,canvas.height*0.5-(z2/y2)*fov,y2];
 }
 function loop(now){
  const t=now*0.001;
  // advance source-anim time inside the [in,out] window
  const sa=scene&&scene.source_anim;
  if(sa&&anim.playing){
   const inS=(typeof RIG_IN==='number'?RIG_IN:0);
   const outS=(typeof RIG_OUT==='number'?RIG_OUT:sa.duration_sec);
   anim.t+=0.016;
   if(anim.t>outS)anim.t=inS;
   if(anim.t<inS)anim.t=inS;
  }
  ctx.fillStyle="#14171c";ctx.fillRect(0,0,canvas.width,canvas.height);
  screenTris=[];
  const evalByBone=(animMode==="ped_walk"||animMode==="ped_squat")?updateBoneTransforms(t):null;
  if(scene){
   for(const p of scene.parts){
    if(p.role==="trash")continue;
    const evalV=p.v.map((_,vi)=>evalVertex(p,vi,t,evalByBone));
    const projV=evalV.map(project);
    const sel=selectedPartIds.has(p.id);
    for(const f of p.f){
     const a=projV[f[0]],b=projV[f[1]],c=projV[f[2]];
     // screen-space backface cull: ~half the tris never need painter slots
     if((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])<=0)continue;
     const va=evalV[f[0]],vb=evalV[f[1]];
     const esq=(va[0]-vb[0])**2+(va[1]-vb[1])**2+(va[2]-vb[2])**2;
     screenTris.push({partId:p.id,depth:(a[2]+b[2]+c[2])*0.333,
      pts:[a,b,c],color:esq>0.12?"#ff2233":(sel?"#00f0ff":p.color)});
    }
   }
   screenTris.sort((x,y)=>y.depth-x.depth);
   ctx.lineWidth=0.6;
   const strokeEdges=screenTris.length<=15000;
   for(const tri of screenTris){
    ctx.beginPath();
    ctx.moveTo(tri.pts[0][0],tri.pts[0][1]);
    ctx.lineTo(tri.pts[1][0],tri.pts[1][1]);
    ctx.lineTo(tri.pts[2][0],tri.pts[2][1]);
    ctx.closePath();
    ctx.fillStyle=tri.color;ctx.fill();
    if(strokeEdges){ctx.strokeStyle="rgba(0,0,0,0.35)";ctx.stroke();}
   }
   if(showSkel&&scene.bones){
    ctx.strokeStyle="rgba(255,234,0,0.55)";ctx.lineWidth=1.2;
    for(const b of scene.bones){
     if(!b.parent)continue;
     const pb=scene.bones.find(x=>x.name===b.parent);if(!pb)continue;
     const a=project(pb.head),c=project(b.head);
     ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(c[0],c[1]);ctx.stroke();
    }
   }
   for(const id of selectedPartIds){
    const p=scene.parts.find(q=>q.id===id);if(!p)continue;
    const sp=project(p.pivot);
    ctx.strokeStyle="#ffea00";ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(sp[0],sp[1],7,0,Math.PI*2);ctx.stroke();
   }
  }
  if(marquee){
   ctx.strokeStyle="#00f0ff";ctx.lineWidth=1;ctx.setLineDash([4,3]);
   ctx.strokeRect(marquee.x0,marquee.y0,marquee.x1-marquee.x0,marquee.y1-marquee.y0);
   ctx.setLineDash([]);
  }
  requestAnimationFrame(loop);
 }
 function wireEvents(){
  let dragging=false,lx=0,ly=0,moved=false;
  canvas.addEventListener("mousedown",e=>{
   const r=canvas.getBoundingClientRect();
   if(e.shiftKey){marquee={x0:e.clientX-r.left,y0:e.clientY-r.top,x1:e.clientX-r.left,y1:e.clientY-r.top};return;}
   dragging=true;moved=false;lx=e.clientX;ly=e.clientY;});
  window.addEventListener("mouseup",()=>{
   if(marquee){
    const x0=Math.min(marquee.x0,marquee.x1),x1=Math.max(marquee.x0,marquee.x1);
    const y0=Math.min(marquee.y0,marquee.y1),y1=Math.max(marquee.y0,marquee.y1);
    for(const p of scene.parts){
     if(p.role==="trash")continue;
     const sp=project(p.pivot);
     if(sp[0]>=x0&&sp[0]<=x1&&sp[1]>=y0&&sp[1]<=y1)selectedPartIds.add(p.id);
    }
    marquee=null;if(window.onSelChanged)window.onSelChanged();return;
   }
   dragging=false;});
  canvas.addEventListener("mousemove",e=>{
   const r=canvas.getBoundingClientRect();
   if(marquee){marquee.x1=e.clientX-r.left;marquee.y1=e.clientY-r.top;return;}
   if(animMode==="tank_aim"&&!dragging){
    mouseAim.yaw=((e.clientX-r.left)/canvas.width-0.5)*Math.PI*1.5;
    mouseAim.pitch=(0.5-(e.clientY-r.top)/canvas.height)*0.8;}
   if(!dragging)return;
   moved=true;
   cam.yaw+=(e.clientX-lx)*0.01;
   cam.pitch=Math.max(-1.4,Math.min(1.4,cam.pitch+(e.clientY-ly)*0.01));
   lx=e.clientX;ly=e.clientY;});
  canvas.addEventListener("wheel",e=>{
   e.preventDefault();
   cam.dist=Math.max(0.5,Math.min(20,cam.dist*(e.deltaY>0?1.1:0.9)));});
  canvas.addEventListener("click",e=>{
   if(moved||marquee)return;
   const r=canvas.getBoundingClientRect();
   const mx=e.clientX-r.left,my=e.clientY-r.top;
   for(let i=screenTris.length-1;i>=0;i--){
    const[a,b,c]=screenTris[i].pts;
    const d1=(mx-b[0])*(a[1]-b[1])-(a[0]-b[0])*(my-b[1]);
    const d2=(mx-c[0])*(b[1]-c[1])-(b[0]-c[0])*(my-c[1]);
    const d3=(mx-a[0])*(c[1]-a[1])-(c[0]-a[0])*(my-a[1]);
    if(!((d1<0||d2<0||d3<0)&&(d1>0||d2>0||d3>0))){
     const id=screenTris[i].partId;
     if(e.ctrlKey||e.metaKey){selectedPartIds.has(id)?selectedPartIds.delete(id):selectedPartIds.add(id);}
     else selectedPartIds=new Set([id]);
     if(window.onSelChanged)window.onSelChanged();
     return;}
   }
   if(!e.ctrlKey&&!e.metaKey){selectedPartIds.clear();if(window.onSelChanged)window.onSelChanged();}
  });
  window.addEventListener("keydown",e=>{
   if(e.key==="Delete"||e.key==="Backspace"){
    for(const id of selectedPartIds){
     const p=scene.parts.find(q=>q.id===id);if(p)p.role="trash";}
    if(window.onSelChanged)window.onSelChanged();}
  });
 }
 return{
  init,
  setAnimMode:m=>{animMode=m;},
  animMode:()=>animMode,
  sourceAnim:()=>scene?scene.source_anim:null,
  animTime:()=>anim.t,
  setTime:s=>{anim.t=s;},
  playPause:()=>{anim.playing=!anim.playing;return anim.playing;},
  // re-basis part verts to the pose at restSec: verts are rotated by the
  // track delta R(rest)·(v - c_rest) + c(rest), and that pose becomes the
  // new rest basis. compile re-bakes at rest_sec so the recipe transform
  // still lands verbatim.
  rebaseTo:restSec=>{
   const sa=scene.source_anim;if(!sa)return 0;
   let moved=0;
   for(const p of scene.parts){
    if(!p.track)continue;
    const s=sampleTrack(p,restSec);if(!s)continue;
    const R=s.R,inv=m3T(p.restR);
    const rel=m3Mul(R,inv);
    p.v=p.v.map(v=>{
     const d=[v[0]-p.restC[0],v[1]-p.restC[1],v[2]-p.restC[2]];
     const w=m3Apply(rel,d);
     return[w[0]+s.c[0],w[1]+s.c[1],w[2]+s.c[2]];});
    const pd=[p.pivot[0]-p.restC[0],p.pivot[1]-p.restC[1],p.pivot[2]-p.restC[2]];
    const pw=m3Apply(rel,pd);
    p.pivot=[pw[0]+s.c[0],pw[1]+s.c[1],pw[2]+s.c[2]];
    p.restC=s.c;p.restR=rel;
    moved++;
   }
   anim.restSec=restSec;
   autoBindNearestBones();
   return moved;
  },
  restSec:()=>anim.restSec,
  toggleSkel:()=>{showSkel=!showSkel;return showSkel;},
  sel:()=>selectedPartIds,
  selectIds:ids=>{selectedPartIds=new Set(ids);},
  part:id=>scene.parts.find(p=>p.id===id),
  parts:()=>scene?scene.parts:[],
  bones:()=>scene?scene.bones:[],
  assetType:()=>scene?scene.asset_type:"prop",
  previewTransform:()=>scene?scene.preview_transform:null,
  userTransform:()=>userTransform,
  scene:()=>scene,
  refit:()=>{
   // recompute scale/offset over non-trash parts; snap ped to skeleton,
   // props to ground origin. Records user_transform for the recipe.
   let xs=[],ys=[],zs=[];
   for(const p of scene.parts){
    if(p.role==="trash")continue;
    for(const v of p.v){xs.push(v[0]);ys.push(v[1]);zs.push(v[2]);}
   }
   if(!xs.length)return null;
   const mn=[Math.min(...xs),Math.min(...ys),Math.min(...zs)];
   const mx=[Math.max(...xs),Math.max(...ys),Math.max(...zs)];
   const zh=Math.max(mx[2]-mn[2],1e-6);
   const target=scene.asset_type==="ped"?1.9:zh;
   const s=target/zh;
   const off=[-0.5*(mn[0]+mx[0])*s,-0.5*(mn[1]+mx[1])*s,-mn[2]*s];
   for(const p of scene.parts){
    p.v=p.v.map(v=>[v[0]*s+off[0],v[1]*s+off[1],v[2]*s+off[2]]);
    p.pivot=[p.pivot[0]*s+off[0],p.pivot[1]*s+off[1],p.pivot[2]*s+off[2]];
   }
   for(const b of scene.bones||[]){ /* bones are the fixed frame -- no move */ }
   userTransform={rot_z_deg:0,scale:s,offset:off};
   autoBindNearestBones();
   return userTransform;
  },
  getRecipe:()=>({
   parts:scene.parts.map(p=>({id:p.id,name:p.name,role:p.role,
    bone_lock:p.bone_lock||"",pivot:p.pivot,
    ...(p.rest_rot&&p.rest_rot.some(a=>Math.abs(a)>1e-6)?{rest_rot:p.rest_rot}:{})})),
   preview_transform:scene.preview_transform,
   user_transform:userTransform,
   anim_capture:(typeof RIG_CAPTURE==='object'&&(RIG_CAPTURE||anim.restSec!==null))?{
    mode:(RIG_CAPTURE&&RIG_CAPTURE.mode)||"none",
    clip_name:(RIG_CAPTURE&&RIG_CAPTURE.clip_name)||"clip",
    rest_sec:anim.restSec,rest_frame:RIG_REST_FRAME,
    in_sec:RIG_IN||0,
    out_sec:RIG_OUT||(scene.source_anim?scene.source_anim.duration_sec:0),
    sample_hz:(RIG_CAPTURE&&RIG_CAPTURE.hz)||20}:undefined,
  }),
 };
})();

// ---- workbench UI glue ----
let RIG_JOB=null,RIG_SRC=null,RIG_IN=0,RIG_OUT=0,RIG_CAPTURE=null,RIG_REST_FRAME=null;
function rigPlayPause(){$('rig_playbtn').innerHTML=RigWorkbench.playPause()?'&#9646;&#9646;':'&#9654;';}
function rigScrub(){
 const sa=RigWorkbench.sourceAnim();if(!sa)return;
 const sec=(+$('rig_scrub').value/100)*sa.duration_sec;
 RigWorkbench.setTime(sec);$('rig_tlabel').textContent=sec.toFixed(2)+'s';
}
function rigUseRest(){
 const sa=RigWorkbench.sourceAnim();if(!sa)return;
 // snap to the nearest sampled frame so the canvas and Blender evaluate
 // the exact same integer frame (no coarse Euler lerp between samples)
 let sec=RigWorkbench.animTime();
 if(sa.sample_times&&sa.sample_times.length){
  sec=sa.sample_times.reduce((a,b)=>Math.abs(b-sec)<Math.abs(a-sec)?b:a);
 }
 RIG_REST_FRAME=Math.round(sec*sa.fps);
 const n=RigWorkbench.rebaseTo(sec);
 rigStatus('re-based '+n+' parts to rest pose at '+sec.toFixed(2)+
  's (frame '+RIG_REST_FRAME+') — compile will bind this pose.');
}
function rigSyncAnimInputs(){
 RIG_IN=+$('rig_in').value||0;RIG_OUT=+$('rig_out').value||0;
 const m=$('rig_capmode').value;
 RIG_CAPTURE=m==='none'?null:{mode:m,clip_name:$('rig_clipname').value.trim()||'clip',hz:+$('rig_hz').value||20};
}
function rigStatus(t){$('rig_status').textContent=t;}
function rigLoadInbox(isig){
 const sel=$('rig_file');if(!sel)return;
 if(sel.dataset.sig===isig)return;sel.dataset.sig=isig;
 sel.innerHTML=STATE.inbox.map(f=>`<option>${esc(f)}</option>`).join('')
  ||'<option value="">inbox is empty</option>';
}
async function rigPreview(){
 const f=$('rig_file').value;if(!f){rigStatus('no inbox file selected.');return;}
 const atype=$('rig_atype').value;
 $('rig_prevbtn').disabled=true;rigStatus('blender is baking the preview (no textures)…');
 const r=await api('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({file:f,asset_type:atype})});
 if(r.error){rigStatus(r.error);$('rig_prevbtn').disabled=false;return;}
 RIG_JOB=r.job;RIG_SRC=f;
 rigStatus('preview job '+r.job+' running — this takes seconds for small files, ~15s for 90MB GLBs.');
}
async function rigPoll(){
 if(!RIG_JOB)return;
 const j=(STATE.jobs||[]).find(x=>x.id===RIG_JOB);if(j&&j.status==='running')return;
 const r=await api('/api/preview?job='+RIG_JOB);
 if(r.status==='running'||r.error){if(r.error){rigStatus('preview failed: '+(r.tail||r.error));RIG_JOB=null;$('rig_prevbtn').disabled=false;}return;}
 RIG_JOB=null;$('rig_prevbtn').disabled=false;
 RigWorkbench.init($('rig_canvas'),r);
 rigPopulateBones();rigRenderParts();
 if(r.source_anim){
  $('rig_animbar').style.display='';$('rig_srcclip_btn').style.display='';
  $('rig_out').value=r.source_anim.duration_sec;
  RIG_OUT=r.source_anim.duration_sec;RIG_REST_FRAME=null;
  rigAnim('source_clip');
 }
 rigStatus('loaded '+r.parts.length+' parts'+(r.bones.length?' + '+r.bones.length+' donor bones':'')
  +'. click parts, assign roles, then compile.');
}
function rigPopulateBones(){
 const sel=$('rig_bone');
 sel.innerHTML='<option value="">(nearest bone)</option>'+
  RigWorkbench.bones().map(b=>`<option>${esc(b.name)}</option>`).join('');
}
function rigSelInfo(){
 const ids=[...RigWorkbench.sel()];
 if(!ids.length){$('rig_sel').textContent='no selection — click a part in the viewport, shift+drag for box select.';
  $('rig_role_row').style.display='none';$('rig_pivot_row').style.display='none';return;}
 $('rig_role_row').style.display='';$('rig_pivot_row').style.display=ids.length===1?'':'none';
 const p=RigWorkbench.part(ids[0]);
 $('rig_sel').textContent=ids.length===1
  ?`${p.name} — ${p.bind_kind||''} island ${p.island_id} — role: ${p.role}${p.bone_lock?' → '+p.bone_lock:''}`
  :`${ids.length} parts selected`;
 if(ids.length===1){
  $('rig_role').value=['smooth_skin','rigid_bone','hull','turret','barrel','rotor','gimbal','trash'].includes(p.role)?p.role:'hull';
  if(p.bone_lock)$('rig_bone').value=p.bone_lock;
  $('rig_px').value=p.pivot[0];$('rig_py').value=p.pivot[1];$('rig_pz').value=p.pivot[2];
  const rr=p.rest_rot||[0,0,0];
  $('rig_rrx').value=rr[0];$('rig_rry').value=rr[1];$('rig_rrz').value=rr[2];
 }
}
window.onSelChanged=()=>{rigSelInfo();rigRenderParts();};
function rigApplyRole(){
 const role=$('rig_role').value,bone=$('rig_bone').value;
 for(const id of RigWorkbench.sel()){
  const p=RigWorkbench.part(id);p.role=role;
  p.bone_lock=role==='rigid_bone'?(bone||p.bone_lock||''):'';
  if(bone){const bi=RigWorkbench.bones().findIndex(b=>b.name===bone);if(bi>=0)p.bone_lock_idx=bi;}
 }
 rigSelInfo();rigRenderParts();
}
function rigTrashSel(){for(const id of RigWorkbench.sel())RigWorkbench.part(id).role='trash';rigRenderParts();}
function rigSelectIsland(){
 const ids=[...RigWorkbench.sel()];if(ids.length!==1)return;
 const isl=RigWorkbench.part(ids[0]).island_id;
 RigWorkbench.selectIds(RigWorkbench.parts().filter(p=>p.island_id===isl).map(p=>p.id));
 rigSelInfo();
}
function rigPivotSet(){
 const ids=[...RigWorkbench.sel()];if(ids.length!==1)return;
 RigWorkbench.part(ids[0]).pivot=[+$('rig_px').value,+$('rig_py').value,+$('rig_pz').value];
}
function rigRestRotSet(){
 const ids=[...RigWorkbench.sel()];if(ids.length!==1)return;
 RigWorkbench.part(ids[0]).rest_rot=[+$('rig_rrx').value,+$('rig_rry').value,+$('rig_rrz').value];
}
function rigAnim(m){RigWorkbench.setAnimMode(m);
 document.querySelectorAll('[data-anim]').forEach(b=>b.classList.toggle('go',b.dataset.anim===m));}
function rigToggleSkel(){$('rig_skel').classList.toggle('go',RigWorkbench.toggleSkel());}
function rigRefit(){
 const ut=RigWorkbench.refit();
 if(ut)rigStatus('re-fit applied — scale '+ut.scale.toFixed(4)+'. compile will use this exact transform.');
}
function rigRenderParts(){
 const el=$('rig_parts');if(!el||!RigWorkbench.scene())return;
 el.innerHTML=RigWorkbench.parts().map(p=>{
  const tr=p.role==='trash';
  return`<div style="opacity:${tr?0.45:1};cursor:pointer" onclick="rigPickPart(${p.id})">`
   +`${tr?'&#10007; ':'&#9632; '}<span style="color:${tr?'#666':p.color}">${esc(p.name)}</span>`
   +` <span class="dim">${esc(p.role)}${p.bone_lock?'→'+esc(p.bone_lock):''} i${p.island_id}</span>`
   +(tr?` <a onclick="event.stopPropagation();rigRestore(${p.id})" style="cursor:pointer">restore</a>`:'')
   +`</div>`;}).join('');
}
function rigPickPart(id){RigWorkbench.selectIds([id]);rigSelInfo();}
function rigRestore(id){RigWorkbench.part(id).role='smooth_skin';rigRenderParts();}
async function rigCompile(){
 if(!RigWorkbench.scene()){rigStatus('load a preview first.');return;}
 let ov;try{ov=buildOverride();}catch(e){alert('advanced override is not valid JSON');return;}
 const recipe=RigWorkbench.getRecipe();
 rigStatus('compiling '+recipe.parts.filter(p=>p.role!=='trash').length+' parts with your recipe…');
 const r=await api('/api/compile-rig',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({file:RIG_SRC,override:ov,rig_recipe:recipe})});
 if(r.error)rigStatus(r.error);else rigStatus('compile job '+r.job+' launched — watch the jobs log.');
 refresh();
}
setInterval(refresh,3000);refresh();
</script></body></html>
"""

# Loopback page for the OAuth implicit flow: sketchfab returns the token in the
# URL fragment, which never reaches the server — this page parses it and posts
# it back, then tells the user to close the tab.
OAUTH_CB_PAGE = """<!doctype html><html><body
 style="background:#0b0f14;color:#8ab;font-family:monospace;padding:40px">
<h3 id=m>connecting sketchfab&hellip;</h3>
<script>
const p=new URLSearchParams(location.hash.slice(1));
const t=p.get('access_token');
const m=document.getElementById('m');
if(!t){m.textContent='no token returned — close this tab and retry';}
else fetch('/api/sf/oauth/complete',{method:'POST',
 headers:{'Content-Type':'application/json'},
 body:JSON.stringify({access_token:t,expires_in:p.get('expires_in'),state:p.get('state')})})
 .then(r=>r.json()).then(d=>{m.textContent=d.ok
  ?('connected as '+(d.username||'?')+' — closing…')
  :('login failed: '+d.error);
  if(d.ok){try{localStorage.setItem('sig_sf_auth',String(Date.now()))}catch(e){}
   setTimeout(()=>window.close(),600);}})
 .catch(e=>{m.textContent='login failed: '+e;});
</script></body></html>"""


def load_dismissed():
    try:
        return set(json.load(open(DISMISSED, encoding="utf-8")))
    except Exception:
        return set()


def save_dismissed(s):
    try:
        with open(DISMISSED, "w", encoding="utf-8") as fh:
            json.dump(sorted(s), fh, indent=1)
    except Exception:
        pass


def scan_published():
    out = []
    if not os.path.isdir(OUT_RES):
        return out
    reg = {}
    if os.path.isfile(REGISTRY):
        try:
            reg = json.load(open(REGISTRY, encoding="utf-8"))
        except Exception:
            pass
    for name in os.listdir(OUT_RES):
        rp = os.path.join(OUT_RES, name, "build_report.json")
        if not os.path.isfile(rp):
            continue
        try:
            with open(rp, encoding="utf-8") as fh:
                r = json.load(fh)
        except Exception:
            continue
        ent = reg.get(name, {})
        try:
            pngs = sorted(f for f in os.listdir(os.path.join(OUT_RES, name))
                          if f.lower().endswith(".png"))
            assembly = [f for f in pngs if f.startswith("_assembly")]
            parts = [f for f in pngs if not f.startswith("_assembly")]
            renders = {"assembly": assembly, "parts": parts}
        except OSError:
            renders = {"assembly": [], "parts": []}
        out.append({
            "name": name, "source": r.get("source_file"),
            "dims": r.get("dimensions_meters"), "tris": r.get("final_triangles"),
            "motion": r.get("motion_mode"), "texture_mode": r.get("texture_mode"),
            "asset_type": r.get("asset_type"),
            "target_generation": r.get("target_generation"),
            "runtime_validation": r.get("runtime_validation"),
            "renders": renders,
            "license": r.get("license") or ent.get("license"),
            "built_at": ent.get("built_at", ""),
            "test": name in FIXTURE_NAMES or bool(r.get("test_fixture")),
        })
    real = [e for e in out if not e["test"]]
    fx = [e for e in out if e["test"]]
    real.sort(key=lambda e: e.get("built_at") or "", reverse=True)
    fx.sort(key=lambda e: e.get("built_at") or "", reverse=True)
    return real + fx


def scan_quarantine():
    dis = load_dismissed()
    out = []
    if not os.path.isdir(QUAR):
        return out
    for d in sorted(os.listdir(QUAR), reverse=True):
        fp = os.path.join(QUAR, d, "failure_report.json")
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as fh:
                r = json.load(fh)
        except Exception:
            continue
        head, sug = human_reason(r.get("reason"))
        out.append({"job": d, "reason": r.get("reason"), "headline": head,
                    "suggestion": sug, "time": r.get("time"),
                    "extra": r.get("extra"), "dismissed": d in dis})
    return out


def scan_inbox():
    if not os.path.isdir(INBOX):
        return []
    return [f for f in sorted(os.listdir(INBOX))
            if os.path.isfile(os.path.join(INBOX, f))
            and f.lower().endswith((".glb", ".gltf", ".fbx", ".obj", ".zip"))]


def launch_job(label, cmd):
    JOB_SEQ[0] += 1
    jid = JOB_SEQ[0]
    job = {"id": jid, "label": label, "cmd": cmd, "status": "running",
           "started": datetime.datetime.now().strftime("%H:%M:%S"), "tail": ""}
    with JOBS_LOCK:
        JOBS[jid] = job
        while len(JOBS) > 12:
            JOBS.pop(min(JOBS))

    def runner():
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            job["tail"] = (p.stdout + p.stderr).strip()[-1800:]
            job["status"] = "done" if p.returncode == 0 else "failed"
        except Exception as exc:
            job["tail"] = str(exc)
            job["status"] = "failed"
    threading.Thread(target=runner, daemon=True).start()
    return jid


def stage_with_override(src_path, override, hint="run"):
    """Copy a local file + generated override.json into a staging dir in inbox/."""
    jobdir = os.path.join(INBOX,
                          time.strftime(hint + "_%Y%m%d_%H%M%S_")
                          + os.path.splitext(os.path.basename(src_path))[0])
    os.makedirs(jobdir, exist_ok=True)
    dst = os.path.join(jobdir, os.path.basename(src_path))
    if os.path.abspath(src_path) != os.path.abspath(dst):
        shutil.copy2(src_path, dst)
    with open(os.path.join(jobdir, "override.json"), "w", encoding="utf-8") as fh:
        json.dump(override, fh, indent=2)
    return dst


def _resolve_source(inbox_name, abs_path):
    """Rig endpoints accept either an inbox filename or an absolute path."""
    if inbox_name:
        p = os.path.join(INBOX, os.path.basename(inbox_name))
        if os.path.isfile(p):
            return p
        return None
    if abs_path and os.path.isfile(abs_path):
        return abs_path
    return None


def _find_blender():
    sys.path.insert(0, ROOT)
    try:
        from orchestrator import find_blender
        return find_blender()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("blender discovery failed: %s" % exc)


def _ped_donor_ydd():
    env = os.environ.get("SFP_PED_DONOR_YDD")
    if env and os.path.isfile(env):
        return env
    for cfg in (os.path.join(ROOT, "setup.json"), DONOR_CFG):
        try:
            with open(cfg, encoding="utf-8") as fh:
                data = json.load(fh)
            y = data.get("ped_donor_ydd") or data.get("ydd")
            if y and os.path.isfile(y):
                return y
        except Exception:
            continue
    return None


def sf_load_oauth():
    try:
        return json.load(open(SF_OAUTH_FILE, encoding="utf-8"))
    except Exception:
        return {}


def sf_save_oauth(d):
    os.makedirs(BUILD, exist_ok=True)
    with open(SF_OAUTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)


def sf_token():
    """-> (token, scheme): OAuth ('Bearer') wins, then env/manual API token
    ('Token'). (None, None) when nobody is logged in."""
    oa = sf_load_oauth()
    if oa.get("access_token") and oa.get("expires_at", 0) > time.time():
        return oa["access_token"], "Bearer"
    tok = os.environ.get("SFP_SKETCHFAB_TOKEN", "").strip()
    if not tok:
        try:
            tok = json.load(open(SF_TOKEN_FILE,
                               encoding="utf-8")).get("token", "")
        except Exception:
            tok = ""
    return (tok, "Token") if tok else (None, None)


def sf_get(path, params=None, token=None, scheme="Bearer"):
    """GET a Sketchfab Data-API v3 endpoint -> parsed JSON. Errors -> RuntimeError."""
    qs = urllib.parse.urlencode(params or {}, doseq=True)
    req = urllib.request.Request(SF_API + path + ("?" + qs if qs else ""))
    req.add_header("User-Agent", "sketchfab-pipeline-dashboard/1.0")
    if token:
        req.add_header("Authorization", scheme + " " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise RuntimeError("sketchfab %s: HTTP %s %s" % (path, exc.code, detail))


def sf_model_brief(m):
    """Trim a search-result/model object to what the browse UI needs."""
    th = (m.get("thumbnails") or {}).get("images") or []
    return {
        "uid": m.get("uid"), "name": m.get("name"),
        "author": (m.get("user") or {}).get("displayName", ""),
        "license": (m.get("license") or {}).get("label", ""),
        "viewer_url": m.get("viewerUrl"),
        "downloadable": bool(m.get("isDownloadable")),
        "animations": m.get("animationCount") or 0,
        "faces": m.get("faceCount") or 0,
        "categories": [c.get("name", "") for c in m.get("categories") or []],
        "thumb": th[1]["url"] if len(th) > 1 else (th[0]["url"] if th else ""),
    }


def launch_sf_job(label, model_uid, archive_url, override, dest_name):
    """Download the Sketchfab archive, stage it, then run the orchestrator."""
    jobdir = os.path.join(INBOX, time.strftime("sf_%Y%m%d_%H%M%S_") + dest_name)
    os.makedirs(jobdir, exist_ok=True)
    fpath = os.path.join(jobdir, dest_name + ".zip")
    JOB_SEQ[0] += 1
    job = {"id": JOB_SEQ[0], "label": label, "cmd": [], "status": "running",
           "started": datetime.datetime.now().strftime("%H:%M:%S"), "tail": "downloading…"}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        while len(JOBS) > 12:
            JOBS.pop(min(JOBS))

    def runner():
        try:
            job["tail"] = "downloading %s" % model_uid
            req = urllib.request.Request(archive_url)
            req.add_header("User-Agent", "sketchfab-pipeline-dashboard/1.0")
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(fpath, "wb") as fh:
                shutil.copyfileobj(r, fh)
            if override:
                with open(os.path.join(jobdir, "override.json"),
                          "w", encoding="utf-8") as fh:
                    json.dump(override, fh, indent=2)
            job["tail"] = "converting…"
            p = subprocess.run([sys.executable, ORCH, "--file", fpath],
                               capture_output=True, text=True, timeout=900)
            job["tail"] = (p.stdout + p.stderr).strip()[-1800:]
            job["status"] = "done" if p.returncode == 0 else "failed"
        except Exception as exc:
            job["tail"] = str(exc)
            job["status"] = "failed"
    threading.Thread(target=runner, daemon=True).start()
    return job["id"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/sf_oauth_cb":
            body = OAUTH_CB_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/img":
            q = urllib.parse.parse_qs(u.query)
            res, fn = q.get("res", [""])[0], q.get("f", [""])[0]
            if (not re.fullmatch(r"[a-z0-9_]+", res)
                    or not re.fullmatch(r"[a-zA-Z0-9_.]+\.png", fn)):
                return self._json({"error": "bad name"}, 400)
            fp = os.path.join(OUT_RES, res, fn)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            body = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: -j["id"])
            oa = sf_load_oauth()
            tok, scheme = sf_token()
            self._json({"published": scan_published(),
                        "quarantined": scan_quarantine(),
                        "inbox": scan_inbox(), "jobs": jobs,
                        "sf": {"connected": bool(tok),
                               "method": scheme,
                               "username": oa.get("username", ""),
                               "has_client_id": bool(oa.get("client_id"))}})
        elif u.path == "/api/sf/oauth/start":
            oa = sf_load_oauth()
            cid = oa.get("client_id", "")
            if not cid:
                return self._json({"error": "no sketchfab client id configured",
                                   "configured": False}, 400)
            SF_STATE[0] = secrets.token_hex(8)
            cb = "http://127.0.0.1:%d/sf_oauth_cb" % self.server.server_port
            url = ("https://sketchfab.com/oauth2/authorize/?" +
                   urllib.parse.urlencode({
                       "response_type": "token", "client_id": cid,
                       "redirect_uri": cb, "state": SF_STATE[0]}))
            self._json({"url": url, "configured": True})
        elif u.path == "/api/sf/search":
            q = urllib.parse.parse_qs(u.query)
            params = {"type": "models", "count": 24,
                      "downloadable": "true",
                      "sort_by": "-publishedAt"}
            if q.get("q", [""])[0].strip():
                params["q"] = q["q"][0].strip()
                params["sort_by"] = "-likeCount"
            if q.get("animated", [""])[0] == "1":
                params["animated"] = "true"
            if q.get("rigged", [""])[0] == "1":
                params["rigged"] = "true"
            lics = [s for s in q.get("lic", [""])[0].split(",") if s]
            if lics:
                params["licenses"] = lics
            if q.get("cursor", [""])[0]:
                params["cursor"] = q["cursor"][0]
            try:
                r = sf_get("/search", params)
            except Exception as exc:
                return self._json({"error": str(exc)}, 502)
            self._json({"results": [sf_model_brief(m) for m in r.get("results", [])],
                        "next": (r.get("cursors") or {}).get("next")})
        elif u.path == "/api/preview":
            q = urllib.parse.parse_qs(u.query)
            try:
                jid = int(q.get("job", ["0"])[0])
            except ValueError:
                return self._json({"error": "bad job"}, 400)
            info = PREVIEWS.get(jid)
            if info is None:
                return self._json({"error": "unknown preview job"}, 404)
            if not os.path.isfile(info["json"]):
                return self._json({"status": "running"})
            try:
                body = open(info["json"], "rb").read()
            except OSError as exc:
                return self._json({"error": str(exc)}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/sf/model":
            q = urllib.parse.parse_qs(u.query)
            uid = q.get("uid", [""])[0].strip()
            if not uid:
                return self._json({"error": "uid required"}, 400)
            try:
                m = sf_get("/models/" + uid)
            except Exception as exc:
                return self._json({"error": str(exc)}, 502)
            b = sf_model_brief(m)
            b["description"] = (m.get("description") or "")[:400]
            self._json(b)
        else:
            self._json({"error": "not found"}, 404)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = self._body()

        if u.path == "/api/run":
            try:
                req = json.loads(body or b"{}")
                p, ov = req.get("path", ""), req.get("override") or {}
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if not p or not os.path.isfile(p):
                return self._json({"error": "file not found: " + p}, 400)
            if ov:
                try:
                    p = stage_with_override(p, ov)
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
            jid = launch_job(os.path.basename(p),
                             [sys.executable, ORCH, "--file", p])
            return self._json({"job": jid})

        if u.path == "/api/preview":
            try:
                req = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            src = _resolve_source(req.get("file"), req.get("path"))
            if src is None:
                return self._json({"error": "file not found"}, 400)
            atype = req.get("asset_type", "prop")
            outdir = os.path.join(
                WORK_DIR, "preview_" + time.strftime("%Y%m%d_%H%M%S"))
            os.makedirs(outdir, exist_ok=True)
            out_json = os.path.join(outdir, "preview_scene.json")
            try:
                blender = _find_blender()
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 500)
            cmd = [blender, "--background", "--factory-startup",
                   "--python", WORKER, "--",
                   "--dump-preview", src, out_json,
                   "--asset-type", atype]
            if atype == "ped":
                donor = _ped_donor_ydd()
                if donor:
                    cmd += ["--ped-donor", donor]
                else:
                    return self._json(
                        {"error": "ped preview needs donor/donor.json ydd"}, 400)
            jid = launch_job("preview " + os.path.basename(src), cmd)
            PREVIEWS[jid] = {"json": out_json, "source": src}
            return self._json({"job": jid})

        if u.path == "/api/compile-rig":
            try:
                req = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            src = _resolve_source(req.get("file"), req.get("path"))
            if src is None:
                return self._json({"error": "file not found"}, 400)
            ov = req.get("override") or {}
            recipe = req.get("rig_recipe")
            if not isinstance(recipe, dict) or not recipe.get("parts"):
                return self._json({"error": "rig_recipe required"}, 400)
            ov["rig_recipe"] = recipe
            try:
                p = stage_with_override(src, ov, hint="rig")
            except OSError as exc:
                return self._json({"error": str(exc)}, 500)
            jid = launch_job(os.path.basename(p),
                             [sys.executable, ORCH, "--file", p])
            return self._json({"job": jid})

        if u.path == "/api/sweep":
            files = scan_inbox()
            for f in files:
                launch_job(f, [sys.executable, ORCH, "--file",
                               os.path.join(INBOX, f)])
            return self._json({"launched": len(files)})

        if u.path == "/api/dismiss":
            try:
                job = json.loads(body or b"{}").get("job", "")
            except json.JSONDecodeError:
                job = ""
            if job:
                dis = load_dismissed()
                dis.add(job)
                save_dismissed(dis)
            return self._json({"ok": True})

        if u.path == "/api/upload":
            fn = os.path.basename(q.get("filename", ["upload.bin"])[0])
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".glb", ".gltf", ".fbx", ".obj", ".zip"):
                return self._json({"error": "unsupported extension " + ext}, 400)
            ctype = self.headers.get("Content-Type", "")
            filebytes, override = body, {}
            if "multipart/form-data" in ctype:
                bnd = ctype.split("boundary=")[-1].encode()
                for part in body.split(b"--" + bnd):
                    if b'Content-Disposition' not in part:
                        continue
                    head, _, data = part.partition(b"\r\n\r\n")
                    data = data.rstrip(b"\r\n-")
                    if b'name="override"' in head:
                        try:
                            override = json.loads(data.decode("utf-8", "replace") or "{}")
                        except json.JSONDecodeError:
                            return self._json({"error": "override is not valid JSON"}, 400)
                    elif b'name="file"' in head:
                        filebytes = data
            if not isinstance(override, dict):
                return self._json({"error": "override must be a JSON object"}, 400)
            jobdir = os.path.join(INBOX,
                                  time.strftime("upl_%Y%m%d_%H%M%S_")
                                  + os.path.splitext(fn)[0])
            os.makedirs(jobdir, exist_ok=True)
            fpath = os.path.join(jobdir, fn)
            with open(fpath, "wb") as fh:
                fh.write(filebytes)
            if override:
                with open(os.path.join(jobdir, "override.json"),
                          "w", encoding="utf-8") as fh:
                    json.dump(override, fh, indent=2)
            jid = launch_job(fn, [sys.executable, ORCH, "--file", fpath])
            return self._json({"job": jid})

        if u.path == "/api/sf/token":
            try:
                tok = (json.loads(body or b"{}").get("token") or "").strip()
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            os.makedirs(BUILD, exist_ok=True)
            with open(SF_TOKEN_FILE, "w", encoding="utf-8") as fh:
                json.dump({"token": tok}, fh)
            return self._json({"ok": True, "set": bool(tok)})

        if u.path == "/api/sf/client_id":
            try:
                cid = (json.loads(body or b"{}").get("client_id") or "").strip()
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            oa = sf_load_oauth()
            if cid:
                oa["client_id"] = cid
            else:
                oa.pop("client_id", None)
            sf_save_oauth(oa)
            return self._json({"ok": True, "set": bool(cid)})

        if u.path == "/api/sf/oauth/complete":
            try:
                d = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if not SF_STATE[0] or d.get("state") != SF_STATE[0]:
                return self._json({"error": "oauth state mismatch — restart the login"}, 400)
            tok = d.get("access_token", "")
            if not tok:
                return self._json({"error": "no access_token in callback"}, 400)
            try:
                me = sf_get("/me", token=tok)
            except Exception as exc:
                return self._json({"error": "token check failed: " + str(exc)}, 502)
            oa = sf_load_oauth()
            oa["access_token"] = tok
            oa["expires_at"] = time.time() + int(d.get("expires_in") or 3600)
            oa["username"] = me.get("username") or me.get("displayName") or ""
            sf_save_oauth(oa)
            SF_STATE[0] = ""
            return self._json({"ok": True, "username": oa["username"]})

        if u.path == "/api/sf/oauth/disconnect":
            oa = sf_load_oauth()
            for k in ("access_token", "expires_at", "username"):
                oa.pop(k, None)
            sf_save_oauth(oa)
            return self._json({"ok": True})

        if u.path == "/api/sf/download":
            try:
                req = json.loads(body or b"{}")
                uid = req.get("uid", "").strip()
                ov = req.get("override") or {}
                meta = req.get("meta") or {}
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if not uid:
                return self._json({"error": "uid required"}, 400)
            tok, scheme = sf_token()
            if not tok:
                return self._json({"error": "not logged in — connect sketchfab or paste "
                                           "your API token first"}, 401)
            try:
                d = sf_get("/models/" + uid + "/download",
                           token=tok, scheme=scheme)
            except Exception as exc:
                return self._json({"error": str(exc)}, 502)
            gltf = (d.get("gltf") or {}).get("url")
            if not gltf:
                return self._json({"error": "model is not downloadable"}, 400)
            slug = re.sub(r"[^a-z0-9_]+", "_",
                          (meta.get("name") or uid).lower()).strip("_") or ("sf_" + uid[:8])
            ov.setdefault("license", meta.get("license") or None)
            ov.setdefault("source_url", meta.get("viewer_url") or None)
            ov.setdefault("attribution", meta.get("author") or None)
            ov = {k: v for k, v in ov.items() if v is not None}
            jid = launch_sf_job(slug, uid, gltf, ov, slug)
            try:
                os.makedirs(BUILD, exist_ok=True)
                with open(os.path.join(BUILD, "attribution_log.jsonl"),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "time": datetime.datetime.now().isoformat(" ", "seconds"),
                        "uid": uid, "model": meta.get("name"),
                        "creator": meta.get("author"),
                        "license": meta.get("license"),
                        "viewer_url": meta.get("viewer_url"),
                        "asset_name": slug}) + "\n")
            except OSError:
                pass
            return self._json({"job": jid})

        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8420)
    args = ap.parse_args()
    os.makedirs(INBOX, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard: http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
