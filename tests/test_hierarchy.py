"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_hierarchy.py

The component hierarchy import (#5): one App::Part per component definition,
an App::Link per placement, one group per SketchUp tag at the top level, and
the merged layout still available with merge=True or the MergeOnImport
preference.

Three kinds of check:

- Structure, on a synthetic model written with openskp's own writer whose
  every expected object follows from how the test builds it (check_structure).
- Placement, on the same model: FreeCAD's own Part.getShape() of each link must
  land where the instance matrix says, including non-uniform scale and a
  mirror (check_link_placements), so the links mean what SketchUp meant
  independently of this add-on's own export walker.
- Equivalence, on the synthetic model and every real fixture: the geometry the
  hierarchy shows (followed through its links) must be the geometry the merged
  import builds - the same faces, area and vertices (check_same_geometry).
  Then the hierarchy exports to the same faces and tags (check_export).
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

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MM = importSKP.INCH_TO_MM

from _synthetic import LEG_SIZE, SHEARED, TOP_LEVEL_LEGS, write_synthetic  # noqa: E402

def _world_shapes(doc, stats):
    """Everything the hierarchy shows, in world space: the model part followed
    down through its tag groups and links (the export walker), each leaf's
    shape mapped through its transform."""
    model_part = doc.getObject(stats["model_part"])
    return [shape.transformGeometry(transform)
            for _obj, shape, transform, _layer in importSKP._export_leaves([model_part])]


def _merged_shapes(path):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True)
        return [o.Shape.copy() for o in doc.Objects if hasattr(o, "Shape")]
    finally:
        App.closeDocument(doc.Name)


def _signature(shapes):
    """Faces, their total area, every vertex, edges and their total length:
    the last two cover loose edges and arcs, which have no face."""
    faces = [f for s in shapes for f in s.Faces]
    edges = [e for s in shapes for e in s.Edges]
    points = sorted({(round(v.X, 3), round(v.Y, 3), round(v.Z, 3)) for s in shapes for v in s.Vertexes})
    return (len(faces), round(sum(f.Area for f in faces), 3), points,
            len(edges), round(sum(e.Length for e in edges), 3))


def check_same_geometry(path):
    """The hierarchy shows exactly the merged import's geometry."""
    merged = _signature(_merged_shapes(path))
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats
        assert not stats["merged"], stats
        got = _signature(_world_shapes(doc, stats))
    finally:
        App.closeDocument(doc.Name)
    name = os.path.basename(path)
    assert got[0] == merged[0], f"{name}: hierarchy shows {got[0]} faces, merged import has {merged[0]}"
    assert abs(got[1] - merged[1]) <= 1e-6 * max(1.0, merged[1]), f"{name}: area {got[1]} vs merged {merged[1]}"
    assert got[2] == merged[2], (
        f"{name}: vertex sets differ; only in hierarchy: {sorted(set(got[2]) - set(merged[2]))[:5]}, "
        f"only in merged: {sorted(set(merged[2]) - set(got[2]))[:5]}"
    )
    assert got[3] == merged[3], f"{name}: hierarchy shows {got[3]} edges, merged import has {merged[3]}"
    assert abs(got[4] - merged[4]) <= 1e-6 * max(1.0, merged[4]), f"{name}: edge length {got[4]} vs merged {merged[4]}"
    print(f"check_same_geometry({name}): OK ({got[0]} faces, area {got[1]} mm2, {len(got[2])} vertices, "
          f"{got[3]} edges, {got[4]} mm of edges)")


def check_structure(path):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats

        assert stats["components"] == 5, f"components {stats['components']} (Leg, Leg (Red), Table, Table (Red), Spare)"
        assert stats["unplaced_components"] == 1, stats["unplaced_components"]
        assert stats["links"] == 12, f"links {stats['links']} (4 + 4 legs in the tables, 2 tables, 2 legs)"
        assert stats["sheared_copies"] == 1, stats["sheared_copies"]

        components = doc.getObject(stats["components_group"])
        assert components.SketchUpDefinitions and not components.Visibility
        labels = sorted(o.Label for o in components.Group)
        assert labels == ["Leg", "Leg (Red)", "Spare", "Table", "Table (Red)"], labels
        for part in components.Group:
            assert part.TypeId == "App::Part", part.TypeId
            assert not part.Visibility, f"{part.Label} is visible at the origin"

        model_part = doc.getObject(stats["model_part"])
        assert model_part.TypeId == "App::Part" and model_part.Label == os.path.splitext(os.path.basename(path))[0]
        tags = {o.Label: o for o in model_part.Group if o.TypeId == "App::DocumentObjectGroup"}
        assert sorted(tags) == ["Closed", "Layer0", "Open"], sorted(tags)
        assert not tags["Open"].Visibility and tags["Closed"].Visibility, "tag Open is hidden in SketchUp"
        assert all(not o.Visibility for o in tags["Open"].Group), "the hidden tag's content must be hidden"
        assert stats["hidden_layers"] == 1, stats["hidden_layers"]
        assert {o.SketchUpLayer for o in tags.values()} == {"Closed", "Layer0", "Open"}

        def link(label, group):
            found = [o for o in group.Group if o.Label == label]
            assert len(found) == 1, f"{label!r} in {group.Label}: {[o.Label for o in group.Group]}"
            return found[0]

        closed_table = link("Table closed", tags["Closed"])
        open_table = link("Table open", tags["Open"])
        assert closed_table.TypeId == "App::Link" and closed_table.LinkedObject.Label == "Table"
        assert open_table.LinkedObject.Label == "Table (Red)", "a painted instance links the painted variant"
        assert closed_table.SketchUpLayer == "Closed" and open_table.SketchUpLayer == "Open"
        legs_in_table = [o for o in closed_table.LinkedObject.Group if o.TypeId == "App::Link"]
        assert sorted(o.Label for o in legs_in_table) == ["Leg 1", "Leg 2", "Leg 3", "Leg 4"]
        assert {o.LinkedObject.Label for o in legs_in_table} == {"Leg"}
        red_legs = [o for o in open_table.LinkedObject.Group if o.TypeId == "App::Link"]
        assert {o.LinkedObject.Label for o in red_legs} == {"Leg (Red)"}

        sheared = link("Sheared leg (sheared copy)", tags["Layer0"])
        assert sheared.TypeId == "Part::Feature" and len(sheared.Shape.Faces) == 6

        # Paint: the unpainted leg faces show the paint of the instance that places
        # them; the table's own blue top keeps its blue in both variants.
        colors = stats["object_face_colors"]

        def geometry_colors(part_label):
            part = [o for o in components.Group if o.Label == part_label][0]
            geometry = [o for o in part.Group if o.TypeId == "Part::Feature"]
            assert len(geometry) == 1, f"{part_label}: {[o.Label for o in part.Group]}"
            return {tuple(round(c, 3) for c in rgba) for rgba in colors[geometry[0].Name]}

        red = (round(200 / 255, 3), round(30 / 255, 3), round(30 / 255, 3), 1.0)
        blue = (round(30 / 255, 3), round(30 / 255, 3), round(200 / 255, 3), 1.0)
        grey = tuple(round(c, 3) for c in importSKP._DEFAULT_FACE_RGBA)
        assert geometry_colors("Leg (Red)") == {red}, geometry_colors("Leg (Red)")
        assert geometry_colors("Leg") == {grey}, geometry_colors("Leg")
        assert geometry_colors("Table") == {blue} and geometry_colors("Table (Red)") == {blue}
        print("check_structure: OK (5 components incl. 1 unplaced and 2 paint variants, 12 links, "
              "1 sheared copy, tags Closed/Layer0/Open with Open hidden)")
    finally:
        App.closeDocument(doc.Name)


def _expected_bbox_mm(m, t, size):
    corners = [(x, y, z) for x in (0, size[0]) for y in (0, size[1]) for z in (0, size[2])]
    pts = [(
        m[0] * x + m[1] * y + m[2] * z + t[0],
        m[3] * x + m[4] * y + m[5] * z + t[1],
        m[6] * x + m[7] * y + m[8] * z + t[2],
    ) for x, y, z in corners]
    xs, ys, zs = zip(*pts)
    return (min(xs) * MM, max(xs) * MM, min(ys) * MM, max(ys) * MM, min(zs) * MM, max(zs) * MM)


def check_link_placements(path):
    """FreeCAD's own view of each top-level leg link (Part.getShape follows the
    link's Placement and ScaleVector) must be where the instance matrix puts
    it, so a link means in FreeCAD what the instance meant in SketchUp."""
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats
        model_part = doc.getObject(stats["model_part"])
        layer0 = [o for o in model_part.Group if o.Label == "Layer0"][0]
        for name, (m, t) in TOP_LEVEL_LEGS.items():
            obj = [o for o in layer0.Group if o.Label == name][0]
            assert obj.TypeId == "App::Link", f"{name} is a {obj.TypeId}, expected a link"
            shape = Part.getShape(obj)
            assert not shape.isNull() and len(shape.Faces) == 6, f"{name}: {len(shape.Faces)} faces through the link"
            box = shape.BoundBox
            got = (box.XMin, box.XMax, box.YMin, box.YMax, box.ZMin, box.ZMax)
            want = _expected_bbox_mm(m, t, LEG_SIZE)
            assert all(abs(g - w) < 1e-4 for g, w in zip(got, want)), f"{name}: link spans {got} mm, expected {want}"
            print(f"check_link_placements: {name} OK (ScaleVector {tuple(round(v, 3) for v in obj.ScaleVector)})")
        sheared = [o for o in layer0.Group if o.Label == "Sheared leg (sheared copy)"][0]
        box = sheared.Shape.BoundBox
        want = _expected_bbox_mm(SHEARED[0], SHEARED[1], LEG_SIZE)
        got = (box.XMin, box.XMax, box.YMin, box.YMax, box.ZMin, box.ZMax)
        assert all(abs(g - w) < 1e-4 for g, w in zip(got, want)), f"sheared copy spans {got} mm, expected {want}"
        print("check_link_placements: sheared copy OK")
    finally:
        App.closeDocument(doc.Name)


def check_export(path):
    """Exporting the imported hierarchy writes the geometry it shows, with the
    tags the import recorded, and nothing from the hidden definitions."""
    import openskp

    doc = App.newDocument()
    out_path = os.path.join(FIXTURES_DIR, "_hierarchy_export_tmp.skp")
    try:
        importSKP.import_skp(path, doc, merge=False)
        # Selecting the model part and everything in it (as Ctrl+A would) must not
        # write anything twice, and the definitions group must add nothing.
        importSKP.export(list(doc.Objects), out_path)
        model = openskp.SkpFile.open(out_path).parse()
        by_tag = {}
        for face in model.root.faces.values():
            by_tag[face.layer] = by_tag.get(face.layer, 0) + 1
        total = sum(by_tag.values())
        assert total == 69, f"exported {total} faces, expected 69 (1 + 25 + 25 + 3 x 6): {by_tag}"
        assert sorted(layer.name for layer in model.layers) == ["Closed", "Layer0", "Open"], [
            layer.name for layer in model.layers]
        assert sorted(by_tag.values()) == [19, 25, 25], by_tag
        print(f"check_export: OK ({total} faces, tags {sorted(layer.name for layer in model.layers)}, "
              f"{sorted(by_tag.values())} faces per tag)")
    finally:
        App.closeDocument(doc.Name)
        if os.path.exists(out_path):
            os.remove(out_path)


def check_merge_preference(path):
    """merge=None follows the MergeOnImport preference; unset means hierarchy."""
    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for value, merged in ((None, False), (True, True), (False, False)):
            if value is None:
                params.RemBool("MergeOnImport")
            else:
                params.SetBool("MergeOnImport", value)
            doc = App.newDocument()
            try:
                importSKP.import_skp(path, doc)
                assert importSKP.last_import_stats["merged"] is merged, (value, importSKP.last_import_stats["merged"])
                parts = [o for o in doc.Objects if o.TypeId == "App::Part"]
                assert bool(parts) is not merged, (value, [o.TypeId for o in doc.Objects])
            finally:
                App.closeDocument(doc.Name)
        print("check_merge_preference: OK (unset -> hierarchy, True -> merged, False -> hierarchy)")
    finally:
        params.RemBool("MergeOnImport")


def _main():
    synthetic = os.path.join(FIXTURES_DIR, "_hierarchy_tmp.skp")
    write_synthetic(synthetic)
    try:
        check_structure(synthetic)
        check_link_placements(synthetic)
        check_same_geometry(synthetic)
        check_export(synthetic)
        check_merge_preference(synthetic)
    finally:
        os.remove(synthetic)
    checked = 0
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_same_geometry(os.path.join(FIXTURES_DIR, name))
            checked += 1
    assert checked >= 7, f"only {checked} fixtures checked"


# freecadcmd runs this file as a module named after it, not "__main__", and exits 0
# when it raises; see tests/_harness.py.
_harness.run(_main)
