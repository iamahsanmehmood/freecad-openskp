"""FreeCAD importer for native SketchUp (.skp) files, built on OpenSKP.

Reads a .skp file's raw parse output (planar faces/loops, not a
triangulated mesh) and builds real B-rep Part.Face/Part.Compound
geometry - matching FreeCAD's own CAD-kernel semantics far better than
importing a triangle soup would.

Instancing: a real building-scale file can place the same component
definition thousands of times (a stud, a bolt, a window). Building full
B-rep geometry from scratch for every PLACEMENT rather than every
UNIQUE definition is not just slower, it's a different complexity class
- confirmed on a real 5,554-instance/2,747-unique-definition production
file, where the naive approach required 358,198 individual Part.Face
constructions (each a real OCC geometric-kernel call) for what is
actually only 216,981 unique faces. Each unique definition's local-space
shape is built exactly once and cached; every placement after the first
is a cheap Shape.copy() + transformShape(), not a rebuild.

Coordinate/unit handling: SketchUp stores geometry in inches; FreeCAD's
native unit is millimetres, so every point is scaled by 25.4 once, at
the point where local-space faces are first built. SketchUp's own
instance-placement matrix is a flat 13-element [row-major 3x3
rotation/scale (9) + translation (3) + trailing scale scalar (1)] form
- verified directly against openskp's own transform_point/
multiply_matrices in _core.py, not the (currently incorrect) "16-element
column-major" docstring on the Instance dataclass itself. Only the
translation component needs the inches->mm scale factor applied when
converting to a FreeCAD.Matrix; the 3x3 rotation/scale part is a
dimensionless ratio, unaffected by units.
"""
from __future__ import annotations

import os
import sys
import time

import FreeCAD as App
import Part

INCH_TO_MM = 25.4
IDENTITY_13 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

# Vendored copy of the openskp package (pure Python, no compiled wheel) -
# lets this addon work standalone without asking users to separately
# `pip install openskp` into FreeCAD's own embedded interpreter.
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)


def _to_freecad_matrix(m13):
    """13-element openskp instance matrix -> FreeCAD.Matrix (row-major
    4x4), scaling only the translation component to millimetres."""
    return App.Matrix(
        m13[0], m13[1], m13[2], m13[9] * INCH_TO_MM,
        m13[3], m13[4], m13[5], m13[10] * INCH_TO_MM,
        m13[6], m13[7], m13[8], m13[11] * INCH_TO_MM,
        0.0, 0.0, 0.0, 1.0,
    )


def _loop_points(definition, loop):
    pts = []
    for edge_id, sign in loop:
        edge = definition.edges.get(edge_id)
        if edge is None:
            return None
        vid = edge.v1_id if sign >= 0 else edge.v2_id
        v = definition.vertices.get(vid)
        if v is None:
            return None
        pts.append((v.x * INCH_TO_MM, v.y * INCH_TO_MM, v.z * INCH_TO_MM))
    return pts


def _make_wire(points_mm):
    vecs = [App.Vector(*p) for p in points_mm]
    if len(vecs) < 3:
        return None
    if vecs[0] != vecs[-1]:
        vecs.append(vecs[0])
    try:
        return Part.makePolygon(vecs)
    except Exception:
        return None


def _make_face_shape(definition, face):
    """Builds one face's shape in the definition's own LOCAL space
    (no world transform applied - that happens once per placement,
    cheaply, via _to_freecad_matrix + transformShape)."""
    wires = []
    for loop in face.loops:
        pts = _loop_points(definition, loop)
        if pts is None:
            return None
        wire = _make_wire(pts)
        if wire is None:
            return None
        wires.append(wire)
    if not wires:
        return None
    try:
        return Part.Face(wires)
    except Exception:
        # Some real files carry holes with inconsistent winding relative
        # to their outer loop - fall back to the outer boundary alone
        # rather than dropping the face's material area entirely.
        try:
            return Part.Face([wires[0]])
        except Exception:
            return None


def _get_local_shape(definition, model, stats, shape_cache, visiting, progress=None):
    """Returns `definition`'s own geometry (its faces plus every nested
    instance's geometry, transformed into this definition's local
    space), built exactly once per unique definition and cached by
    identity for the lifetime of this import call."""
    key = id(definition)
    if key in shape_cache:
        return shape_cache[key]
    if key in visiting:
        return None  # a real cycle in the instance graph - bail, don't spin
    visiting = visiting | {key}

    shapes = []
    for face in definition.faces.values():
        stats["faces_built"] += 1
        shape = _make_face_shape(definition, face)
        if shape is not None:
            shapes.append(shape)
        else:
            stats["faces_skipped"] += 1

    for inst in definition.instances:
        child_def = model.definitions.get(inst.ref_idx)
        if child_def is None:
            continue
        child_local = _get_local_shape(child_def, model, stats, shape_cache, visiting, progress)
        if child_local is None:
            continue
        stats["placements"] += 1
        placed = child_local.copy()
        placed.transformShape(_to_freecad_matrix(inst.matrix))
        shapes.append(placed)

    result = Part.makeCompound(shapes) if shapes else None
    shape_cache[key] = result

    # A real building-scale file can have thousands of unique definitions;
    # a totally silent multi-minute call looks identical to a hang from
    # the outside without some visible sign of progress. The GUI gets
    # FreeCAD's own native progress bar (Base.ProgressIndicator, the same
    # mechanism its Draft/DXF importer uses); the console print is the
    # fallback for headless/freecadcmd runs, where there's no status bar
    # to draw a bar in at all.
    if progress is not None:
        progress.next()
    elif len(shape_cache) % 200 == 0:
        print(f"  ...{len(shape_cache)} unique definitions built so far")

    return result


def import_skp(filepath, doc=None):
    """Import a .skp file into a FreeCAD document, returning the document.

    Every placed instance's geometry (walked through the full scene
    graph, definitions resolved and cached, each unique definition's
    shape built exactly once) becomes part of a single Part::Feature
    compound. Materials/layers are not yet carried over - geometry
    only, for now.
    """
    import openskp

    if doc is None:
        doc = App.ActiveDocument or App.newDocument("SketchUpImport")

    print(f"openskp: parsing {os.path.basename(filepath)} "
          "(can take a while for a large file - this is real work, not a hang)...")
    t0 = time.time()
    skp = openskp.SkpFile.open(filepath)
    model = skp.parse()
    print(f"openskp: parsed in {time.time() - t0:.1f}s, building geometry...")

    t0 = time.time()
    stats = {"faces_built": 0, "faces_skipped": 0, "placements": 0}
    shape_cache: dict = {}

    # FreeCAD's own native progress bar (the same Base.ProgressIndicator
    # its Draft/DXF importer uses) - visible in the GUI's status bar
    # during the build phase, so a multi-minute import on a large file
    # shows real, moving progress instead of looking frozen. Total step
    # count is an upper bound (every definition in the file, not just the
    # ones actually reachable from the placed scene graph) since the
    # exact reachable count isn't known without doing the walk first -
    # close enough for a progress indicator, not exact enough to promise
    # a precise percentage.
    progress = None
    if App.GuiUp:
        progress = App.Base.ProgressIndicator()
        progress.start("Building SketchUp geometry...", max(1, len(model.definitions) + 1))

    try:
        root_shape = _get_local_shape(model.root, model, stats, shape_cache, frozenset(), progress)
    finally:
        if progress is not None:
            progress.stop()

    if root_shape is not None:
        obj = doc.addObject("Part::Feature", "SketchUpImport")
        obj.Shape = root_shape

    doc.recompute()
    print(
        f"openskp: {len(shape_cache)} unique definitions built in {time.time() - t0:.1f}s "
        f"({stats['faces_built']} faces, {stats['faces_skipped']} skipped), "
        f"{stats['placements']} instances placed"
    )
    return doc


def open(filename):  # noqa: A001 - FreeCAD's own import-module contract
    """Called by FreeCAD when opening a .skp file directly (File > Open)."""
    docname = os.path.splitext(os.path.basename(filename))[0]
    doc = App.newDocument(docname)
    doc.Label = docname
    return insert(filename, doc.Name)


def insert(filename, docname):
    """Called by FreeCAD when importing a .skp file into an existing
    document (File > Import)."""
    try:
        doc = App.getDocument(docname)
    except NameError:
        doc = App.newDocument(docname)
    App.ActiveDocument = doc
    return import_skp(filename, doc)
