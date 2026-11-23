"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_assemblies.py

The ImportAssemblies import option (#22): in the component hierarchy, every
definition that contains other components or groups becomes a FreeCAD 1.x
Assembly::AssemblyObject (with an empty Assembly::JointGroup, since SketchUp
stores no joints), while leaf definitions with only their own geometry stay
App::Part. Without FreeCAD's Assembly module, the import falls back to
App::Part. Export follows Assembly::AssemblyObject containers and skips their
non-geometry metadata groups (Joints, Views, BOM, Simulations).
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


def _world_shapes(doc, stats):
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
    faces = [f for s in shapes for f in s.Faces]
    edges = [e for s in shapes for e in s.Edges]
    points = sorted({(round(v.X, 3), round(v.Y, 3), round(v.Z, 3)) for s in shapes for v in s.Vertexes})
    return (len(faces), round(sum(f.Area for f in faces), 3), points,
            len(edges), round(sum(e.Length for e in edges), 3))


def check_structure(path):
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        assert stats["import_assemblies"] is True, stats
        assert stats["components"] == 5, stats
        assert stats["assemblies"] == 2, f"expected 2 assemblies (Table, Table (Red)), got {stats['assemblies']}"
        assert stats["unplaced_components"] == 1, stats
        assert stats["links"] == 12, stats
        assert stats["sheared_copies"] == 1, stats

        components = doc.getObject(stats["components_group"])
        by_label = {o.Label: o for o in components.Group}
        assert sorted(by_label) == ["Leg", "Leg (Red)", "Spare", "Table", "Table (Red)"], sorted(by_label)

        # Definitions with nested instances become Assembly::AssemblyObject; leaf parts stay App::Part.
        for label in ("Table", "Table (Red)"):
            asm = by_label[label]
            assert asm.TypeId == "Assembly::AssemblyObject", (label, asm.TypeId)
            assert asm.Type == "Assembly", (label, asm.Type)
            assert not asm.Visibility, f"{label} is visible at the origin"
            joint_groups = [o for o in asm.Group if o.TypeId == "Assembly::JointGroup"]
            assert len(joint_groups) == 1 and len(joint_groups[0].Group) == 0, (label, len(joint_groups))
            geom = [o for o in asm.Group if o.TypeId == "Part::Feature"]
            assert len(geom) == 1 and len(geom[0].Shape.Faces) == 1, (label, len(geom))
            links = [o for o in asm.Group if o.TypeId == "App::Link"]
            assert len(links) == 4, (label, [o.Label for o in links])
            if label == "Table":
                assert sorted(o.Label for o in links) == ["Leg 1", "Leg 2", "Leg 3", "Leg 4"], [
                    o.Label for o in links
                ]
            expected_leg = "Leg" if label == "Table" else "Leg (Red)"
            assert {o.LinkedObject.Label for o in links} == {expected_leg}, [o.LinkedObject.Label for o in links]

            # Solving an ungrounded imported assembly leaves every link's placement untouched.
            before = [o.Placement.copy() for o in links]
            asm.solve()
            doc.recompute()
            after = [o.Placement for o in links]
            assert all(a.isSame(b, 1e-9) for a, b in zip(before, after)), f"{label} solve moved placements"

        for label in ("Leg", "Leg (Red)", "Spare"):
            part = by_label[label]
            assert part.TypeId == "App::Part", (label, part.TypeId)
            assert not part.Visibility, f"{label} is visible at the origin"

        # Top-level model container stays App::Part holding tag groups, and links to
        # Assembly::AssemblyObject targets resolve their full geometry through Part.getShape.
        model_part = doc.getObject(stats["model_part"])
        assert model_part.TypeId == "App::Part", model_part.TypeId
        closed_group = [o for o in model_part.Group if o.Label == "Closed"][0]
        closed_table = [o for o in closed_group.Group if o.Label == "Table closed"][0]
        assert closed_table.TypeId == "App::Link" and closed_table.LinkedObject == by_label["Table"]
        table_shape = Part.getShape(closed_table)
        assert len(table_shape.Faces) == 25, len(table_shape.Faces)
        box = table_shape.BoundBox
        got = (box.XMin, box.XMax, box.YMin, box.YMax, box.ZMin, box.ZMax)
        want = (0.0, 40.0 * MM, 0.0, 20.0 * MM, 0.0, 30.0 * MM)
        assert all(abs(g - w) < 1e-4 for g, w in zip(got, want)), (got, want)

        print("check_structure: OK (Table and Table (Red) are Assembly::AssemblyObject, "
              "Leg/Leg (Red)/Spare stay App::Part, placements preserved across solve())")
    finally:
        App.closeDocument(doc.Name)


def check_nested_assemblies():
    """Three-level nesting (Room -> Table -> Leg) plus a leaf (Chair) and an
    unplaced sub-assembly (SpareBench -> Leg)."""
    import openskp

    b = openskp.create()
    with b.add_component_definition("Leg") as leg:
        leg.add_face([(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)])
    with b.add_component_definition("Table") as table:
        table.add_face([(0, 0, 30), (40, 0, 30), (40, 20, 30), (0, 20, 30)])
        table.add_instance(leg, name="Leg 1", translation=(0, 0, 0))
        table.add_instance(leg, name="Leg 2", translation=(38, 18, 0))
    with b.add_component_definition("Chair") as chair:
        chair.add_face([(0, 0, 18), (16, 0, 18), (16, 16, 18), (0, 16, 18)])
    with b.add_component_definition("Room") as room:
        room.add_instance(table, name="Dining table", translation=(10, 10, 0))
        room.add_instance(chair, name="Dining chair", translation=(15, -20, 0))
    with b.add_component_definition("SpareBench") as bench:
        bench.add_instance(leg, name="Bench leg", translation=(0, 0, 0))
    b.add_instance(room, name="Main room")

    path = os.path.join(FIXTURES_DIR, "_nested_assemblies_tmp.skp")
    with open(path, "wb") as f:
        f.write(b.to_bytes())
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        assert stats["components"] == 5 and stats["assemblies"] == 3, stats
        components = doc.getObject(stats["components_group"])
        by_label = {o.Label: o for o in components.Group}
        for label in ("Room", "Table", "SpareBench"):
            assert by_label[label].TypeId == "Assembly::AssemblyObject", (label, by_label[label].TypeId)
        for label in ("Leg", "Chair"):
            assert by_label[label].TypeId == "App::Part", (label, by_label[label].TypeId)
        assert _signature(_world_shapes(doc, stats)) == _signature(_merged_shapes(path))
        print("check_nested_assemblies: OK (Room, Table, SpareBench are assemblies; Leg, Chair stay App::Part)")
    finally:
        App.closeDocument(doc.Name)
        if os.path.exists(path):
            os.remove(path)


def check_with_solids(path):
    """ImportAssemblies and MakeSolids compose: Table is an Assembly whose Leg
    links target solid leaf App::Parts."""
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, solids=True, assemblies=True)
        stats = importSKP.last_import_stats
        assert stats["make_solids"] and stats["import_assemblies"], stats
        assert stats["assemblies"] == 2 and stats["solids_built"] == 2, stats
        components = doc.getObject(stats["components_group"])
        by_label = {o.Label: o for o in components.Group}
        assert by_label["Table"].TypeId == "Assembly::AssemblyObject"
        assert by_label["Leg"].TypeId == "App::Part"
        leg_geom = [o for o in by_label["Leg"].Group if o.TypeId == "Part::Feature"][0]
        assert len(leg_geom.Shape.Solids) == 1 and abs(leg_geom.Shape.Volume - LEG_VOLUME) < 1e-3
        print("check_with_solids: OK (assemblies link solid leaf parts)")
    finally:
        App.closeDocument(doc.Name)


def check_export(path):
    """Exporting an imported assembly hierarchy preserves component definitions
    and instances while skipping both the hidden definitions group and any
    joints inside Joints (#22, #28)."""
    import JointObject
    import openskp

    doc = App.newDocument()
    out_path = os.path.join(FIXTURES_DIR, "_assemblies_export_tmp.skp")
    try:
        importSKP.import_skp(path, doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        components = doc.getObject(stats["components_group"])
        table = [o for o in components.Group if o.Label == "Table"][0]
        joint_group = [o for o in table.Group if o.TypeId == "Assembly::JointGroup"][0]
        leg1 = [o for o in table.Group if o.Label == "Leg 1"][0]

        # Add a real grounded joint inside Table's JointGroup to verify export skips joints.
        grounded = joint_group.newObject("App::FeaturePython", "GroundedJoint")
        JointObject.GroundedJoint(grounded, leg1)
        doc.recompute()

        importSKP.export(list(doc.Objects), out_path)
        model = openskp.SkpFile.open(out_path).parse()
        defs_by_name = {d.name: d for d in model.definitions.values() if d is not model.root}
        assert sorted(defs_by_name) == ["Leg", "Leg (Red)", "Table", "Table (Red)"], sorted(defs_by_name)
        assert len(model.root.instances) == 4 and len(model.root.faces) == 7
        assert sorted(layer.name for layer in model.layers) == ["Closed", "Layer0", "Open"]

        re_doc = App.newDocument()
        try:
            importSKP.import_skp(out_path, re_doc, merge=True)
            per_tag = sorted(len(o.Shape.Faces) for o in re_doc.Objects if hasattr(o, "Shape"))
            assert sum(per_tag) == 69 and per_tag == [19, 25, 25], per_tag
        finally:
            App.closeDocument(re_doc.Name)
        print("check_export: OK (4 definitions + 4 root instances -> 69 placed faces across Closed/Layer0/Open, joints skipped)")
    finally:
        App.closeDocument(doc.Name)
        if os.path.exists(out_path):
            os.remove(out_path)


def check_export_standalone_assembly():
    """Exporting a top-level FreeCAD Assembly::AssemblyObject directly exports
    its linked part as a component definition, places its links with the
    assembly + link transforms, and skips its JointGroup."""
    import AssemblyApp  # noqa: F401
    import openskp

    doc = App.newDocument()
    out_path = os.path.join(FIXTURES_DIR, "_standalone_assembly_export_tmp.skp")
    try:
        leaf = doc.addObject("App::Part", "LeafPart")
        box = doc.addObject("Part::Feature", "Box")
        box.Shape = Part.makeBox(25.4, 25.4, 25.4)
        leaf.addObject(box)
        leaf.Visibility = False

        asm = doc.addObject("Assembly::AssemblyObject", "TopAssembly")
        asm.Label = "TopAssembly"
        asm.Type = "Assembly"
        asm.newObject("Assembly::JointGroup", "Joints")
        asm.Placement = App.Placement(App.Vector(254.0, 0.0, 0.0), App.Rotation())

        link1 = doc.addObject("App::Link", "Link1")
        link1.LinkedObject = leaf
        asm.addObject(link1)

        link2 = doc.addObject("App::Link", "Link2")
        link2.LinkedObject = leaf
        link2.Placement = App.Placement(App.Vector(0.0, 50.8, 0.0), App.Rotation())
        asm.addObject(link2)
        doc.recompute()

        importSKP.export([asm], out_path)
        model = openskp.SkpFile.open(out_path).parse()
        defs = [d for d in model.definitions.values() if d is not model.root]
        assert len(defs) == 1 and defs[0].name == "LeafPart" and len(defs[0].faces) == 6, defs
        assert len(model.root.instances) == 2 and len(model.root.faces) == 0
        translations = sorted((round(i.matrix[9], 3), round(i.matrix[10], 3), round(i.matrix[11], 3)) for i in model.root.instances)
        assert translations == [(10.0, 0.0, 0.0), (10.0, 2.0, 0.0)], translations

        re_doc = App.newDocument()
        try:
            importSKP.import_skp(out_path, re_doc, merge=True)
            shapes = [o.Shape for o in re_doc.Objects if hasattr(o, "Shape")]
            assert sum(len(s.Faces) for s in shapes) == 12
            xs = {round(v.X / MM, 3) for s in shapes for v in s.Vertexes}
            ys = {round(v.Y / MM, 3) for s in shapes for v in s.Vertexes}
            assert xs == {10.0, 11.0}, xs
            assert ys == {0.0, 1.0, 2.0, 3.0}, ys
        finally:
            App.closeDocument(re_doc.Name)
        print("check_export_standalone_assembly: OK (1 definition + 2 instances at exact assembly + link coordinates)")
    finally:
        App.closeDocument(doc.Name)
        if os.path.exists(out_path):
            os.remove(out_path)


def check_preference(path):
    """assemblies=None follows ImportAssemblies; explicit argument overrides it."""
    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for pref, arg, want_flag, want_count in (
            (None, None, False, 0),
            (True, None, True, 2),
            (False, None, False, 0),
            (True, False, False, 0),
            (False, True, True, 2),
        ):
            if pref is None:
                params.RemBool("ImportAssemblies")
            else:
                params.SetBool("ImportAssemblies", pref)
            doc = App.newDocument()
            try:
                importSKP.import_skp(path, doc, merge=False, assemblies=arg)
                stats = importSKP.last_import_stats
                assert stats["import_assemblies"] is want_flag, (pref, arg, stats)
                assert stats["assemblies"] == want_count, (pref, arg, stats)
            finally:
                App.closeDocument(doc.Name)
        print("check_preference: OK (unset -> False, True -> 2 assemblies, explicit arg overrides)")
    finally:
        params.RemBool("ImportAssemblies")


def check_fallback_without_assembly_module(path):
    """When FreeCAD's Assembly module cannot be imported, assemblies=True falls
    back cleanly to App::Part with unchanged geometry."""
    saved = sys.modules.get("AssemblyApp", ...)
    sys.modules["AssemblyApp"] = None
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        assert stats["import_assemblies"] is True, stats
        assert stats["assemblies"] == 0 and stats["components"] == 5, stats
        components = doc.getObject(stats["components_group"])
        assert all(o.TypeId == "App::Part" for o in components.Group), [o.TypeId for o in components.Group]
        assert len([f for s in _world_shapes(doc, stats) for f in s.Faces]) == 69
        print("check_fallback_without_assembly_module: OK (all 5 definitions fall back to App::Part)")
    finally:
        App.closeDocument(doc.Name)
        if saved is ...:
            sys.modules.pop("AssemblyApp", None)
        else:
            sys.modules["AssemblyApp"] = saved


def check_same_geometry(path):
    """Importing with assemblies=True shows the exact geometry of the merged import."""
    merged = _signature(_merged_shapes(path))
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        got = _signature(_world_shapes(doc, stats))
    finally:
        App.closeDocument(doc.Name)
    name = os.path.basename(path)
    assert got[0] == merged[0], f"{name}: {got[0]} faces vs merged {merged[0]}"
    assert abs(got[1] - merged[1]) <= 1e-6 * max(1.0, merged[1]), f"{name}: area {got[1]} vs {merged[1]}"
    assert got[2] == merged[2], f"{name}: vertex sets differ"
    assert got[3] == merged[3], f"{name}: {got[3]} edges vs merged {merged[3]}"
    assert abs(got[4] - merged[4]) <= 1e-6 * max(1.0, merged[4]), f"{name}: edge length {got[4]} vs {merged[4]}"
    print(f"check_same_geometry({name}): OK ({got[0]} faces, {stats['assemblies']} assemblies)")


def _main():
    synthetic = os.path.join(FIXTURES_DIR, "_assemblies_tmp.skp")
    write_synthetic(synthetic)
    try:
        check_structure(synthetic)
        check_nested_assemblies()
        check_with_solids(synthetic)
        check_export(synthetic)
        check_export_standalone_assembly()
        check_preference(synthetic)
        check_fallback_without_assembly_module(synthetic)
        check_same_geometry(synthetic)
    finally:
        if os.path.exists(synthetic):
            os.remove(synthetic)
    checked = 0
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_same_geometry(os.path.join(FIXTURES_DIR, name))
            checked += 1
    assert checked >= 7, f"only {checked} fixtures checked"


_harness.run(_main)
