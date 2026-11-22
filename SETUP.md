# Setup

The pipeline is Python **standard-library only** — no `pip install`. The
only external requirements are the toolchain below.

## 1. Blender + Sollumz

- Install **Blender 5.2 LTS** (other 5.x may work; the worker pins a
  Sollumz version check — see `blender_worker.py` `WARN_SOLLUMZ_VERSION`).
- Install **Sollumz 2.9.0** as a Blender add-on (enabled in every
  background job via `bpy.ops.preferences.addon_enable`).
- If Blender isn't in the default install dir
  (`C:\Program Files\Blender Foundation\Blender *\blender.exe`), set:

```bat
set SFP_BLENDER_EXE=C:\path\to\blender.exe
```

## 2. texconv (textures → DDS)

`texconv.exe` is Microsoft's DirectXTex command-line tool (MIT). It is
**not vendored** — download a release build:

1. Get `texconv.exe` from the DirectXTex GitHub releases
   (`microsoft/DirectXTex`, release asset `texconv.exe`).
2. Drop it into `bin/` in this repo.
3. Alternative: set `SFP_TEXCONV=C:\path\to\texconv.exe`.

If missing, texture conversion degrades to generated flat-color DDS only —
props still export, they just won't carry source textures.

## 3. Donor ped skeleton (only needed for ped/clothing work)

Ped conversion retargets source rigs onto a real GTA V ped skeleton
(`SKEL_*` bones) read from a donor `.ydd` drawable dictionary. **The donor
is Take-Two copyrighted content and is never shipped** — you supply it
from your own GTA V install:

1. Install **CodeWalker** (dexyfex/CodeWalker).
2. In CodeWalker, open your GTA V directory, navigate into
   `update/update.rpf/x64/data/cdimages/` and find a simple single-mesh
   ped component drawable (e.g. `a_m_y_skater_01.ydd`, or a component
   drawable like `head_000_r.ydd` from `x64/models/cdimages/componentpeds_*.rpf`).
3. Export it as `.ydd` somewhere on disk.
4. Point the pipeline at it — any of:
   - `set SFP_PED_DONOR_YDD=C:\path\to\donor.ydd` (env var, checked first)
   - `setup.json` in the repo root: `{"ped_donor_ydd": "C:/path/to/donor.ydd"}`
   - `donor/donor.json`: copy `donor/donor.example.json` and edit `ydd`.

All three paths are gitignored. The donor is *referenced in place* —
the pipeline never copies or redistributes it.

Without a donor, `asset_type: "ped"` jobs fail with
`ERR_PED_DONOR_MISSING` and everything else (props, multi-part assemblies,
the Rig Workbench) works fine.

## 4. Optional: per-user `setup.json`

For convenience, all local tool paths can live in one gitignored file:

```json
{
  "gta5_dir": "C:/Games/Grand Theft Auto V",
  "codewalker_dir": "C:/Tools/CodeWalker",
  "ped_donor_ydd": "C:/path/to/donor.ydd"
}
```

`gta5_dir`/`codewalker_dir` are reserved for future donor-extraction
tooling; only `ped_donor_ydd` is consumed today.

## 5. Sketchfab API (optional)

Drop-file conversion needs nothing else. Sketchfab search/download in the
dashboard uses the official OAuth flow — the token is stored at
`build/sketchfab_oauth.json` (gitignored) and never leaves your machine.
Only assets your account is licensed to download can be fetched; per-asset
license/attribution is carried through to every `build_report.json`.

## Verify

```bat
python test_harness.py
```

Generates synthetic fixtures, runs the full gate/quarantine matrix plus a
rig-recipe compile end-to-end. Requires Blender + Sollumz; needs no donor.
