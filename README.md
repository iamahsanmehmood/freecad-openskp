# OpenSKP Import/Export for FreeCAD

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Latest release](https://img.shields.io/github/v/release/iamahsanmehmood/freecad-openskp?label=release)](https://github.com/iamahsanmehmood/freecad-openskp/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/iamahsanmehmood/freecad-openskp/total?label=downloads)](https://github.com/iamahsanmehmood/freecad-openskp/releases)
[![GitHub Stars](https://img.shields.io/github/stars/iamahsanmehmood/freecad-openskp?style=social)](https://github.com/iamahsanmehmood/freecad-openskp)

Native SketchUp (`.skp`) import **and export** for FreeCAD — built on
[OpenSKP](https://github.com/iamahsanmehmood/openskp), an MIT-licensed,
from-scratch `.skp` reader/writer. No Trimble SDK, no SketchUp
installation, no compiled extension — pure Python, vendored directly into
this addon.

## What it does

**Import** parses a `.skp` file's real geometry (planar faces and loops,
not a triangulated mesh) and rebuilds it as native FreeCAD `Part.Face` /
`Part.Compound` B-rep geometry - walking the full placed scene graph
(components, groups, nested instances) and composing world-space
transforms, so a component placed 50 times ends up in the right 50
places, not just modeled once.

Each *unique* component/group definition's geometry is built exactly
once and cached; every placement after the first is a cheap
`Shape.copy()` + `transformShape()`, not a rebuild from scratch. This
matters a lot in practice - see the large-file numbers below.

Loose edges (construction lines/structural framing - a light-gauge-steel
member is routinely drawn this way, not as a solid) import too, as
`Part.Wire` geometry in the same compound as the faces - a definition
can have both. Found missing on a real structural-framing file (1,077
runs across 93 of 145 definitions, silently invisible before this) - the
same gap already found and fixed in the
[Blender addon](https://github.com/iamahsanmehmood/blender-openskp).

**Export** (File -> Export, or File -> Save As with a .skp extension)
flattens the selected FreeCAD objects' faces (global placement
resolved, so translated/nested objects land in the right world
position) into a new .skp file - each face's outer boundary and any
holes preserved. There's no way to infer "this should be one reusable
component" purely from arbitrary FreeCAD geometry, so export doesn't
reconstruct component/group structure - everything lands flat at the
file's root. Round-trip-verified: exported coordinates match the
original geometry exactly (see tests/test_export.py).

**Materials** (import only, for now) carry over too, as
`ViewObject.DiffuseColor` (one RGBA entry per `Shape.Faces`, FreeCAD's
own per-face-color mechanism - the same one `Part::Feature` objects with
a mixed-material compound already use elsewhere): each face's own paint,
or whatever an ancestor group/component painted itself with when the
face has none, or a neutral default when nothing was ever painted -
matching SketchUp's own paint-inheritance rule. Solid colors and opacity
only for this pass - no texture images, and (deliberately, for now) not
a face's individual layer-color fallback, since that needs a layer
id → color lookup openskp doesn't expose publicly yet; real painted
files resolve through material_id long before that fallback would
matter. Only applied under the real GUI (`ViewObject` doesn't exist
under headless `freecadcmd`) - the underlying color resolution itself is
exercised either way (see `tests/test_import.py`).

**Layers** (SketchUp calls them "tags", import only) carry over too -
unlike materials, which fit onto the existing single-compound design as
one color per face, a layer needs its own object to be independently
toggleable at all, since FreeCAD has no per-face visibility the way it
has per-face color. So import now produces **one `Part::Feature` object
per distinct layer** actually used in the file (not always a single
"SketchUpImport" object anymore), each labeled with its real layer name.
A layer switched off in SketchUp's own Tags panel imports with that
object's `Visibility` already off - one click (or `Space`) to toggle it
back on, independent of every other layer. Verified against a real
145-definition structural-framing production file: 11 distinct layers,
2 genuinely hidden in the source file (cladding layers) - both objects
correctly imported hidden, structural framing alone visible by default.

**Not yet carried over, either direction:** layer export (FreeCAD →
`.skp`), material export. A face's shape, position, and (import-only)
color/layer all carry over correctly now; nothing carries back out
beyond geometry yet.

## Performance on large files — read this before importing a big model

CAD kernels (FreeCAD's underlying OpenCASCADE) construct exact,
parametric, topologically-validated B-rep geometry — real curves,
validated closed wires, planar faces that support later fillets/booleans/
precise measurement. That's inherently more expensive per-object than a
renderer's triangle-placement job, so a 200,000+-face building model
accumulates real per-piece cost regardless of implementation quality.
This is a limitation of the CAD-kernel approach in general (true of
Revit/ArchiCAD/Navisworks on big federated models too), not a defect
unique to this addon.

Measured on a real production file — 5,554 placed instances, 2,747
unique component/group definitions, 358,188 faces in the final compound:

| Phase | Time |
|---|---|
| Parse (`openskp` reads the file) | ~93s |
| Build + place geometry (unique definitions cached, placements copied) | ~135s |
| Assign shape / other | ~70s |
| **Total** | **~297s (~5 min)** |

Progress is visible throughout — FreeCAD's native progress bar
(`Base.ProgressIndicator`) in the GUI, periodic console output under
`freecadcmd` — so a multi-minute import doesn't look identical to a hang.

The importer deliberately builds each *unique* definition's geometry once
and caches it (see [What it does](#what-it-does) above); the naive
per-placement approach would have needed 358,198 individual `Part.Face`
constructions here instead of ~217,000, a different complexity class, not
just a slower constant.

## Installation

Not yet published on FreeCAD's Addon Manager. Two ways to install manually:

**Download (no git required)**: grab the zip from the
[latest release](https://github.com/iamahsanmehmood/freecad-openskp/releases/latest),
extract it, and copy the resulting `OpenSKPImporter` folder into FreeCAD's
`Mod/` directory.

**Git clone (to track updates)**:

```bash
git clone https://github.com/iamahsanmehmood/freecad-openskp.git
```

Copy (or symlink) the cloned folder into FreeCAD's `Mod/` directory.

Either way, `Help → About → "Report an issue"` shows your `Mod/` path if
unsure. Restart FreeCAD, then **File → Open** or **File → Import** a `.skp`
file.

## Verification status, stated plainly

The core geometry pipeline (parse → walk scene graph → compose transforms →
build B-rep faces) has been tested end-to-end against real `.skp` fixtures
via FreeCAD's headless `freecadcmd`, calling `importSKP.import_skp()`
directly:

| Fixture | Faces | Loose edges | Result |
|---|---|---|---|
| `capilla_quiroz_v17.skp` | 192 | 20 | 192 faces + 20 loose edges imported, 0 invalid |
| `gondola_v20.skp` | 39,352 | — | 39,352 imported, 0 invalid |
| `SU_File.skp` | 32 | 0 | 32 imported, 0 invalid |
| `Untitled.skp` | 1,588 | — | 1,554 imported, 34 skipped, 6 invalid — all traced to the same known overlapping-hole geometry documented in [openskp#285](https://github.com/iamahsanmehmood/openskp/issues/285); the importer falls back to the face's outer boundary alone rather than dropping it, and still fails only on the genuinely degenerate cases |
| A real structural-framing file (outside the repo) | 9,652 | 1,077 | All faces and loose edges imported, 0 skipped — 93 of the file's 145 definitions were entirely or partly loose-edge (light-gauge-steel members drawn as construction lines), previously silently invisible |

Materials are cross-checked against `openskp`'s own resolved
`materials_by_id` for the same fixtures (not just "some color got
produced"): `capilla_quiroz_v17.skp`'s 16 declared materials resolve
down to 9 distinct face colors actually used, including its two
translucent glass materials (alpha 0.5 and 0.7 exactly) landing on the
right faces; `SU_File.skp` carries no materials at all, and every one of
its 32 faces correctly falls back to the shared default. On the same
real structural-framing production file above, 9,677 faces resolved to
6 distinct colors from the file's 35 declared materials in 4.4s, every
face getting exactly one color (verified 1:1 against `Shape.Faces`,
since `ViewObject.DiffuseColor` is positional).

Layers were verified the same way, plus visually in a real interactive
FreeCAD session: on the same structural-framing production file, 11
`Part::Feature` objects were created (one per layer, matching `Layer0`
plus the 10 layers actually used by placements), the two cladding layers
(`wall_external_cladding_1`, `wall_internal_cladding_1`) showed the
closed-eye Outliner icon and rendered hidden in the 3D view exactly as
SketchUp's own Tags panel has them, and every other layer rendered
visible - the structural framing skeleton alone, cladding hidden, without
manually toggling anything.

**Verified interactively, not just headlessly:** `Init.py`'s
`addImportType` registration - **File → Open** on a real `.skp` file in
FreeCAD's actual GUI (not just `freecadcmd`) opens it correctly, producing
the expected per-layer objects with materials and hidden-layer visibility
applied. `freecadcmd`'s headless mode still can't exercise
`ViewObject`-dependent behavior at all (materials/layer visibility), so
that half is checked both ways - data-only under `freecadcmd`, visually
under the real GUI - rather than headlessly alone.

## Contributing

Bug reports, feature requests, and PRs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, running the tests, and
the PR process. This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
