"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_export.py

Round-trips real FreeCAD geometry (a face with a hole, plus a
translated box) through export() and back through openskp's own
Python reader, checking structure (loop counts) and exact coordinates
survive the mm<->inch conversion. Also checks layer (SketchUp "tag")
export: each object's own Label becomes its exported layer.

Material export reads obj.ViewObject.DiffuseColor, which doesn't exist
at all under headless freecadcmd (same limitation materials-import
already has - see check_face_colors() in test_import.py) - so
test_material_export() below exercises the real dedup/registration
logic (_register_face_materials/_material_key/_material_name) against
a lightweight stand-in object carrying a fake ViewObject with real
DiffuseColor data, rather than skipping material export from this
suite entirely. The full path (does the real GUI's own
ViewObject.DiffuseColor actually reach export() this same way) was
verified once directly in a real interactive FreeCAD session - see the
README.
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

    # Layer export: obj1 ("FaceWithHole", 1 face) and obj2 ("Box", 6
    # faces) have different Labels, so must land in 2 distinct Face.layer
    # groups of the right size - not just "some layer got written."
    layer_names = sorted(l.name for l in model.layers)
    assert layer_names == ["Box", "FaceWithHole", "Layer0"], layer_names
    by_layer = {}
    for f in model.root.faces.values():
        by_layer.setdefault(f.layer, 0)
        by_layer[f.layer] += 1
    assert len(by_layer) == 2, f"expected 2 distinct Face.layer groups (one per object), got {by_layer}"
    assert sorted(by_layer.values()) == [1, 6], by_layer

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

    print(
        "export round-trip: OK (7 faces, loop counts", loop_counts,
        ", coordinates exact, layers", layer_names, ")"
    )


class _FakeViewObject:
    def __init__(self, diffuse_color):
        self.DiffuseColor = diffuse_color


class _FakeObjWithColors:
    """A minimal stand-in for a real FreeCAD DocumentObject - export()
    only ever touches .Shape/.ViewObject/.Label/.Placement (and
    hasattr()-checks for .getGlobalPlacement, deliberately absent here
    to exercise that fallback too), so a plain object with just those
    four attributes exercises the exact same code real export() runs
    against a document object, without needing a real GUI session to
    get a real ViewObject from."""

    def __init__(self, shape, diffuse_color, label):
        self.Shape = shape
        self.ViewObject = _FakeViewObject(diffuse_color)
        self.Label = label
        self.Placement = App.Placement()


def test_material_export():
    """Regression test for material export: a Shape's own per-face
    ViewObject.DiffuseColor becomes a SketchUp material, deduplicated
    across faces AND across objects (the same color reused registers
    once). Runs under App.GuiUp forced True with a fake ViewObject
    (see module docstring for why headless freecadcmd needs this)."""
    orig_gui_up = App.GuiUp
    App.GuiUp = True
    try:
        red = (1.0, 0.0, 0.0, 1.0)
        blue_translucent = (0.0, 0.0, 1.0, 0.5)

        box1 = Part.makeBox(10, 10, 10)
        obj1 = _FakeObjWithColors(box1, [red] * len(box1.Faces), "RedBox")

        box2 = Part.makeBox(10, 10, 10)
        box2.translate(App.Vector(50, 0, 0))
        colors2 = [red, red, red, blue_translucent, blue_translucent, blue_translucent]
        assert len(colors2) == len(box2.Faces)
        obj2 = _FakeObjWithColors(box2, colors2, "MixedBox")

        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_material_export_test.skp")
        importSKP.export([obj1, obj2], out_path)

        import openskp

        model = openskp.SkpFile.open(out_path).parse()
        os.remove(out_path)

        materials_by_name = {m.name: m for m in model.materials}
        assert len(materials_by_name) == 2, f"expected 2 distinct materials (red reused), got {materials_by_name}"

        red_mats = [m for m in model.materials if m.color[:3] == (255, 0, 0)]
        assert len(red_mats) == 1, red_mats
        assert red_mats[0].transparency == 1.0, red_mats[0].transparency

        blue_mats = [m for m in model.materials if m.color[:3] == (0, 0, 255)]
        assert len(blue_mats) == 1, blue_mats
        assert abs(blue_mats[0].transparency - 0.5) < 1e-3, blue_mats[0].transparency

        by_material = {}
        for f in model.root.faces.values():
            by_material.setdefault(f.material_id, 0)
            by_material[f.material_id] += 1
        assert len(by_material) == 2, f"expected 2 distinct Face.material_id groups, got {by_material}"
        assert sorted(by_material.values()) == [3, 9], (
            f"expected 9 red faces (6 from RedBox + 3 from MixedBox) and 3 blue, got {by_material}"
        )

        print("test_material_export: OK (2 distinct materials, red reused across objects, correctly split)")
    finally:
        App.GuiUp = orig_gui_up


# Note: freecadcmd runs a script file as a module named after the file,
# not "__main__" - see tests/test_import.py's own note on this.
run()
test_material_export()
