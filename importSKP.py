"""FreeCAD importer for native SketchUp (.skp) files, built on OpenSKP.

Reads a .skp file's raw parse output (planar faces/loops, not a
triangulated mesh) and builds real B-rep Part.Face/Part.Compound
geometry - matching FreeCAD's own CAD-kernel semantics far better than
importing a triangle soup would.

Coordinate/unit handling: SketchUp stores geometry in inches; FreeCAD's
native unit is millimetres, so every point is scaled by 25.4 on the way
in. SketchUp's own instance-placement matrix is a flat 13-element
[row-major 3x3 rotation/scale (9) + translation (3) + trailing scale
scalar (1)] form - verified directly against openskp's own transform_point/
multiply_matrices in _core.py, not the (currently incorrect) "16-element
column-major" docstring on the Instance dataclass itself.
"""
from __future__ import annotations

import os
import sys

import FreeCAD as App
import Part

# Vendored copy of the openskp package (pure Python, no compiled wheel) -
# lets this addon work standalone without asking users to separately
# `pip install openskp` into FreeCAD's own embedded interpreter.
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

INCH_TO_MM = 25.4
IDENTITY_13 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _transform_point(m, p):
    x, y, z = p
    return (
        m[0] * x + m[1] * y + m[2] * z + m[9],
        m[3] * x + m[4] * y + m[5] * z + m[10],
        m[6] * x + m[7] * y + m[8] * z + m[11],
    )


def _multiply(parent, child):
    """Same math as openskp._core.multiply_matrices - composes two
    13-element instance-placement matrices, parent-then-child."""
    p_r0 = [parent[0], parent[1], parent[2], parent[9]]
    p_r1 = [parent[3], parent[4], parent[5], parent[10]]
    p_r2 = [parent[6], parent[7], parent[8], parent[11]]
    c_c0 = [child[0], child[3], child[6], 0]
    c_c1 = [child[1], child[4], child[7], 0]
    c_c2 = [child[2], child[5], child[8], 0]
    c_c3 = [child[9], child[10], child[11], 1]

    def dot(row, col):
        return sum(r * c for r, c in zip(row, col))

    out = [0.0] * 13
    out[0], out[1], out[2] = dot(p_r0, c_c0), dot(p_r0, c_c1), dot(p_r0, c_c2)
    out[3], out[4], out[5] = dot(p_r1, c_c0), dot(p_r1, c_c1), dot(p_r1, c_c2)
    out[6], out[7], out[8] = dot(p_r2, c_c0), dot(p_r2, c_c1), dot(p_r2, c_c2)
    out[9], out[10], out[11] = dot(p_r0, c_c3), dot(p_r1, c_c3), dot(p_r2, c_c3)
    out[12] = parent[12] * child[12]
    return out


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
        pts.append((v.x, v.y, v.z))
    return pts


def _make_wire(points_inches, world_matrix):
    world_pts = [_transform_point(world_matrix, p) for p in points_inches]
    mm_pts = [(x * INCH_TO_MM, y * INCH_TO_MM, z * INCH_TO_MM) for x, y, z in world_pts]
    vecs = [App.Vector(*p) for p in mm_pts]
    if len(vecs) < 3:
        return None
    if vecs[0] != vecs[-1]:
        vecs.append(vecs[0])
    try:
        return Part.makePolygon(vecs)
    except Exception:
        return None


def _make_face_shape(definition, face, world_matrix):
    wires = []
    for loop in face.loops:
        pts = _loop_points(definition, loop)
        if pts is None:
            return None
        wire = _make_wire(pts, world_matrix)
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


def _walk(model, definition, world_matrix, shapes, stats, seen_ids, depth=0):
    if depth > 64 or id(definition) in seen_ids:
        return
    seen_ids = seen_ids | {id(definition)}
    for face in definition.faces.values():
        stats["faces_seen"] += 1
        shape = _make_face_shape(definition, face, world_matrix)
        if shape is not None:
            shapes.append(shape)
        else:
            stats["faces_skipped"] += 1
    for inst in definition.instances:
        child_def = model.definitions.get(inst.ref_idx)
        if child_def is None:
            continue
        child_world = _multiply(world_matrix, inst.matrix)
        _walk(model, child_def, child_world, shapes, stats, seen_ids, depth + 1)


def import_skp(filepath, doc=None):
    """Import a .skp file into a FreeCAD document, returning the document.

    Every planar face in the model (walked through the full placed scene
    graph, definitions/instances resolved and transformed to world space)
    becomes one Part.Face; all faces are grouped into a single
    Part::Feature compound. Materials/layers are not yet carried over -
    geometry only, for now.
    """
    import openskp

    if doc is None:
        doc = App.ActiveDocument or App.newDocument("SketchUpImport")

    skp = openskp.SkpFile.open(filepath)
    model = skp.parse()

    shapes = []
    stats = {"faces_seen": 0, "faces_skipped": 0}
    _walk(model, model.root, IDENTITY_13, shapes, stats, set())

    if shapes:
        compound = Part.makeCompound(shapes)
        obj = doc.addObject("Part::Feature", "SketchUpImport")
        obj.Shape = compound

    doc.recompute()
    print(
        f"openskp import: {stats['faces_seen']} faces seen, "
        f"{len(shapes)} imported, {stats['faces_skipped']} skipped"
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


