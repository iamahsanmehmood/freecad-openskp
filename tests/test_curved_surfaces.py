"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_curved_surfaces.py

FitCurvedSurfaces (#18, parts 1 & 2):
- Analytic circles and circular arcs in Definition.arcs that bound flat faces
  import as true Part.Circle / Part.Arc boundary edges.
- Faces joined by soft or smooth edges are grouped into candidate surfaces;
  cylinders and cones/frustums (including partial revolutions, pointed cones,
  and inward cylindrical hole bores) recognised from Definition.arcs become
  true Part.Cylinder / Part.Cone faces, sewing with their caps into exact
  solids when MakeSolids is enabled.
- Smoothed groups that do not fit stay faceted (counted in
  curved_groups_unfitted), and any arc shared with an unfitted wall stays
  polygonal so neighbouring faces remain water-tight connected.
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
    print(f"check_arcs_fixture({fixture_name}): OK")


def check_cylinder(fixture_name):
    """cylinder_2017/2021.skp (r = 5 in, h = 12 in, 24 segments):
    - default: 26 flat faces.
    - curved_surfaces=True: 3 faces (2 circular discs + 1 Part.Cylinder wall).
    - curved_surfaces=True, solids=True: 1 valid Part.Solid of exact volume pi * 5^2 * 12 in3."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["cylinders_built"] == 1 and stats["cones_built"] == 0, stats
            assert stats["face_arcs_built"] == 2 and stats["curved_groups_unfitted"] == 0, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = [f for s in shapes for f in s.Faces]
            assert len(faces) == 3 and all(f.isValid() for f in faces), len(faces)
            cyl_faces = [f for f in faces if isinstance(f.Surface, Part.Cylinder)]
            planar_faces = [f for f in faces if isinstance(f.Surface, Part.Plane)]
            assert len(cyl_faces) == 1 and len(planar_faces) == 2, (len(cyl_faces), len(planar_faces))
            cyl = cyl_faces[0]
            assert abs(cyl.Surface.Radius - 5.0 * MM) < 1e-5, cyl.Surface.Radius
            assert abs(cyl.Area - 2.0 * math.pi * 5.0 * 12.0 * MM ** 2) < 1e-1, cyl.Area
            u0, u1, v0, v1 = cyl.ParameterRange
            um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
            pt = cyl.Surface.value(um, vm)
            rad = App.Vector(pt.x, pt.y, 0.0)
            assert cyl.normalAt(um, vm).dot(rad) > 0.0, "cylinder normal must point outward"
        finally:
            App.closeDocument(doc.Name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=True, curved_surfaces=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        assert len(solids) == 1 and solids[0].isValid() and len(solids[0].Faces) == 3, len(solids)
        want_vol = math.pi * (5.0 ** 2) * 12.0 * MM ** 3
        assert abs(solids[0].Volume - want_vol) < 1.0, (solids[0].Volume, want_vol)
    finally:
        App.closeDocument(doc.Name)
    print(f"check_cylinder({fixture_name}): OK (1 Part.Cylinder + 2 caps -> 1 exact solid)")


def check_frustum(fixture_name):
    """frustum_2017/2021.skp (r1 = 6 in, r2 = 3 in, h = 4 in, slant = 5 in):
    - curved_surfaces=True: 3 faces (2 circular discs + 1 Part.Cone wall of area 45*pi in2).
    - curved_surfaces=True, solids=True: 1 valid Part.Solid of volume 84*pi in3."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["cones_built"] == 1 and stats["cylinders_built"] == 0, stats
            assert stats["face_arcs_built"] == 2 and stats["curved_groups_unfitted"] == 0, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = [f for s in shapes for f in s.Faces]
            assert len(faces) == 3 and all(f.isValid() for f in faces), len(faces)
            cone_faces = [f for f in faces if isinstance(f.Surface, Part.Cone)]
            assert len(cone_faces) == 1, len(cone_faces)
            cone = cone_faces[0]
            want_lat_area = math.pi * (6.0 + 3.0) * 5.0 * MM ** 2
            assert abs(cone.Area - want_lat_area) < 1e-1, (cone.Area, want_lat_area)
            assert abs(abs(cone.Surface.SemiAngle) - math.atan2(3.0, 4.0)) < 1e-6, cone.Surface.SemiAngle
        finally:
            App.closeDocument(doc.Name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=True, curved_surfaces=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        assert len(solids) == 1 and solids[0].isValid() and len(solids[0].Faces) == 3, len(solids)
        want_vol = (math.pi * 4.0 / 3.0) * (36.0 + 18.0 + 9.0) * MM ** 3
        assert abs(solids[0].Volume - want_vol) < 1.0, (solids[0].Volume, want_vol)
    finally:
        App.closeDocument(doc.Name)
    print(f"check_frustum({fixture_name}): OK (1 Part.Cone + 2 caps -> 1 exact solid)")


def check_plate_with_hole(fixture_name):
    """plate_with_hole_2017/2021.skp (20 x 20 x 2 in plate with r = 4 in bore):
    - curved_surfaces=True: 7 faces (2 perforated faces with Part.Circle holes,
      4 flat outer sides, 1 inward-facing Part.Cylinder bore).
    - curved_surfaces=True, solids=True: 1 valid Part.Solid of volume (400 - 16*pi)*2 in3."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    expected_face_area = (20.0 * 20.0 - math.pi * 4.0 ** 2) * MM ** 2
    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["cylinders_built"] == 1 and stats["face_arcs_built"] == 2, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = [f for s in shapes for f in s.Faces]
            assert len(faces) == 7 and all(f.isValid() for f in faces), len(faces)

            cyls = [f for f in faces if isinstance(f.Surface, Part.Cylinder)]
            assert len(cyls) == 1 and abs(cyls[0].Surface.Radius - 4.0 * MM) < 1e-5
            u0, u1, v0, v1 = cyls[0].ParameterRange
            um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
            pt = cyls[0].Surface.value(um, vm)
            rad = App.Vector(pt.x, pt.y, 0.0)
            assert cyls[0].normalAt(um, vm).dot(rad) < 0.0, "hole bore normal must point inward"

            holed = sorted([f for f in faces if len(f.Wires) == 2], key=lambda f: f.CenterOfMass.z)
            assert len(holed) == 2, len(holed)
            for face, nz in zip(holed, (-1.0, 1.0)):
                assert abs(face.Area - expected_face_area) < 1e-2, (face.Area, expected_face_area)
                fu0, fu1, fv0, fv1 = face.ParameterRange
                normal = face.normalAt(0.5 * (fu0 + fu1), 0.5 * (fv0 + fv1))
                assert abs(normal.z - nz) < 1e-6, tuple(normal)
        finally:
            App.closeDocument(doc.Name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=True, curved_surfaces=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        assert len(solids) == 1 and solids[0].isValid() and len(solids[0].Faces) == 7, len(solids)
        want_vol = (400.0 - 16.0 * math.pi) * 2.0 * MM ** 3
        assert abs(solids[0].Volume - want_vol) < 1.0, (solids[0].Volume, want_vol)
    finally:
        App.closeDocument(doc.Name)
    print(f"check_plate_with_hole({fixture_name}): OK (7 faces incl. inward Part.Cylinder bore -> 1 solid)")


def check_smoothed_blob_stays_faceted(fixture_name):
    """smoothed_blob_2017/2021.skp: an asymmetric wavy closed surface whose edges
    are all soft+smooth; must stay 168 planar facets and report 1 unfitted group."""
    path = os.path.join(FIXTURES_DIR, fixture_name)
    doc_off = App.newDocument()
    doc_on = App.newDocument()
    try:
        importSKP.import_skp(path, doc_off, merge=True, curved_surfaces=False)
        sig_off = _signature([o.Shape.copy() for o in doc_off.Objects if hasattr(o, "Shape")])

        importSKP.import_skp(path, doc_on, merge=True, curved_surfaces=True)
        stats = importSKP.last_import_stats
        assert stats["cylinders_built"] == 0 and stats["cones_built"] == 0, stats
        assert stats["curved_groups_unfitted"] == 1, stats
        sig_on = _signature([o.Shape.copy() for o in doc_on.Objects if hasattr(o, "Shape")])
        assert sig_on == sig_off, (sig_on[0], sig_off[0])
    finally:
        App.closeDocument(doc_off.Name)
        App.closeDocument(doc_on.Name)
    print(f"check_smoothed_blob_stays_faceted({fixture_name}): OK (168 facets unchanged, 1 unfitted group)")


def check_sphere(fixture_name):
    """sphere_2017/2021.skp (r = 5 in, 128 smoothed facets, no ArcCurve records):
    - default: 128 flat faces.
    - curved_surfaces=True: 1 closed Part.Sphere face (r = 127 mm, area 4*pi*r^2).
    - curved_surfaces=True, solids=True: 1 valid Part.Solid of volume (4/3)*pi*r^3."""
    path = os.path.join(FIXTURES_DIR, fixture_name)

    for merge in (True, False):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path, doc, merge=merge, curved_surfaces=True)
            stats = importSKP.last_import_stats
            assert stats["spheres_built"] == 1 and stats["cylinders_built"] == 0 and stats["cones_built"] == 0, stats
            assert stats["curved_groups_unfitted"] == 0, stats
            shapes = _world_shapes(doc, stats, merge)
            faces = [f for s in shapes for f in s.Faces]
            assert len(faces) == 1 and faces[0].isValid(), len(faces)
            sph = faces[0]
            assert isinstance(sph.Surface, Part.Sphere), type(sph.Surface).__name__
            assert abs(sph.Surface.Radius - 5.0 * MM) < 1e-4, sph.Surface.Radius
            assert sph.Surface.Center.Length < 1e-4, tuple(sph.Surface.Center)
            want_area = 4.0 * math.pi * (5.0 * MM) ** 2
            assert abs(sph.Area - want_area) < 1e-1, (sph.Area, want_area)
            n_eq = sph.normalAt(0.0, 0.0)
            p_eq = sph.Surface.value(0.0, 0.0)
            assert n_eq.dot(p_eq) > 0.0, "sphere normal must point outward"
        finally:
            App.closeDocument(doc.Name)

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=True, curved_surfaces=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        assert len(solids) == 1 and solids[0].isValid() and len(solids[0].Faces) == 1, len(solids)
        want_vol = (4.0 / 3.0) * math.pi * (5.0 * MM) ** 3
        assert abs(solids[0].Volume - want_vol) < 1.0, (solids[0].Volume, want_vol)
    finally:
        App.closeDocument(doc.Name)
    print(f"check_sphere({fixture_name}): OK (128 facets -> 1 Part.Sphere -> 1 exact solid)")


def check_hemisphere_and_open_dome():
    """A translated hemisphere closed by a circular ArcCurve base sews into a
    2-face solid of volume (2/3)*pi*r^3, and an open spherical polar cap without
    ArcCurve metadata becomes a trimmed Part.Sphere face."""
    from openskp.model import ArcCurve, Definition, Edge, Face, SkpModel, Vertex

    # 1. Closed hemisphere at centre (30, -40, 10) in, r = 6 in, z >= 10 in
    cx, cy, cz, r = 30.0, -40.0, 10.0, 6.0
    n_rings, n_lon = 5, 16
    verts = {0: Vertex(id=0, x=cx, y=cy, z=cz + r)}
    rings = []
    vid = 1
    for ri in range(n_rings):
        lat = (0.5 * math.pi) * ri / n_rings
        ring = []
        for li in range(n_lon):
            lon = 2.0 * math.pi * li / n_lon
            verts[vid] = Vertex(
                id=vid,
                x=cx + r * math.cos(lat) * math.cos(lon),
                y=cy + r * math.cos(lat) * math.sin(lon),
                z=cz + r * math.sin(lat),
            )
            ring.append(vid)
            vid += 1
        rings.append(ring)

    edges = {}
    eid = 0
    ring_eids = []
    for ri, ring in enumerate(rings):
        re_list = []
        for li in range(n_lon):
            v1, v2 = ring[li], ring[(li + 1) % n_lon]
            is_rim = (ri == 0)
            edges[eid] = Edge(id=eid, v1_id=v1, v2_id=v2, soft=not is_rim, smooth=not is_rim,
                              curve_id=1 if is_rim else None)
            re_list.append(eid)
            eid += 1
        ring_eids.append(re_list)

    meridian_eids = []
    for ri in range(n_rings - 1):
        me = []
        for li in range(n_lon):
            edges[eid] = Edge(id=eid, v1_id=rings[ri][li], v2_id=rings[ri + 1][li], soft=True, smooth=True)
            me.append(eid)
            eid += 1
        meridian_eids.append(me)

    pole_eids = []
    for li in range(n_lon):
        edges[eid] = Edge(id=eid, v1_id=rings[-1][li], v2_id=0, soft=True, smooth=True)
        pole_eids.append(eid)
        eid += 1

    faces = {0: Face(id=0, loops=[[(e, -1) for e in reversed(ring_eids[0])]])}
    fid = 1
    for ri in range(n_rings - 1):
        for li in range(n_lon):
            nj = (li + 1) % n_lon
            faces[fid] = Face(
                id=fid,
                loops=[[(ring_eids[ri][li], 1), (meridian_eids[ri][nj], 1),
                        (ring_eids[ri + 1][li], -1), (meridian_eids[ri][li], -1)]],
            )
            fid += 1
    for li in range(n_lon):
        nj = (li + 1) % n_lon
        faces[fid] = Face(
            id=fid,
            loops=[[(ring_eids[-1][li], 1), (pole_eids[nj], 1), (pole_eids[li], -1)]],
        )
        fid += 1

    defn = Definition(
        id=0, name="Hemisphere", vertices=verts, edges=edges, faces=faces,
        arcs={1: ArcCurve((cx, cy, cz), (0.0, 0.0, 1.0), (r, 0.0, 0.0), (0.0, r, 0.0), 0.0, 2 * math.pi)},
    )
    stats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
             "arcs_built": 0, "make_solids": True, "fit_curved_surfaces": True,
             "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0,
             "spheres_built": 0, "curved_groups_unfitted": 0}
    shapes, _ = importSKP._build_own_geometry(defn, SkpModel(root=defn), stats, None)
    assert stats["spheres_built"] == 1 and stats["face_arcs_built"] == 1 and stats["solids_built"] == 1, stats
    assert len(shapes) == 1 and shapes[0].ShapeType == "Solid" and len(shapes[0].Faces) == 2
    want_hemi_vol = (2.0 / 3.0) * math.pi * (r * MM) ** 3
    assert abs(shapes[0].Volume - want_hemi_vol) < 1.0, (shapes[0].Volume, want_hemi_vol)

    # 2. Open dome (no base face and no ArcCurve)
    open_faces = {k: v for k, v in faces.items() if k != 0}
    for e in edges.values():
        e.curve_id = None
    open_defn = Definition(id=0, name="OpenDome", vertices=verts, edges=edges, faces=open_faces, arcs={})
    ostats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
              "arcs_built": 0, "make_solids": False, "fit_curved_surfaces": True,
              "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0,
              "spheres_built": 0, "curved_groups_unfitted": 0}
    oshapes, _ = importSKP._build_own_geometry(open_defn, SkpModel(root=open_defn), ostats, None)
    assert ostats["spheres_built"] == 1 and len(oshapes) == 1, ostats
    assert isinstance(oshapes[0].Surface, Part.Sphere)
    assert abs(oshapes[0].Area - 2.0 * math.pi * (r * MM) ** 2) < 1e-1, oshapes[0].Area

    # 3. Two-rim spherical zone (truncated hemisphere between lat 0 and 75 deg)
    lat_top = 0.5 * math.pi * ((n_rings - 1) / n_rings)
    r_top = r * math.cos(lat_top)
    z_top = cz + r * math.sin(lat_top)
    pole_set = set(pole_eids)
    z_edges = {k: v for k, v in edges.items() if k not in pole_set}
    for eid_b in ring_eids[0]:
        e = z_edges[eid_b]
        z_edges[eid_b] = Edge(id=e.id, v1_id=e.v1_id, v2_id=e.v2_id, soft=False, smooth=False, curve_id=1)
    for eid_t in ring_eids[-1]:
        e = z_edges[eid_t]
        z_edges[eid_t] = Edge(id=e.id, v1_id=e.v1_id, v2_id=e.v2_id, soft=False, smooth=False, curve_id=2)
    n_quad_faces = (n_rings - 1) * n_lon
    z_faces = {fid_k: faces[fid_k] for fid_k in range(0, 1 + n_quad_faces)}
    z_faces[999] = Face(id=999, loops=[[(e, 1) for e in ring_eids[-1]]])
    z_defn = Definition(
        id=0, name="SphericalZone", vertices=verts, edges=z_edges, faces=z_faces,
        arcs={
            1: ArcCurve((cx, cy, cz), (0.0, 0.0, 1.0), (r, 0.0, 0.0), (0.0, r, 0.0), 0.0, 2 * math.pi),
            2: ArcCurve((cx, cy, z_top), (0.0, 0.0, 1.0), (r_top, 0.0, 0.0), (0.0, r_top, 0.0), 0.0, 2 * math.pi),
        },
    )
    zstats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
              "arcs_built": 0, "make_solids": True, "fit_curved_surfaces": True,
              "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0,
              "spheres_built": 0, "curved_groups_unfitted": 0}
    zshapes, _ = importSKP._build_own_geometry(z_defn, SkpModel(root=z_defn), zstats, None)
    assert zstats["spheres_built"] == 1 and zstats["face_arcs_built"] == 2 and zstats["solids_built"] == 1, zstats
    assert len(zshapes) == 1 and zshapes[0].ShapeType == "Solid" and len(zshapes[0].Faces) == 3, (
        [s.ShapeType for s in zshapes],
        zstats,
    )
    h_mm = r * math.sin(lat_top) * MM
    r_mm = r * MM
    want_zone_vol = math.pi * (r_mm * r_mm * h_mm - (h_mm ** 3) / 3.0)
    assert abs(zshapes[0].Volume - want_zone_vol) < 1.0, (zshapes[0].Volume, want_zone_vol)
    print("check_hemisphere_and_open_dome: OK (hemisphere solid + open dome + 2-rim spherical zone solid)")


def check_pointed_cone_and_partial_cylinder():
    """In-memory checks for a pointed cone (base circle + apex) and a 180 deg
    half-cylinder (two 180 deg arcs + 2 half-disc caps + 1 flat back face)."""
    from openskp.model import ArcCurve, Definition, Edge, Face, SkpModel, Vertex

    # 1. Pointed cone: r = 5 in at z = 0, apex (0, 0, 12) in (slant = 13 in)
    n = 24
    r, h = 5.0, 12.0
    verts = {i: Vertex(id=i, x=r * math.cos(2 * math.pi * i / n), y=r * math.sin(2 * math.pi * i / n), z=0.0)
             for i in range(n)}
    verts[n] = Vertex(id=n, x=0.0, y=0.0, z=h)
    edges = {}
    for i in range(n):
        edges[i] = Edge(id=i, v1_id=i, v2_id=(i + 1) % n, curve_id=1)
        edges[n + i] = Edge(id=n + i, v1_id=i, v2_id=n, soft=True, smooth=True)
    faces = {0: Face(id=0, loops=[[(i, -1) for i in reversed(range(n))]])}
    for i in range(n):
        j = (i + 1) % n
        faces[i + 1] = Face(id=i + 1, loops=[[(i, 1), (n + j, 1), (n + i, -1)]])
    defn = Definition(
        id=0, name="PointedCone", vertices=verts, edges=edges, faces=faces,
        arcs={1: ArcCurve((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (r, 0.0, 0.0), (0.0, r, 0.0), 0.0, 2 * math.pi)},
    )
    stats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
             "arcs_built": 0, "make_solids": True, "fit_curved_surfaces": True,
             "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0, "curved_groups_unfitted": 0}
    shapes, _ = importSKP._build_own_geometry(defn, SkpModel(root=defn), stats, None)
    assert stats["cones_built"] == 1 and stats["solids_built"] == 1, stats
    assert len(shapes) == 1 and shapes[0].ShapeType == "Solid" and len(shapes[0].Faces) == 2
    assert abs(shapes[0].Volume - (math.pi / 3.0) * 25.0 * 12.0 * MM ** 3) < 1.0, shapes[0].Volume

    # 2. Half-cylinder: r = 6 in, z = 0..10 in, 0..pi (12 segments)
    m = 12
    rc, hc = 6.0, 10.0
    cverts = {}
    for k, z in enumerate((0.0, hc)):
        for i in range(m + 1):
            ang = math.pi * i / m
            cverts[k * (m + 1) + i] = Vertex(id=k * (m + 1) + i, x=rc * math.cos(ang), y=rc * math.sin(ang), z=z)
    cedges = {}
    eid = 0
    bot_arc_eids, top_arc_eids = [], []
    for i in range(m):
        cedges[eid] = Edge(id=eid, v1_id=i, v2_id=i + 1, curve_id=10)
        bot_arc_eids.append(eid)
        eid += 1
    for i in range(m):
        b = m + 1
        cedges[eid] = Edge(id=eid, v1_id=b + i, v2_id=b + i + 1, curve_id=11)
        top_arc_eids.append(eid)
        eid += 1
    bot_chord_eid = eid
    cedges[eid] = Edge(id=eid, v1_id=m, v2_id=0)
    eid += 1
    top_chord_eid = eid
    cedges[eid] = Edge(id=eid, v1_id=2 * m + 1, v2_id=m + 1)
    eid += 1
    vert_eids = []
    for i in range(m + 1):
        is_interior = 0 < i < m
        cedges[eid] = Edge(id=eid, v1_id=i, v2_id=(m + 1) + i, soft=is_interior, smooth=is_interior)
        vert_eids.append(eid)
        eid += 1

    cfaces = {
        0: Face(id=0, loops=[[(bot_chord_eid, -1)] + [(e, -1) for e in reversed(bot_arc_eids)]]),
        1: Face(id=1, loops=[[(e, 1) for e in top_arc_eids] + [(top_chord_eid, 1)]]),
        2: Face(id=2, loops=[[(bot_chord_eid, 1), (vert_eids[0], 1), (top_chord_eid, -1), (vert_eids[m], -1)]]),
    }
    for i in range(m):
        cfaces[3 + i] = Face(
            id=3 + i,
            loops=[[(bot_arc_eids[i], 1), (vert_eids[i + 1], 1), (top_arc_eids[i], -1), (vert_eids[i], -1)]],
        )
    cdefn = Definition(
        id=0, name="HalfCyl", vertices=cverts, edges=cedges, faces=cfaces,
        arcs={
            10: ArcCurve((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (rc, 0.0, 0.0), (0.0, rc, 0.0), 0.0, math.pi),
            11: ArcCurve((0.0, 0.0, hc), (0.0, 0.0, 1.0), (rc, 0.0, 0.0), (0.0, rc, 0.0), 0.0, math.pi),
        },
    )
    cstats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
              "arcs_built": 0, "make_solids": True, "fit_curved_surfaces": True,
              "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0, "curved_groups_unfitted": 0}
    cshapes, _ = importSKP._build_own_geometry(cdefn, SkpModel(root=cdefn), cstats, None)
    assert cstats["cylinders_built"] == 1 and cstats["face_arcs_built"] == 2 and cstats["solids_built"] == 1, cstats
    assert len(cshapes) == 1 and cshapes[0].ShapeType == "Solid" and len(cshapes[0].Faces) == 4
    assert abs(cshapes[0].Volume - 0.5 * math.pi * 36.0 * 10.0 * MM ** 3) < 1.0, cshapes[0].Volume
    print("check_pointed_cone_and_partial_cylinder: OK (both sew into exact closed solids)")


def check_unfitted_extrusion_keeps_cap_connected():
    """When a circle is extruded to a warped non-circular top so the smoothed
    side fails fitting, its circular base cap keeps its 24 straight segments so
    the cap and walls stay water-tight connected and still sew into a solid."""
    from openskp.model import ArcCurve, Definition, Edge, Face, SkpModel, Vertex

    n = 12
    r, h = 5.0, 8.0
    verts = {}
    for i in range(n):
        ang = 2 * math.pi * i / n
        verts[i] = Vertex(id=i, x=r * math.cos(ang), y=r * math.sin(ang), z=0.0)
        rw = r * (1.0 + 0.35 * math.cos(2 * ang))
        verts[n + i] = Vertex(id=n + i, x=rw * math.cos(ang), y=rw * math.sin(ang), z=h)
    edges = {}
    for i in range(n):
        j = (i + 1) % n
        edges[i] = Edge(id=i, v1_id=i, v2_id=j, curve_id=1)
        edges[n + i] = Edge(id=n + i, v1_id=n + i, v2_id=n + j)
        edges[2 * n + i] = Edge(id=2 * n + i, v1_id=i, v2_id=n + i, soft=True, smooth=True)
    faces = {
        0: Face(id=0, loops=[[(i, -1) for i in reversed(range(n))]]),
        1: Face(id=1, loops=[[(n + i, 1) for i in range(n)]]),
    }
    for i in range(n):
        j = (i + 1) % n
        faces[2 + i] = Face(id=2 + i, loops=[[(i, 1), (2 * n + j, 1), (n + i, -1), (2 * n + i, -1)]])
    defn = Definition(
        id=0, name="WarpedExtrusion", vertices=verts, edges=edges, faces=faces,
        arcs={1: ArcCurve((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (r, 0.0, 0.0), (0.0, r, 0.0), 0.0, 2 * math.pi)},
    )
    stats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
             "arcs_built": 0, "make_solids": True, "fit_curved_surfaces": True,
             "face_arcs_built": 0, "cylinders_built": 0, "cones_built": 0, "curved_groups_unfitted": 0}
    shapes, _ = importSKP._build_own_geometry(defn, SkpModel(root=defn), stats, None)
    assert stats["curved_groups_unfitted"] == 1 and stats["face_arcs_built"] == 0, stats
    assert stats["solids_built"] == 1 and len(shapes) == 1 and shapes[0].ShapeType == "Solid"
    print("check_unfitted_extrusion_keeps_cap_connected: OK (cap stays polygonal, sews into 1 solid)")


def check_ellipse_stays_polygon():
    from openskp.model import ArcCurve, Definition, Edge, Face, SkpModel, Vertex

    n = 24
    rx, ry = 10.0, 5.0
    verts = {
        i: Vertex(id=i, x=rx * math.cos(2 * math.pi * i / n), y=ry * math.sin(2 * math.pi * i / n), z=0.0)
        for i in range(n)
    }
    edges = {i: Edge(id=i, v1_id=i, v2_id=(i + 1) % n, curve_id=1) for i in range(n)}
    defn = Definition(
        id=0, name="EllipseDef", vertices=verts, edges=edges,
        faces={1: Face(id=1, loops=[[(i, 1) for i in range(n)]], normal=(0.0, 0.0, 1.0))},
        arcs={1: ArcCurve((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (rx, 0.0, 0.0), (0.0, ry, 0.0), 0.0, 2 * math.pi)},
    )
    stats = {"faces_built": 0, "faces_skipped": 0, "edge_runs_built": 0, "edge_runs_skipped": 0,
             "arcs_built": 0, "fit_curved_surfaces": True, "face_arcs_built": 0}
    shapes, _ = importSKP._build_own_geometry(defn, SkpModel(root=defn), stats, None)
    assert len(shapes) == 1 and stats["face_arcs_built"] == 0, stats
    assert len(shapes[0].Edges) == n and all(isinstance(e.Curve, Part.Line) for e in shapes[0].Edges)
    print("check_ellipse_stays_polygon: OK")


def check_export_round_trip():
    """Exporting imported curved surfaces (discs, circular holes, cylinders and
    frustums) writes valid SketchUp faces without dropping curved walls or caps."""
    import openskp

    for fixture_name, min_faces, want_holed in (
        ("arcs_2021.skp", 2, 0),
        ("plate_with_hole_2021.skp", 10, 2),
        ("cylinder_2021.skp", 10, 0),
        ("frustum_2021.skp", 10, 0),
        ("sphere_2021.skp", 20, 0),
    ):
        doc = App.newDocument()
        out_path = os.path.join(FIXTURES_DIR, f"_curved_export_{fixture_name}")
        try:
            importSKP.import_skp(os.path.join(FIXTURES_DIR, fixture_name), doc, merge=False, curved_surfaces=True)
            importSKP.export(list(doc.Objects), out_path)
            model = openskp.SkpFile.open(out_path).parse()
            assert len(model.root.faces) >= min_faces, (fixture_name, len(model.root.faces))
            holed = sum(1 for f in model.root.faces.values() if len(f.loops) == 2)
            assert holed == want_holed, (fixture_name, holed)
        finally:
            App.closeDocument(doc.Name)
            if os.path.exists(out_path):
                os.remove(out_path)
    print("check_export_round_trip: OK")


def check_preference():
    path = os.path.join(FIXTURES_DIR, "cylinder_2021.skp")
    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for pref, arg, want_flag, want_cyl in (
            (None, None, False, 0),
            (True, None, True, 1),
            (False, None, False, 0),
            (True, False, False, 0),
            (False, True, True, 1),
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
                assert stats["cylinders_built"] == want_cyl, (pref, arg, stats)
            finally:
                App.closeDocument(doc.Name)
        print("check_preference: OK")
    finally:
        params.RemBool("FitCurvedSurfaces")


def check_hierarchy_matches_merged(path):
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
    check_cylinder("cylinder_2017.skp")
    check_cylinder("cylinder_2021.skp")
    check_frustum("frustum_2017.skp")
    check_frustum("frustum_2021.skp")
    check_plate_with_hole("plate_with_hole_2017.skp")
    check_plate_with_hole("plate_with_hole_2021.skp")
    check_smoothed_blob_stays_faceted("smoothed_blob_2017.skp")
    check_smoothed_blob_stays_faceted("smoothed_blob_2021.skp")
    check_sphere("sphere_2017.skp")
    check_sphere("sphere_2021.skp")
    check_hemisphere_and_open_dome()
    check_pointed_cone_and_partial_cylinder()
    check_unfitted_extrusion_keeps_cap_connected()
    check_ellipse_stays_polygon()
    check_export_round_trip()
    check_preference()
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_hierarchy_matches_merged(os.path.join(FIXTURES_DIR, name))


_harness.run(_main)
