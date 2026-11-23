# Guided-UX Overhaul Spec (Argon)

1 · Clarification questions (answer to override the defaults) 
 The spec below is final and assumes the default under each question. If the user answers differently, change only the sections listed. 
 Q1 · How should "external assist" work? 
 The README's "External converters — excluded by policy" rule says no automated job may contact gtax.dev or upload assets. Manual human use is allowed and sits outside the pipeline. Options: (a) a manual handoff : the dashboard prepares the file, shows the quota, the user uploads it by hand, then imports the result back with provenance recorded. (b) an in-dashboard API call after a per-job opt-in, which requires amending PIPELINE_SPEC.json and the README policy. 
 Default assumed: (a) manual handoff. The pipeline itself still makes zero network calls. Affects: §10, §11. 
 Q2 · What should a bipedal mech become in-game? 
 (a) A walking character : a ped .ydd made only of rigid_bone parts on the GTA donor skeleton. It moves with GTA's own walk animations but needs a consumer ( downstream_consumer_required ). (b) A placeable, scriptable prop : a multi-part prop whose turret, arms and guns rotate on hinges, with no walking. 
 Default assumed: the wizard asks per asset. It recommends (a) when the proportions look humanoid and (b) otherwise. Neither needs external rigging. Affects: §8.4, §9. 
 Q3 · May the overhaul touch Python outside PAGE ? 
 AGENTS.md says UI work must never touch Python outside PAGE . Some features would benefit from small additive backend pieces: a quota/provenance log for external assist, an "import external artifact" route, and a .ymt pre-check flag in /api/state . 
 Default assumed: Phases 1–3 are PAGE-only ; the quota counter lives in localStorage . Phase 4 (importing external results) is blocked until the user approves additive endpoints. Affects: §3, §10. 
 2 · Invariants & tripwires (read first) 
 Never rename or remove any existing id , including rig_file rig_atype rig_canvas rig_sel rig_parts rig_role rig_bone rig_px rig_py rig_pz rig_rrx rig_rry rig_rrz rig_animbar rig_scrub rig_playbtn rig_srcclip_btn rig_capmode rig_clipname rig_in rig_out rig_hz rig_skel rig_tlabel rig_pivot_row rig_role_row rig_status rig_prevbtn , every tab-* / panel-* pair, f_* , sf_* , st_* , inboxn / inboxn2 , joblog , readylist , problist , dropcard . Do not move them out of their current parent, because some code uses closest() or row toggles. 
 Add, don't replace. New UI goes in new elements with the prefixes wz_* (wizard), hl_* (help layer) and xa_* (external assist). Existing controls keep their handlers. The wizard drives them by setting values and calling the existing functions ( rigPreview() , rigApplyRole() , rigTrashSel() , rigSelectIsland() , rigAnim() , rigCompile() …). It never re-implements them. 
 Hidden panels stay in the DOM. Never set display:none on an existing tripwire element. Hide wizard-irrelevant controls with the class .wz-dim (opacity .35, still clickable) or by collapsing a wrapper that is new . 
 The 3 s render() must not wipe wizard state. The wizard keeps its own state in WZ (JS object) plus localStorage.sig_wz . It re-applies any .wz-* classes after render() through a wzAfterRender() call appended at the end of render() . That is the only edit inside render() . 
 Endpoints and payloads are frozen: /api/preview (GET list, POST launch, GET ?job= poll) and /api/compile-rig {file, override, rig_recipe} . The recipe shape {parts, preview_transform, user_transform, anim_capture} is unchanged. The wizard produces exactly what manual mode produces. 
 Keep the architecture: one PAGE = r"""...""" string, stdlib backend, no build step, no CDN, no libraries. Because it's a raw string, never introduce the sequence """ inside PAGE. Backslashes are literal. 
 Role vocabulary is fixed to the values the worker accepts: hull turret barrel rotor gimbal rigid_bone smooth_skin trash . NOTE thruster_tilt does not exist in the worker. The wizard shows a plain label "tilting thruster" that writes role:"gimbal" , which the worker already treats as a hinge. No new role value is added. 
 Both themes: the Settings "appearance" toggle (classic neon / workbench flat) must render every new component legibly. Use the theme's existing CSS variables, not hard-coded colours. 
 3 · Build order (each phase ships independently) 
 # Phase Delivers Touches Done when 
 1 Help layer core #hl_tip singleton, delegated handler, .cy-tip-i "i" buttons, Help toggle in header PAGE CSS + JS + one header button Hover, focus or tap on any [data-tip] shows its tip; works after switching tabs; tips never appear for elements in hidden panels 
 2 Tip coverage data-tip on every button, input, radio, select, legend and section title in all 4 tabs (copy per §5) Attributes only The coverage audit in §11 reports 0 missing 
 3 Rig wizard #wz_bar stepper, #wz_card step prompt, Guided/Expert switch, branch logic, auto-advance, smart defaults, early warnings PAGE: new markup above #rig_canvas + JS module wz* All 5 branch scripts in §8 can be completed end-to-end; Expert mode is byte-identical to today's UI apart from the new switch 
 3b Discover-tab guidance Early .ymt warning, kind helper, "rig it first" handoff to the workbench PAGE only Picking "ped clothing" shows the warning before Convert is pressed 
 4 External assist #xa_card handoff kit, quota display, provenance capture PAGE; import-back is blocked on Q3 Never visible unless the user opens it; nothing leaves the machine automatically 
 4 · Deliverable A — Help layer component 
 4.1 Markup contract 
 The tip text lives on the control itself: data-tip="What it does + when to use.||If you skip it: …" . The part after || renders as a dimmer second line (the "skip" consequence). 
 Radios: put data-tip on the wrapping <label> , not on the <input> , so hovering the label text works. Selects and inputs: on the element. Section titles ( h2 , legend , summary , .formsec captions): append one <button type="button" class="cy-tip-i" data-tip="…" aria-label="Help: <title>">i</button> inside the title. 
 Canvas ( #rig_canvas ): gets data-tip for the controls summary ("Drag to orbit · wheel to zoom · click to select · shift-drag to box-select"). 
 Copy is stored in one JS object HL_COPY = { selectorOrId: "text||skip" } . hlApply() runs once at load and sets data-tip where it isn't already in the markup. This keeps the copy in one reviewable block and lets you add tips to elements without touching their markup. Hard-coded data-tip attributes take precedence. 
 4.2 Behaviour (one delegated handler, about 60 lines) 
 Singleton <div id="hl_tip" role="tooltip" aria-hidden="true"> appended to <body> (outside every panel, so a panel's hidden state or overflow can't clip it). position:fixed; z-index above the problems rail and the <dialog> lightbox backdrop. 
 Listeners are attached to document only: pointerover (250 ms delay), pointerout , focusin (immediate), focusout , keydown Escape, and scroll / resize (hide). Because they are delegated, elements that render() rebuilds every 3 s keep working, as do hidden panels once they are shown. 
 Hidden-panel guard: before showing, if el.getBoundingClientRect() has zero width and height (or el.closest('[hidden]') matches), do nothing. 
 Touch: on pointerType==="touch" , the first tap on a .cy-tip-i shows the tip and a tap anywhere else hides it. Taps on real controls never show a tip, so a control is never blocked. 
 Placement: above the target, centred, clamped to an 8 px viewport margin; flip below if there's no room. Max width 320 px. Text is inserted with textContent (never innerHTML) so the copy can't inject markup. 
 A11y: on show, set aria-describedby="hl_tip" on the target; remove it on hide. .cy-tip-i is a real <button> , reachable with the keyboard. 
 Help toggle: new header button #hl_toggle ("? help: on/off") persisted in localStorage.sig_help . Off = hover tips disabled; the "i" buttons still work on click. Default on for first run. 
 Must not interfere with canvas dragging: suppress tips while any pointer button is down and for 300 ms after a drag on #rig_canvas . 
 4.3 Live demo (reference handler running on this page) 
 PREVIEW 
 collision ▾ 
 role assignment i 
 ← hover, tab-focus or tap the "i" 
 4.4 CSS (append to PAGE <style>; use theme vars) 
 #hl_tip{position:fixed;z-index:10000;max-width:320px;padding:8px 10px;border-radius:6px;
 font-size:12px;line-height:1.4;pointer-events:none;opacity:0;transform:translateY(4px);
 transition:opacity .12s,transform .12s;background:var(--hl-bg,#071017);
 color:var(--hl-ink,#dff6ff);border:1px solid var(--hl-line,#1d5560)}
#hl_tip.show{opacity:1;transform:none}
#hl_tip .k{display:block;opacity:.7;font-size:11px;margin-top:4px}
.cy-tip-i{/* 16px round "i" button; inherits currentColor */}
body.hl-off [data-tip]:not(.cy-tip-i){cursor:auto}
@media (prefers-reduced-motion:reduce){#hl_tip{transition:none}} 
 5 · Tooltip copy style guide + copy table 
 5.1 Rules 
 Max 2 lines at 320 px (~140 characters total). Line 1 = what it does + when you'd want it. Line 2 (after || ) = "If you skip it: …", the default behaviour. 
 No term of art without a parenthetical on first use in that tip: "collision (the invisible bump shape)", "pivot (the point it rotates around)", ".ydr (GTA's prop file)", "donor skeleton (a GTA character's bone set)". 
 Second person, present tense, sentence case, no exclamation marks, no blame ("the file has…" not "you broke…"). 
 Numbers get units and realistic ranges ("1.8 m is a person"). Wait times are stated ("takes ~30 s"). 
 Irreversible or slow actions say so: "Starts a full build (1–3 min)", "Removed from the export, not from your file". 
 Never promise in-game behaviour the pipeline doesn't verify (runtime is NOT_PERFORMED ). Say "should" or "is built to". 
 5.2 Glossary (reuse these parentheticals verbatim) 
 hull (the main body) · turret (part that swivels left/right) · barrel (part that tilts up/down, mounted on a turret) · rotor (part that spins nonstop) · gimbal (part that tilts on a hinge) · rigid bone (stays stiff, follows one bone) · smooth skin (bends with the body) · trash (left out of the export) · island (a group of touching parts) · pivot (the point it rotates around) · rest rotation (how the part sits before animating) · capture (recording the motion into the file) · generation (legacy = most servers, enhanced = newer GTA build) · donor skeleton (a GTA character's bone set from your own game copy) · collision (the invisible bump shape) · .ymt (GTA's clothing registration file, which this tool can't make yet) 
 5.3 Copy table (binding for these ids; same style for anything unlisted) 
 Target Line 1 Line 2 (skip) 
 #sf_q Search Sketchfab for free downloadable models. Press Enter to search. If you skip it: drop your own file in the convert box instead. 
 #sf_anim / #sf_rig Only show models that come with movement / with a skeleton (bones for posing). If you skip it: all models are listed. 
 .sflic labels Only show this licence. CC BY means you must credit the creator. If all are unchecked: every licence shows, including non-commercial. 
 #sfbtn Downloads this model and starts converting it with the form settings below. — 
 #f_name The name you'll type in-game to spawn it. Lowercase letters, numbers, _ only. If you skip it: made from the file name, starting with sf_. 
 kind: static One solid object that never moves: crates, statues, furniture. This is the default. 
 kind: snapshot flipbook Freezes a few poses of an animated model as separate props to swap between. If you skip it: only the first pose is kept. 
 kind: moving parts Splits parts that move (rotors, turrets) so they can spin or aim in-game. For fine control, use the Rig Workbench tab instead. 
 kind: ped clothing Rigged character or outfit for GTA bodies. ⚠ Clothing packs can't fully register yet (.ymt). Needs a donor skeleton (a GTA character's bone set) set up first. 
 #f_height Resize so it's exactly this tall, in metres. 1.8 = a person, 3 = a door frame. If you skip it: keeps the file's own size. 
 #f_col Collision (the invisible bump shape). Convex hull fits most props; none for walk-through. If you skip it: convex hull is used. Characters always use none. 
 tex radios Embed = textures inside the prop. Separate = a shared texture file (.ytd). If you skip it: embedded, the simplest option. 
 #f_gen Which GTA version to build for. Legacy works on most servers. "Both" makes two copies, the second ending in _g9. 
 #f_lic / #f_url Licence and source link, saved in the build report so credit is never lost. Filled in automatically for Sketchfab picks. 
 #f_adv summary Expert settings as raw JSON. Anything here wins over the form. Leave it empty unless a guide tells you otherwise. 
 #convertbtn Starts the conversion (about 1–3 min). Progress shows in the log on the right. — 
 #rig_file Pick a model waiting in the inbox to rig. Guided mode starts the preview automatically. Files appear here after Download, or when dropped into inbox/. 
 #rig_atype Prop = objects and machines. Ped = a character that bends with GTA's skeleton. If you skip it: prop. 
 #rig_prevbtn Builds a fast 3D preview (no textures) so you can pick parts. Takes ~10–40 s. Without it you can't rig. Plain Convert still works. 
 #rig_role What the selected parts do. Parts with the same job move together as one piece. Unassigned parts become hull (the main body). 
 #rig_bone Which GTA bone this stiff part follows, e.g. armour plate → upper arm. If you skip it: the nearest bone is picked automatically. 
 #rig_px/py/pz Pivot (the point it rotates around), in metres. Put it at the joint or axle. If you skip it: the centre of the selected parts. 
 #rig_rrx/rry/rrz Rest rotation in degrees, e.g. lay a sideways propeller flat with X = 90. If you skip it: parts keep the angle they were modelled at. 
 #rig_scrub / #rig_playbtn Scrub or play the preview motion to check that parts move as expected. Preview only; nothing is saved. 
 #rig_srcclip_btn Use the animation that came with the model as the motion source. Only shown when the file has an animation. 
 #rig_capmode Capture (recording motion into the file): clip slice = a time range; drone flight = auto-generated hover moves. If you skip it: no motion is recorded. 
 #rig_in / #rig_out / #rig_hz Start and end of the recorded clip (seconds) and samples per second. 30 is smooth. If you skip them: the whole clip at the default rate. 
 #rig_skel Show the GTA skeleton over your model to check that joints line up. Display only. 
 COMPILE TO RAGE Builds the final GTA files from your choices (1–3 min). RAGE is GTA's engine. Your rig choices are kept if it fails, so you can fix and retry. 
 #sf_login / token rows Connect your own Sketchfab account so downloads work. Only Sketchfab is contacted. Search works without it; downloads don't. 
 inbox sweep Convert every file waiting in inbox/ with its own settings, one at a time. — 
 Library view buttons Details = table, preview = cards with renders, grid = thumbnails only. Your choice is remembered. 
 6 · Deliverable B — Wizard state machine (Rig Workbench) 
 6.1 State object (new; JS + localStorage.sig_wz , keyed by file) 
 WZ = { mode:"guided"|"expert", file:"", cls:null, // cls ∈ static|machine|drone|ped|clothing|mech_ped|mech_prop
 clsSource:"sketchfab"|"name"|"geometry"|"user", step:"S0",
 done:{S0..S7:bool}, pending:[], // pending = suggested parts not yet confirmed
 warnings:[], previewJob:null, compileT:0, xa:null } 
 6.2 States 
 S0 PICK ─file→ S1 CLASS ─auto→ S2 PREVIEW ─parts→ S3 TRIAGE ─done→ S4 ROLES ─all set→
S5 PIVOTS ─confirmed→ S6 MOTION ─chosen→ S7 REVIEW ─[user clicks]→ S8 COMPILE ─┬→ S9 DONE
 └→ S9F FAILED → jump to mapped step
Any S≥2 ─class change→ S2 (re-preview; roles reset after confirm) Any ─Back/stepper click→ that step (if reached) 
 State Entry action Advance guard (auto unless noted) Skippable when 
 S0 PICK Card: "Pick a model from the inbox". Empty inbox → link to the Discover tab. #rig_file change with a value — 
 S1 CLASS Suggest a class (§6.4). Show 5 big option tiles with the suggestion pre-selected. Set #rig_atype (ped for ped/clothing/mech_ped). Clothing → the .ymt warning card appears here. Immediately calls rigPreview() with the suggestion. The tiles stay clickable while the preview runs. Never hidden; always confirmable 
 S2 PREVIEW Progress card with an elapsed-time counter. Wrap rigStatus output in plain words. rigPoll returns parts. On error → error card (§7.3) + Retry. — 
 S3 TRIAGE Dim the parts preview_bake marked trash . List islands by size. Buttons: "Trash outlier islands", "Trash selected", "Keep all". Human: "Done — next". Auto-skip (with toast + Back) if there is one island and no trash suggestions 
 S4 ROLES Apply smart defaults (§6.5) as pending (hatched outline). Branch-specific role chips replace the raw select (the select still updates underneath). Every non-trash part has a role and the user clicked "Looks right" (human) static: auto-complete (all hull) 
 S5 PIVOTS For each moving cluster: auto-pivot (§6.5), a pivot gizmo in the canvas, rest-rotation presets ("lay flat", "stand up"). Human: confirm each cluster (checkmark list) static, ped (smooth/rigid only), clothing 
 S6 MOTION Offer only the branch's anim buttons + capture mode. Auto-play a preview anim. Choice made, including "no motion" static, clothing 
 S7 REVIEW Checklist: name, height, collision, generation, warnings, parts per role, licence. Inline edit fields mirror #f_name , #f_height , #f_col , #f_gen (writes into those inputs). Never auto. The user presses Compile, which calls rigCompile() . — 
 S8 COMPILE Watch STATE.jobs . Show the pipeline strip caption in plain words. The asset name appears in STATE.published with a newer built_at , or a new STATE.quarantined entry for this file — 
 S9 / S9F DONE: spawn snippet + copy button + "Open in Library". FAILED: human_reason headline + "Fix it" button that jumps to the step in §8.6. — — 
 6.3 Guided vs Expert 
 New segmented switch #wz_mode (Guided | Expert) at the top-right of panel-rig , persisted in localStorage.sig_wzmode . Default Guided on first run. 
 Expert = today's UI exactly. #wz_bar and #wz_card are collapsed, .wz-dim classes are removed, and no auto-actions run. Tips still work. 
 Switching modes mid-flow keeps everything. Both modes read and write the same recipe state the existing rig* functions maintain. Expert → Guided recomputes WZ.done from that state and lands on the first incomplete step. 
 6.4 Class suggestion (first match wins; always overridable) 
 The file came from a Sketchfab pick: map the sfSuggest kind (ped → ped, ped clothing → clothing, moving parts → machine, else static). Store the pick in WZ keyed by the inbox filename when sfGo succeeds. 
 Filename keywords (word-boundary, case-insensitive): drone|quad|uav|copter → drone; mech|robot|walker|exo → mech_ped; shirt|jacket|hoodie|pants|outfit|dress|shoe → clothing; character|man|woman|soldier|npc|ped → ped; tank|turret|crane|heli|fan|turbine → machine. 
 Otherwise static, but after the preview loads, re-check geometry (§6.5). If bind_kind has any SKINNED: part, show a one-click banner "This model has a skeleton. Rig as a character?" (never an automatic switch). 
 6.5 Smart defaults (client-side, computed from preview_scene.json) 
 Trash: start from preview_bake 's guesses (span > max(0.5 m, 3× median), plus bridge-island singletons). Add "outlier islands": islands whose combined bounding box is < 5% of the main island's volume and whose centre is > 1.5× the main island's radius away. Also any part whose name matches ground|floor|plane|base|backdrop|env|sky|grid|shadow . These are suggestions only , applied by the one-click button and never on their own. 
 Rotor: a part with a source_anim track whose rotation about one local axis grows steadily (≥ 300° over the clip), or a flat disc (two near-equal bounding-box sides, third side < 15%) whose name matches prop|rotor|blade|fan . 
 Turret / barrel (machine): tracked rotation mostly about Z → turret. Rotation mostly about X/Y, on a part sitting above a turret → barrel. With no animation: name match only ( turret|barrel|gun|cannon ). Otherwise leave it unassigned and ask. 
 Ped: keep the bake's guess ( BONE_PARENT → rigid_bone , else smooth_skin ). Parts with ≤ 3 % bend potential (rigid name hints armor|plate|helmet|visor|pad|gun|holster ) are suggested as rigid_bone with the nearest donor bone. 
 Pivot: rotor = disc centre; turret = bottom-centre of the cluster; barrel = the end of the cluster nearest the turret's top; gimbal = the bounding-box face centre closest to the hull. Each starts marked "unconfirmed". 
 7 · Exact behaviour changes per screen 
 7.1 Header (all tabs) 
 Add #hl_toggle after #st_prob . Add data-tip to #st_status ("Green = idle and ready. Cyan = a conversion is running."), #st_ready and #st_prob ("Conversions that failed and why. Dismissing hides a problem; it doesn't delete it."). 
 Pipeline caption: add a plain-words line under the existing 4-step caption. "Step 2 of 4: shrinking the model and building its bump shape (collision)". Map from the existing caption text; don't change the existing caption. 
 7.2 Discover & Convert 
 Kind helper: under the kind radios, a new #wz_kindhint line that re-renders on kindChanged (hook by adding one call at the end of kindChanged ). It gives a one-sentence example per kind plus "Needs rigging? → Open in Rig Workbench" (selects the file in #rig_file and calls activateTab('rig') ). 
 Early .ymt warning ( #wz_ymtwarn , amber callout) shown when kind = ped clothing or the raw JSON contains "apparel_pack": true . Text: "Heads up: wearable clothing packs need a .ymt (GTA's clothing registration file) this tool can't make yet. The 3D mesh still converts, but the job will stop at publish and land in Problems. You can still use the mesh with a custom script." The user must tick "I understand" before Convert is enabled for that job (session-only). 
 Ped prerequisites: when kind = ped, show "Needs a donor skeleton from your own GTA copy (see SETUP.md §3). Collision is set to none automatically." Set #f_col to none and lock it with a tip explaining why. Restore the previous value when the kind changes back. 
 Height sanity: if #f_height > 80 or < 0.02, show an inline warning before submit ("GTA rejects props outside 0.02–80 m"; this mirrors Gate 3). 
 Name check: on #f_name input, sanitize as you type (lowercase, [a-z0-9_] ). If the name exists in STATE.published , show "Already used. Re-converting the same file updates it; a different file with this name will be rejected." 
 Jobs log: unchanged markup. Each quarantined job line gets a "What now?" link that opens the problems rail on that entry. 
 7.3 Rig Workbench layout (Guided) 
 panel-rig
├─ [NEW] #wz_mode (Guided | Expert) top-right
├─ [NEW] #wz_bar stepper: Pick · Type · Preview · Clean up · Parts · Pivots · Motion · Review
├─ [NEW] #wz_card current-step prompt: title, 1–2 sentences, primary button, secondary links, warnings
├─ existing toolbar (#rig_file #rig_atype #rig_prevbtn) ← unchanged
├─ existing canvas + side panel (#rig_canvas, #rig_sel, #rig_parts, #rig_role_row, #rig_pivot_row, #rig_animbar…)
│ Guided: controls irrelevant to the current step get .wz-dim (still usable)
└─ [NEW] #wz_islands island list (S3 only): swatch · part count · size · "trash" toggle 
 Stepper items: reached steps are clickable, unreached ones are disabled, and the current step is highlighted. Done steps show ✓. Steps marked "skipped" show a dash and stay clickable. 
 Part list ( #rig_parts ): after each rigRenderParts , wzAfterRender adds a role badge (plain label) and a "suggested" hatch to pending parts. It must not rebuild the list. 
 Role chips: #wz_roles shows only the branch's roles as big buttons with plain labels. A click sets #rig_role 's value and calls rigApplyRole() . 
 Preview error card: map rigStatus text. Timeout → "The preview took too long. Very heavy models can take minutes; try again or raise the timeout." Ped with no donor → "Character mode needs your donor skeleton (SETUP.md §3)." Anything else → show the raw text in a <details> . 
 Undo: a wizard-level one-step undo ( #wz_undo ) for the last trash or role action. It snapshots the per-part role map before each wizard action and restores it via rigApplyRole . 
 7.4 Asset Library 
 Only tips plus badges. Add a badge for provenance.external_assist (purple "EXTERNAL TOOL") when present. Add a downstream_consumer_required badge ("needs a script to use") for ped output. No new endpoints. 
 7.5 Settings & Tools 
 Add tips everywhere. Add card #xa_settings "Outside help (off by default)", described in §10. Add a "Reset guided tips & wizard" button that clears sig_wz* and sig_help . 
 8 · Asset-class branches 
 Legend: AUTO pre-filled, user just confirms · HUMAN needs judgment · CHEAP wrong choice fixed in the browser instantly · RE-PREVIEW ~10–40 s redo · RE-COMPILE only fixable by building again (1–3 min). 
 Every step before S8 is CHEAP unless marked otherwise; only the class choice (prop ↔ ped) costs a re-preview. Anything discovered after compile is RE-COMPILE. 
 8.1 Static prop (cls = static) 
 Step Behaviour Type If wrong 
 S1–S2 rig_atype=prop , auto-preview AUTO RE-PREVIEW 
 S3 Trash display bases and ground planes (name + outlier rules) HUMAN one click CHEAP 
 S4 All remaining parts → hull. Auto-completed, shown as ✓. AUTO CHEAP 
 S5–S6 Skipped — — 
 S7–S8 Review → compile. Shortcut note: "This didn't need rigging. Next time, plain Convert does the same." HUMAN RE-COMPILE 
 8.2 Machine with moving parts — tank, crane, turret (cls = machine) 
 Step Behaviour Type If wrong 
 S3 Trash suggestions plus the island list HUMAN CHEAP 
 S4a "Click everything that swivels left/right" → turret. Pre-selected from the animation and name guesses. HUMAN membership CHEAP 
 S4b "Now click what tilts up/down on top of it" → barrel. Validation: a barrel cluster must sit above or inside the turret's bounding box, else warn. HUMAN membership CHEAP 
 S4c Rest → hull automatically. Same-role separate clusters export as <asset>_turret_0 , _1 … (tell the user) AUTO CHEAP 
 S5 Auto-pivots (turret bottom-centre, barrel at the turret end). The user confirms each by watching tank_aim follow the mouse. AUTO + confirm CHEAP before compile 
 S6 Anim buttons shown: none / tank_aim / source_clip (if any). Capture: clip_slice only when a source clip exists. HUMAN CHEAP 
 8.3 Prop / thruster drone (cls = drone) 
 Step Behaviour Type If wrong 
 S3 Trash the display stand or landing pad if it's a separate island HUMAN CHEAP 
 S4 Role chips: body (hull) · spinning propeller (rotor) · tilting thruster (writes gimbal ) · trash. Rotors are auto-detected (disc / spin-track rule); each rotor island becomes its own cluster → rotor_0..n . AUTO rotors · HUMAN thrusters CHEAP 
 S5 Rotor pivot = disc centre (spin axis +Z is fixed by the worker). If a rotor disc is edge-on (thin axis not Z), auto-suggest rest rotation "lay flat" (90° on the thin axis) with a before/after preview. Thruster hinge pivot is human-confirmed. AUTO + confirm CHEAP / RE-COMPILE if missed 
 S6 Auto-play drone_spin . Capture default drone_reactive_flight with in/out/hz defaults (0 / 4 s / 30). Plain explanation: "records hover and tilt moves so it looks alive in-game". AUTO CHEAP 
 8.4 Humanoid character (cls = ped) 
 Step Behaviour Type If wrong 
 S1 rig_atype=ped . Card checks prerequisites: donor skeleton (SETUP.md §3); "collision will be none"; "triangle budget is a hard limit for characters (no auto-shrink)". AUTO RE-PREVIEW 
 S2 Preview with donor bones. #rig_skel is turned on automatically. AUTO — 
 S3 Trash the second character, weapons on the floor, the display base. The island list is crucial here. HUMAN CHEAP 
 S3b Fit check: "Does the GTA skeleton line up with your character?" Shows rigRefit ("Re-fit to kept parts") and height. If the bounding box isn't upright (Y-up import), offer rotation presets. HUMAN CHEAP 
 S4 Default: body/clothes → smooth skin (bends). Hard pieces (armour, helmet, gear) → rigid bone with the nearest bone pre-filled in #rig_bone . The user reviews by playing ped_walk ; red spike edges (> ~35 cm stretch) are flagged as "fix these parts". AUTO + HUMAN review CHEAP before compile 
 S6 Preview only: ped_walk / ped_squat (stretch test). No capture; GTA animates peds. AUTO — 
 S7 Review shows "Result is a character file (.ydd) that needs a script or clothing setup to appear in-game". — RE-COMPILE 
 8.5 Clothing (cls = clothing) 
 S1 blocks before preview with the .ymt card (same copy as §7.2) and two choices: "Convert the mesh only (works with a script)" → continue as ped without apparel_pack ; "I need a wearable shop item" → explain that the job will stop at publish with ERR_PED_APPAREL_YMT_UNSUPPORTED and the .ydd lands in quarantine/<job>/stream/ . This requires an explicit "Continue anyway" tick. 
 Then S2–S4 as in 8.4, with smooth_skin as the default for everything and rigid-bone hints only for buckles and armour. Warn about cloth physics: if the raw JSON has clothing_fix.cloth , warn that it's unsupported ( ERR_PED_CLOTH_PHYSICS_UNSUPPORTED ). 
 One component per job (from the README). If the preview shows islands at both the torso and the legs, suggest "split into two jobs" and let the user trash one set per job. 
 8.6 Failure → step mapping (S9F "Fix it" button) 
 Quarantine reason Plain headline Jump to 
 ERR_GATE3_DIMS_OUT_OF_RANGE Size is outside what GTA accepts (2 cm–80 m) S7 height field 
 ERR_GATE4_NAME_COLLISION / _ARCHETYPE_COLLISION That name is taken by a different model S7 name field 
 ERR_PED_NO_WEIGHTS / _SKELETON_MISMATCH The character couldn't be attached to the skeleton S4 (check smooth/rigid roles), S3b fit 
 ERR_PED_APPAREL_YMT_UNSUPPORTED Expected: clothing packs can't register yet S1 clothing choice 
 triangle-budget failure (ped) Too detailed for a GTA character S3 (trash hidden or interior parts) 
 ERR_WORKER_FAILED / other human_reason() text S7 + "Show details" 
 9 · Decision point: bipedal mech / unusual rig 
 9.1 Evaluation 
 Option How it works Fits a hard-surface mech? Verdict 
 A. Rigid-bone character (local) asset_type:"ped" ; every plate is rigid_bone locked to the nearest donor bone; cables and fabric only → smooth_skin . The worker's strict order keeps rigid groups uncontaminated. Yes. Plates stay stiff and pivot at the joints. GTA's own walk animations drive it. This kills the "rubber-bending plates" failure. Default for humanoid proportions. Limits: human joint layout; .ydd needs a consumer. 
 B. Articulated prop (local) Multi-part prop: legs + pelvis = hull, torso = turret, arms/guns = barrel or gimbal, fans = rotor. Optional source_clip capture. Yes for "stands and aims" (sentry, parked walker, set dressing). No walking. Default for non-humanoid shapes (reverse-knee legs, > 3.5 m, 4+ legs) 
 C. UniRig-style auto-rig (external / GPU) A transformer predicts a new, model-specific skeleton + skin weights. Needs a CUDA GPU with ≥ 8 GB VRAM and PyTorch. No. GTA peds must use GTA's skeleton, so a custom skeleton can't be used, and the local ped path already auto-weights onto the donor skeleton. Smooth weights on hard plates bring back rubber-bending. Rejected. It also breaks the zero-dependency rule. Don't build it. 
 Conclusion: bipedal mechs are best served by A or B, both fully local. The wizard never offers external assist for mechs, peds or clothing. External help (§10) is only offered for static props that fail locally , as a manual, opt-in diagnostic. 
 9.2 Wizard flow for cls = mech (S1 sub-question, mandatory) 
 Question card: "Should it walk around like a character, or stand and move parts (turn, aim)?" Two tiles. Walk → mech_ped ( rig_atype=ped ); stand → mech_prop ( rig_atype=prop ). 
 Proportion guard (after preview, mech_ped only): height > 3.5 m, legs bending backwards (knee point behind the hip–ankle line, estimated from leg-island bounding boxes), or more than 2 leg islands touching the ground → amber card: "GTA's human skeleton will bend this mech's legs like a person's. Switch to 'stand and move parts'?" [Switch] [Keep walking]. Switching costs a re-preview (cheap). 
 mech_ped: S3 trash → S3b fit (skeleton overlay on, hip and knee alignment check) → S4 all parts default to rigid_bone with the nearest bone. The user switches only flexible bits to smooth skin → S6 ped_walk / ped_squat : plates that clash or gap at joints are highlighted. Explain that gaps at knees and elbows are normal for rigid armour → S7 review. 
 mech_prop: S3 → S4 chips: body (hull) / swiveling torso (turret) / aiming arm or gun (barrel) / hinge (gimbal) / spinning (rotor) → S5 pivots (torso = waist point, arms = shoulder) → S6 tank_aim preview, capture clip_slice if a source clip exists → S7. 
 10 · External assist — explicit, per-job, manual opt-in 
 Standing rule preserved: production conversion stays local. The dashboard makes no request to any converter. "Outside help" is a manual handoff : the human uploads by hand in their own browser tab. This is the use the README already allows ("manual human comparison/diagnostic use … outside the pipeline"). 
 10.1 Eligibility (all must be true, or the offer never renders) 
 Settings master switch #xa_enable is ON (default OFF, stored in localStorage.sig_xa ). 
 Class is static (one-piece prop). Never for ped, clothing, mech, drone or machine. 
 This source has failed locally at least once with ERR_WORKER_FAILED , ERR_EXPORT_ARTIFACT_MISSING , ERR_GATE1_* or ERR_GATE2_YTYP_INVALID , and the user has already seen the local "Fix it" suggestion. 
 The licence isn't empty. If it's ND or unknown, add "Check that the licence lets you upload the model to another service." 
 10.2 Components 
 Id Where Content / behaviour 
 #xa_settings Settings tab card "Outside help (off by default)" Toggle #xa_enable . Service row: "gtax.dev GLB→YDR: free/basic 3 per day, Pro 100, Ultra 300 (their published limits; check their site)". Local usage counter: "Handoffs in the last 24 h on this computer: n" (from localStorage.sig_xa_log : timestamp, file, sha). No API-key field; nothing is stored for the service. 
 #xa_card S9F failure screen, below the primary local "Fix it / Retry" button, collapsed ( <details> ), purple accent Title "Try an outside converter (optional)". Lists exactly what would leave the computer (file name, size, licence). Quota line "Used today: n / 3 on the free tier (counted on this computer, may differ from the site)". Status pill: Not started · Handed off · Result imported · Abandoned . 
 #xa_ack inside #xa_card Unchecked checkbox, required per job, never remembered: "I choose to upload this model to gtax.dev myself. The local pipeline stays the default." 
 #xa_go inside #xa_card Disabled until #xa_ack is ticked. Click: copy the source path to the clipboard, log the handoff to sig_xa_log , set the status to "Handed off" and window.open the gtax.dev page in a new tab (user-initiated navigation, no fetch). Shows "Upload the file, download the result, then come back." 
 10.3 Return path and provenance 
 Phase 4a (PAGE-only, default): the card explains that returned files are outside the pipeline . They aren't registered and get no build report, so they're marked as a manual comparison. "Abandon" resets the status. This exactly satisfies the current README policy. 
 Phase 4b (blocked until the user approves Q3 and amends PIPELINE_SPEC.json + the README "excluded by policy" section): new additive route POST /api/xa/import {job_id, files[]} . It runs Gates 1, 3, 4, 4b and the manifest gate on the returned .ydr/.ytyp/.ytd , then publishes with build_report.provenance.external_assist = {service:"gtax.dev glb-to-ydr", mode:"manual_handoff", consented_at, local_failure_reason, source_sha256, returned_sha256[]} , runtime_validation:"NOT_PERFORMED" , plus a registry flag. The Library shows the purple "EXTERNAL TOOL" badge. Imported output is never used to validate or "fix" local builds. 
 Before 4b, check that the sidecar/override schema doesn't reject unknown keys. If it does, provenance goes through orchestrator code, not override.json . 
 If Q1 = (b) API mode instead: keep every rule above, replace #xa_go with a confirm dialog that repeats what will be uploaded, add a key field stored only in build/ (gitignored), show the server-reported quota, and make the request from the backend only after a per-job confirm. This requires the policy amendment first. 
 11 · What stays untouched + acceptance checks 
 11.1 Untouched 
 All Python outside PAGE (unless Q3 is approved for Phase 4b only), OAUTH_CB_PAGE , human_reason() wording (the wizard wraps it, never edits it), and the vendored CSS kits (add new rules after them). 
 Endpoints and payloads: /api/state , /api/upload , /api/run , /api/sweep , /api/dismiss , /api/preview , /api/compile-rig , /api/sf/* , /img . 
 Existing functions keep their names and signatures. Only these are allowed one appended hook line: render() → wzAfterRender() , kindChanged() → wzKindHint() , rigRenderParts() → wzAfterRender() , rigPoll() success/error → wzOnPreview(ok, data) . 
 localStorage keys sig_tab and sig_view and their semantics. New keys: sig_help sig_wz sig_wzmode sig_xa sig_xa_log . 
 Zero-dependency rule: no CDN, no fonts, no libraries, no network calls added to the page. 
 11.2 Acceptance checks (run after every phase) 
 Tripwire diff: the id set before ⊆ the id set after.
 python -c "import re,dashboard as d;open('ids.txt','w').write('\n'.join(sorted(set(re.findall(r'id=\"([^\"]+)\"',d.PAGE)))))"
# run before edits → ids_before.txt, after → ids_after.txt; then:
comm -23 ids_before.txt ids_after.txt # must print nothing 
 Tip coverage audit (paste in the browser console; must log 0):
 [...document.querySelectorAll('button,input,select,textarea,legend,summary,h2,[role=tab]')]
 .filter(e=>!e.closest('#hl_tip')&&!(e.dataset.tip||e.closest('label[data-tip]')||e.querySelector?.('.cy-tip-i')))
 .map(e=>e.id||e.outerHTML.slice(0,60)) // → [] 
 Hidden panels: load on the Library tab, switch to Rig, hover #rig_prevbtn → the tip appears. While on Discover, call show() programmatically on #rig_role → no tip. 
 Poll safety: select 3 parts and wait 10 s (3+ render() passes). The selection, wizard step, pending hatches and the open tip all persist. 
 Branch scripts: walk each branch in §8/§9 with a fixture (crate, tank, quad drone, humanoid, shirt, mech). The rig_recipe POSTed by Guided must deep-equal the one Expert mode produces for the same choices (log both). 
 Early warnings: picking kind = ped clothing shows #wz_ymtwarn before any request is sent; Convert stays disabled until acknowledged. 
 No silent network: with DevTools Network open, run every flow with #xa_enable off and on. No request to any host except 127.0.0.1:8420 and the Sketchfab API. The gtax.dev page opens only as a user-clicked new tab. 
 Raw-string safety: python -c "import dashboard" succeeds, and PAGE contains no """ . 
 Themes and keyboard: every new component is legible in classic and workbench themes; the stepper, chips and "i" buttons are reachable with Tab; Escape closes the tip. 
 Expert parity: in Expert mode, the visible UI equals today's plus the #wz_mode switch and the "i" buttons. No auto-actions fire (verify by picking a file: no preview starts). 
 Agent instruction: implement phases in §3 order. One phase per commit; small anchored edits inside PAGE ; run 11.2 #1 and #8 after every edit batch. If a decision isn't covered here, choose the option that keeps Expert mode unchanged and adds no network or dependency, then note it in the commit message. 
