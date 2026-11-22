# Vendored CSS kits

Both are MIT-licensed, zero-dependency, pure CSS. They are **inlined verbatim**
into `dashboard.py`'s `PAGE` string (the dashboard serves one self-contained
page — there is no static-file route and the tool must work offline). These
files are the provenance/license of record and the source to re-inline from
when upgrading.

| File | Package | Version | Source | Layers |
|---|---|---|---|---|
| `cybercore.min.css` | `cybercore-css` (sebyx07) | 0.3.0 | https://unpkg.com/cybercore-css@0.3.0/dist/cybercore.min.css | `reset, base, theme, components, utilities` |
| `cyberpunk-ui.css` | `@laddtnov/cyberpunk-ui` | 0.11.1 | https://cdn.jsdelivr.net/npm/@laddtnov/cyberpunk-ui@0.11.1/cyberpunk-ui.css | `cyberpunk-ui` |

Usage split in the page: cybercore = structure (cards/buttons/tables/inputs/
badges); cyberpunk-ui = effects (text-glow, terminal card, `progress.cy-progress`).
Both are cascade-layered, so the page's un-layered custom CSS always wins.
`@layer` declaration order in the page: `reset, base, theme, components,
utilities, cyberpunk-ui`.
