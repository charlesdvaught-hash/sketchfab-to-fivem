# Testing — beta build renders for external diagnosis

Renders baked by the Sketchfab→FiveM pipeline vs official Sketchfab
thumbnails (REFERENCE_*.jpg — ground truth from the model pages).

## The question

`buster_drone` is published as a 5-part articulated prop (body, rotor_0/1,
thruster_tilt_0/1). In the REFERENCE image both ducted rotor rings flank
the body symmetrically at shoulder height. In `buster_assembly_deployed_*`
one nacelle sits under the hull and the right fan floats far off-body.
Is this a pivot offset, a wrong sampled animation frame used for
placement, or the source rest pose?

## Files

- `buster_assembly_{deployed,mid,folded}_{iso,side,top}.png` — multi-part
  assembly sampled at three animation times
- `sf_sf_buster_drone2_{body,rotor_0,rotor_1,thruster_tilt_0,thruster_tilt_1}_*.png`
  — per-part renders (each part is geometrically correct)
- `sf_sf_robot_humanoid_*.png` — humanoid prop build (shape OK, but grey
  vs the red/white REFERENCE — texture binding suspect)
- `military_mech_{000,045,090}.png` — mech source turntable (matches
  REFERENCE silhouette; published as rigid-bone ped, no render gate)
- `REFERENCE_*.jpg` — official Sketchfab thumbnails for the three models

## RESOLVED — double-rotation fix

`buster_FIXED_*.png` are the same assembly renders after canonicalising
part verts to pivot-local orientation (`blender_worker.py` parts loop:
verts now `D @ B⁻¹ @ (v − hinge)`, emitted basis compensated by `D⁻¹`
for `rest_rot`). Folded pose now matches REFERENCE_buster.jpg.
Root cause: verts carried baked world rotation W0 while the manifest
also emitted W0 as folded/initial_rot -> applied twice.
