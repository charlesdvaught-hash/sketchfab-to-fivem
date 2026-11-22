# Third-Party Notices

This project is licensed AGPL-3.0-only (see `LICENSE`). It depends on —
and in the UI embeds — third-party components with the following terms:

## Required dependencies (not vendored)

| Component | License | Role |
|---|---|---|
| **Blender** (5.x, `bpy` Python API) | GPL-3.0 | Headless geometry/rig worker host. Install separately; this project's license choice reflects the GPL linkage. |
| **Sollumz** (2.9.0 Blender add-on) | GPL-3.0 | GTA V RAGE format export (.ydr/.ydd/.yft). Install separately as a Blender add-on. |
| **CodeWalker** (optional, ped donor prep) | See CodeWalker's own terms | Used only to *export* a donor `.ydd` from the user's own GTA V install. Never bundled, never automated against a copy the user doesn't own. |

## Vendored assets (shipped in this repo)

| Component | License | Location |
|---|---|---|
| **cybercore-css 0.3.0** | MIT | `vendor/` — inlined into the dashboard `PAGE` string |
| **@laddtnov/cyberpunk-ui 0.11.1** | MIT | `vendor/` — inlined into the dashboard `PAGE` string |

## Downloaded at setup (not shipped)

| Component | License | Role |
|---|---|---|
| **DirectXTex `texconv.exe`** (Microsoft) | MIT | PNG→DDS BC texture compression. Download a release build into `bin/` — see `SETUP.md`. |

## Content the pipeline never ships

- Donor ped `.ydd` files and any other GTA V game assets are Take-Two
  Interactive copyrighted content. The pipeline *references* them in place
  on the user's machine; they are excluded from this repository by
  `.gitignore` and by design.
- Converted output resources carry the source asset's own license
  (enforced via `license`/`source_url`/`attribution` sidecar fields,
  echoed into every `build_report.json`).
