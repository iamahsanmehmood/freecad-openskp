"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_solids.py

The MakeSolids import option (#21): each component's own faces are sewn and every
closed shell becomes a solid; faces that close no volume stay faces.

Checked on tests/_synthetic.py's model (the 2 x 2 x 28 in leg is a closed box, the
table top and the spare are single faces), on an open box written here, on
Test_Component.skp (which tests/test_nonplanar.py already shows sews into one closed
solid of 19006.65 mm3), and on every fixture: with or without the option an import
has the same faces, area and vertices.
"""
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

from _synthetic import LEG_SIZE, write_synthetic  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MM = importSKP.INCH_TO_MM
LEG_VOLUME = LEG_SIZE[0] * LEG_SIZE[1] * LEG_SIZE[2] * MM ** 3


def _close(a, b, rel=1e-6):
    return abs(a - b) <= rel * max(1.0, abs(b))


def check_hierarchy(path):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, solids=True)
        stats = importSKP.last_import_stats
        assert stats["make_solids"]
        components = doc.getObject(stats["components_group"])

        def geometry(label):
            part = [o for o in components.Group if o.Label == label][0]
            found = [o for o in part.Group if o.TypeId == "Part::Feature"]
            assert len(found) == 1, f"{label}: {[o.Label for o in part.Group]}"
            return found[0]

        for label in ("Leg", "Leg (Red)"):
            leg = geometry(label)
            assert len(leg.Shape.Solids) == 1 and not leg.Shape.Shells[0].isNull(), label
            assert _close(leg.Shape.Volume, LEG_VOLUME), f"{label}: volume {leg.Shape.Volume}, expected {LEG_VOLUME}"
            assert leg.Shape.isValid(), label
            colors = stats["object_face_colors"][leg.Name]
            assert len(colors) == len(leg.Shape.Faces) == 6, (label, len(colors), len(leg.Shape.Faces))
        red = (200 / 255, 30 / 255, 30 / 255, 1.0)
        assert all(_close(a, b) for c in stats["object_face_colors"][geometry("Leg (Red)").Name]
                   for a, b in zip(c, red)), "the red legs' faces keep their colour through sewing"
        for label in ("Table", "Spare"):
            shape = geometry(label).Shape
            assert not shape.Solids and len(shape.Faces) == 1, f"{label} is one face, which closes nothing"
        assert stats["solids_built"] == 2, stats["solids_built"]  # Leg and Leg (Red), each built once
        assert stats["faces_left_open"] == 4, stats["faces_left_open"]  # Table, Table (Red), Spare, the loose face

        # Through the links: FreeCAD's own view of each top-level leg is a solid with the
        # volume its matrix gives (scaled 2 / 3 / 0.5: 3x; mirrored: 1x), and the sheared
        # copy is a solid too (a shear keeps volume).
        model_part = doc.getObject(stats["model_part"])
        layer0 = [o for o in model_part.Group if o.Label == "Layer0"][0]
        for name, factor in (("Scaled leg", 3.0), ("Mirrored leg", 1.0), ("Sheared leg (sheared copy)", 1.0)):
            obj = [o for o in layer0.Group if o.Label == name][0]
            shape = Part.getShape(obj)
            assert len(shape.Solids) == 1, f"{name}: {len(shape.Solids)} solids"
            assert _close(abs(shape.Volume), LEG_VOLUME * factor, 1e-5), (
                f"{name}: volume {shape.Volume}, expected {LEG_VOLUME * factor}")
        print("check_hierarchy: OK (legs are solids of the right volume, through scaled, mirrored and "
              "sheared placements; single faces stay faces; colours follow their faces)")
    finally:
        App.closeDocument(doc.Name)


def check_merged(path):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        # the 4 + 4 table legs, then the scaled (3x volume), mirrored and sheared ones
        assert len(solids) == 11, f"{len(solids)} solids"
        total = sum(abs(s.Volume) for s in solids)
        expected = LEG_VOLUME * (8 + 3 + 1 + 1)
        assert _close(total, expected, 1e-5), (total, expected)
        print(f"check_merged: OK ({len(solids)} solids, total volume {total:.1f} mm3)")
    finally:
        App.closeDocument(doc.Name)


def check_open_box():
    """Five sides of a box close nothing: they stay five faces."""
    import openskp

    b = openskp.create()
    with b.add_component_definition("OpenBox") as box:
        x, y, z = 10, 10, 10
        box.add_face([(0, 0, 0), (0, y, 0), (x, y, 0), (x, 0, 0)])
        box.add_face([(0, 0, 0), (x, 0, 0), (x, 0, z), (0, 0, z)])
        box.add_face([(0, y, 0), (0, y, z), (x, y, z), (x, y, 0)])
        box.add_face([(0, 0, 0), (0, 0, z), (0, y, z), (0, y, 0)])
        box.add_face([(x, 0, 0), (x, y, 0), (x, y, z), (x, 0, z)])
    b.add_instance(box)
    path = os.path.join(FIXTURES_DIR, "_open_box_tmp.skp")
    with open(path, "wb") as f:
        f.write(b.to_bytes())
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, solids=True)
        stats = importSKP.last_import_stats
        assert stats["solids_built"] == 0 and stats["faces_left_open"] == 5, stats
        print("check_open_box: OK (5 faces, no solid)")
    finally:
        App.closeDocument(doc.Name)
        os.remove(path)


def check_test_component():
    """tests/test_nonplanar.py sews this fixture's 124 faces into one closed solid of
    19006.649617 mm3; the option gives that solid."""
    doc = App.newDocument()
    try:
        importSKP.import_skp(os.path.join(FIXTURES_DIR, "Test_Component.skp"), doc, merge=True, solids=True)
        solids = [s for o in doc.Objects if hasattr(o, "Shape") for s in o.Shape.Solids]
        assert len(solids) == 1, f"{len(solids)} solids"
        assert abs(solids[0].Volume - 19006.649617) < 1e-3, solids[0].Volume
        assert solids[0].isValid()
        print(f"check_test_component: OK (one solid, {solids[0].Volume:.6f} mm3)")
    finally:
        App.closeDocument(doc.Name)


def _faces_signature(path, solids):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True, solids=solids)
        faces = [f for o in doc.Objects if hasattr(o, "Shape") for f in o.Shape.Faces]
        points = sorted({(round(v.X, 3), round(v.Y, 3), round(v.Z, 3)) for f in faces for v in f.Vertexes})
        return len(faces), round(sum(f.Area for f in faces), 3), points
    finally:
        App.closeDocument(doc.Name)


def check_same_faces(path):
    plain, solid = _faces_signature(path, False), _faces_signature(path, True)
    name = os.path.basename(path)
    assert plain[0] == solid[0], f"{name}: {solid[0]} faces with solids, {plain[0]} without"
    assert _close(plain[1], solid[1]), f"{name}: area {solid[1]} with solids, {plain[1]} without"
    assert plain[2] == solid[2], f"{name}: the vertices differ"
    print(f"check_same_faces({name}): OK ({plain[0]} faces either way)")


def check_preference(path):
    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for value, expected in ((None, False), (True, True), (False, False)):
            if value is None:
                params.RemBool("MakeSolids")
            else:
                params.SetBool("MakeSolids", value)
            doc = App.newDocument()
            try:
                importSKP.import_skp(path, doc, merge=False)
                assert importSKP.last_import_stats["make_solids"] is expected, value
            finally:
                App.closeDocument(doc.Name)
        print("check_preference: OK (unset -> faces, True -> solids)")
    finally:
        params.RemBool("MakeSolids")


def _main():
    synthetic = os.path.join(FIXTURES_DIR, "_solids_tmp.skp")
    write_synthetic(synthetic)
    try:
        check_hierarchy(synthetic)
        check_merged(synthetic)
        check_preference(synthetic)
    finally:
        os.remove(synthetic)
    check_open_box()
    check_test_component()
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_same_faces(os.path.join(FIXTURES_DIR, name))


# freecadcmd runs this file as a module named after it, not "__main__", and exits 0
# when it raises; see tests/_harness.py.
_harness.run(_main)
