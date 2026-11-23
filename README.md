# OpenSKP Import/Export for FreeCAD

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Latest release](https://img.shields.io/github/v/release/iamahsanmehmood/freecad-openskp?label=release)](https://github.com/iamahsanmehmood/freecad-openskp/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/iamahsanmehmood/freecad-openskp/total?label=downloads)](https://github.com/iamahsanmehmood/freecad-openskp/releases)
[![GitHub Stars](https://img.shields.io/github/stars/iamahsanmehmood/freecad-openskp?style=social)](https://github.com/iamahsanmehmood/freecad-openskp)

Native SketchUp (`.skp`) import **and export** for FreeCAD — built on
[OpenSKP](https://github.com/iamahsanmehmood/openskp), an MIT-licensed,
from-scratch `.skp` reader/writer. No Trimble SDK, no SketchUp
installation, no compiled extension required — pure Python, vendored directly
into this addon.

## Featured

This add-on, together with the Blender one, was covered by [Blender 3D Architect](https://www.youtube.com/@blender3darchitect):

<table>
<tr>
<td align="center" width="60%">
<a href="https://www.youtube.com/watch?v=q-Gjsa4tuPU"><img src="https://img.youtube.com/vi/q-Gjsa4tuPU/mqdefault.jpg" alt="Blender and FreeCAD Can Now Read and Write SketchUp Files (video)" width="100%"></a><br>
<b>Blender and FreeCAD Can Now Read and Write SketchUp Files</b><br>
<sub>Blender 3D Architect · October 2026</sub>
</td>
</tr>
</table>

## What it does

**Import** parses a `.skp` file's real geometry (planar faces and loops,
not a triangulated mesh) and rebuilds it as native FreeCAD `Part.Face` /
`Part.Compound` B-rep geometry - walking the full placed scene graph
(components, groups, nested instances) and composing world-space
transforms, so a component placed 50 times ends up in the right 50
places, not just modeled once.

**Component hierarchy** (the default): the model tree follows SketchUp's
own structure, so each part can be inspected, measured or rebuilt on its
own.

- Every component definition becomes an `App::Part`, kept in a hidden
  **SketchUp components** group, holding its own faces and loose edges as
  one shape, and a link for every component or group placed inside it.
  Components that are defined in the file but not placed anywhere are
  imported too.
- Every placement is an `App::Link` to its definition, labelled with the
  instance's name, carrying its position, rotation, non-uniform scale and
  mirroring. Edit a definition and every placement follows.
- The model itself is an `App::Part` named after the file, with one group
  per SketchUp tag used at its top level. A tag hidden in SketchUp imports
  hidden; a tag on a nested instance is kept in the link's `SketchUpLayer`
  property, and hides that link when the tag is hidden.
- An unpainted face shows the paint of the instance that places it, as in
  SketchUp. Where one component is placed with different paints, each
  paint gets its own definition, labelled with the material name (for
  example "Leg (Red)").
- A placement that is sheared (a 3x3 that is not a rotation and a scale per
  axis, which SketchUp allows and a link cannot hold) is imported as a
  placed copy of the component's geometry instead, labelled "(sheared
  copy)", and reported in the console.

**Merged** (the previous layout): one compound per SketchUp tag, with no
component objects. It is lighter for a very large model, where thousands of
definitions would each become an object (see
[Performance](#performance-on-large-files--read-this-before-importing-a-big-model)).
To use it, open Tools > Edit parameters, go to
`BaseApp/Preferences/Mod/OpenSKP` and add a Boolean `MergeOnImport` set to
`true`; from Python, `importSKP.import_skp(path, doc, merge=True)`.

**Solids** (an option, off by default): SketchUp stores only faces, so a
board or a leg arrives as a hollow set of faces. With `MakeSolids` set to
`true` (Tools > Edit parameters, `BaseApp/Preferences/Mod/OpenSKP`, Boolean)
or `import_skp(path, doc, solids=True)`, each component's own faces are sewn
and every closed shell becomes a solid, with its volume, ready for booleans
and measurements. Faces that close no volume stay faces, and the console
says how many. It works in both layouts; in the hierarchy each component's
geometry object becomes the solid and its links place it. Colours stay on
their faces.

**Assemblies** (an option, off by default): in the component hierarchy,
setting `ImportAssemblies` to `true` (Tools > Edit parameters,
`BaseApp/Preferences/Mod/OpenSKP`, Boolean) or passing
`import_skp(path, doc, assemblies=True)` builds every component or group
definition that contains other components or groups as a FreeCAD 1.x
`Assembly::AssemblyObject` instead of an `App::Part`, ready to open and add
joints in the Assembly workbench. Leaf definitions with only their own
geometry stay `App::Part`, and since SketchUp stores no kinematic joints,
imported assemblies start with an empty `Joints` group and keep every link's
placement as imported. When FreeCAD's Assembly module is not present, the
import falls back to `App::Part`. Export follows `Assembly::AssemblyObject`
containers the same way it follows `App::Part` and `App::Link`, skipping
joint/view/BOM/simulation metadata groups.

**Curved surfaces — circles, cylinders, cones and spheres** (`FitCurvedSurfaces`,
off by default): SketchUp has no curved B-rep surfaces, storing cylinders, cones
and spheres as flat facets joined by soft/smooth edges (alongside analytic
`ArcCurve`s on circular rims). With `FitCurvedSurfaces` set to `true` (Tools >
Edit parameters, `BaseApp/Preferences/Mod/OpenSKP`, Boolean) or
`import_skp(path, doc, curved_surfaces=True)`:

- Faces joined by soft or smooth edges are grouped into candidate surfaces:
  cylinders and cones/frustums (including partial revolutions, pointed cones,
  and inward cylindrical hole bores) recognised from `Definition.arcs` become
  true FreeCAD `Part.Cylinder` / `Part.Cone` faces, and full spheres, domes and
  spherical bands whose vertices fit a single radius within `1e-3 * r` become
  true `Part.Sphere` faces.
- Analytic circles and circular arcs bounding planar faces (discs, end caps,
  circular holes in plates, half-discs) become true `Part.Circle` / `Part.Arc`
  edges, matching the fitted curved walls so `MakeSolids` sews them into exact
  B-rep solids (and promotes closed `Part.Sphere` faces directly to solids).
- Smoothed groups that do not fit within tolerance stay faceted (with a count
  in the console report), and any circle shared with a wall that stayed faceted
  keeps its polygon segments so neighbouring faces stay water-tight connected.

The two layouts show exactly the same geometry: the tests import every
fixture both ways and compare faces, areas, vertices and edges
(`tests/test_hierarchy.py`), and check in FreeCAD's real GUI that the 3D
view draws the same extent for both (`tests/gui_hierarchy.py`).

If a source polygon is slightly non-planar and cannot be constructed as a
planar face, a simple outline is triangulated without moving any of
its 3-D boundary vertices. Concave outlines are supported; already valid
planar faces stay single faces. Non-planar face triangulation needs no optional
mesh dependencies. Each triangle inherits its source face's material,
and component/layer nesting is unchanged. Repaired and rejected source faces
are reported with their face ID and component name.

This new fallback only handles simple, single-loop polygons; the importer's
existing handling of faces with holes is unchanged. Self-intersecting or
degenerate projected outlines are not triangulated.

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
holes preserved. An imported hierarchy can be exported as it is: parts,
tag groups and links are followed down to the geometry they show, with
each link's placement and scale, and the hidden definitions are not
written a second time. Export does not write components yet, so
everything lands flat at the file's root, on the tags the import
recorded. Round-trip-verified: exported coordinates match the
original geometry exactly (see tests/test_export.py).

**Layer export**: each exported object's own `Label` becomes its
SketchUp layer/tag. This is an exact match for what layers-import
already produces (one object per layer, `Label` set to the real layer
name) - re-exporting an unmodified, just-imported set of layer objects
reproduces the *same tags exactly*, verified directly on the real
145-definition structural-framing file: all 11 imported layer names
came back identical after a round trip through export and a fresh,
independent re-parse. For freshly modelled FreeCAD content that was
never imported, each object's own `Label` simply doubles as its
exported tag.

**Material export**: each object's own `ViewObject.DiffuseColor` (one
RGBA entry per `Shape.Faces` - the same property materials-import
applies) becomes a SketchUp material per distinct color, deduplicated
across faces *and* across objects (the same color reused anywhere
registers exactly once). Solid colors and opacity only, matching
import's own scope - no texture export. Only available under the real
GUI, same limitation as materials-import (`ViewObject` doesn't exist
under headless `freecadcmd`) - a headless export still carries geometry
and layers, just no color.

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

**Layers in the merged layout** (SketchUp calls them "tags") carry over too -
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

Geometry, layers, and materials now all carry over in both directions.

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

Those numbers are for the merged layout. In the component hierarchy the
same file becomes one `App::Part` per definition and one `App::Link` per
placement (thousands of objects, each `App::Part` with its own origin), so
for a model of that size set `MergeOnImport` (see
[What it does](#what-it-does)).

**Hierarchy vs. merged layout (`tests/bench_large_hierarchy.py`)** — measured on
FreeCAD 1.1.3 (Linux x86_64) across three nested synthetic `.skp` models written
with `openskp.create()` (80% 6-face box leaf parts, 10% sub-assemblies placing 4
leaves + 1 base face, 10% top-level bays placing 2 sub-assemblies + 1 leaf across
4 tags), each run in a fresh process:

| Definitions | Layout | Document objects | Headless (`freecadcmd`) | FreeCAD GUI (`AppRun`) |
|---:|---|---:|---|---|
| **100** | Merged (`merge=True`) | **4** (`4 Part::Feature`) | 0.21 s · +34 MB RSS (134 MB peak) | **1.04 s** · +65 MB RSS (602 MB peak) |
| **100** | Hierarchy (`merge=False`) | **1,124** (`101 App::Part`, `707` origin objs, `90 Part::Feature`, `120 App::Link`, `5` groups) | 0.15 s · +34 MB RSS (133 MB peak) | **2.71 s** · +187 MB RSS (723 MB peak) |
| **1,000** | Merged (`merge=True`) | **4** (`4 Part::Feature`) | 1.60 s · +195 MB RSS (309 MB peak) | **3.98 s** · +324 MB RSS (874 MB peak) |
| **1,000** | Hierarchy (`merge=False`) | **11,114** (`1,001 App::Part`, `7,007` origin objs, `900 Part::Feature`, `1,200 App::Link`, `5` groups) | 1.29 s · +187 MB RSS (302 MB peak) | **23.29 s** · +1,490 MB RSS (2,040 MB peak) |
| **5,000** | Merged (`merge=True`) | **4** (`4 Part::Feature`) | 8.20 s · +863 MB RSS (1,088 MB peak) | **18.09 s** · +1,389 MB RSS (2,053 MB peak) |
| **5,000** | Hierarchy (`merge=False`) | **55,514** (`5,001 App::Part`, `35,007` origin objs, `4,500 Part::Feature`, `6,000 App::Link`, `5` groups) | 14.34 s · +827 MB RSS (1,051 MB peak) | **168.38 s** · +7,160 MB RSS (7,808 MB peak) |

Headless `freecadcmd` stays fast at every size because no Coin3D view providers
or Qt model-tree items exist, whereas in the real GUI every `App::Part` brings 7
origin objects (8 view providers per part before its geometry and links), pushing
1,000 definitions past +1.5 GB / 23 s and 5,000 definitions past +7.1 GB / 2m 48s
(over 9x the time and 5x the memory of `merge=True`). Importing a file with
**1,000 or more** definitions in the hierarchy layout therefore prints a console
warning suggesting `MergeOnImport` (`Tools > Edit parameters >
BaseApp/Preferences/Mod/OpenSKP`, or `merge=True` from Python).

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
| `Test_Component.skp` | 124 | 0 | 106 source polygons; 14 non-planar polygons become 32 planar triangles, 0 skipped. All original boundary vertices retained; sews into one valid closed solid and passes the detailed BOP check. See `tests/test_nonplanar.py` |
| `arcs_2017.skp`, `arcs_2021.skp` | 2 | 1 | The same model saved as SketchUp 2017 and 2021: a disc and a half disc (2 faces) and one loose 90 degree arc, which imports as a single real arc edge (radius 254 mm, length 398.98 mm), not a polyline. Pinned in `tests/test_import.py` (`check_arcs`) |
| `instance_scale_term_2021.skp`, `instance_scale_term_v6.skp` | 12 | 0 | One 100 in cube placed twice, scaled through the 3x3 and through the matrix's scale term (openskp#444), saved as SketchUp 2021 and 6. Both cubes import 1 in wide, at x = 0 and 20 in, as SketchUp reports. Pinned in `tests/test_import.py` (`check_scale_term`) |
| `plate_with_hole_2017.skp`, `plate_with_hole_2021.skp` | 30 | 0 | A 20 x 20 x 2 in plate with a radius 4 in circular through-hole, saved via SketchUp's own library as 2017 and 2021. With `FitCurvedSurfaces` + `MakeSolids`, imports as a 7-face closed solid with true `Part.Circle` hole wires and an inward `Part.Cylinder` bore (`tests/test_curved_surfaces.py`) |
| `cylinder_2017.skp`, `cylinder_2021.skp`, `frustum_2017.skp`, `frustum_2021.skp`, `sphere_2017.skp`, `sphere_2021.skp`, `smoothed_blob_2017.skp`, `smoothed_blob_2021.skp` | 26 / 26 / 128 / 168 | 0 | Cylinder (`r = 5`, `h = 12` in), frustum (`r1 = 6`, `r2 = 3`, `h = 4` in), sphere (`r = 5` in), and an asymmetric smoothed blob, saved via SketchUp's library as 2017 and 2021. With `FitCurvedSurfaces`, cylinder, frustum and sphere become `Part.Cylinder`, `Part.Cone` and `Part.Sphere` B-rep solids while the smoothed blob stays 168 planar facets (`tests/test_curved_surfaces.py`) |
| `Untitled.skp` | 1,588 | — | 1,554 imported, 34 skipped, 6 invalid — all traced to the same known overlapping-hole geometry documented in [openskp#285](https://github.com/iamahsanmehmood/openskp/issues/285); the importer falls back to the face's outer boundary alone rather than dropping it, and still fails only on the genuinely degenerate cases |
| A real structural-framing file (outside the repo) | 9,652 | 1,077 | All faces and loose edges imported, 0 skipped — 93 of the file's 145 definitions were entirely or partly loose-edge (light-gauge-steel members drawn as construction lines), previously silently invisible |

**Arcs, circles, cylinders, cones and spheres.** A loose arc or circle drawn in SketchUp (a run of straight
edges that SketchUp also stores as one exact arc) imports as a real FreeCAD arc or circle with a centre and
a radius, not a polygon. Faces stay faceted by default; with `FitCurvedSurfaces` enabled, circles and
circular arcs bounding flat faces become real circle/arc edges and soft/smooth cylinder, cone/frustum and
sphere groups become real `Part.Cylinder`, `Part.Cone` and `Part.Sphere` surfaces. A non-uniformly scaled
arc (an ellipse) stays a polyline.

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

Layer export was round-tripped on the same structural-framing production
file: imported (11 layer objects), immediately re-exported with no
changes, re-parsed independently, and the resulting `model.layers` names
matched the original 11 exactly - a real end-to-end check, not just "an
object's Label got read."

Material export was checked with a stand-in object carrying real
`ViewObject.DiffuseColor` data (headless `freecadcmd` has no real
`ViewObject` at all, the same constraint materials-import has - see
`tests/test_export.py`'s own module docstring): two boxes, one solid
red, one mixed red/50%-translucent-blue, exported and re-parsed -
exactly 2 distinct materials registered (red deduplicated across both
objects, not registered twice), and faces split 9-red/3-blue, matching
exactly. The underlying `ViewObject.DiffuseColor` property itself was
already confirmed correct in a real interactive session for
materials-*import*; export reads that same property back.

**Non-uniform scale** is verified explicitly, not just assumed to work
because rotation/translation do: a synthetic file places one component
three ways - identity, a 2x/3x/0.5x non-uniform scale, and that same
scale combined with a 90° rotation - with the expected world-space
bounding box for each computed directly from the placement matrices,
independent of any openskp/FreeCAD code. This caught a real bug (see
`importSKP.py`'s own "Non-uniform scale" docstring section): without
`checkScale=True` on `Shape.transformShape()`, FreeCAD/OCCT silently
collapses non-uniform scale to its geometric mean, so a 2x/3x/0.5x
placement came back a flat ~1.44x on every axis instead. Fixed, and
pinned as `tests/test_import.py`'s `check_nonuniform_scale()`.

**Verified interactively, not just headlessly:** the
`addImportType` registration (now in `freecad/openskp_importer/init.py`;
`tests/test_registration.py` checks that FreeCAD performs it at startup) - **File → Open** on a real `.skp` file in
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
