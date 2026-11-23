"""FreeCAD importer/exporter for native SketchUp (.skp) files, built on OpenSKP.

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
multiply_matrices in _core.py (the Instance.matrix docstring itself was
fixed to match, openskp#320). Only the translation component needs the
inches->mm scale factor applied when converting to a FreeCAD.Matrix; the
3x3 rotation/scale part is a dimensionless ratio, unaffected by units.

Non-uniform scale: every placed instance is applied via
Shape.transformShape(matrix, False, True) - that trailing `True` is
checkScale, and it matters. Without it, transformShape() silently falls
back to OCCT's similarity-only gp_Trsf transform, which cannot represent
non-uniform scale (different factors on different axes) and collapses
it to the geometric mean of the three instead - confirmed directly, not
theoretical: a real 2x/3x/0.5x placement came back a flat ~1.44x
(cube-root of 2*3*0.5) on every axis before this was found. checkScale
makes transformShape() detect that case and fall back to the general
(BRepBuilderAPI_GTransform) transform instead, which preserves
non-uniform scale exactly - same cost as before for the common case
(uniform scale, or rotation/translation only), only paying for the more
general math when a placement genuinely needs it. See
tests/test_import.py's check_nonuniform_scale() for the regression test
this was found and fixed against.

Loose edges (construction lines/structural framing - a light-gauge-steel
member is routinely drawn this way, not as a solid) come from openskp's
public openskp.loose_edge_runs(definition) - added specifically for this
addon, since the only place that grouping previously existed was inside
build_scene()/build_instanced_scene()'s internal, triangulated-mesh-only
output. Each run becomes its own Part.Wire in the same compound as the
faces (a definition can have both), built once per unique definition and
cached exactly like face geometry is. Found missing by testing this
importer against a real structural-framing file, the same gap already
found and fixed in the Blender addon (github.com/iamahsanmehmood/
blender-openskp) - studs/king studs/header jack studs drawn as loose
edges were silently invisible before this.

Arcs and circles: a loose-edge run that is exactly one whole arc or circle
(SketchUp keeps the exact arc beside its run of straight edges: openskp's
Definition.arcs) is imported as a real FreeCAD arc or circle, with a centre
and a radius, instead of the polyline (see _make_arc_edge). Faces keep their
polygon boundaries by default; with curved_surfaces=True (or FitCurvedSurfaces),
analytic circles and circular arcs bounding planar faces become real FreeCAD
circle/arc edges too (see _make_curved_loop_wire).

Component hierarchy (the default; see _HierarchyImport): one App::Part per
component definition and one App::Link per placement, the way SketchUp
organises the model (freecad-openskp#5). The merged layout described in the
rest of this docstring is kept for very large models (merge=True or the
MergeOnImport preference); both show the same geometry.

Layers in the merged layout (SketchUp calls them "tags"): unlike materials, which fit onto
the existing single-compound design as one ViewObject.DiffuseColor per
face, a layer needs its own Part::Feature object to be independently
toggleable at all - FreeCAD has no per-face visibility mechanism the way
it has per-face color. So the import is split, one compound per distinct
layer actually used in the file (via _get_local_shape's layer buckets,
threaded down the same way as paint-inheritance: Instance.layer
overrides an inherited default the same way Instance.material_id does -
see model.py's docstrings for both). A layer switched off in the source
file's own Tags panel (model.layers' Layer.hidden, read straight off the
layer manager's own visibility byte for both legacy and modern files
alike) imports with that object's own ViewObject.Visibility already
False - one click to toggle back on, independent of every other layer.
Deliberately ignores a FACE's own individual layer override (Face.layer)
in favor of just the enclosing placement's layer - the common real-world
case is tagging a whole group/component, not individual faces within an
untagged one, and Face.layer has no public id->name lookup exposed by
openskp yet to resolve against anyway (same scope cut as materials'
layer-color fallback, see _resolve_face_color's own docstring).

Layer EXPORT is the clean inverse of this: each exported object's own
Label becomes its layer (see export()'s own docstring) - since import
already produces exactly one object per layer with Label set to the
real layer name, re-exporting an unmodified, just-imported set of layer
objects reproduces the same tags exactly, verified directly on a real
production file (see tests/test_export.py and the README).
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
import time

import FreeCAD as App
import Part

# Captured before this module defines its own `open()` below (FreeCAD's
# own import-module contract requires that exact name) - export() needs
# the real builtin to write the output file, not FreeCAD's file-open
# handler this module itself becomes.
_real_open = open

INCH_TO_MM = 25.4
IDENTITY_13 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _load_vendored_openskp():
    """Registers the vendored openskp package (pure Python, no compiled
    wheel - lets this addon work standalone without asking users to
    separately `pip install openskp` into FreeCAD's own embedded
    interpreter) directly into sys.modules, instead of prepending its
    directory to sys.path - the latter is flagged practice for legacy
    (non-namespaced) FreeCAD addons, since it leaks into every other
    module's import resolution for the rest of the session, not just
    this one's. Registering by spec keeps openskp's own internal
    relative imports (e.g. `from .create import ...`) working exactly
    as they would under a normal sys.path-based import."""
    if "openskp" in sys.modules:
        return sys.modules["openskp"]
    vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    spec = importlib.util.spec_from_file_location(
        "openskp",
        os.path.join(vendor_dir, "openskp", "__init__.py"),
        submodule_search_locations=[os.path.join(vendor_dir, "openskp")],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["openskp"] = module
    spec.loader.exec_module(module)
    return module


openskp = _load_vendored_openskp()


def _to_freecad_matrix(m13):
    """13-element openskp instance matrix -> FreeCAD.Matrix (row-major
    4x4), scaling only the translation component to millimetres.

    Index 12 is a uniform scale SketchUp can keep in the transformation's
    homogeneous term instead of in the 3x3 (openskp#444): a point maps to
    ``s * (R p + t)``. It is folded into the other twelve values first, as
    openskp does itself; ignoring it placed such an instance 1/s times too
    big and 1/s times too far from its parent's origin."""
    s = m13[12] if len(m13) > 12 else 1.0
    if s != 1.0:
        m13 = [v * s for v in m13[:12]] + [1.0]
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


def _make_loose_edge_wire(points_mm, closed):
    """Builds one loose-edge run's wire in the definition's own LOCAL
    space - a plain open or closed polyline, never auto-closed the way
    _make_wire() forces a face loop closed, since an open structural
    member (a stud running from A to B) must stay open, not become a
    closed triangle back to its own start."""
    vecs = [App.Vector(*p) for p in points_mm]
    if len(vecs) < 2:
        return None
    if closed and len(vecs) > 2 and vecs[0] != vecs[-1]:
        vecs.append(vecs[0])
    try:
        return Part.makePolygon(vecs)
    except Exception:
        return None


def _circle_arc_params(arc):
    """Validates an openskp ``ArcCurve`` as a true circular arc (not a
    non-uniformly scaled ellipse) and returns ``(center, ax, ay, normal,
    radius)`` in millimetres, with ``normal`` a unit ``App.Vector``, or
    ``None``."""
    cx, cy, cz = (c * INCH_TO_MM for c in arc.center)
    ax = [c * INCH_TO_MM for c in arc.x_axis]
    ay = [c * INCH_TO_MM for c in arc.y_axis]
    radius = math.sqrt(sum(c * c for c in ax))
    if radius <= 0.0:
        return None
    dot = sum(a * b for a, b in zip(ax, ay))
    if abs(math.sqrt(sum(c * c for c in ay)) - radius) > 1e-6 * radius or abs(dot) > 1e-6 * radius * radius:
        return None  # an ellipse (a non-uniformly scaled arc), not a circle
    normal = App.Vector(
        ax[1] * ay[2] - ax[2] * ay[1],
        ax[2] * ay[0] - ax[0] * ay[2],
        ax[0] * ay[1] - ax[1] * ay[0],
    )
    if normal.Length == 0.0:
        return None
    normal.normalize()
    return App.Vector(cx, cy, cz), ax, ay, normal, radius


def _make_arc_edge(definition, edge_ids):
    """Builds a real FreeCAD arc (or circle) for one loose-edge run that is
    exactly one whole arc curve of SketchUp's, in the definition's local
    space, or returns ``None`` so the caller keeps the polyline.

    SketchUp stores an arc or circle as a run of straight edges *and* as the
    exact arc (centre, axes, start and end angle: openskp's
    ``Definition.arcs``, keyed by the edges' ``curve_id``), so a circle drawn
    in SketchUp can arrive in FreeCAD as a circle with a centre and a radius
    rather than as a 24-sided polygon. Only a run that is the *whole* curve
    qualifies (a curve some of whose edges bound a face has no single arc
    to state), and only a true circle: an arc scaled non-uniformly is an
    ellipse, which stays a polyline here.

    The arc is built from three points on the curve rather than from angles,
    because FreeCAD measures a Part.Circle's angles from an x direction it
    picks itself, which would not be SketchUp's.
    """
    arcs = getattr(definition, "arcs", None)
    if not arcs:
        return None
    curve_ids = {definition.edges[e].curve_id for e in edge_ids if e in definition.edges}
    if len(curve_ids) != 1:
        return None
    curve_id = next(iter(curve_ids))
    arc = arcs.get(curve_id)
    if arc is None:
        return None
    if sum(1 for e in definition.edges.values() if e.curve_id == curve_id) != len(edge_ids):
        return None

    params = _circle_arc_params(arc)
    if params is None:
        return None
    center, ax, ay, normal, radius = params

    def point(t):
        return App.Vector(
            center.x + math.cos(t) * ax[0] + math.sin(t) * ay[0],
            center.y + math.cos(t) * ax[1] + math.sin(t) * ay[1],
            center.z + math.cos(t) * ax[2] + math.sin(t) * ay[2],
        )

    try:
        if arc.is_full_circle:
            return Part.Circle(center, normal, radius).toShape()
        t0, t1 = arc.start_angle, arc.end_angle
        return Part.Arc(point(t0), point((t0 + t1) / 2.0), point(t1)).toShape()
    except Exception:
        return None


_DEFAULT_FACE_RGBA = (200 / 255, 200 / 255, 200 / 255, 1.0)  # matches openskp.Material's own default color


def _material_rgba(material):
    """openskp.Material -> (r, g, b, a) floats in 0-1, FreeCAD's own
    DiffuseColor format. Two independent things can make a material
    translucent - the color record's own alpha byte, and the separate
    `transparency` factor (SketchUp's useTrans) - multiplying both is
    safe either way since an untouched one defaults to fully opaque."""
    r, g, b, a = material.color
    alpha = (a / 255.0) * material.transparency
    return (r / 255.0, g / 255.0, b / 255.0, alpha)


def _resolve_face_color(face, model, inherited_material_id):
    """A face's effective display color: its own front material if
    painted directly, else whatever material an ancestor group/component
    instance painted itself with (`inherited_material_id`, threaded down
    through _get_local_shape the same way SketchUp itself resolves an
    unpainted face's color), else a neutral default.

    Deliberately front-material only (back_material_id is ignored) and
    deliberately skips the layer-color fallback build_instanced_scene()
    also has: FreeCAD's Face.layer is a layer ID with no public id->color
    lookup exposed by openskp yet, and the vast majority of painted real
    files resolve through material_id long before that fallback would
    ever matter - a reasonable, documented scope cut for the same
    "solid colors first" pass as the Blender addon's import, not an
    oversight.
    """
    mat_id = face.material_id if face.material_id is not None else inherited_material_id
    if mat_id is None:
        return _DEFAULT_FACE_RGBA
    mat = model.materials_by_id.get(mat_id)
    if mat is None:
        return _DEFAULT_FACE_RGBA
    return _material_rgba(mat)


def _nonplanar_triangles(points):
    """Triangulate a simple nonplanar loop, retaining its exact 3-D vertices.

    Project only to choose diagonals. Dependency-free ear clipping handles
    concave outlines too; a triangle fan would incorrectly fill their notches.
    Return None for planar, degenerate or self-intersecting input. Faces with
    holes are handled by the caller, never by dropping their inner loops.
    """
    if len(points) < 4:
        return None
    vectors = [App.Vector(*p) for p in points]
    origin = sum(vectors, App.Vector()) / len(vectors)
    relative = [p - origin for p in vectors]
    normal = App.Vector()
    for a, b in zip(relative, relative[1:] + relative[:1]):
        normal += a.cross(b)
    if normal.Length == 0:
        return None
    normal.normalize()
    deviation = max(abs(p.dot(normal)) for p in relative)
    if deviation <= 1e-7:  # mm; do not reinterpret other face-construction errors
        return None

    # Drop the dominant normal axis and normalize the projection for stable
    # predicates, including components far from their local origin.
    axis = max(range(3), key=lambda i: abs(normal[i]))
    coords = [tuple(p[i] for i in range(3) if i != axis) for p in relative]
    scale = max(max(p[i] for p in coords) - min(p[i] for p in coords) for i in (0, 1))
    if scale == 0:
        return None
    coords = [(x / scale, y / scale) for x, y in coords]
    eps = 1e-12

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a, b, p):
        return (
            min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
        )

    def intersect(a, b, c, d):
        ab_c, ab_d = cross(a, b, c), cross(a, b, d)
        cd_a, cd_b = cross(c, d, a), cross(c, d, b)
        opposite_ab = ab_c > eps and ab_d < -eps or ab_c < -eps and ab_d > eps
        opposite_cd = cd_a > eps and cd_b < -eps or cd_a < -eps and cd_b > eps
        if opposite_ab and opposite_cd:
            return True
        return any(
            abs(value) <= eps and on_segment(start, end, point)
            for value, start, end, point in (
                (ab_c, a, b, c), (ab_d, a, b, d),
                (cd_a, c, d, a), (cd_b, c, d, b),
            )
        )

    count = len(coords)
    for i in range(count):
        a, b = coords[i], coords[(i + 1) % count]
        if (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= eps ** 2:
            return None
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if intersect(a, b, coords[j], coords[(j + 1) % count]):
                return None
    area2 = sum(
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(coords, coords[1:] + coords[:1])
    )
    if abs(area2) <= eps:
        return None
    winding = 1 if area2 > 0 else -1

    def point_in_triangle(point, a, b, c):
        return all(
            winding * cross(start, end, point) >= -eps
            for start, end in ((a, b), (b, c), (c, a))
        )

    remaining = list(range(count))
    triangles = []
    while len(remaining) > 3:
        for offset, current in enumerate(remaining):
            previous = remaining[offset - 1]
            following = remaining[(offset + 1) % len(remaining)]
            a, b, c = coords[previous], coords[current], coords[following]
            if winding * cross(a, b, c) <= eps:
                continue
            # Include triangle boundaries here: a diagonal must not skip over
            # another boundary vertex, including collinear vertices.
            if any(
                point_in_triangle(coords[index], a, b, c)
                for index in remaining
                if index not in (previous, current, following)
            ):
                continue
            triangles.append((previous, current, following))
            remaining.pop(offset)
            break
        else:
            return None
    if winding * cross(*(coords[i] for i in remaining)) <= eps:
        return None
    triangles.append(tuple(remaining))
    return triangles


def _make_curved_loop_wire(definition, loop, pts, curve_edge_counts):
    """Builds a planar boundary ``Part.Wire`` replacing any complete circular
    ``ArcCurve`` in ``loop`` with a true ``Part.Circle`` or ``Part.Arc`` edge
    (#18), returning ``(wire, arc_count)`` or ``None`` when ``loop`` contains
    no replaceable circular arcs (or if curve/wire construction fails).

    A full-circle loop's axis is oriented to match ``loop``'s own Newell
    winding rather than ``arc.normal`` directly, so inner hole loops keep
    their opposite (clockwise) winding when ``Part.Face(wires)`` adds them to
    the outer wire.
    """
    arcs = getattr(definition, "arcs", None)
    if not arcs or len(loop) < 3:
        return None
    directed = []
    for edge_id, sign in loop:
        edge = definition.edges.get(edge_id)
        if edge is None:
            return None
        v_start = edge.v1_id if sign >= 0 else edge.v2_id
        v_end = edge.v2_id if sign >= 0 else edge.v1_id
        if v_start not in definition.vertices or v_end not in definition.vertices:
            return None
        directed.append((edge, v_start, v_end))
    for i in range(len(directed)):
        if directed[i][2] != directed[(i + 1) % len(directed)][1]:
            return None

    vecs = [App.Vector(*p) for p in pts]
    origin = sum(vecs, App.Vector()) / len(vecs)
    rel = [p - origin for p in vecs]
    loop_normal = App.Vector()
    for a, b in zip(rel, rel[1:] + rel[:1]):
        loop_normal += a.cross(b)
    if loop_normal.Length <= 1e-12:
        return None
    loop_normal.normalize()
    if max(abs(p.dot(loop_normal)) for p in rel) > 1e-4:
        return None

    first_cid = directed[0][0].curve_id
    if (
        first_cid is not None
        and all(e.curve_id == first_cid for e, _, _ in directed)
        and len(directed) == curve_edge_counts.get(first_cid, 0)
    ):
        arc = arcs.get(first_cid)
        if arc is not None and arc.is_full_circle:
            params = _circle_arc_params(arc)
            if params is not None:
                center, _ax, _ay, normal, radius = params
                plane_tol = max(1e-4, 1e-3 * radius)
                if (
                    abs(abs(normal.dot(loop_normal)) - 1.0) <= 1e-4
                    and all(
                        abs((p - center).Length - radius) <= 1e-3 * radius
                        and abs((p - center).dot(normal)) <= plane_tol
                        for p in vecs
                    )
                ):
                    axis = normal if normal.dot(loop_normal) >= 0.0 else -normal
                    try:
                        wire = Part.Wire([Part.Circle(center, axis, radius).toShape()])
                        if wire.isClosed() and wire.isValid():
                            return wire, 1
                    except Exception:
                        return None
        return None

    if directed[0][0].curve_id is not None and directed[0][0].curve_id == directed[-1][0].curve_id:
        for k in range(1, len(directed)):
            if directed[k][0].curve_id != directed[0][0].curve_id:
                directed = directed[k:] + directed[:k]
                vecs = vecs[k:] + vecs[:k]
                break

    runs = []
    for (edge, vs, ve), p_start in zip(directed, vecs):
        cid = edge.curve_id
        if cid is not None and runs and runs[-1][0] == cid:
            runs[-1][1].append((edge, vs, ve, p_start))
        else:
            runs.append((cid, [(edge, vs, ve, p_start)]))

    seen_cids = set()
    repeated_cids = set()
    for cid, _items in runs:
        if cid is not None:
            if cid in seen_cids:
                repeated_cids.add(cid)
            seen_cids.add(cid)

    wire_edges = []
    arc_count = 0
    try:
        for cid, items in runs:
            built_arc = None
            if (
                cid is not None
                and cid not in repeated_cids
                and len(items) >= 2
                and len(items) == curve_edge_counts.get(cid, 0)
            ):
                arc = arcs.get(cid)
                if arc is not None and not arc.is_full_circle:
                    params = _circle_arc_params(arc)
                    if params is not None:
                        center, ax, ay, normal, radius = params
                        end_v = definition.vertices[items[-1][2]]
                        p_end = App.Vector(end_v.x * INCH_TO_MM, end_v.y * INCH_TO_MM, end_v.z * INCH_TO_MM)
                        run_pts = [it[3] for it in items] + [p_end]
                        plane_tol = max(1e-4, 1e-3 * radius)
                        if (
                            abs(abs(normal.dot(loop_normal)) - 1.0) <= 1e-4
                            and all(
                                abs((p - center).Length - radius) <= 1e-3 * radius
                                and abs((p - center).dot(normal)) <= plane_tol
                                for p in run_pts
                            )
                        ):
                            t_mid = 0.5 * (arc.start_angle + arc.end_angle)
                            p_mid = App.Vector(
                                center.x + math.cos(t_mid) * ax[0] + math.sin(t_mid) * ay[0],
                                center.y + math.cos(t_mid) * ax[1] + math.sin(t_mid) * ay[1],
                                center.z + math.cos(t_mid) * ax[2] + math.sin(t_mid) * ay[2],
                            )
                            p_start = items[0][3]
                            if (
                                (p_end - p_start).Length > 1e-6
                                and (p_mid - p_start).Length > 1e-6
                                and (p_end - p_mid).Length > 1e-6
                            ):
                                built_arc = Part.Arc(p_start, p_mid, p_end).toShape()
            if built_arc is not None:
                wire_edges.append(built_arc)
                arc_count += 1
            else:
                for _edge, _vs, ve_id, p_s in items:
                    ve = definition.vertices[ve_id]
                    p_e = App.Vector(ve.x * INCH_TO_MM, ve.y * INCH_TO_MM, ve.z * INCH_TO_MM)
                    wire_edges.append(Part.makeLine(p_s, p_e))
        if arc_count == 0:
            return None
        wire = Part.Wire(wire_edges)
        if wire.isClosed() and wire.isValid():
            return wire, arc_count
    except Exception:
        return None
    return None


def _make_face_shape(definition, face, curve_edge_counts=None, stats=None):
    """Builds one face's shape in the definition's own LOCAL space
    (no world transform applied - that happens once per placement,
    cheaply, via _to_freecad_matrix + transformShape). Returns one Face,
    a compound of planar triangles for a warped simple polygon, or None.
    """
    wires = []
    curved_wires = []
    curved_arc_count = 0
    loops = []
    for loop in face.loops:
        pts = _loop_points(definition, loop)
        if pts is None:
            return None
        wire = _make_wire(pts)
        if wire is None:
            return None
        wires.append(wire)
        loops.append(pts)
        if curve_edge_counts:
            curved = _make_curved_loop_wire(definition, loop, pts, curve_edge_counts)
            if curved is not None:
                curved_wires.append(curved[0])
                curved_arc_count += curved[1]
            else:
                curved_wires.append(wire)
    if not wires:
        return None
    if curved_arc_count:
        try:
            curved_face = Part.Face(curved_wires)
            if curved_face.isValid() and curved_face.Area > 0:
                if stats is not None:
                    stats["face_arcs_built"] = stats.get("face_arcs_built", 0) + curved_arc_count
                return curved_face
        except Exception:
            pass
    try:
        return Part.Face(wires)
    except Exception:
        pass
    # Preserve the existing handling of faces with holes. Triangulating such
    # boundaries needs a separate implementation; it is outside this fallback.
    if len(loops) != 1:
        try:
            return Part.Face([wires[0]])
        except Exception:
            return None
    # A single SketchUp polygon can be slightly warped. Split it into planar
    # triangles instead of dropping it or moving shared boundary vertices.
    # _make_wire closes a polygon from its start vertices. Before repairing a
    # rejected face, ensure that this does not bridge missing source segments.
    loop = face.loops[0]
    for (edge_id, sign), (next_id, next_sign) in zip(loop, loop[1:] + loop[:1]):
        edge, following = definition.edges[edge_id], definition.edges[next_id]
        end = edge.v2_id if sign >= 0 else edge.v1_id
        start = following.v1_id if next_sign >= 0 else following.v2_id
        if end != start:
            return None
    indices = _nonplanar_triangles(loops[0])
    if indices is None:
        return None
    triangles = []
    try:
        for triangle in indices:
            shape = Part.Face(_make_wire([loops[0][i] for i in triangle]))
            if not shape.isValid() or shape.Area <= 0:
                return None
            triangles.append(shape)
    except Exception:
        return None
    App.Console.PrintWarning(
        f"openskp: triangulated non-planar face {face.id} in "
        f"{definition.name or '(model root)'!r} into {len(triangles)} faces\n"
    )
    return Part.makeCompound(triangles)


_SEW_TOLERANCE_MM = 1e-4  # far below SketchUp's own vertex merge distance (~0.001 in)


def _face_key(face):
    c = face.CenterOfMass
    return (round(c.x, 4), round(c.y, 4), round(c.z, 4), round(face.Area, 4))


def _make_solids(faces, colors, stats):
    """Sews ``faces`` (one definition's own faces, with one RGBA each in
    ``colors``) and turns every closed shell into a solid (#21). Returns
    ``(shapes, colors)``: the solids, then every face no closed shell
    took, as they were, with the colours in the order of the result's
    faces. Sewing rebuilds faces, so each colour follows its face by
    centre of mass and area; a face that matches nothing keeps the default
    colour."""
    if not faces:
        return [], []
    color_by_key = {}
    indices_by_key = {}
    coms = []
    areas = []
    for idx, (face, rgba) in enumerate(zip(faces, colors)):
        key = _face_key(face)
        color_by_key.setdefault(key, rgba)
        indices_by_key.setdefault(key, []).append(idx)
        coms.append(face.CenterOfMass)
        areas.append(face.Area)
    try:
        sewn = Part.makeCompound([f.copy() for f in faces])
        sewn.sewShape(_SEW_TOLERANCE_MM)
    except Exception:
        return faces, colors
    solids = []
    solid_face_colors = []
    used_indices = set()

    def _claim_source_face(sf):
        sf_key = _face_key(sf)
        for cand in indices_by_key.get(sf_key, ()):
            if cand not in used_indices:
                used_indices.add(cand)
                return colors[cand]
        sf_com = sf.CenterOfMass
        sf_area = sf.Area
        tol_pos = max(1e-2, 1e-4 * sf.BoundBox.DiagonalLength)
        best_idx = None
        best_err = float("inf")
        for cand in range(len(faces)):
            if cand in used_indices:
                continue
            tol_area = max(1e-2, 1e-4 * max(sf_area, areas[cand]))
            d_area = abs(sf_area - areas[cand])
            if d_area > tol_area:
                continue
            d_pos = (sf_com - coms[cand]).Length
            if d_pos > tol_pos:
                continue
            err = d_pos + d_area
            if err < best_err:
                best_err = err
                best_idx = cand
        if best_idx is not None:
            used_indices.add(best_idx)
            return colors[best_idx]
        return color_by_key.get(sf_key, _DEFAULT_FACE_RGBA)

    for shell in sewn.Shells:
        if not shell.isClosed():
            continue
        try:
            solid = Part.Solid(shell)
            if solid.Volume < 0:
                solid.reverse()
            if not solid.isValid() or solid.Volume <= 0:
                continue
        except Exception:
            continue
        solids.append(solid)
        solid_face_colors.extend(_claim_source_face(f) for f in solid.Faces)
    # A single self-closed curved face (such as a full Part.Sphere) has no
    # inter-face seam for sewShape() to wrap into sewn.Shells; promote its
    # 1-face closed shell directly.
    for idx, (face, rgba) in enumerate(zip(faces, colors)):
        if idx in used_indices or isinstance(face.Surface, Part.Plane):
            continue
        try:
            sh = Part.Shell([face.copy()])
            if not sh.isClosed():
                continue
            solid = Part.Solid(sh)
            if solid.Volume < 0:
                solid.reverse()
            if solid.isValid() and solid.Volume > 0:
                solids.append(solid)
                solid_face_colors.append(rgba)
                used_indices.add(idx)
        except Exception:
            continue
    if not solids:
        stats["faces_left_open"] = stats.get("faces_left_open", 0) + len(faces)
        return faces, colors
    out_shapes = list(solids)
    out_colors = list(solid_face_colors)
    for idx, (face, rgba) in enumerate(zip(faces, colors)):
        if idx not in used_indices:
            out_shapes.append(face)
            out_colors.append(rgba)
            stats["faces_left_open"] = stats.get("faces_left_open", 0) + 1
    stats["solids_built"] = stats.get("solids_built", 0) + len(solids)
    return out_shapes, out_colors


def _arc_point_mm(center, ax, ay, angle):
    return App.Vector(
        center.x + math.cos(angle) * ax[0] + math.sin(angle) * ay[0],
        center.y + math.cos(angle) * ax[1] + math.sin(angle) * ay[1],
        center.z + math.cos(angle) * ax[2] + math.sin(angle) * ay[2],
    )


def _group_smooth_faces(definition, model, inherited_material_id):
    """Groups faces joined by soft or smooth edges (and sharing the same
    resolved display colour, since FreeCAD stores one DiffuseColor per face)
    into candidate curved-surface components (#18). Returns
    ``(candidate_groups, edge_faces)`` where each entry of ``candidate_groups``
    is ``(faces_list, rgba)`` with ``len(faces_list) >= 2``."""
    faces_by_id = definition.faces
    if len(faces_by_id) < 2:
        return [], {}
    colors_by_id = {
        fid: _resolve_face_color(face, model, inherited_material_id)
        for fid, face in faces_by_id.items()
    }
    edge_faces: dict = {}
    for fid, face in faces_by_id.items():
        for loop in face.loops:
            for eid, _sign in loop:
                fids = edge_faces.setdefault(eid, [])
                if not fids or fids[-1] != fid:
                    fids.append(fid)

    adj: dict = {fid: [] for fid in faces_by_id}
    for eid, fids in edge_faces.items():
        if len(fids) != 2:
            continue
        edge = definition.edges.get(eid)
        if edge is None or not (edge.soft or edge.smooth):
            continue
        f1, f2 = fids
        if f1 != f2 and colors_by_id[f1] == colors_by_id[f2]:
            adj[f1].append(f2)
            adj[f2].append(f1)

    visited = set()
    groups = []
    for fid in faces_by_id:
        if fid in visited or not adj[fid]:
            continue
        comp = []
        stack = [fid]
        visited.add(fid)
        while stack:
            cur = stack.pop()
            comp.append(faces_by_id[cur])
            for nxt in adj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        if len(comp) >= 2:
            groups.append((comp, colors_by_id[fid]))
    return groups, edge_faces


def _fit_cylinder_or_cone(definition, group_faces, curve_edge_counts):
    """Tries to recognise ``group_faces`` (a connected soft/smooth facet group)
    as a cylinder or a cone/frustum from SketchUp's analytic ``Definition.arcs``
    (#18). Returns ``("cylinder" | "cone", face_shape)`` or ``None``."""
    arcs = getattr(definition, "arcs", None)
    if not arcs or not curve_edge_counts:
        return None

    edge_use_count = {}
    face_loops_pts = []
    all_vids = set()
    for face in group_faces:
        if len(face.loops) != 1:
            return None
        pts = _loop_points(definition, face.loops[0])
        if pts is None or len(pts) < 3:
            return None
        face_loops_pts.append(pts)
        for eid, _sign in face.loops[0]:
            edge = definition.edges.get(eid)
            if edge is None:
                return None
            edge_use_count[eid] = edge_use_count.get(eid, 0) + 1
            all_vids.add(edge.v1_id)
            all_vids.add(edge.v2_id)

    if any(cnt > 2 for cnt in edge_use_count.values()):
        return None
    boundary_eids = [eid for eid, cnt in edge_use_count.items() if cnt == 1]
    if not boundary_eids:
        return None

    boundary_by_cid: dict = {}
    non_arc_eids = []
    for eid in boundary_eids:
        cid = definition.edges[eid].curve_id
        if cid is not None and cid in arcs:
            boundary_by_cid.setdefault(cid, []).append(eid)
        else:
            non_arc_eids.append(eid)

    arc_entries = []
    for cid, eids in boundary_by_cid.items():
        if len(eids) != curve_edge_counts.get(cid, 0):
            return None
        arc = arcs[cid]
        params = _circle_arc_params(arc)
        if params is None:
            return None
        arc_entries.append((cid, eids, arc, params))

    if len(arc_entries) == 2:
        (_cid1, _eids1, arc1, p1), (_cid2, _eids2, arc2, p2) = arc_entries
        c1, ax1, ay1, n1, r1 = p1
        c2, ax2, ay2, n2, r2 = p2
        if abs(abs(n1.dot(n2)) - 1.0) > 1e-4:
            return None
        delta = c2 - c1
        height = delta.Length
        r_max = max(r1, r2)
        if height <= 1e-4 or ((delta - n1 * delta.dot(n1)).Length > 1e-3 * r_max):
            return None
        axis_dir = App.Vector(delta.x / height, delta.y / height, delta.z / height)
        if abs(abs(axis_dir.dot(n1)) - 1.0) > 1e-4:
            return None

        if arc1.is_full_circle and arc2.is_full_circle:
            if non_arc_eids:
                return None
            is_full = True
            p1_s = p1_m = p1_e = None
        elif not arc1.is_full_circle and not arc2.is_full_circle:
            def rad_dir(pt):
                rv = (pt - c1) - axis_dir * (pt - c1).dot(axis_dir)
                if rv.Length <= 1e-9:
                    return None
                return App.Vector(rv.x / rv.Length, rv.y / rv.Length, rv.z / rv.Length)

            p1_s = _arc_point_mm(c1, ax1, ay1, arc1.start_angle)
            p1_m = _arc_point_mm(c1, ax1, ay1, 0.5 * (arc1.start_angle + arc1.end_angle))
            p1_e = _arc_point_mm(c1, ax1, ay1, arc1.end_angle)
            p2_s = _arc_point_mm(c2, ax2, ay2, arc2.start_angle)
            p2_m = _arc_point_mm(c2, ax2, ay2, 0.5 * (arc2.start_angle + arc2.end_angle))
            p2_e = _arc_point_mm(c2, ax2, ay2, arc2.end_angle)
            d1_s, d1_m, d1_e = rad_dir(p1_s), rad_dir(p1_m), rad_dir(p1_e)
            d2_s, d2_m, d2_e = rad_dir(p2_s), rad_dir(p2_m), rad_dir(p2_e)
            if None in (d1_s, d1_m, d1_e, d2_s, d2_m, d2_e):
                return None
            if d1_m.dot(d2_m) < 1.0 - 1e-4:
                return None
            if d1_s.dot(d2_s) >= 1.0 - 1e-4 and d1_e.dot(d2_e) >= 1.0 - 1e-4:
                gen_pairs = ((p1_s, p2_s), (p1_e, p2_e))
            elif d1_s.dot(d2_e) >= 1.0 - 1e-4 and d1_e.dot(d2_s) >= 1.0 - 1e-4:
                gen_pairs = ((p1_s, p2_e), (p1_e, p2_s))
            else:
                return None
            if not non_arc_eids:
                return None
            gen_tol = 1e-3 * max(r_max, height)
            for eid in non_arc_eids:
                e = definition.edges[eid]
                for vid in (e.v1_id, e.v2_id):
                    vv = definition.vertices[vid]
                    pt = App.Vector(vv.x * INCH_TO_MM, vv.y * INCH_TO_MM, vv.z * INCH_TO_MM)
                    on_gen = False
                    for ga, gb in gen_pairs:
                        seg = gb - ga
                        if seg.Length <= 1e-9:
                            continue
                        proj = max(0.0, min(1.0, (pt - ga).dot(seg) / (seg.Length ** 2)))
                        if (pt - (ga + seg * proj)).Length <= gen_tol:
                            on_gen = True
                            break
                    if not on_gen:
                        return None
            is_full = False
        else:
            return None
    elif len(arc_entries) == 1 and not non_arc_eids:
        # A pointed cone: one full circular base arc and a single axial apex.
        (_cid1, eids1, arc1, p1) = arc_entries[0]
        if not arc1.is_full_circle:
            return None
        c1, _ax1, _ay1, n1, r1 = p1
        r2 = 0.0
        r_max = r1
        base_vids = {definition.edges[eid].v1_id for eid in eids1} | {
            definition.edges[eid].v2_id for eid in eids1
        }
        off_vids = all_vids - base_vids
        if not off_vids:
            return None
        best_vid = max(
            off_vids,
            key=lambda vid: abs(
                (
                    App.Vector(
                        definition.vertices[vid].x * INCH_TO_MM,
                        definition.vertices[vid].y * INCH_TO_MM,
                        definition.vertices[vid].z * INCH_TO_MM,
                    )
                    - c1
                ).dot(n1)
            ),
        )
        va = definition.vertices[best_vid]
        c2 = App.Vector(va.x * INCH_TO_MM, va.y * INCH_TO_MM, va.z * INCH_TO_MM)
        delta = c2 - c1
        height = delta.Length
        if height <= 1e-4 or ((delta - n1 * delta.dot(n1)).Length > 1e-3 * r1):
            return None
        axis_dir = App.Vector(delta.x / height, delta.y / height, delta.z / height)
        is_full = True
        p1_s = p1_m = p1_e = None
    else:
        return None

    tol_r = 1e-3 * r_max
    tol_z = max(1e-4, 1e-3 * max(height, r_max))
    for vid in all_vids:
        vv = definition.vertices[vid]
        pt = App.Vector(vv.x * INCH_TO_MM, vv.y * INCH_TO_MM, vv.z * INCH_TO_MM)
        t = (pt - c1).dot(axis_dir)
        if t < -tol_z or t > height + tol_z:
            return None
        tc = max(0.0, min(height, t))
        r_exp = r1 + (r2 - r1) * (tc / height)
        rad_vec = (pt - c1) - axis_dir * t
        if abs(rad_vec.Length - r_exp) > tol_r:
            return None

    radial_dot_sum = 0.0
    facet_area_sum = 0.0
    facet_sign = 0
    for pts in face_loops_pts:
        vecs = [App.Vector(*p) for p in pts]
        centroid = sum(vecs, App.Vector()) / len(vecs)
        rel = [v - centroid for v in vecs]
        nf = App.Vector()
        for a, b in zip(rel, rel[1:] + rel[:1]):
            nf += a.cross(b)
        area_2 = nf.Length
        if area_2 <= 1e-12:
            return None
        facet_area_sum += 0.5 * area_2
        tc = max(0.0, min(height, (centroid - c1).dot(axis_dir)))
        r_exp_c = r1 + (r2 - r1) * (tc / height)
        rad_c = (centroid - c1) - axis_dir * (centroid - c1).dot(axis_dir)
        if r_exp_c > 1e-4 and rad_c.Length < 0.45 * r_exp_c:
            return None
        if rad_c.Length > 1e-9:
            dot = nf.dot(rad_c) / rad_c.Length
            s = 1 if dot > 0.0 else -1
            if facet_sign and s != facet_sign:
                return None
            facet_sign = s
            radial_dot_sum += dot

    if facet_sign == 0 or abs(radial_dot_sum) <= 1e-9:
        return None

    try:
        if r2 > 0.0 and abs(r1 - r2) <= min(1e-6 * r_max, 0.5 * _SEW_TOLERANCE_MM):
            kind = "cylinder"
            r = 0.5 * (r1 + r2)
            surf = Part.Cylinder(Part.Circle(c1, axis_dir, r))
            v_max = height
        else:
            kind = "cone"
            surf = Part.Cone(c1, c2, r1, r2)
            v_max = math.hypot(height, r2 - r1)

        if is_full:
            u_min, u_max = 0.0, 2.0 * math.pi
        else:
            two_pi = 2.0 * math.pi
            u_s, _ = surf.parameter(p1_s)
            u_m, _ = surf.parameter(p1_m)
            u_e, _ = surf.parameter(p1_e)
            du_sm = (u_m - u_s) % two_pi
            du_se = (u_e - u_s) % two_pi
            if 0.0 < du_sm < du_se:
                u_min, u_max = u_s, u_s + du_se
            else:
                du_es = (u_s - u_e) % two_pi
                u_min, u_max = u_e, u_e + du_es

        face_shape = surf.toShape(u_min, u_max, 0.0, v_max)
        u_mid = 0.5 * (u_min + u_max)
        v_mid = 0.5 * v_max
        n_built = face_shape.normalAt(u_mid, v_mid)
        pt_mid = surf.value(u_mid, v_mid)
        rad_mid = (pt_mid - c1) - axis_dir * (pt_mid - c1).dot(axis_dir)
        if n_built.dot(rad_mid) * facet_sign < 0.0:
            face_shape.reverse()
        if not face_shape.isValid() or face_shape.Area <= 0.0:
            return None
        if not (0.80 * face_shape.Area <= facet_area_sum <= 1.05 * face_shape.Area):
            return None
        return kind, face_shape
    except Exception:
        return None


def _solve_linear(mat, rhs):
    """Gaussian elimination with partial pivoting on a small dense n x n system;
    returns the solution list or ``None`` when singular."""
    n = len(rhs)
    aug = [list(mat[r]) + [rhs[r]] for r in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) <= 1e-11:
            return None
        if piv != col:
            aug[col], aug[piv] = aug[piv], aug[col]
        inv_diag = 1.0 / aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] *= inv_diag
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor != 0.0:
                for j in range(col, n + 1):
                    aug[r][j] -= factor * aug[col][j]
    return [aug[r][n] for r in range(n)]


def _fit_sphere_center_radius(points_xyz):
    """Fits ``(cx, cy, cz, radius)`` to 3-D points via coordinate-centred
    algebraic least squares followed by geometric Gauss-Newton refinement, or
    returns ``None`` when the point set is coplanar/degenerate."""
    n = len(points_xyz)
    if n < 8:
        return None
    mx = sum(p[0] for p in points_xyz) / n
    my = sum(p[1] for p in points_xyz) / n
    mz = sum(p[2] for p in points_xyz) / n
    scale = max(math.sqrt((p[0] - mx) ** 2 + (p[1] - my) ** 2 + (p[2] - mz) ** 2) for p in points_xyz)
    if scale <= 1e-4:
        return None

    ata = [[0.0] * 4 for _ in range(4)]
    atb = [0.0] * 4
    for x, y, z in points_xyz:
        qx, qy, qz = (x - mx) / scale, (y - my) / scale, (z - mz) / scale
        row = (2.0 * qx, 2.0 * qy, 2.0 * qz, 1.0)
        b = qx * qx + qy * qy + qz * qz
        for i in range(4):
            ri = row[i]
            atb[i] += ri * b
            for j in range(4):
                ata[i][j] += ri * row[j]

    sol = _solve_linear(ata, atb)
    if sol is None:
        return None
    sx, sy, sz, d = sol
    rho_sq = d + sx * sx + sy * sy + sz * sz
    if rho_sq <= 1e-12:
        return None
    cx = mx + scale * sx
    cy = my + scale * sy
    cz = mz + scale * sz
    radius = scale * math.sqrt(rho_sq)

    # Variable-projection Gauss-Newton refinement on geometric radial residuals.
    for _ in range(3):
        dists = []
        dirs = []
        ok = True
        for x, y, z in points_xyz:
            dx, dy, dz = x - cx, y - cy, z - cz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist <= 1e-9:
                ok = False
                break
            dists.append(dist)
            dirs.append((dx / dist, dy / dist, dz / dist))
        if not ok:
            break
        r_mean = sum(dists) / n
        ux = sum(u[0] for u in dirs) / n
        uy = sum(u[1] for u in dirs) / n
        uz = sum(u[2] for u in dirs) / n
        jtj = [[0.0] * 3 for _ in range(3)]
        jtr = [0.0] * 3
        for dist, (dux, duy, duz) in zip(dists, dirs):
            jx, jy, jz = dux - ux, duy - uy, duz - uz
            res = dist - r_mean
            jrow = (jx, jy, jz)
            for i in range(3):
                jtr[i] += jrow[i] * res
                for j in range(3):
                    jtj[i][j] += jrow[i] * jrow[j]
        step = _solve_linear(jtj, jtr)
        if step is None:
            break
        cx += step[0]
        cy += step[1]
        cz += step[2]
        radius = sum(
            math.sqrt((x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2) for x, y, z in points_xyz
        ) / n
        if step[0] ** 2 + step[1] ** 2 + step[2] ** 2 <= 1e-20 * radius * radius:
            break
    return cx, cy, cz, radius


def _fit_sphere(definition, group_faces, curve_edge_counts, edge_faces):
    """Tries to recognise ``group_faces`` as a full sphere, spherical cap/dome,
    or spherical band by algebraic/geometric fitting (#18: SketchUp stores no
    analytic sphere record). Returns a ``Part.Face`` backed by ``Part.Sphere``
    or ``None``."""
    if len(group_faces) < 6:
        return None
    edge_use_count = {}
    face_loops_pts = []
    all_vids = set()
    for face in group_faces:
        if len(face.loops) != 1:
            return None
        pts = _loop_points(definition, face.loops[0])
        if pts is None or len(pts) < 3:
            return None
        face_loops_pts.append(pts)
        for eid, _sign in face.loops[0]:
            edge = definition.edges.get(eid)
            if edge is None:
                return None
            edge_use_count[eid] = edge_use_count.get(eid, 0) + 1
            all_vids.add(edge.v1_id)
            all_vids.add(edge.v2_id)

    if len(all_vids) < 8 or any(cnt > 2 for cnt in edge_use_count.values()):
        return None

    pts_xyz = []
    for vid in sorted(all_vids):
        vv = definition.vertices.get(vid)
        if vv is None:
            return None
        pts_xyz.append((vv.x * INCH_TO_MM, vv.y * INCH_TO_MM, vv.z * INCH_TO_MM))

    est = _fit_sphere_center_radius(pts_xyz)
    if est is None:
        return None
    cx, cy, cz, radius = est
    if radius <= 1e-4:
        return None
    tol_r = 1e-3 * radius
    for x, y, z in pts_xyz:
        if abs(math.sqrt((x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2) - radius) > tol_r:
            return None

    center = App.Vector(cx, cy, cz)
    facet_area_sum = 0.0
    facet_sign = 0
    for pts in face_loops_pts:
        vecs = [App.Vector(*p) for p in pts]
        centroid = sum(vecs, App.Vector()) / len(vecs)
        rel = [v - centroid for v in vecs]
        nf = App.Vector()
        for a, b in zip(rel, rel[1:] + rel[:1]):
            nf += a.cross(b)
        area_2 = nf.Length
        if area_2 <= 1e-12:
            return None
        facet_area_sum += 0.5 * area_2
        rad_c = centroid - center
        if rad_c.Length < 0.45 * radius:
            return None
        cos_ang = nf.dot(rad_c) / (area_2 * rad_c.Length)
        if abs(cos_ang) < 0.65:
            return None
        s = 1 if cos_ang > 0.0 else -1
        if facet_sign and s != facet_sign:
            return None
        facet_sign = s

    if facet_sign == 0:
        return None

    boundary_eids = [eid for eid, cnt in edge_use_count.items() if cnt == 1]
    arcs = getattr(definition, "arcs", None) or {}
    if not boundary_eids:
        xs, ys, zs = zip(*pts_xyz)
        if min(max(cs) - min(cs) for cs in (xs, ys, zs)) < 1.5 * radius:
            return None
        axis_dir = App.Vector(0.0, 0.0, 1.0)
        v_min, v_max = -0.5 * math.pi, 0.5 * math.pi
    else:
        boundary_cids = {definition.edges[eid].curve_id for eid in boundary_eids}
        if (
            len(boundary_cids) == 1
            and None not in boundary_cids
            and curve_edge_counts is not None
        ):
            cid = next(iter(boundary_cids))
            arc = arcs.get(cid)
            if (
                arc is None
                or not arc.is_full_circle
                or len(boundary_eids) != curve_edge_counts.get(cid, 0)
            ):
                return None
            params = _circle_arc_params(arc)
            if params is None:
                return None
            rim_c, _ax, _ay, rim_n, rim_r = params
            h_proj = (rim_c - center).dot(rim_n)
            if ((rim_c - center) - rim_n * h_proj).Length > 2e-3 * radius:
                return None
            if abs(math.hypot(h_proj, rim_r) - radius) > 2e-3 * radius:
                return None
            rel_hs = [(App.Vector(*p) - rim_c).dot(rim_n) for p in pts_xyz]
            if min(rel_hs) >= -2e-3 * radius and max(rel_hs) > 0.05 * radius:
                axis_dir = rim_n
            elif max(rel_hs) <= 2e-3 * radius and min(rel_hs) < -0.05 * radius:
                axis_dir = -rim_n
            else:
                return None
            # Snap (center, radius) onto the exact ArcCurve rim so the cap's
            # Part.Circle edge coincides with the dome boundary within sewShape.
            h_rim = (rim_c - center).dot(axis_dir)
            c_exact = rim_c - axis_dir * h_rim
            r_exact = math.hypot(h_rim, rim_r)
            for x, y, z in pts_xyz:
                if abs(math.sqrt((x - c_exact.x) ** 2 + (y - c_exact.y) ** 2 + (z - c_exact.z) ** 2) - r_exact) > 1e-3 * r_exact:
                    return None
            center, radius = c_exact, r_exact
            v_min = math.asin(max(-1.0, min(1.0, h_rim / radius)))
            v_max = 0.5 * math.pi
        elif (
            len(boundary_cids) == 2
            and None not in boundary_cids
            and curve_edge_counts is not None
        ):
            cids = list(boundary_cids)
            by_c = {
                c: [e for e in boundary_eids if definition.edges[e].curve_id == c]
                for c in cids
            }
            if any(len(by_c[c]) != curve_edge_counts.get(c, 0) for c in cids):
                return None
            a1, a2 = arcs.get(cids[0]), arcs.get(cids[1])
            if a1 is None or a2 is None or not (a1.is_full_circle and a2.is_full_circle):
                return None
            p1, p2 = _circle_arc_params(a1), _circle_arc_params(a2)
            if p1 is None or p2 is None:
                return None
            rc1, _, _, rn1, rr1 = p1
            rc2, _, _, rn2, rr2 = p2
            if abs(abs(rn1.dot(rn2)) - 1.0) > 1e-4:
                return None
            axis_dir = rn1
            if (rc2 - rc1).dot(axis_dir) < 0.0:
                rc1, rc2 = rc2, rc1
                rr1, rr2 = rr2, rr1
            span_h = (rc2 - rc1).dot(axis_dir)
            if span_h <= 1e-3 * radius or ((rc2 - rc1) - axis_dir * span_h).Length > 1e-3 * radius:
                return None
            d1 = (rr1 * rr1 - rr2 * rr2 - span_h * span_h) / (2.0 * span_h)
            d2 = d1 + span_h
            c_exact = 0.5 * ((rc1 - axis_dir * d1) + (rc2 - axis_dir * d2))
            r_exact = 0.5 * (math.hypot(d1, rr1) + math.hypot(d2, rr2))
            if (c_exact - center).Length > 5e-3 * radius or abs(r_exact - radius) > 2e-3 * radius:
                return None
            for x, y, z in pts_xyz:
                if abs(math.sqrt((x - c_exact.x) ** 2 + (y - c_exact.y) ** 2 + (z - c_exact.z) ** 2) - r_exact) > 1e-3 * r_exact:
                    return None
            hs = [(App.Vector(*p) - c_exact).dot(axis_dir) for p in pts_xyz]
            if min(hs) < d1 - 2e-3 * r_exact or max(hs) > d2 + 2e-3 * r_exact:
                return None
            center, radius = c_exact, r_exact
            v_min = math.asin(max(-1.0, min(1.0, d1 / radius)))
            v_max = math.asin(max(-1.0, min(1.0, d2 / radius)))
        else:
            # Open spherical cap without ArcCurve metadata: only fit when no
            # adjacent face shares the straight boundary segments.
            if any(len(edge_faces.get(eid, ())) > 1 for eid in boundary_eids):
                return None
            b_adj: dict = {}
            for eid in boundary_eids:
                e = definition.edges[eid]
                b_adj.setdefault(e.v1_id, []).append(e.v2_id)
                b_adj.setdefault(e.v2_id, []).append(e.v1_id)
            if len(b_adj) < 4 or any(len(nb) != 2 for nb in b_adj.values()):
                return None
            ordered_vids = [next(iter(b_adj))]
            prev_vid = None
            while True:
                cur_vid = ordered_vids[-1]
                nxt_vids = [v for v in b_adj[cur_vid] if v != prev_vid]
                if not nxt_vids:
                    return None
                nxt_vid = nxt_vids[0]
                if nxt_vid == ordered_vids[0]:
                    break
                ordered_vids.append(nxt_vid)
                prev_vid = cur_vid
                if len(ordered_vids) > len(b_adj):
                    return None
            if len(ordered_vids) != len(b_adj):
                return None
            b_vecs = []
            for vid in ordered_vids:
                vv = definition.vertices[vid]
                b_vecs.append(App.Vector(vv.x * INCH_TO_MM, vv.y * INCH_TO_MM, vv.z * INCH_TO_MM))
            b_mean = sum(b_vecs, App.Vector()) / len(b_vecs)
            b_rel = [v - b_mean for v in b_vecs]
            rim_n = App.Vector()
            for a, b in zip(b_rel, b_rel[1:] + b_rel[:1]):
                rim_n += a.cross(b)
            if rim_n.Length <= 1e-9:
                return None
            rim_n.normalize()
            if max(abs(v.dot(rim_n)) for v in b_rel) > 2e-3 * radius:
                return None
            h_rim = (b_mean - center).dot(rim_n)
            hs = [(App.Vector(*p) - center).dot(rim_n) for p in pts_xyz]
            if min(hs) >= h_rim - 2e-3 * radius and max(hs) > h_rim + 0.05 * radius:
                axis_dir = rim_n
                v_min = math.asin(max(-1.0, min(1.0, h_rim / radius)))
                v_max = 0.5 * math.pi
            elif max(hs) <= h_rim + 2e-3 * radius and min(hs) < h_rim - 0.05 * radius:
                axis_dir = -rim_n
                v_min = math.asin(max(-1.0, min(1.0, -h_rim / radius)))
                v_max = 0.5 * math.pi
            else:
                return None

    if v_max - v_min <= 1e-4:
        return None
    try:
        sph = Part.Sphere()
        sph.Center = center
        sph.Radius = radius
        sph.Axis = axis_dir
        face_shape = sph.toShape(0.0, 2.0 * math.pi, v_min, v_max)
        u_mid = math.pi
        v_mid = 0.5 * (v_min + v_max)
        n_built = face_shape.normalAt(u_mid, v_mid)
        rad_mid = sph.value(u_mid, v_mid) - center
        if n_built.dot(rad_mid) * facet_sign < 0.0:
            face_shape.reverse()
        if not face_shape.isValid() or face_shape.Area <= 0.0:
            return None
        if not (0.80 * face_shape.Area <= facet_area_sum <= 1.05 * face_shape.Area):
            return None
        return face_shape
    except Exception:
        return None


def _build_own_geometry(definition, model, stats, inherited_material_id):
    """One definition's OWN faces and loose-edge runs (not its nested
    instances), in its local space: ``(shapes, colors)``, where ``colors``
    holds one RGBA per face in the order the faces appear in ``shapes``
    (wires and arcs add no colour, since DiffuseColor is indexed by
    Shape.Faces). Shared by the merged import (_get_local_shape) and the
    component hierarchy (_HierarchyImport)."""
    import openskp

    fit_curved = bool(stats.get("fit_curved_surfaces"))
    curve_edge_counts = None
    if fit_curved and getattr(definition, "arcs", None):
        curve_edge_counts = {}
        for edge in definition.edges.values():
            if edge.curve_id is not None:
                curve_edge_counts[edge.curve_id] = curve_edge_counts.get(edge.curve_id, 0) + 1

    shapes = []
    colors = []
    fitted_face_ids = set()
    edge_faces = {}
    if fit_curved:
        groups, edge_faces = _group_smooth_faces(definition, model, inherited_material_id)
        for group_faces, group_color in groups:
            fitted = _fit_cylinder_or_cone(definition, group_faces, curve_edge_counts)
            if fitted is None:
                sph_face = _fit_sphere(definition, group_faces, curve_edge_counts, edge_faces)
                if sph_face is not None:
                    fitted = ("sphere", sph_face)
            if fitted is not None:
                kind, surf_face = fitted
                fitted_face_ids.update(f.id for f in group_faces)
                stats["faces_built"] += len(group_faces)
                key = {
                    "cylinder": "cylinders_built",
                    "cone": "cones_built",
                    "sphere": "spheres_built",
                }[kind]
                stats[key] = stats.get(key, 0) + 1
                shapes.append(surf_face)
                colors.append(group_color)
            else:
                stats["curved_groups_unfitted"] = stats.get("curved_groups_unfitted", 0) + 1

    # Keep neighbouring flat faces connected (#18): an arc whose edges also
    # belong to a non-coplanar face that stayed faceted must keep its polygon
    # segments on the cap too, rather than opening a crescent gap along the rim.
    allowed_curve_counts = None
    if curve_edge_counts:
        allowed_curve_counts = dict(curve_edge_counts)
        for cid in list(allowed_curve_counts):
            arc = definition.arcs.get(cid)
            params = _circle_arc_params(arc) if arc is not None else None
            if params is None:
                allowed_curve_counts.pop(cid, None)
                continue
            center, _ax, _ay, normal, radius = params
            plane_tol = max(1e-4, 1e-3 * radius)
            blocked = False
            for eid, edge in definition.edges.items():
                if edge.curve_id != cid:
                    continue
                for fid in edge_faces.get(eid, ()):
                    if fid in fitted_face_ids:
                        continue
                    f_other = definition.faces[fid]
                    for lp in f_other.loops:
                        for feid, _fsign in lp:
                            fe = definition.edges.get(feid)
                            if fe is None:
                                continue
                            for vid in (fe.v1_id, fe.v2_id):
                                vv = definition.vertices.get(vid)
                                if vv is None:
                                    continue
                                pt = App.Vector(vv.x * INCH_TO_MM, vv.y * INCH_TO_MM, vv.z * INCH_TO_MM)
                                if abs((pt - center).dot(normal)) > plane_tol:
                                    blocked = True
                                    break
                            if blocked:
                                break
                        if blocked:
                            break
                    if blocked:
                        break
                if blocked:
                    break
            if blocked:
                allowed_curve_counts.pop(cid, None)

    for face in definition.faces.values():
        if face.id in fitted_face_ids:
            continue
        stats["faces_built"] += 1
        shape = _make_face_shape(definition, face, allowed_curve_counts, stats)
        if shape is not None:
            faces = shape.Faces
            # Flatten only this source face's triangles, preserving component
            # nesting and a face-only leaf compound for downstream CAD tools.
            shapes.extend(faces)
            color = _resolve_face_color(face, model, inherited_material_id)
            colors.extend([color] * len(faces))
            if len(faces) > 1:
                stats["faces_triangulated"] = stats.get("faces_triangulated", 0) + 1
                stats["triangles_built"] = stats.get("triangles_built", 0) + len(faces)
        else:
            stats["faces_skipped"] += 1
            App.Console.PrintWarning(
                f"openskp: skipped face {face.id} in {definition.name or '(model root)'!r}: "
                "could not construct a valid face without changing its boundaries\n"
            )

    # Loose edges (SketchUp's own way of storing drawing/construction
    # geometry: a light-gauge-steel or structural-framing member is
    # routinely drawn this way, not as a solid) - deliberately outside
    # the faces loop above, since a curve-only definition has no faces
    # at all and would otherwise contribute nothing to the import. Runs
    # each become their own Part.Wire in the same compound as the faces -
    # FreeCAD's compounds hold mixed face/wire content natively. No color
    # entry - DiffuseColor is indexed by Shape.Faces, which a wire never
    # contributes to.
    for edge_ids, vertex_ids, closed in openskp.loose_edge_runs(definition):
        stats["edge_runs_built"] += 1
        pts = []
        complete = True
        for vid in vertex_ids:
            v = definition.vertices.get(vid)
            if v is None:
                complete = False
                break
            pts.append((v.x * INCH_TO_MM, v.y * INCH_TO_MM, v.z * INCH_TO_MM))
        arc_edge = _make_arc_edge(definition, edge_ids) if complete else None
        if arc_edge is not None:
            stats["arcs_built"] += 1
            shapes.append(arc_edge)
            continue
        wire = _make_loose_edge_wire(pts, closed) if complete else None
        if wire is not None:
            shapes.append(wire)
        else:
            stats["edge_runs_skipped"] += 1
    if stats.get("make_solids"):
        faces = [s for s in shapes if s.ShapeType == "Face"]
        others = [s for s in shapes if s.ShapeType != "Face"]
        solid_shapes, colors = _make_solids(faces, colors, stats)
        shapes = solid_shapes + others
    return shapes, colors


def _get_local_shape(
    definition, model, stats, shape_cache, visiting, progress=None,
    inherited_material_id=None, inherited_layer="Layer0",
):
    """Returns ``{layer_name: (compound_shape, colors)}`` - this
    definition's own faces/loose-edge runs (all attributed to
    ``inherited_layer``, the layer this whole placement context is on -
    see the module docstring's "Layers" section for why per-FACE layer
    overrides are deliberately out of scope), plus every nested
    instance's own per-layer geometry, transformed into this
    definition's local space and merged in - a definition placed once
    under an unlayered context and once inside a "Studs"-tagged group
    can genuinely split across two different output objects.
    ``colors`` is a list of RGBA tuples matching ``compound_shape.Faces``
    order, same invariant materials already established.

    Cached by (definition identity, inherited_material_id,
    inherited_layer): the SAME definition can legitimately render with
    different fallback colors AND land in different layer buckets
    depending on what an ancestor group/component painted itself with or
    tagged itself as (SketchUp's own paint/tag-inheritance rules) -
    caching on identity alone would silently merge those into whichever
    context was built first, same class of bug already found and fixed
    for build_instanced_scene()'s mesh_resource_for.
    """
    key = (id(definition), inherited_material_id, inherited_layer)
    if key in shape_cache:
        return shape_cache[key]
    if key in visiting:
        return {}  # a real cycle in the instance graph - bail, don't spin
    visiting = visiting | {key}

    buckets: dict = {}  # layer_name -> {"shapes": [...], "colors": [...]}

    def bucket(layer_name):
        return buckets.setdefault(layer_name, {"shapes": [], "colors": []})

    own_shapes, own_colors = _build_own_geometry(definition, model, stats, inherited_material_id)
    if own_shapes:
        b = bucket(inherited_layer)
        b["shapes"].extend(own_shapes)
        b["colors"].extend(own_colors)

    for inst in definition.instances:
        child_def = model.definitions.get(inst.ref_idx)
        if child_def is None:
            continue
        # An instance's own painted material_id/explicit layer overrides
        # whatever this definition itself inherited, for both that
        # child's own unpainted/untagged content AND everything nested
        # further inside it - matching SketchUp's own paint/tag-
        # inheritance rules (see model.py's Instance.material_id and
        # Instance.layer docstrings).
        child_material = inst.material_id if inst.material_id is not None else inherited_material_id
        child_layer = inst.layer if inst.layer else inherited_layer
        child_buckets = _get_local_shape(
            child_def, model, stats, shape_cache, visiting, progress, child_material, child_layer
        )
        if not child_buckets:
            continue
        stats["placements"] += 1
        matrix = _to_freecad_matrix(inst.matrix)
        for layer_name, (child_shape, child_colors) in child_buckets.items():
            placed = child_shape.copy()
            # checkScale=True: without it, Shape.transformShape() silently
            # falls back to OCCT's similarity-only gp_Trsf transform, which
            # cannot represent non-uniform scale and collapses it to the
            # geometric mean of the three axes instead - confirmed directly
            # (a 2x/3x/0.5x placement came out a flat ~1.44x on every axis).
            # checkScale=True makes it detect that case and use the general
            # (BRepBuilderAPI_GTransform) transform instead, which preserves
            # non-uniform scale exactly - same cost for the common case
            # (uniform scale/rotation-only placements, the vast majority),
            # only doing the more general math when a placement actually
            # needs it.
            placed.transformShape(matrix, False, True)
            b = bucket(layer_name)
            b["shapes"].append(placed)
            b["colors"].extend(child_colors)

    result = {}
    for layer_name, b in buckets.items():
        if not b["shapes"]:
            continue
        result[layer_name] = (Part.makeCompound(b["shapes"]), b["colors"])
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


_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/OpenSKP"

# The statistics of the most recent import_skp() call, for tests and scripts:
# under headless freecadcmd there is no ViewObject to read colours back from.
last_import_stats: dict = {}


def _bool_preference(name):
    """A Boolean preference of BaseApp/Preferences/Mod/OpenSKP, False when unset."""
    try:
        return App.ParamGet(_PARAM_PATH).GetBool(name, False)
    except Exception:
        return False


def _merge_on_import_preference():
    """The ``MergeOnImport`` preference (Tools > Edit parameters >
    BaseApp/Preferences/Mod/OpenSKP), False when unset: by default an import
    keeps SketchUp's component hierarchy."""
    try:
        return App.ParamGet(_PARAM_PATH).GetBool("MergeOnImport", False)
    except Exception:
        return False


def _assembly_available():
    """Whether FreeCAD's built-in Assembly module (1.0+) is available and its
    object types are registered."""
    try:
        import AssemblyApp  # noqa: F401

        if App.GuiUp:
            import AssemblyGui  # noqa: F401
        return True
    except Exception:
        return False


def _link_transform(m13):
    """Splits a 13-value instance matrix into what an App::Link can hold:
    ``(placement, scale_vector)``, the placement in millimetres, or ``None``
    when the 3x3 is not a rotation times a per-axis scale (a sheared
    placement, or a degenerate one with a zero axis).

    An App::Link applies its ScaleVector in the linked object's own axes,
    then its Placement, which is ``R @ diag(s)``: each column of the 3x3 is
    one rotated, scaled axis. So the scale factors are the column lengths
    and the rotation is the normalised columns, which must be orthogonal. A
    mirrored placement (negative determinant) puts the reflection into the
    x scale, keeping the rotation a rotation. The scale term at index 12 is
    folded in first, as in _to_freecad_matrix."""
    s = m13[12] if len(m13) > 12 else 1.0
    m = [v * s for v in m13[:12]]
    cols = [[m[j], m[3 + j], m[6 + j]] for j in range(3)]
    lengths = [math.sqrt(sum(c * c for c in col)) for col in cols]
    if min(lengths) <= 1e-12:
        return None
    unit = [[c / length for c in col] for col, length in zip(cols, lengths)]
    for i in range(3):
        for j in range(i + 1, 3):
            if abs(sum(a * b for a, b in zip(unit[i], unit[j]))) > 1e-6:
                return None  # sheared: no rotation and per-axis scale gives this
    a, b, c = unit
    det = (a[0] * (b[1] * c[2] - b[2] * c[1])
           - a[1] * (b[0] * c[2] - b[2] * c[0])
           + a[2] * (b[0] * c[1] - b[1] * c[0]))
    if det < 0:
        lengths[0] = -lengths[0]
        unit[0] = [-v for v in unit[0]]
    rotation = App.Matrix(
        unit[0][0], unit[1][0], unit[2][0], m[9] * INCH_TO_MM,
        unit[0][1], unit[1][1], unit[2][1], m[10] * INCH_TO_MM,
        unit[0][2], unit[1][2], unit[2][2], m[11] * INCH_TO_MM,
        0.0, 0.0, 0.0, 1.0,
    )
    return App.Placement(rotation), App.Vector(*lengths)


def _set_layer_property(obj, layer_name):
    """Records the SketchUp tag an object stands for in its own
    ``SketchUpLayer`` property, which export reads back."""
    if not layer_name:
        return
    if "SketchUpLayer" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "SketchUpLayer", "SketchUp",
                        "The SketchUp tag (layer) of this object; export writes it back")
    obj.SketchUpLayer = layer_name


class _HierarchyImport:
    """Builds the document the way SketchUp organises the model (#5).

    - Every component definition becomes one ``App::Part`` in a hidden
      "SketchUp components" group: its own faces and loose edges as one
      Part::Feature, and an ``App::Link`` for every component or group placed
      inside it. Definitions that are not placed anywhere are included too.
      With ``assemblies=True`` (#22), a definition that contains other
      components or groups becomes a FreeCAD 1.x ``Assembly::AssemblyObject``
      instead (with an empty ``Assembly::JointGroup``, since SketchUp stores
      no joints), while leaf definitions stay ``App::Part``.
    - The model itself is an ``App::Part`` named after the file, with one
      group per SketchUp tag used at its top level; each holds the links of
      the top-level instances on that tag (and the model's own loose
      geometry, which is on Layer0). A tag hidden in SketchUp's Tags panel
      imports hidden.
    - A link carries the instance's placement, including non-uniform scale
      and mirroring (see _link_transform). A sheared placement cannot be a
      link: that instance is imported as a placed copy of the component's
      geometry instead, and counted in the statistics.
    - Paint inheritance: an unpainted face in a component takes the paint of
      the instance that places it. Where instances of one definition pass
      different paints to unpainted faces, each paint gets its own
      definition object, labelled with the material name, because a link
      shows its target's colours as they are.

    A tag on a nested instance is kept in that link's ``SketchUpLayer``
    property, and the link is hidden when the tag is; FreeCAD has no layers
    that cut across the hierarchy the way SketchUp's tags do.
    """

    def __init__(self, doc, model, stats, progress, title, assemblies=False):
        self.doc = doc
        self.model = model
        self.stats = stats
        self.progress = progress
        self.title = title
        self.assemblies = bool(assemblies)
        self.layer_hidden = {layer.name: layer.hidden for layer in model.layers}
        self.parts = {}  # (id(definition), effective inherited material) -> App::Part / AssemblyObject
        self.built_definitions = set()  # id(definition)
        self.paint_memo = {}
        self.merged_cache = {}  # for sheared placements, see _place
        self.components = None

    # -- helpers ---------------------------------------------------------

    def _add(self, type_id, name, label):
        obj = self.doc.addObject(type_id, name)
        obj.Label = label
        return obj

    def _material_name(self, material_id):
        material = self.model.materials_by_id.get(material_id)
        return material.name if material is not None and material.name else f"material {material_id}"

    def _passes_paint_down(self, definition, visiting=frozenset()):
        """Whether anything in this definition's subtree shows its placing
        instance's paint: an unpainted face, or an unpainted instance of a
        definition that does."""
        key = id(definition)
        if key in self.paint_memo:
            return self.paint_memo[key]
        if key in visiting:
            return False
        visiting = visiting | {key}
        result = any(face.material_id is None for face in definition.faces.values())
        if not result:
            for inst in definition.instances:
                child = self.model.definitions.get(inst.ref_idx)
                if child is not None and inst.material_id is None and self._passes_paint_down(child, visiting):
                    result = True
                    break
        self.paint_memo[key] = result
        return result

    def _geometry(self, definition, inherited_material_id, label):
        shapes, colors = _build_own_geometry(definition, self.model, self.stats, inherited_material_id)
        if not shapes:
            return None
        obj = self._add("Part::Feature", "Geometry", label)
        obj.Shape = Part.makeCompound(shapes)
        self._apply_colors(obj, colors)
        return obj

    def _apply_colors(self, obj, colors):
        self.stats["object_face_colors"][obj.Name] = colors
        if App.GuiUp and len(colors) == len(obj.Shape.Faces):
            obj.ViewObject.DiffuseColor = colors

    @staticmethod
    def _hide(obj):
        obj.Visibility = False
        if App.GuiUp:
            obj.ViewObject.Visibility = False

    def _has_child_instances(self, definition):
        return any(self.model.definitions.get(inst.ref_idx) is not None for inst in definition.instances)

    # -- the hierarchy ---------------------------------------------------

    def component(self, definition, inherited_material_id, visiting=frozenset()):
        """The App::Part (or Assembly::AssemblyObject when ``self.assemblies``
        is enabled and ``definition`` contains nested instances) for
        ``definition`` as it looks when placed with ``inherited_material_id``
        passed down, built on first use."""
        material_key = inherited_material_id if self._passes_paint_down(definition) else None
        key = (id(definition), material_key)
        if key in self.parts:
            return self.parts[key]
        if key in visiting:
            return None  # a cycle in the instance graph
        name = definition.name or ("Group" if definition.is_group else "Component")
        label = name if material_key is None else f"{name} ({self._material_name(material_key)})"
        if self.assemblies and self._has_child_instances(definition):
            part = self._add("Assembly::AssemblyObject", "Assembly", label)
            part.Type = "Assembly"
            joints = part.newObject("Assembly::JointGroup", "Joints")
            joints.Label = "Joints"
            self.stats["assemblies"] += 1
        else:
            part = self._add("App::Part", "Component", label)
        self.components.addObject(part)
        self._hide(part)  # shown through its links, not at the origin
        self.parts[key] = part
        self.built_definitions.add(id(definition))
        self.stats["components"] += 1

        geometry = self._geometry(definition, material_key, f"{label} geometry")
        if geometry is not None:
            part.addObject(geometry)
        for inst in definition.instances:
            placed = self._place(inst, material_key, visiting | {key})
            if placed is not None:
                part.addObject(placed)
                if inst.layer and self.layer_hidden.get(inst.layer, False):
                    self._hide(placed)

        if self.progress is not None:
            self.progress.next()
        return part

    def _place(self, inst, inherited_material_id, visiting):
        """An App::Link placing ``inst``, or a placed copy when its matrix is
        sheared; ``None`` when it references no definition."""
        child = self.model.definitions.get(inst.ref_idx)
        if child is None:
            return None
        child_material = inst.material_id if inst.material_id is not None else inherited_material_id
        label = inst.name or child.name or ("Group" if child.is_group else "Component")
        self.stats["placements"] += 1
        split = _link_transform(inst.matrix)
        if split is None:
            obj = self._placed_copy(inst, child, child_material, label)
        else:
            target = self.component(child, child_material, visiting)
            if target is None:
                return None
            obj = self._add("App::Link", "Link", label)
            obj.LinkedObject = target
            placement, scale = split
            obj.Placement = placement
            if any(abs(v - 1.0) > 1e-12 for v in scale):
                obj.ScaleVector = scale
            self.stats["links"] += 1
        _set_layer_property(obj, inst.layer)
        return obj

    def _placed_copy(self, inst, child, child_material, label):
        """A sheared placement: the component's whole geometry (nested
        instances included), transformed and stored as one Part::Feature,
        built the same way as the merged import."""
        scratch = {"faces_built": 0, "faces_skipped": 0, "placements": 0,
                   "edge_runs_built": 0, "edge_runs_skipped": 0, "arcs_built": 0,
                   "make_solids": self.stats.get("make_solids", False),
                   "fit_curved_surfaces": self.stats.get("fit_curved_surfaces", False),
                   "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0,
                   "spheres_built": 0, "curved_groups_unfitted": 0}
        buckets = _get_local_shape(child, self.model, scratch, self.merged_cache, frozenset(), None,
                                   child_material, "Layer0")
        shapes, colors = [], []
        for shape, layer_colors in buckets.values():
            shapes.append(shape)
            colors.extend(layer_colors)
        compound = Part.makeCompound(shapes)
        compound.transformShape(_to_freecad_matrix(inst.matrix), False, True)
        obj = self._add("Part::Feature", "PlacedCopy", f"{label} (sheared copy)")
        obj.Shape = compound
        self._apply_colors(obj, colors)
        self.stats["sheared_copies"] += 1
        App.Console.PrintWarning(
            f"openskp: {label!r} is placed with a sheared transformation, which a link cannot hold; "
            "imported as a placed copy of its geometry\n"
        )
        return obj

    def run(self):
        self.components = self._add("App::DocumentObjectGroup", "SketchUpComponents", "SketchUp components")
        self.components.addProperty(
            "App::PropertyBool", "SketchUpDefinitions", "SketchUp",
            "Holds the component definitions; export reaches them only through their links")
        self.components.SketchUpDefinitions = True
        self._hide(self.components)

        model_part = self._add("App::Part", "SketchUpModel", self.title)
        groups = {}

        def layer_group(layer_name):
            if layer_name not in groups:
                group = self._add("App::DocumentObjectGroup", "Tag", layer_name)
                _set_layer_property(group, layer_name)
                model_part.addObject(group)
                if self.layer_hidden.get(layer_name, False):
                    self._hide(group)
                    self.stats["hidden_layers"] += 1
                groups[layer_name] = group
            return groups[layer_name]

        root = self.model.root
        geometry = self._geometry(root, None, f"{self.title} geometry")
        if geometry is not None:
            layer_group("Layer0").addObject(geometry)
        for inst in root.instances:
            placed = self._place(inst, None, frozenset())
            if placed is None:
                continue
            layer = inst.layer or "Layer0"
            layer_group(layer).addObject(placed)
            if self.layer_hidden.get(layer, False):
                self._hide(placed)

        # Components that are defined in the file but not placed anywhere
        # (#5: "this would also allow recovering any Components that are not
        # placed anywhere"). Groups and images only exist where placed.
        for definition in self.model.definitions.values():
            if id(definition) in self.built_definitions or definition is root:
                continue
            if definition.is_group or getattr(definition, "is_image", False):
                continue
            if not definition.faces and not definition.instances and not getattr(definition, "edges", None):
                continue
            self.component(definition, None)
            self.stats["unplaced_components"] += 1

        self.stats["layers"] = len(groups)
        self.stats["model_part"] = model_part.Name
        self.stats["components_group"] = self.components.Name
        return model_part


def import_skp(filepath, doc=None, merge=None, solids=None, assemblies=None, curved_surfaces=None):
    """Import a .skp file into a FreeCAD document, returning the document.

    Two layouts:

    - **Component hierarchy** (the default, ``merge=False``): one object per
      component definition and a link per placement, the way SketchUp
      organises the model; see _HierarchyImport.
    - **Merged** (``merge=True``): one Part::Feature compound per SketchUp
      tag; see _import_merged. Lighter for a very large model, where
      thousands of definitions would each become an object.

    ``merge=None`` reads the ``MergeOnImport`` preference (Tools > Edit
    parameters, BaseApp/Preferences/Mod/OpenSKP), which is False unless set.

    ``solids=True`` sews each component's own faces and turns every closed
    shell into a solid (#21); faces that close no volume stay faces. In
    either layout. ``solids=None`` reads the ``MakeSolids`` preference
    (False unless set).

    ``assemblies=True`` builds every definition that contains other
    components or groups as a FreeCAD 1.x ``Assembly::AssemblyObject``
    instead of an ``App::Part`` (#22), while leaf parts stay ``App::Part``;
    falls back to ``App::Part`` when FreeCAD's Assembly module is not
    available. ``assemblies=None`` reads the ``ImportAssemblies`` preference
    (False unless set).

    ``curved_surfaces=True`` groups faces joined by soft or smooth edges and
    replaces cylinders and cones/frustums recognised from ``Definition.arcs``
    and fitted spheres/domes with true ``Part.Cylinder`` / ``Part.Cone`` /
    ``Part.Sphere`` faces (leaving non-fitting groups faceted), and imports
    circles and circular arcs bounding flat faces as true FreeCAD circle/arc
    edges (#18). ``curved_surfaces=None`` reads the ``FitCurvedSurfaces``
    preference (False unless set).

    Each face's resolved color/opacity (its own paint, or whatever an
    ancestor group/component painted itself with, or a neutral default)
    is applied as ViewObject.DiffuseColor, one entry per Shape.Faces -
    only when running under the real GUI, since ViewObject doesn't exist
    under headless freecadcmd (the colours are in ``last_import_stats``
    either way).
    """
    import openskp

    global last_import_stats

    if doc is None:
        doc = App.ActiveDocument or App.newDocument("SketchUpImport")
    if merge is None:
        merge = _merge_on_import_preference()
    if solids is None:
        solids = _bool_preference("MakeSolids")
    if assemblies is None:
        assemblies = _bool_preference("ImportAssemblies")
    if curved_surfaces is None:
        curved_surfaces = _bool_preference("FitCurvedSurfaces")

    use_assemblies = False
    if assemblies and not merge:
        if _assembly_available():
            use_assemblies = True
        else:
            App.Console.PrintWarning(
                "openskp: FreeCAD Assembly module is not available; "
                "importing components as App::Part instead\n"
            )

    print(f"openskp: parsing {os.path.basename(filepath)} "
          "(can take a while for a large file - this is real work, not a hang)...")
    t0 = time.time()
    skp = openskp.SkpFile.open(filepath)
    model = skp.parse()
    print(f"openskp: parsed in {time.time() - t0:.1f}s, building geometry...")

    t0 = time.time()
    stats = {
        "faces_built": 0, "faces_skipped": 0, "placements": 0,
        "edge_runs_built": 0, "edge_runs_skipped": 0, "arcs_built": 0,
        "hidden_layers": 0, "merged": bool(merge),
        "make_solids": bool(solids), "solids_built": 0, "faces_left_open": 0,
        "import_assemblies": bool(assemblies), "assemblies": 0,
        "fit_curved_surfaces": bool(curved_surfaces), "face_arcs_built": 0,
        "cylinders_built": 0, "cones_built": 0, "spheres_built": 0,
        "curved_groups_unfitted": 0,
    }
    last_import_stats = stats

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
        if merge:
            built = _import_merged(doc, model, stats, progress)
        else:
            stats.update({"components": 0, "links": 0, "sheared_copies": 0,
                          "unplaced_components": 0, "object_face_colors": {}})
            title = os.path.splitext(os.path.basename(filepath))[0] or "SketchUp model"
            _HierarchyImport(doc, model, stats, progress, title, assemblies=use_assemblies).run()
            built = stats["components"]
    finally:
        if progress is not None:
            progress.stop()

    doc.recompute()
    common = (
        f"({stats['faces_built']} source faces, {stats['faces_skipped']} skipped; "
        f"{stats.get('faces_triangulated', 0)} non-planar faces triangulated into "
        f"{stats.get('triangles_built', 0)} triangles; "
        f"{stats['edge_runs_built']} loose-edge runs, {stats['edge_runs_skipped']} skipped)"
    )
    if solids:
        common += (f"; {stats['solids_built']} solids, {stats['faces_left_open']} faces "
                   "closing no volume left as faces")
    if curved_surfaces:
        common += (
            f"; {stats['cylinders_built']} cylinders, {stats['cones_built']} cones, "
            f"{stats['spheres_built']} spheres, "
            f"{stats['curved_groups_unfitted']} smoothed groups left faceted, "
            f"{stats['face_arcs_built']} circle/arc face edges"
        )
    if merge:
        print(
            f"openskp: {built} unique definitions built in {time.time() - t0:.1f}s {common}, "
            f"{stats['placements']} instances placed; {stats['layers']} layers "
            f"({stats['hidden_layers']} hidden)"
        )
    else:
        comp_summary = (
            f"{stats['components']} components ({stats['assemblies']} assemblies)"
            if assemblies
            else f"{stats['components']} components"
        )
        print(
            f"openskp: {comp_summary} built in {time.time() - t0:.1f}s {common}, "
            f"{stats['links']} links, {stats['sheared_copies']} sheared copies, "
            f"{stats['unplaced_components']} unplaced components; {stats['layers']} tags at the top level "
            f"({stats['hidden_layers']} hidden)"
        )
    return doc


def _import_merged(doc, model, stats, progress):
    """One Part::Feature compound per distinct SketchUp layer/tag actually
    used by the file (walked through the full scene graph, definitions
    resolved and cached, each unique (definition, layer, paint) context
    built exactly once) - not one single object for the whole file, so
    that a layer switched off in SketchUp's own Tags panel can import
    already hidden (ViewObject.Visibility) and toggled independently of
    the rest of the model, the same way FreeCAD's own Std_ToggleVisibility
    already works object-by-object. Object internal Names are sanitized/
    de-duplicated by FreeCAD itself; each object's Label is set to the
    real, human-readable layer name. Returns the number of unique
    definitions built."""
    shape_cache: dict = {}
    root_buckets = _get_local_shape(model.root, model, stats, shape_cache, frozenset(), progress)

    layer_hidden = {layer.name: layer.hidden for layer in model.layers}
    # Kept on stats (not just applied to the ViewObject below) so this is
    # checkable headlessly under freecadcmd/CI, where ViewObject doesn't
    # exist at all - not just "assume the GUI path works."
    stats["layer_face_colors"] = {}
    for layer_name, (shape, colors) in root_buckets.items():
        base_name = "".join(c if c.isalnum() else "_" for c in layer_name) or "Layer"
        obj = doc.addObject("Part::Feature", f"SketchUpImport_{base_name}")
        obj.Label = layer_name
        obj.Shape = shape
        stats["layer_face_colors"][layer_name] = colors
        if App.GuiUp:
            if len(colors) == len(shape.Faces):
                obj.ViewObject.DiffuseColor = colors
            if layer_hidden.get(layer_name, False):
                obj.ViewObject.Visibility = False
                stats["hidden_layers"] += 1
    stats["layers"] = len(root_buckets)
    return len(shape_cache)


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


def _wire_points_inches(wire, placement):
    """A wire's ordered vertex points, in world space (``placement`` is an
    App.Matrix or App.Placement, anything with multVec) and converted from FreeCAD's native millimetres
    back to SketchUp's native inches. No closing/repeated final point -
    openskp's add_face expects the polygon open, matching how
    OrderedVertexes already returns it. Curved edges (circles/arcs) are
    discretised in wire traversal order so circular discs and holes survive
    export."""
    edges = wire.OrderedEdges
    if all(isinstance(e.Curve, Part.Line) for e in edges):
        raw_pts = [v.Point for v in wire.OrderedVertexes]
    else:
        raw_pts = []
        for e in edges:
            if isinstance(e.Curve, Part.Line):
                pts_e = [e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter)]
            else:
                sweep = abs(e.LastParameter - e.FirstParameter) if hasattr(e.Curve, "Radius") else 2 * math.pi
                segs = max(4, round(24 * sweep / (2 * math.pi)))
                pts_e = list(e.discretize(Number=segs + 1))
            if e.Orientation == "Reversed":
                pts_e = pts_e[::-1]
            raw_pts.extend(pts_e[:-1])
    pts = []
    for pt in raw_pts:
        p = placement.multVec(pt)
        pts.append((p.x / INCH_TO_MM, p.y / INCH_TO_MM, p.z / INCH_TO_MM))
    return pts


def _material_key(rgba):
    """Rounds a DiffuseColor RGBA (floats 0-1) to a stable, hashable key
    for deduplication - (r, g, b) as 0-255 ints, alpha as a 0-100 int
    percentage (matching openskp's own opacity convention: 1.0/100 is
    fully opaque)."""
    r, g, b, a = rgba
    return (round(r * 255), round(g * 255), round(b * 255), round(a * 100))


def _material_name(key):
    r, g, b, a100 = key
    name = f"Color_{r:02X}{g:02X}{b:02X}"
    if a100 < 100:
        name += f"_A{a100:02d}"
    return name


def _register_face_materials(obj, builder, material_handle_by_key):
    """Returns a list of material handles, one per obj.Shape.Faces entry
    (all None where no per-face color is available), registering any
    newly-seen distinct color via openskp's builder.add_material() as
    it's first encountered - the same Material reused across many faces
    (or objects) registers exactly once, not once per face.

    Reads obj.ViewObject.DiffuseColor - only meaningful under the real
    GUI (ViewObject doesn't exist under headless freecadcmd, matching
    materials-import's own limitation) and only trusted when its length
    already matches Shape.Faces exactly (the same invariant import
    itself relies on when applying colors - a mismatch means the color
    data doesn't actually describe this exact shape, e.g. it's stale
    from before some edit, so this skips it rather than misapplying
    colors to the wrong faces)."""
    shape = obj.Shape
    if not (App.GuiUp and hasattr(obj, "ViewObject")):
        return [None] * len(shape.Faces)
    try:
        colors = list(obj.ViewObject.DiffuseColor)
    except Exception:
        return [None] * len(shape.Faces)
    if len(colors) != len(shape.Faces):
        return [None] * len(shape.Faces)

    handles = []
    for c in colors:
        key = _material_key(c)
        if key not in material_handle_by_key:
            r, g, b, a100 = key
            opacity = a100 / 100.0
            material_handle_by_key[key] = builder.add_material(
                _material_name(key), (r, g, b), opacity=None if opacity >= 1.0 else opacity
            )
        handles.append(material_handle_by_key[key])
    return handles


def _add_shape_faces(builder, shape, transform, stats, layer, material_handles):
    """Writes every face of ``shape`` with its points mapped through
    ``transform`` (an App.Matrix into world millimetres). A mirroring
    transform reverses each loop so the face keeps facing outwards."""
    mirrored = transform.determinant() < 0
    for i, face in enumerate(shape.Faces):
        material = material_handles[i] if material_handles else None
        if isinstance(face.Surface, (Part.Cylinder, Part.Cone, Part.Sphere, Part.Toroid)):
            try:
                tol = max(0.05, 0.01 * face.BoundBox.DiagonalLength)
                verts_mm, tris = face.tessellate(tol)
            except Exception:
                stats["faces_skipped"] += 1
                continue
            pts_in = []
            for v in verts_mm:
                p = transform.multVec(v)
                pts_in.append((p.x / INCH_TO_MM, p.y / INCH_TO_MM, p.z / INCH_TO_MM))
            for a, b, c in tris:
                tri_pts = [pts_in[a], pts_in[c], pts_in[b]] if mirrored else [pts_in[a], pts_in[b], pts_in[c]]
                try:
                    builder.add_face(tri_pts, layer=layer, material=material)
                    stats["faces_written"] += 1
                except Exception:
                    stats["faces_skipped"] += 1
            continue
        outer = face.OuterWire
        outer_pts = _wire_points_inches(outer, transform)
        if len(outer_pts) < 3:
            stats["faces_skipped"] += 1
            continue
        holes = []
        for w in face.Wires:
            if w.isSame(outer):
                continue
            hole_pts = _wire_points_inches(w, transform)
            if len(hole_pts) >= 3:
                holes.append(hole_pts[::-1] if mirrored else hole_pts)
        if mirrored:
            outer_pts = outer_pts[::-1]
        try:
            builder.add_face(outer_pts, holes=holes, layer=layer, material=material)
            stats["faces_written"] += 1
        except Exception:
            # A degenerate/non-planar/self-intersecting face from
            # arbitrary FreeCAD geometry (e.g. a curved NURBS face
            # flattened badly, or a sliver from a boolean operation)
            # isn't something openskp's writer can represent - skip it
            # rather than aborting the whole export.
            stats["faces_skipped"] += 1


def _is_type(obj, type_id):
    is_derived = getattr(obj, "isDerivedFrom", None)
    return bool(is_derived and is_derived(type_id))


def _parent_of(obj):
    for getter in ("getParentGroup", "getParentGeoFeatureGroup"):
        method = getattr(obj, getter, None)
        if method is not None:
            parent = method()
            if parent is not None:
                return parent
    return None


def _ancestors(obj):
    seen = set()
    parent = _parent_of(obj)
    while parent is not None and parent.Name not in seen:
        seen.add(parent.Name)
        yield parent
        parent = _parent_of(parent)


def _is_part_or_assembly(obj):
    return _is_type(obj, "App::Part") or _is_type(obj, "Assembly::AssemblyObject")


def _is_container(obj):
    return (
        _is_type(obj, "App::Link")
        or _is_part_or_assembly(obj)
        or _is_type(obj, "App::DocumentObjectGroup")
    )


def _can_export(obj):
    shape = getattr(obj, "Shape", None)
    return _is_container(obj) or (shape is not None and not shape.isNull())


def _direct_children(container):
    """A container's own children. An App::Part's Group also lists what sits
    in the plain groups inside it (the tag groups of an import), which are
    reached through those groups instead."""
    for child in container.Group:
        parent = _parent_of(child)
        if parent is None or parent.Name == container.Name:
            yield child


def _is_definitions_group(obj):
    return bool(getattr(obj, "SketchUpDefinitions", False))


_ASSEMBLY_META_GROUPS = (
    "Assembly::JointGroup",
    "Assembly::ViewGroup",
    "Assembly::BomGroup",
    "Assembly::SimulationGroup",
)


def _is_skipped_export_group(obj):
    return _is_definitions_group(obj) or any(_is_type(obj, t) for t in _ASSEMBLY_META_GROUPS)


def _export_leaves(exportList):
    """Yields ``(obj, shape, transform, layer)`` for every piece of geometry
    the selection shows, flattened into world space.

    The selection can be plain shapes (as before) or the hierarchy an import
    builds (#5, #22): App::Part and Assembly::AssemblyObject containers, tag
    groups and App::Link placements, each followed down to the Part::Features
    they show, with the links' placements and scale applied. An object whose
    ancestor is also selected is reached through that ancestor, not twice, and
    the hidden "SketchUp components" group (plus Assembly joint/view/BOM/
    simulation metadata groups) is skipped, since definitions sit at the
    origin and are already exported wherever a link places them.

    ``transform`` maps the shape's own points (which already include the
    object's own Placement) into world millimetres. ``layer`` is the nearest
    ``SketchUpLayer`` on the way down, else the selected object's Label (the
    rule for plain shapes: each object's Label is its tag)."""
    selected = {o.Name for o in exportList if hasattr(o, "Name")}

    def walk(obj, matrix, layer, through_link):
        if _is_skipped_export_group(obj):
            return
        layer = getattr(obj, "SketchUpLayer", "") or layer
        if _is_type(obj, "App::Link"):
            target = obj.LinkedObject
            if target is None:
                return
            link_matrix = obj.Placement.toMatrix()
            scale = getattr(obj, "ScaleVector", None)
            if scale is not None and (scale.x, scale.y, scale.z) != (1.0, 1.0, 1.0):
                s = App.Matrix()
                s.scale(scale)
                link_matrix = link_matrix * s
            if getattr(obj, "LinkTransform", False) and hasattr(target, "Placement"):
                link_matrix = link_matrix * target.Placement.toMatrix()
            yield from walk(target, matrix * link_matrix, layer, True)
            return
        # Only App::Part / Assembly::AssemblyObject are opened up: a PartDesign
        # Body is a container too, but its Shape (the tip) is the solid it stands for.
        if _is_part_or_assembly(obj):
            inner = matrix if through_link else matrix * obj.Placement.toMatrix()
            for child in _direct_children(obj):
                yield from walk(child, inner, layer, False)
            return
        if _is_type(obj, "App::DocumentObjectGroup"):
            for child in _direct_children(obj):
                yield from walk(child, matrix, layer, False)
            return
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            return
        # The shape's points include the object's own Placement; through a
        # link the link's placement replaces it, so take it back out.
        leaf = matrix * obj.Placement.toMatrix().inverse() if through_link else matrix
        yield obj, shape, leaf, layer

    for obj in exportList:
        if any(a.Name in selected or _is_skipped_export_group(a) for a in _ancestors(obj)):
            continue
        parent_group = getattr(obj, "getParentGeoFeatureGroup", lambda: None)()
        base = parent_group.getGlobalPlacement().toMatrix() if parent_group is not None else App.Matrix()
        yield from walk(obj, base, getattr(obj, "Label", ""), False)


def export(exportList, filename):
    """Called by FreeCAD when exporting to .skp (File > Export).

    Everything the selection shows (see _export_leaves: plain shapes, and
    the parts, tag groups and links of an imported hierarchy) is flattened
    into a flat set of faces at the root of a new .skp file, each in its
    world position. No component/group structure is written (yet), so an
    imported hierarchy exports as its geometry.

    Each object's own Label becomes its SketchUp layer/tag - unlike
    Blender's exporter (which has to fall back to Collection membership,
    since one Blender object has no per-object "layer" slot), this is an
    exact match for what the merged import produces: one object per
    layer, Label set to the real layer name - so re-exporting an
    unmodified, just-imported set of layer objects reproduces the same
    tags exactly, not an approximation. In an imported hierarchy the tags
    come from the ``SketchUpLayer`` property the import set on tag groups
    and links. For freshly modelled FreeCAD content that was never
    imported, this just means each object's own Label doubles as its
    exported tag - a predictable, if occasionally literal (a "Box" exports
    to a layer named "Box"), rule rather than trying to guess which
    Labels look "meaningful."

    Each object's own ViewObject.DiffuseColor (one RGBA entry per
    Shape.Faces, the same property materials-import applies) becomes a
    SketchUp material per distinct color - solid colors and opacity
    only, matching import's own scope; no texture export. Only available
    under the real GUI (ViewObject doesn't exist under headless
    freecadcmd, so a headless export carries geometry/layers but no
    colors - matching how headless import can't apply colors either). An
    object with no per-face color data (or running headless) exports
    unpainted, same as before this existed.
    """
    from openskp import create

    builder = create()
    leaves = list(_export_leaves(exportList))

    # openskp's writer requires materials, then layers, then faces, in
    # that order - add_material must precede add_layer (both depend on
    # the final material count for their own slot numbering), and both
    # must precede the first add_face call. So materials are resolved
    # and registered first here, then layers - two dedup passes over
    # the leaves, not two export passes, and the same ordering the
    # Blender exporter's own export_skp() uses.
    material_handle_by_key = {}
    object_material_handles = {}
    for obj, _shape, _transform, _layer in leaves:
        if id(obj) not in object_material_handles:
            object_material_handles[id(obj)] = _register_face_materials(obj, builder, material_handle_by_key)

    layer_handle_by_label = {}
    for _obj, _shape, _transform, layer in leaves:
        if layer and layer not in layer_handle_by_label:
            layer_handle_by_label[layer] = builder.add_layer(layer)

    stats = {
        "faces_written": 0, "faces_skipped": 0, "objects_skipped": 0,
        "materials_written": len(material_handle_by_key), "layers_written": len(layer_handle_by_label),
    }
    stats["objects_skipped"] = sum(1 for o in exportList if not _can_export(o))

    for obj, shape, transform, layer in leaves:
        _add_shape_faces(builder, shape, transform, stats, layer_handle_by_label.get(layer),
                         object_material_handles.get(id(obj)))

    with _real_open(filename, "wb") as f:
        f.write(builder.to_bytes())

    print(
        f"openskp export: {stats['faces_written']} faces written, "
        f"{stats['faces_skipped']} skipped, {stats['objects_skipped']} objects skipped, "
        f"{stats['materials_written']} materials, {stats['layers_written']} layers"
    )
