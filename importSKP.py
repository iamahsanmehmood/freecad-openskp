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
and a radius, instead of the polyline (see _make_arc_edge). Faces are not
changed: SketchUp has no curved surfaces, so a disc is still a flat face with
a polygon boundary, and an arc that bounds a face stays part of it.

Layers (SketchUp calls them "tags"): unlike materials, which fit onto
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

    cx, cy, cz = (c * INCH_TO_MM for c in arc.center)
    ax = [c * INCH_TO_MM for c in arc.x_axis]
    ay = [c * INCH_TO_MM for c in arc.y_axis]
    radius = math.sqrt(sum(c * c for c in ax))
    if radius <= 0.0:
        return None
    dot = sum(a * b for a, b in zip(ax, ay))
    if abs(math.sqrt(sum(c * c for c in ay)) - radius) > 1e-6 * radius or abs(dot) > 1e-6 * radius * radius:
        return None  # an ellipse (a non-uniformly scaled arc), not a circle

    def point(t):
        return App.Vector(
            cx + math.cos(t) * ax[0] + math.sin(t) * ay[0],
            cy + math.cos(t) * ax[1] + math.sin(t) * ay[1],
            cz + math.cos(t) * ax[2] + math.sin(t) * ay[2],
        )

    try:
        if arc.is_full_circle:
            normal = App.Vector(
                ax[1] * ay[2] - ax[2] * ay[1],
                ax[2] * ay[0] - ax[0] * ay[2],
                ax[0] * ay[1] - ax[1] * ay[0],
            )
            if normal.Length == 0.0:
                return None
            return Part.Circle(App.Vector(cx, cy, cz), normal, radius).toShape()
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


def _make_face_shape(definition, face):
    """Builds one face's shape in the definition's own LOCAL space
    (no world transform applied - that happens once per placement,
    cheaply, via _to_freecad_matrix + transformShape). Returns one Face,
    a compound of planar triangles for a warped simple polygon, or None.
    """
    wires = []
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
    if not wires:
        return None
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


def _build_own_geometry(definition, model, stats, inherited_material_id):
    """One definition's OWN faces and loose-edge runs (not its nested
    instances), in its local space: ``(shapes, colors)``, where ``colors``
    holds one RGBA per face in the order the faces appear in ``shapes``
    (wires and arcs add no colour, since DiffuseColor is indexed by
    Shape.Faces). Shared by the merged import (_get_local_shape) and the
    component hierarchy (_HierarchyImport)."""
    import openskp

    shapes = []
    colors = []
    for face in definition.faces.values():
        stats["faces_built"] += 1
        shape = _make_face_shape(definition, face)
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


def _merge_on_import_preference():
    """The ``MergeOnImport`` preference (Tools > Edit parameters >
    BaseApp/Preferences/Mod/OpenSKP), False when unset: by default an import
    keeps SketchUp's component hierarchy."""
    try:
        return App.ParamGet(_PARAM_PATH).GetBool("MergeOnImport", False)
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

    def __init__(self, doc, model, stats, progress, title):
        self.doc = doc
        self.model = model
        self.stats = stats
        self.progress = progress
        self.title = title
        self.layer_hidden = {layer.name: layer.hidden for layer in model.layers}
        self.parts = {}  # (id(definition), effective inherited material) -> App::Part
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

    # -- the hierarchy ---------------------------------------------------

    def component(self, definition, inherited_material_id, visiting=frozenset()):
        """The App::Part for ``definition`` as it looks when placed with
        ``inherited_material_id`` passed down, built on first use."""
        material_key = inherited_material_id if self._passes_paint_down(definition) else None
        key = (id(definition), material_key)
        if key in self.parts:
            return self.parts[key]
        if key in visiting:
            return None  # a cycle in the instance graph
        name = definition.name or ("Group" if definition.is_group else "Component")
        label = name if material_key is None else f"{name} ({self._material_name(material_key)})"
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
                   "edge_runs_built": 0, "edge_runs_skipped": 0, "arcs_built": 0}
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


def import_skp(filepath, doc=None, merge=None):
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
            _HierarchyImport(doc, model, stats, progress, title).run()
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
    if merge:
        print(
            f"openskp: {built} unique definitions built in {time.time() - t0:.1f}s {common}, "
            f"{stats['placements']} instances placed; {stats['layers']} layers "
            f"({stats['hidden_layers']} hidden)"
        )
    else:
        print(
            f"openskp: {stats['components']} components built in {time.time() - t0:.1f}s {common}, "
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
    OrderedVertexes already returns it."""
    pts = []
    for v in wire.OrderedVertexes:
        p = placement.multVec(v.Point)
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
        material = material_handles[i] if material_handles else None
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


def _is_container(obj):
    return _is_type(obj, "App::Link") or _is_type(obj, "App::Part") or _is_type(obj, "App::DocumentObjectGroup")


def _can_export(obj):
    shape = getattr(obj, "Shape", None)
    return _is_container(obj) or (shape is not None and not shape.isNull())


def _is_definitions_group(obj):
    return bool(getattr(obj, "SketchUpDefinitions", False))


def _export_leaves(exportList):
    """Yields ``(obj, shape, transform, layer)`` for every piece of geometry
    the selection shows, flattened into world space.

    The selection can be plain shapes (as before) or the hierarchy an import
    builds (#5): App::Part containers, tag groups and App::Link placements,
    each followed down to the Part::Features they show, with the links'
    placements and scale applied. An object whose ancestor is also selected
    is reached through that ancestor, not twice, and the hidden "SketchUp
    components" group is skipped, since its definitions sit at the origin
    and are already exported wherever a link places them.

    ``transform`` maps the shape's own points (which already include the
    object's own Placement) into world millimetres. ``layer`` is the nearest
    ``SketchUpLayer`` on the way down, else the selected object's Label (the
    rule for plain shapes: each object's Label is its tag)."""
    selected = {o.Name for o in exportList if hasattr(o, "Name")}

    def walk(obj, matrix, layer, through_link):
        if _is_definitions_group(obj):
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
        # Only App::Part is opened up: a PartDesign Body is a container too,
        # but its Shape (the tip) is the solid it stands for.
        if _is_type(obj, "App::Part"):
            inner = matrix if through_link else matrix * obj.Placement.toMatrix()
            for child in obj.Group:
                yield from walk(child, inner, layer, False)
            return
        if _is_type(obj, "App::DocumentObjectGroup"):
            for child in obj.Group:
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
        if any(a.Name in selected or _is_definitions_group(a) for a in _ancestors(obj)):
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
