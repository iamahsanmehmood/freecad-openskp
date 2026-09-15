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
    import openskp

    key = (id(definition), inherited_material_id, inherited_layer)
    if key in shape_cache:
        return shape_cache[key]
    if key in visiting:
        return {}  # a real cycle in the instance graph - bail, don't spin
    visiting = visiting | {key}

    buckets: dict = {}  # layer_name -> {"shapes": [...], "colors": [...]}

    def bucket(layer_name):
        return buckets.setdefault(layer_name, {"shapes": [], "colors": []})

    for face in definition.faces.values():
        stats["faces_built"] += 1
        shape = _make_face_shape(definition, face)
        if shape is not None:
            b = bucket(inherited_layer)
            b["shapes"].append(shape)
            b["colors"].append(_resolve_face_color(face, model, inherited_material_id))
        else:
            stats["faces_skipped"] += 1

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
        wire = _make_loose_edge_wire(pts, closed) if complete else None
        if wire is not None:
            bucket(inherited_layer)["shapes"].append(wire)
        else:
            stats["edge_runs_skipped"] += 1

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


def import_skp(filepath, doc=None):
    """Import a .skp file into a FreeCAD document, returning the document.

    One Part::Feature compound per distinct SketchUp layer/tag actually
    used by the file (walked through the full scene graph, definitions
    resolved and cached, each unique (definition, layer, paint) context
    built exactly once) - not one single object for the whole file, so
    that a layer switched off in SketchUp's own Tags panel can import
    already hidden (ViewObject.Visibility) and toggled independently of
    the rest of the model, the same way FreeCAD's own Std_ToggleVisibility
    already works object-by-object. Object internal Names are sanitized/
    de-duplicated by FreeCAD itself; each object's Label is set to the
    real, human-readable layer name.

    Each face's resolved color/opacity (its own paint, or whatever an
    ancestor group/component painted itself with, or a neutral default)
    is applied as ViewObject.DiffuseColor, one entry per Shape.Faces -
    only when running under the real GUI, since ViewObject doesn't exist
    under headless freecadcmd. Materials are import-only (not written
    back out on export); layer export is not yet supported either.
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
    stats = {
        "faces_built": 0, "faces_skipped": 0, "placements": 0,
        "edge_runs_built": 0, "edge_runs_skipped": 0,
    }
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
        root_buckets = _get_local_shape(model.root, model, stats, shape_cache, frozenset(), progress)
    finally:
        if progress is not None:
            progress.stop()

    layer_hidden = {layer.name: layer.hidden for layer in model.layers}
    # Kept on stats (not just applied to the ViewObject below) so this is
    # checkable headlessly under freecadcmd/CI, where ViewObject doesn't
    # exist at all - not just "assume the GUI path works."
    stats["layer_face_colors"] = {}
    stats["hidden_layers"] = 0
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

    doc.recompute()
    print(
        f"openskp: {len(shape_cache)} unique definitions built in {time.time() - t0:.1f}s "
        f"({stats['faces_built']} faces, {stats['faces_skipped']} skipped; "
        f"{stats['edge_runs_built']} loose-edge runs, {stats['edge_runs_skipped']} skipped), "
        f"{stats['placements']} instances placed; {len(root_buckets)} layers "
        f"({stats['hidden_layers']} hidden)"
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


def _wire_points_inches(wire, placement):
    """A wire's ordered vertex points, in world space (via the object's
    global placement) and converted from FreeCAD's native millimetres
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


def _add_shape_faces(builder, shape, placement, stats, layer, material_handles):
    for i, face in enumerate(shape.Faces):
        outer = face.OuterWire
        outer_pts = _wire_points_inches(outer, placement)
        if len(outer_pts) < 3:
            stats["faces_skipped"] += 1
            continue
        holes = []
        for w in face.Wires:
            if w.isSame(outer):
                continue
            hole_pts = _wire_points_inches(w, placement)
            if len(hole_pts) >= 3:
                holes.append(hole_pts)
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


def export(exportList, filename):
    """Called by FreeCAD when exporting to .skp (File > Export).

    Every Part::Feature (and anything else exposing a real .Shape) in
    exportList is flattened into a flat set of faces at the root of a
    new .skp file - global placement resolved so nested/linked objects
    land in the right position. No component/group structure is
    reconstructed (there's no reliable way to infer "this should be one
    reusable component" purely from arbitrary FreeCAD geometry).

    Each object's own Label becomes its SketchUp layer/tag - unlike
    Blender's exporter (which has to fall back to Collection membership,
    since one Blender object has no per-object "layer" slot), this is an
    exact match for what layers-import already produces: one object per
    layer, Label set to the real layer name (see this module's own
    "Layers" docstring section above) - so re-exporting an unmodified,
    just-imported set of layer objects reproduces the same tags exactly,
    not an approximation. For freshly modelled FreeCAD content that was
    never imported, this just means each object's own Label doubles as
    its exported tag - a predictable, if occasionally literal (a "Box"
    exports to a layer named "Box"), rule rather than trying to guess
    which Labels look "meaningful."

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

    # openskp's writer requires materials, then layers, then faces, in
    # that order - add_material must precede add_layer (both depend on
    # the final material count for their own slot numbering), and both
    # must precede the first add_face call. So materials are resolved
    # and registered first here, then layers - two dedup passes over
    # exportList, not two export passes, and the same ordering the
    # Blender exporter's own export_skp() uses.
    material_handle_by_key = {}
    object_material_handles = {}
    for obj in exportList:
        if not hasattr(obj, "Shape") or obj.Shape is None or obj.Shape.isNull():
            continue
        object_material_handles[id(obj)] = _register_face_materials(obj, builder, material_handle_by_key)

    layer_handle_by_label = {}
    for obj in exportList:
        if not hasattr(obj, "Shape") or obj.Shape is None or obj.Shape.isNull():
            continue
        label = getattr(obj, "Label", None)
        if label and label not in layer_handle_by_label:
            layer_handle_by_label[label] = builder.add_layer(label)

    stats = {
        "faces_written": 0, "faces_skipped": 0, "objects_skipped": 0,
        "materials_written": len(material_handle_by_key), "layers_written": len(layer_handle_by_label),
    }

    for obj in exportList:
        if not hasattr(obj, "Shape") or obj.Shape is None or obj.Shape.isNull():
            stats["objects_skipped"] += 1
            continue
        placement = obj.getGlobalPlacement() if hasattr(obj, "getGlobalPlacement") else obj.Placement
        layer = layer_handle_by_label.get(getattr(obj, "Label", None))
        material_handles = object_material_handles.get(id(obj))
        _add_shape_faces(builder, obj.Shape, placement, stats, layer, material_handles)

    with _real_open(filename, "wb") as f:
        f.write(builder.to_bytes())

    print(
        f"openskp export: {stats['faces_written']} faces written, "
        f"{stats['faces_skipped']} skipped, {stats['objects_skipped']} objects skipped, "
        f"{stats['materials_written']} materials, {stats['layers_written']} layers"
    )
