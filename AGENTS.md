# sketchfab_pipeline — agent map

Headless Sketchfab → FiveM asset converter. **Zero
Python dependencies (stdlib only), zero network calls** except the optional
Sketchfab API. Spec of record: `PIPELINE_SPEC.json` (v1.1.0). Motion modes:
`MOTION_SPEC.md`. `README.md` is the living file map.

## File map

| File | Runs | What it owns |
|---|---|---|
| `orchestrator.py` | host | Job lifecycle: safe zip extraction, sidecar merge+validate, Blender subprocess, Gates 1–4b + manifest/lua gates, publish → registry |
| `blender_worker.py` | inside Blender | Normalization (EC_02–EC_10), prop + ped_clothing export via Sollumz, texture prep + texconv, motion modes, `--dump-preview` + `rig_recipe` consumption, `worker_result.json` |
| `preview_bake.py` | inside Blender (imported by worker) | Evaluated world-space bake + `preview_scene.json` export for the Rig Workbench: per-part tris/verts, padded-AABB islands, role guesses, `source_anim` driver-matrix tracks. Contract: `reference/RIG_WORKBENCH.md` |
| `render_check.py` | inside Blender, post-publish | Advisory re-import + iso/side/top + `_assembly_*` PNG renders. `WARN_RENDER_CHECK`, never quarantines |
| `inspect_sollumz.py` | host | Capability probe → `build/toolchain_caps.json` (cached; delete to re-probe) |
| `autorig_check.py` | host | Auto-rig donor-wrap verification |
| `dashboard.py` | host | Web UI on `:8420` — see UI section below |
| `test_harness.py` | host | E2E regression suite — run after worker/orchestrator changes |
| `bin/texconv.exe` | — | PNG→DDS converter — user-downloaded (SETUP.md §2), never shipped |
| `vendor/` | — | Vendored CSS kits (see UI section) |
| `SETUP.md` | — | Prerequisites: Blender/Sollumz/texconv/donor ped skeleton |
| `LICENSE` / `THIRD_PARTY_NOTICES.md` | — | AGPL-3.0-only (bpy+Sollumz GPL linkage) + dependency licenses |
| `donor/donor.example.json` | — | Template → copy to `donor/donor.json` (gitignored) for ped work |

Directories: `inbox/` (drop files), `work/` (per-job scratch, deleted on publish),
`quarantine/<job>/` (failures + `failure_report.json`), `donor/` (rig donors),
`build/` (registry, toolchain caps, `resources/[sketchfab_props]/<name>/` output),
`testserver/` (runtime check scratch).

## dashboard.py — UI architecture (session 2026-11-22)

**The entire UI is one `PAGE = r"""..."""` string.** HTML + CSS + JS live inside
it; there is no static-file route. Editing the UI = editing that string only —
backend tests (`test_harness.py`) never parse it, but **never touch Python code
outside `PAGE`** for UI work. (`OAUTH_CB_PAGE` is a separate tiny page that
parses the Sketchfab OAuth URL fragment and POSTs the token back — it is
deliberately unstyled/minimal.)

### Chrome (always visible, outside tabs)

- `.ph` pinned header: title (`.sig cy-text-glow`), `#st_status` chip
  (`.chip.ok` green OPERATIONAL / `.chip.run` pulsing cyan RUNNING: n),
  `#st_ready` chip (last published + ago), `#st_prob` chip (`.chip.amber`,
  shows `× n` count when >1 undismissed) — a **button** that opens the
  problems rail (`aria-expanded`/`aria-controls`), `#clock`.
- `.pipe` strip: `<progress id="pipeline-progress" class="cy-progress" max="4">`
  — `value` removed while any `jobs[]` is `running` (native indeterminate);
  `<output id="pipeline-status" role="status">` shows `PIPELINE — IDLE` when
  idle, the full 4-step caption + job labels while running.
- `.tabs` `role="tablist"` → four `.tabbtn` `role="tab"` buttons:
  `tab-discover` / `tab-library` / `tab-rig` / `tab-settings`, each
  `aria-controls` the matching `panel-*` section.
- `#problems-rail`: fixed right drawer (translateX toggled by `.open`), holds
  the rail head (`#qtoggle` show-all toggle, close ×) and `#problist` —
  quarantined jobs with headline/suggestion/dismiss/raw-report `<details>`.

### Tabs

- **Discover & Convert** (`panel-discover`): Sketchfab search card —
  `#sf_q` query (Enter submits), `.sflic` license checkboxes, `#sf_anim`/`#sf_rig`
  toggles, `#sf_res` results, `#sf_sel` selected-model preview (name + guessed
  kind tag + description + Sketchfab embed iframe), `#sf_go_row` with `#sfbtn`
  DOWNLOAD + CONVERT and "more results" pagination. Below: `.disc-grid` two-col —
  `#dropcard` convert form (file drop `#drop`/`#upl`, `#f_name` spawn name,
  `kind` radios static/snapshot/multi/ped with `#posesrow`/`#pedrow` conditional
  rows, `#f_height`, `#f_col` collision, `tex` radios, `#f_gen` gen,
  `#f_lic`/`#f_url`, `#f_adv` raw override.json `<details>`, `#convertbtn`,
  `pathbox` local-path runner, inbox-sweep link) + `#joblog` conversions log
  inside a `.cy-terminal .termcard` card (dark bg + scanline overlay +
  auto-scroll-on-new + `copyLog` header button; no "clear" — JOBS has no
  clear endpoint). Form grouped into `.formsec` sections (prop identity /
  scale & collision / textures & generation / attribution) with
  `fieldset.cy-fieldset>legend` radio groups; inputs use kit classes
  `.cy-radio`/`.cy-checkbox`; `.drop.picked` = selected-file state.
- **Asset Library** (`panel-library`): `.view-switch` three `data-view` buttons
  (`details` / `preview` / `grid`, `aria-pressed` state) + `#readylist`.
  `STATE.published` renders as: details → `.libtable` rows (name click-to-copy,
  tags, dims, tris, runtime_validation, built); preview → `.item` cards with
  `propTech` technical-details `<details>` + 6-angle renders; grid → `.libgrid`
  contact sheet. Test fixtures (`r.test`) collapse under `.fxgrp` in every mode.
- **Rig Workbench** (`panel-rig`): interactive part-rigging canvas —
  `#rig_file` inbox-file select + `#rig_atype` (prop / ped) + `#rig_prevbtn`
  (`rigPreview()` → POST `/api/preview` → `--dump-preview` subprocess →
  `preview_scene.json`). `#rig_canvas` zero-dependency `<canvas>` renderer
  (click select, shift+drag marquee, backface culled, stroke skipped above
  ~15k screen tris), `#rig_sel` selection readout, `#rig_parts` part list,
  `#rig_role_row` role assign row (`#rig_role` role select, `#rig_bone`
  bone-lock select, trash / select-island buttons), `#rig_pivot_row` pivot
  editor (`#rig_px`/`#rig_py`/`#rig_pz`), `#rig_animbar` animation controls
  (`#rig_scrub` scrubber, `#rig_playbtn`, `#rig_srcclip_btn` source-clip
  pick, `#rig_capmode` capture mode, `#rig_clipname`, `#rig_in`/`#rig_out`/
  `#rig_hz`, rest-pose button), `#rig_skel` GTA-skeleton overlay toggle
  (ped mode), `#rig_tlabel`/`#rig_status` readouts. `rigCompile()` POSTs
  `/api/compile-rig` with `{file, override, rig_recipe}` — the recipe keys
  parts by **source object name**; same-role multi-clusters export as
  `<asset>_<role>_<idx>` (`rotor_0`, `rotor_1`, …) so `.ydr` names never
  collide. Data contract: `reference/RIG_WORKBENCH.md`.
- **Settings & Tools** (`panel-settings`): Sketchfab auth block (`#sf_status`,
  `#sf_login`/`#sf_logout`, `#sf_cidrow`, `#sf_tokrow`) and inbox card
  (`#inboxn2` count + sweep button). NOTE: `#inboxn` also exists inside the
  convert-form hint — **both** are updated by `render()`.

### JS surface (inside the same string)

Core: `$` (getElementById), `api(u,opt)` fetch→json, `esc`, `ago`, `dims`,
`KIND`/`KINDTAG` maps (motion_mode → STATIC/FLIPBOOK/MOVING PARTS/PED CLOTH tag).
Tabs/rail/views: `activateTab(name)`, `toggleProblemsRail()`, `setView(v)`
(clears `#readylist` sig then `render()`), `data-view` buttons wired at load.
Forms: `pickedFile`, `kindChanged` (shows `#posesrow`/`#pedrow`),
`buildOverride()` → the `override` payload sent to run/upload/sf-download,
`convert()` (multipart `/api/upload`), `showPath`/`runPath` (`/api/run`),
`sweep` (`/api/sweep`), `dismiss` (`/api/dismiss`), `copyNm` (clipboard spawn name).
Sketchfab: `sfSearch(more)` (`/api/sf/search` + license/anim/rig params),
`sfSuggest` (name/category heuristic → kind radio), `sfPick` (fetches model,
**auto-fills `#f_name`/`#f_lic`/`#f_url` and the kind radio**, shows preview),
`sfGo` (`/api/sf/download` + override + meta), `sfSaveToken`/`sfSaveCid`/
`sfLogin`/`sfLogout` (oauth endpoints).
Library render: `propMeta` (kind/gen/tags), `propRenders` (assembly thumbs in
`.render-grid-6` frames + collapsible parts set), `propTech` (kv block),
`renderReady(STATE)` — the `rsig` cache key **includes `viewMode`** so mode
switches re-render but identical polls don't.
Chrome updates: `updateProgress` (indeterminate bar + caption), `render()` —
the single 3s render pass (header chips, inbox counts, sf auth visibility,
`renderReady`, `#problist` with `psig`, `#joblog` with `jsig`), `refresh()`
(`/api/state` → `render`, polled every 3s).
Dialog: `lbox(src,cap)` → `showModal`, `hideLbox` → `close`; Esc is native
`<dialog>` behavior, backdrop-click closes via a `target===dialog` listener.
Rig Workbench: `rigPreview`/`rigPoll` (`/api/preview` job + poll),
`rigRenderParts`, `rigPickPart`/`rigSelInfo`/`rigSelectIsland`, `rigApplyRole`,
`rigPivotSet`, `rigTrashSel`, `rigPopulateBones`, `rigToggleSkel`,
`rigAnim`/`rigPlayPause`/`rigScrub`/`rigSyncAnimInputs`/`rigUseRest`
(source-anim playback + rest-pose rebase — `rest_sec` snaps to a sampled
time and ships `rest_frame` so JS and Blender evaluate the same frame),
`rigCompile` (`/api/compile-rig`), `rigLoadInbox`, `rigRestore`, `rigStatus`,
`rigRefit`.

### Data, persistence, endpoints

- `STATE` = `/api/state` poll → `{published, quarantined, inbox, jobs, sf}`.
  **No per-prop endpoint** — library detail is inspection-only, no re-convert.
- `localStorage`: `sig_tab` (active tab), `sig_view` (library view mode) —
  restored on load so the 3s poll never kicks the user.
- `dataset.sig` caching on `#readylist`/`#problist`/`#joblog` — cheap diff so
  unchanged lists aren't rebuilt each poll.
- Endpoints used: `/api/state`, `/api/upload`, `/api/run`, `/api/sweep`,
  `/api/dismiss`, `/api/preview` (GET list + POST launch `--dump-preview`),
  `/api/compile-rig` (POST `{file, override, rig_recipe}`),
  `/api/sf/{search,model,download,token,client_id, oauth/start,
  oauth/complete, oauth/disconnect}`, `/img?res=<name>&f=<png>` for renders.
- Drag & drop is bound to **`#dropcard`** (the whole card), files go through
  `pickedFile`.

### CSS

Two vendored kits inlined verbatim in `<style>`, preceded by
`@layer reset, base, theme, components, utilities, cyberpunk-ui;`:
cybercore = structure (`cyber-card`, `cyber-btn`, `cyber-input`, `cyber-badge`,
`cyber-table`…), cyberpunk-ui = effects (`cy-text-glow`, `cy-terminal`,
`progress.cy-progress`). Custom un-layered CSS comes last and always wins —
layout, `.hidden`, `.ph`/`.pipe`/`.tabs`/`.tabbtn`, `#problems-rail`,
`.libtable`/`.libgrid`/`.render-grid-6`/`.render-frame`, `#lbox` dialog, and the
legacy classes JS templates emit (`.item .nm .sub .tag .kv .prob .empty .fxgrp
.thumb .mono .dim .sec .hint2`). Render thumbs must stay neutral mid-gray
(`filter:none`, `#6b6f76` frame) — no scanlines/tint. `.hidden{display:none!important}`
must stay after all kit CSS.

### Tripwires

- Every JS-referenced `id` must exist in markup — `f_*`, `sf_*`, `st_*`,
  `joblog`, `jobsn`, `readylist`, `problist`, `qtoggle`, `inboxn`, `inboxn2`,
  `drop`, `dropcard`, `dropfn`, `dropchg`, `upl`, `convertbtn`, `sfbtn`, `clock`,
  `copylog`,
  `lbox`, `lbx_img`, `lbx_cap`, `lbx_tab`, `pathbox`, `posesrow`, `pedrow`,
  `sf_res`, `sf_sel`, `sf_go_row`, `pipeline-progress`, `pipeline-status`,
  `problems-rail`, `panel-*`/`tab-*` pairs.
- Rig Workbench IDs (all JS-referenced): `tab-rig`, `panel-rig`, `rig_file`,
  `rig_atype`, `rig_prevbtn`, `rig_status`, `rig_canvas`, `rig_sel`,
  `rig_parts`, `rig_role_row`, `rig_role`, `rig_bone`, `rig_pivot_row`,
  `rig_px`, `rig_py`, `rig_pz`, `rig_rrx`, `rig_rry`, `rig_rrz` (rest_rot
  degrees inputs), `rig_animbar`, `rig_scrub`, `rig_playbtn`,
  `rig_srcclip_btn`, `rig_capmode`, `rig_clipname`, `rig_in`, `rig_out`,
  `rig_hz`, `rig_skel`, `rig_tlabel`. Removing any breaks the workbench
  silently (no backend test parses `PAGE`).
- Hidden panels stay in the DOM — `render()` writes into all of them every poll.
- Inline handlers exist (`onclick=` in markup and JS-generated markup) — keep
  function names stable.
- Upgrading kits: download new dist → `vendor/` → re-inline into `PAGE`
  (see `vendor/README.md`).

## Run / verify

```sh
python dashboard.py            # http://127.0.0.1:8420
python test_harness.py         # full E2E (needs Blender + Sollumz)
python orchestrator.py --file model.glb
```
