"""Run with freecadcmd tests/test_nonplanar.py (no triangulation dependencies).

Check preservation of boundaries, not just triangle counts: moving a shared
vertex or filling a concave notch can create valid faces but break the solid.
"""
import math
import os
import sys
from collections import Counter
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402  - freecadcmd exits 0 on a failing script; see tests/_harness.py

try:
    import importSKP  # noqa: E402
    import Part  # noqa: E402
except BaseException:
    _harness.die()


def polygon(points, holes=()):
    vertices, edges, loops = {}, {}, []
    for ring in [points] + list(holes):
        ids = []
        for point in ring:
            index = len(vertices) + 1
            vertices[index] = SimpleNamespace(
                x=point[0] / 25.4, y=point[1] / 25.4, z=point[2] / 25.4,
            )
            ids.append(index)
        loop = []
        for start, end in zip(ids, ids[1:] + ids[:1]):
            index = len(edges) + 1
            edges[index] = SimpleNamespace(id=index, v1_id=start, v2_id=end, curve_id=None)
            loop.append((index, 1))
        loops.append(loop)
    face = SimpleNamespace(id=17, loops=loops, material_id=None)
    definition = SimpleNamespace(
        name="Synthetic polygon", faces={17: face}, vertices=vertices,
        edges=edges, instances=[], arcs={},
    )
    return definition, face


def vertex_key(point):
    return tuple(round(float(value), 9) for value in point)


def edge_key(a, b):
    return tuple(sorted((vertex_key(a), vertex_key(b))))


def check_polygon(points, projected_area):
    definition, face = polygon(points)
    shape = importSKP._make_face_shape(definition, face)
    assert shape is not None and shape.isValid()
    assert len(shape.Faces) == len(points) - 2
    assert all(len(f.Edges) == 3 and f.Area > 0 for f in shape.Faces)
    # Exact original 3-D positions; projection must never flatten the output.
    assert {vertex_key(v.Point) for v in shape.Vertexes} == {vertex_key(p) for p in points}
    uses = Counter(
        edge_key(edge.Vertexes[0].Point, edge.Vertexes[1].Point)
        for triangle in shape.Faces for edge in triangle.Edges
    )
    boundary = {edge_key(a, b) for a, b in zip(points, points[1:] + points[:1])}
    assert {edge for edge, count in uses.items() if count == 1} == boundary
    assert all(count == (1 if edge in boundary else 2) for edge, count in uses.items())
    area = 0
    signed = []
    for triangle in shape.Faces:
        pts = [vertex.Point for vertex in triangle.OuterWire.OrderedVertexes]
        cross = (pts[1] - pts[0]).cross(pts[2] - pts[0]).z
        signed.append(cross)
        area += abs(cross) / 2
    assert math.isclose(area, projected_area, abs_tol=1e-8), (area, projected_area)
    assert all(value > 0 for value in signed) or all(value < 0 for value in signed)
    return shape


def check_synthetic():
    quad = [(0, 0, 0), (10, 0, 0), (10, 10, 0.01), (0, 10, 0)]
    check_polygon(quad, 100)
    check_polygon(list(reversed(quad)), 100)
    # The fixed v0-v2 diagonal lies outside this concave quadrilateral.
    check_polygon([(0, 0, 0), (1, 0.5, 0.01), (3, 0, 0), (0, 3, 0)], 3.75)
    concave = [(0, 0, 0), (6, 0, 0), (6, 6, 0.01), (4, 6, 0),
               (4, 2, 0), (2, 2, 0), (2, 6, 0), (0, 6, 0)]
    shape = check_polygon(concave, 28)
    assert all(not (2 < f.CenterOfMass.x < 4 and f.CenterOfMass.y > 2) for f in shape.Faces)
    check_polygon([(0, 0, 0), (5, 0, 0), (10, 0, 0), (10, 10, 0.01), (0, 10, 0)], 100)
    # Planar faces must stay single faces, including a face containing a hole.
    flat = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    definition, face = polygon(flat)
    assert importSKP._make_face_shape(definition, face).ShapeType == "Face"
    hole = [(3, 3, 0), (3, 7, 0), (7, 7, 0), (7, 3, 0)]
    definition, face = polygon(flat, [hole])
    shape = importSKP._make_face_shape(definition, face)
    assert shape.ShapeType == "Face" and len(shape.Wires) == 2
    assert abs(shape.Area - 84) < 1e-8
    # Unsupported nonplanar holes must not become a face with its hole filled.
    definition, face = polygon(quad, [hole])
    assert importSKP._make_face_shape(definition, face) is None
    for points in [
        [(0, 0, 0), (3, 3, 0.01), (0, 2, 0), (3, 0, 0)],  # self intersection
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)],  # degenerate
    ]:
        assert importSKP._nonplanar_triangles(points) is None
        definition, face = polygon(points)
        # Preserve the importer's existing handling of a kernel-constructed
        # face, even if invalid. The new fallback must not triangulate it.
        shape = importSKP._make_face_shape(definition, face)
        assert shape is None or shape.ShapeType == "Face"
    definition, face = polygon(quad)
    del definition.edges[1]
    assert importSKP._make_face_shape(definition, face) is None
    definition, face = polygon(quad)
    definition.edges[1].v2_id = 3  # broken connectivity despite all vertices existing
    assert importSKP._make_face_shape(definition, face) is None

    # One source face -> two triangles, each carrying the inherited material;
    # no new compound nesting should appear inside the component leaf.
    definition, face = polygon(quad)
    model = SimpleNamespace(
        materials_by_id={7: SimpleNamespace(color=(12, 34, 56, 255), transparency=0.5)},
        definitions={},
    )
    stats = dict(faces_built=0, faces_skipped=0, placements=0,
                 edge_runs_built=0, edge_runs_skipped=0, arcs_built=0)
    shape, colors = importSKP._get_local_shape(
        definition, model, stats, {}, frozenset(),
        inherited_material_id=7, inherited_layer="Warped",
    )["Warped"]
    assert len(shape.childShapes()) == 2
    assert all(child.ShapeType == "Face" for child in shape.childShapes())
    assert colors == [(12 / 255, 34 / 255, 56 / 255, 0.5)] * 2
    assert stats["faces_triangulated"] == 1 and stats["triangles_built"] == 2
    print("PASS: planar/warped/concave/reversed/collinear boundaries, holes, rejection, material and hierarchy")


def check_real_component():
    import openskp

    path = os.path.join(os.path.dirname(__file__), "fixtures", "Test_Component.skp")
    model = openskp.SkpFile.open(path).parse()
    assert len(model.root.faces) == 106
    stats = dict(faces_built=0, faces_skipped=0, placements=0,
                 edge_runs_built=0, edge_runs_skipped=0, arcs_built=0)
    buckets = importSKP._get_local_shape(model.root, model, stats, {}, frozenset())
    assert stats["faces_built"] == 106 and stats["faces_skipped"] == 0
    assert stats["faces_triangulated"] == 14 and stats["triangles_built"] == 32
    assert len(buckets) == 1
    shape, colors = next(iter(buckets.values()))
    assert len(shape.Faces) == len(colors) == 124
    expected_points = {
        vertex_key(p) for face in model.root.faces.values() for loop in face.loops
        for p in importSKP._loop_points(model.root, loop)
    }
    assert {vertex_key(v.Point) for v in shape.Vertexes} == expected_points
    sewn = shape.copy()
    sewn.sewShape()
    assert len(sewn.Shells) == 1 and sewn.Shells[0].isClosed()
    assert len(sewn.Shells[0].Faces) == 124  # no faces left outside the shell
    solid = Part.makeSolid(sewn.Shells[0])
    assert solid.isValid() and solid.Volume > 0
    solid.check(True)
    print(f"PASS: Test_Component.skp: 106 source faces -> 124 CAD faces, no skipped faces; closed solid {solid.Volume:.6f} mm^3, BOP check passed")


def main():
    # Non-planar face triangulation must work without the optional dependencies
    # used by the reader's mesh pipeline.
    for module in ("numpy", "shapely", "mapbox_earcut"):
        sys.modules[module] = None
    check_synthetic()
    check_real_component()


_harness.run(main)
