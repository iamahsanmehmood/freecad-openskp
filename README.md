# OpenSKP Importer for FreeCAD

Native SketchUp (`.skp`) import for FreeCAD — built on [OpenSKP](https://github.com/iamahsanmehmood/openskp),
an MIT-licensed, from-scratch `.skp` reader. No Trimble SDK, no SketchUp
installation, no compiled extension — pure Python, vendored directly into
this addon.

## What it does

Parses a `.skp` file's real geometry (planar faces and loops, not a
triangulated mesh) and rebuilds it as native FreeCAD `Part.Face` /
`Part.Compound` B-rep geometry — walking the full placed scene graph
(components, groups, nested instances) and composing world-space
transforms, so a component placed 50 times ends up in the right 50
places, not just modeled once.

**Not yet carried over:** materials, layers, and layer visibility. Geometry
only, for now — a face's shape and position import correctly; its color and
which layer it lives on do not (yet).

## Installation

Via FreeCAD's Addon Manager once published there, or manually:

```bash
git clone https://github.com/iamahsanmehmood/freecad-openskp.git
```

Copy (or symlink) the cloned folder into FreeCAD's `Mod/` directory
(`Help → About → "Report an issue"` shows your `Mod/` path if unsure),
restart FreeCAD, then **File → Open** or **File → Import** a `.skp` file.

## Verification status, stated plainly

The core geometry pipeline (parse → walk scene graph → compose transforms →
build B-rep faces) has been tested end-to-end against real `.skp` fixtures
via FreeCAD's headless `freecadcmd`, calling `importSKP.import_skp()`
directly:

| Fixture | Faces | Result |
|---|---|---|
| `capilla_quiroz_v17.skp` | 192 | 192 imported, 0 invalid |
| `gondola_v20.skp` | 39,352 | 39,352 imported, 0 invalid |
| `SU_File.skp` | 32 | 32 imported, 0 invalid |
| `Untitled.skp` | 1,588 | 1,554 imported, 34 skipped, 6 invalid — all traced to the same known overlapping-hole geometry documented in [openskp#285](https://github.com/iamahsanmehmood/openskp/issues/285); the importer falls back to the face's outer boundary alone rather than dropping it, and still fails only on the genuinely degenerate cases |

**Not yet verified:** the `Init.py`/`addImportType` registration that makes
**File → Open** work from FreeCAD's real GUI. It follows the exact,
documented convention FreeCAD's own bundled importers (OBJ, DAE, 3DS) use —
but `addImportType`'s dispatch only activates under the real GUI, which
`freecadcmd`'s headless mode doesn't exercise. Calling `importSKP.open()`/
`importSKP.insert()` directly (as the fixtures above do) is fully verified;
whether the GUI's own File dialog wires up to it correctly needs a real,
interactive FreeCAD session to confirm.

## License

MIT — see [LICENSE](LICENSE).
