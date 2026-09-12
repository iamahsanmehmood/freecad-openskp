"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_export.py

Round-trips real FreeCAD geometry (a face with a hole, plus a
translated box) through export() and back through openskp's own
Python reader, checking structure (loop counts) and exact coordinates
survive the mm<->inch conversion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importSKP  # noqa: E402

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402


def _make_wire(pts):
    vecs = [App.Vector(*p) for p in pts] + [App.Vector(*pts[0])]
    return Part.makePolygon(vecs)


def run():
    doc = App.newDocument()

    outer = [(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0)]
    hole = [(30, 30, 0), (30, 70, 0), (70, 70, 0), (70, 30, 0)]
    face_with_hole = Part.Face([_make_wire(outer), _make_wire(hole)])

    box = Part.makeBox(50, 50, 50)
    box.translate(App.Vector(200, 0, 0))

    obj1 = doc.addObject("Part::Feature", "FaceWithHole")
    obj1.Shape = face_with_hole
    obj2 = doc.addObject("Part::Feature", "Box")
    obj2.Shape = box
    doc.recompute()

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_export_test.skp")
    importSKP.export([obj1, obj2], out_path)

    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "vendor",
        ),
    )
    import openskp

    model = openskp.SkpFile.open(out_path).parse()
    os.remove(out_path)

    assert len(model.root.faces) == 7, f"expected 7 faces, got {len(model.root.faces)}"
    loop_counts = sorted(len(f.loops) for f in model.root.faces.values())
    assert loop_counts == [1, 1, 1, 1, 1, 1, 2], loop_counts

    # exact-coordinate check on the hole-bearing face's outer boundary -
    # confirms the mm<->inch round trip is lossless, not just structurally
    # plausible
    hole_face = next(f for f in model.root.faces.values() if len(f.loops) == 2)
    outer_loop = hole_face.loops[0]
    pts_mm = []
    for edge_id, sign in outer_loop:
        edge = model.root.edges[edge_id]
        vid = edge.v1_id if sign >= 0 else edge.v2_id
        v = model.root.vertices[vid]
        pts_mm.append((round(v.x * 25.4, 3), round(v.y * 25.4, 3), round(v.z * 25.4, 3)))
    expected = {(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0), (0.0, 100.0, 0.0)}
    assert set(pts_mm) == expected, (pts_mm, expected)

    print("export round-trip: OK (7 faces, loop counts", loop_counts, ", coordinates exact)")


# Note: freecadcmd runs a script file as a module named after the file,
# not "__main__" - see tests/test_import.py's own note on this.
run()
