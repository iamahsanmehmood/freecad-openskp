"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_curved_surfaces.py

Part 1 of FitCurvedSurfaces (#18): analytic circles and circular arcs in
Definition.arcs that bound flat faces (discs, circular holes in plates,
half-discs) import as true FreeCAD Part.Circle / Part.Arc boundary edges when
curved_surfaces=True (or BaseApp/Preferences/Mod/OpenSKP FitCurvedSurfaces is
true), while staying polygonal by default. Ellipses (non-uniformly scaled
arcs) stay polygonal, and export discretises curved face wires cleanly.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402  - freecadcmd exits 0 on a failing script; see tests/_harness.py

try:
    import importSKP  # noqa: E402

    import FreeCAD as App  # noqa: E402
    import Part  # noqa: E402
except BaseException:
    _harness.die()

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MM = importSKP.INCH_TO_MM


def _world_shapes(doc, stats, merge):
    if merge:
        return [o.Shape.copy() for o in doc.Objects if hasattr(o, "Shape")]
    model_part = doc.getObject(stats["model_part"])
    out = []
    for _obj, shape, transform, _layer in importSKP._export_leaves([model_part]):
        placed = shape.copy()
        placed.transformShape(transform, False, True)
        out.append(placed)
    return out


def _signature(shapes):
    faces = [f for s in shapes for f in s.Faces]
    edges = [e for s in shapes for e in s.Edges]
    points = sorted({(round(v.X, 3), round(v.Y, 3), round(v.Z, 3)) for s in shapes for v in s.Vertexes})
    return (len(faces), round(sum(f.Area for f in faces), 3),
            points, len(edges), round(sum(e.Length for e in edges), 3))


def check_arcs_fixture(fixture_name):
    """In arcs_2017.skp and arcs_2021.skp:
    - default (curved_surfaces=False): disc has 24 straight edges, half-disc has 13.
    - curved_surfaces=True: disc has 1 true Part.Circle edge (r = 5 in), half-disc
      has 1 Part.Arc edge (r = 8 in, 180 deg) + 1 straight chord edge, with exact
      circle/semicircle areas."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True)
        stats = importSKP.last_import_stats
        assert stats["fit_curved_surfaces"] is False and stats["face_arcs_built"] == 0, stats
        faces = [f for o in doc.Objects if hasattr(o, "Shape") for f in o.Shape.Faces]
        assert sorted(len(f.Edges) for f in faces) == [13, 24], [len(f.Edges) for f in faces]
    finally:
        App.closeDocument(doc.Name)

    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["fit_curved_surfaces"] is True and stats["face_arcs_built"] == 2, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = sorted([f for s in shapes for f in s.Faces], key=lambda f: f.CenterOfMass.x)
            assert len(faces) == 2, len(faces)

            disc, half_disc = faces
            assert disc.isValid() and len(disc.Wires) == 1 and len(disc.Edges) == 1, len(disc.Edges)
            disc_curve = disc.Edges[0].Curve
            assert hasattr(disc_curve, "Radius"), type(disc_curve).__name__
            assert abs(disc_curve.Radius - 5.0 * MM) < 1e-5, disc_curve.Radius
            assert abs(disc_curve.Center.x - 50.0 * MM) < 1e-5, tuple(disc_curve.Center)
            assert abs(disc.Area - math.pi * (5.0 * MM) ** 2) < 1e-2, disc.Area

            assert half_disc.isValid() and len(half_disc.Wires) == 1 and len(half_disc.Edges) == 2, len(half_disc.Edges)
            curved_edges = [e for e in half_disc.Edges if hasattr(e.Curve, "Radius")]
            line_edges = [e for e in half_disc.Edges if isinstance(e.Curve, Part.Line)]
            assert len(curved_edges) == 1 and len(line_edges) == 1, (len(curved_edges), len(line_edges))
            assert abs(curved_edges[0].Curve.Radius - 8.0 * MM) < 1e-5, curved_edges[0].Curve.Radius
            assert abs(curved_edges[0].Length - math.pi * 8.0 * MM) < 1e-3, curved_edges[0].Length
            assert abs(line_edges[0].Length - 16.0 * MM) < 1e-3, line_edges[0].Length
            assert abs(half_disc.Area - 0.5 * math.pi * (8.0 * MM) ** 2) < 1e-2, half_disc.Area
        finally:
            App.closeDocument(doc.Name)
    print(f"check_arcs_fixture({fixture_name}): OK (disc = 1 circle edge, half-disc = 1 arc + 1 chord)")


def check_plate_with_hole(fixture_name):
    """In plate_with_hole_2017.skp and plate_with_hole_2021.skp (20 x 20 x 2 in
    plate with an r = 4 in circular through-hole):
    - default: top and bottom perforated faces have 4 outer + 24 inner line edges.
    - curved_surfaces=True: top and bottom perforated faces each have a 4-line
      outer wire and a 1-edge Part.Circle inner hole wire of radius 101.6 mm,
      exact subtracted area (400 - 16*pi) in2, and outward +/-Z normals."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True)
        faces = [f for o in doc.Objects if hasattr(o, "Shape") for f in o.Shape.Faces]
        holed = [f for f in faces if len(f.Wires) == 2]
        assert len(faces) == 30 and len(holed) == 2, (len(faces), len(holed))
        assert all(len(f.Edges) == 28 for f in holed), [len(f.Edges) for f in holed]
    finally:
        App.closeDocument(doc.Name)

    expected_area = (20.0 * 20.0 - math.pi * 4.0 ** 2) * MM ** 2
    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["face_arcs_built"] == 2, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = [f for s in shapes for f in s.Faces]
            assert len(faces) == 30 and all(f.isValid() for f in faces), len(faces)

            holed = sorted([f for f in faces if len(f.Wires) == 2], key=lambda f: f.CenterOfMass.z)
            assert len(holed) == 2, len(holed)
            for face, z_in, nz in zip(holed, (0.0, 2.0), (-1.0, 1.0)):
                outer = face.OuterWire
                inner = [w for w in face.Wires if not w.isSame(outer)][0]
                assert len(outer.Edges) == 4, len(outer.Edges)
                assert all(isinstance(e.Curve, Part.Line) for e in outer.Edges)
                assert len(inner.Edges) == 1, len(inner.Edges)
                circle = inner.Edges[0].Curve
                assert hasattr(circle, "Radius"), type(circle).__name__
                assert abs(circle.Radius - 4.0 * MM) < 1e-5, circle.Radius
                assert abs(circle.Center.x) < 1e-5 and abs(circle.Center.y) < 1e-5, tuple(circle.Center)
                assert abs(circle.Center.z - z_in * MM) < 1e-5, circle.Center.z
                assert abs(face.Area - expected_area) < 1e-2, (face.Area, expected_area)
                u0, u1, v0, v1 = face.ParameterRange
                normal = face.normalAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
                assert abs(normal.x) < 1e-6 and abs(normal.y) < 1e-6 and abs(normal.z - nz) < 1e-6, tuple(normal)
        finally:
            App.closeDocument(doc.Name)
    print(f"check_plate_with_hole({fixture_name}): OK (top & bottom holes are true Part.Circle wires, exact area)")


def check_ellipse_stays_polygon():
    """A non-uniformly scaled ArcCurve (|x_axis| != |y_axis|) is an ellipse, not
    a circle: its face boundary stays polygonal."""
    import openskp

    n = 24
    rx, ry = 10.0, 5.0
    verts = {
        i: openskp.Vertex(id=i, x=rx * math.cos(2 * math.pi * i / n), y=ry * math.sin(2 * math.pi * i / n), z=0.0)
        for i in range(n)
    }
    edges = {
        i: openskp.Edge(id=i, v1_id=i, v2_id=(i + 1) % n, curve_id=1)
        for i in range(n)
    }
    loop = [(i, 1) for i in range(n)]
    defn = openskp.Definition(
        id=0,
        name="EllipseDef",
        vertices=verts,
        edges=edges,
        faces={1: openskp.Face(id=1, loops=[loop], normal=(0.0, 0.0, 1.0))},
        arcs={
            1: openskp.ArcCurve(
                center=(0.0, 0.0, 0.0),
                normal=(0.0, 0.0, 1.0),
                x_axis=(rx, 0.0, 0.0),
                y_axis=(0.0, ry, 0.0),
                start_angle=0.0,
                end_angle=2.0 * math.pi,
            )
        },
    )
    model = openskp.SkpModel(root=defn)
    stats = {
        "faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0,
        "edge_runs_skipped": 0, "arcs_built": 0,
        "fit_curved_surfaces": True, "face_arcs_built": 0,
    }
    shapes, _colors = importSKP._build_own_geometry(defn, model, stats, None)
    assert len(shapes) == 1 and stats["face_arcs_built"] == 0, stats
    assert len(shapes[0].Edges) == n and all(isinstance(e.Curve, Part.Line) for e in shapes[0].Edges)
    print("check_ellipse_stays_polygon: OK (ellipse stays 24-sided polygon)")


def check_export_round_trip():
    """Exporting faces with true Part.Circle / Part.Arc wires discretises them
    cleanly instead of dropping discs or circular holes."""
    import openskp

    for fixture_name, want_faces, want_holed in (
        ("arcs_2021.skp", 2, 0),
        ("plate_with_hole_2021.skp", 30, 2),
    ):
        doc = App.newDocument()
        out_path = os.path.join(FIXTURES_DIR, f"_curved_export_{fixture_name}")
        try:
            importSKP.import_skp(os.path.join(FIXTURES_DIR, fixture_name), doc, merge=False, curved_surfaces=True)
            importSKP.export(list(doc.Objects), out_path)
            model = openskp.SkpFile.open(out_path).parse()
            assert len(model.root.faces) == want_faces, (fixture_name, len(model.root.faces))
            holed = sum(1 for f in model.root.faces.values() if len(f.loops) == 2)
            assert holed == want_holed, (fixture_name, holed)
        finally:
            App.closeDocument(doc.Name)
            if os.path.exists(out_path):
                os.remove(out_path)
    print("check_export_round_trip: OK (discs, half-discs and circular holes round-trip through export)")


def check_preference():
    path = os.path.join(FIXTURES_DIR, "plate_with_hole_2021.skp")
    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for pref, arg, want_flag, want_arcs in (
            (None, None, False, 0),
            (True, None, True, 2),
            (False, None, False, 0),
            (True, False, False, 0),
            (False, True, True, 2),
        ):
            if pref is None:
                params.RemBool("FitCurvedSurfaces")
            else:
                params.SetBool("FitCurvedSurfaces", pref)
            doc = App.newDocument()
            try:
                importSKP.import_skp(path, doc, merge=True, curved_surfaces=arg)
                stats = importSKP.last_import_stats
                assert stats["fit_curved_surfaces"] is want_flag, (pref, arg, stats)
                assert stats["face_arcs_built"] == want_arcs, (pref, arg, stats)
            finally:
                App.closeDocument(doc.Name)
        print("check_preference: OK (FitCurvedSurfaces unset -> False, True -> 2 circle wires, arg overrides)")
    finally:
        params.RemBool("FitCurvedSurfaces")


def check_hierarchy_matches_merged(path):
    """With curved_surfaces=True, hierarchy and merged layouts produce identical geometry."""
    doc_m = App.newDocument()
    try:
        importSKP.import_skp(path, doc_m, merge=True, curved_surfaces=True)
        sig_m = _signature(_world_shapes(doc_m, importSKP.last_import_stats, True))
    finally:
        App.closeDocument(doc_m.Name)

    doc_h = App.newDocument()
    try:
        importSKP.import_skp(path, doc_h, merge=False, curved_surfaces=True)
        sig_h = _signature(_world_shapes(doc_h, importSKP.last_import_stats, False))
    finally:
        App.closeDocument(doc_h.Name)

    name = os.path.basename(path)
    assert sig_h[0] == sig_m[0], f"{name}: faces {sig_h[0]} vs {sig_m[0]}"
    assert abs(sig_h[1] - sig_m[1]) <= 1e-6 * max(1.0, sig_m[1]), f"{name}: area {sig_h[1]} vs {sig_m[1]}"
    assert sig_h[2] == sig_m[2], f"{name}: vertex sets differ"
    assert sig_h[3] == sig_m[3], f"{name}: edges {sig_h[3]} vs {sig_m[3]}"
    assert abs(sig_h[4] - sig_m[4]) <= 1e-6 * max(1.0, sig_m[4]), f"{name}: edge len {sig_h[4]} vs {sig_m[4]}"
    print(f"check_hierarchy_matches_merged({name}): OK ({sig_h[0]} faces, {sig_h[3]} edges)")


def _main():
    check_arcs_fixture("arcs_2017.skp")
    check_arcs_fixture("arcs_2021.skp")
    check_plate_with_hole("plate_with_hole_2017.skp")
    check_plate_with_hole("plate_with_hole_2021.skp")
    check_ellipse_stays_polygon()
    check_export_round_trip()
    check_preference()
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_hierarchy_matches_merged(os.path.join(FIXTURES_DIR, name))


_harness.run(_main)
