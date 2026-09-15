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

**Not yet carried over, either direction:** materials, layers, and layer
visibility. Geometry only, for now - a face's shape and position import
and export correctly; its color and which layer it lives on do not
(yet).

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

**Not yet verified:** the `Init.py`/`addImportType` registration that makes
**File → Open** work from FreeCAD's real GUI. It follows the exact,
documented convention FreeCAD's own bundled importers (OBJ, DAE, 3DS) use —
but `addImportType`'s dispatch only activates under the real GUI, which
`freecadcmd`'s headless mode doesn't exercise. Calling `importSKP.open()`/
`importSKP.insert()` directly (as the fixtures above do) is fully verified;
whether the GUI's own File dialog wires up to it correctly needs a real,
interactive FreeCAD session to confirm.

## Contributing

Bug reports, feature requests, and PRs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, running the tests, and
the PR process. This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
