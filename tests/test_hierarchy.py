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


def _save_export_sample(src_path, out_name):
    out_dir = os.environ.get("SKP_EXPORT_OUT")
    if out_dir:
        import shutil

        os.makedirs(out_dir, exist_ok=True)
        shutil.copyfile(src_path, os.path.join(out_dir, out_name))


def _face_signature(shapes):
    """Face count, total face area, and face-boundary vertex set (excluding
    loose-edge wires, which export does not write)."""
    faces = [f for s in shapes for f in s.Faces]
    points = sorted({(round(v.X, 2), round(v.Y, 2), round(v.Z, 2)) for f in faces for v in f.Vertexes})
    return len(faces), round(sum(f.Area for f in faces), 2), points


def _flat_export_file(export_list, filename):
    """Writes ``export_list`` flattened through ``_export_leaves`` (the pre-#28
    flat export) so tests can check exact geometry parity against it."""
    import openskp

    b = openskp.create()
    leaves = list(importSKP._export_leaves(export_list))
    layers = {}
    for _obj, _shape, _tr, layer in leaves:
        if layer and layer != "Layer0" and layer not in layers:
            layers[layer] = b.add_layer(layer)
    stats = {"faces_written": 0, "faces_skipped": 0}
    for _obj, shape, tr, layer in leaves:
        importSKP._add_shape_faces(b, shape, tr, stats, layers.get(layer), None)
    with open(filename, "wb") as f:
        f.write(b.to_bytes())


def _fold_m13(m13):
    s = m13[12] if len(m13) > 12 else 1.0
    return tuple(round(v * s, 6) for v in m13[:12])


def check_export(path):
    """Exporting the imported hierarchy preserves component definitions and
    instances (including nested links, non-uniform scale, mirroring and tags),
    keeps sheared copies and root faces as loose geometry, omits unplaced
    definitions, and matches the flat export's world geometry (#28)."""
    import openskp

    doc = App.newDocument()
    out_path = os.path.join(FIXTURES_DIR, "_hierarchy_export_tmp.skp")
    try:
        importSKP.import_skp(path, doc, merge=False)
        want_sig = _signature(_world_shapes(doc, importSKP.last_import_stats))
        # Selecting the model part and everything in it (as Ctrl+A would) must not
        # write anything twice, and the definitions group must add nothing.
        importSKP.export(list(doc.Objects), out_path)
        _save_export_sample(out_path, "synthetic_roundtrip.skp")
        model = openskp.SkpFile.open(out_path).parse()

        defs_by_name = {d.name: d for d in model.definitions.values() if d is not model.root}
        assert sorted(defs_by_name) == ["Leg", "Leg (Red)", "Table", "Table (Red)"], sorted(defs_by_name)
        assert len(defs_by_name["Leg"].faces) == 6 and len(defs_by_name["Leg"].instances) == 0
        assert len(defs_by_name["Leg (Red)"].faces) == 6 and len(defs_by_name["Leg (Red)"].instances) == 0
        for tname, leg_name in (("Table", "Leg"), ("Table (Red)", "Leg (Red)")):
            tdef = defs_by_name[tname]
            assert len(tdef.faces) == 1 and len(tdef.instances) == 4, (tname, len(tdef.faces), len(tdef.instances))
            assert sorted(i.name for i in tdef.instances) == ["Leg 1", "Leg 2", "Leg 3", "Leg 4"]
            assert {model.definitions[i.ref_idx].name for i in tdef.instances} == {leg_name}
            got_xy = sorted((round(i.matrix[9], 3), round(i.matrix[10], 3)) for i in tdef.instances)
            assert got_xy == [(0.0, 0.0), (0.0, 18.0), (38.0, 0.0), (38.0, 18.0)], got_xy

        # Root loose faces: 1 root quad + 6 faces from the sheared copy (7 total).
        assert len(model.root.faces) == 7, len(model.root.faces)
        assert sorted(layer.name for layer in model.layers) == ["Closed", "Layer0", "Open"]

        root_insts = {i.name: i for i in model.root.instances}
        assert sorted(root_insts) == ["Mirrored leg", "Scaled leg", "Table closed", "Table open"], sorted(root_insts)
        assert model.definitions[root_insts["Table closed"].ref_idx].name == "Table"
        assert root_insts["Table closed"].layer == "Closed"
        assert model.definitions[root_insts["Table open"].ref_idx].name == "Table (Red)"
        assert root_insts["Table open"].layer == "Open"
        assert _fold_m13(root_insts["Table open"].matrix)[9:12] == (100.0, 0.0, 0.0)

        for name, (m, t) in TOP_LEVEL_LEGS.items():
            inst = root_insts[name]
            assert model.definitions[inst.ref_idx].name == "Leg"
            assert inst.layer in ("", "Layer0"), inst.layer
            assert _fold_m13(inst.matrix) == tuple(float(v) for v in (*m, *t)), (name, _fold_m13(inst.matrix))

        # Re-importing the exported .skp reproduces the exact 69 placed faces,
        # vertex set, total area, and [19, 25, 25] per-tag face counts.
        re_doc = App.newDocument()
        try:
            importSKP.import_skp(out_path, re_doc, merge=True)
            re_shapes = [o.Shape.copy() for o in re_doc.Objects if hasattr(o, "Shape")]
            got_sig = _signature(re_shapes)
            # Synthetic model has no loose edges, so face count, area, vertices, and edge count/length match.
            assert got_sig[0] == 69 and got_sig[:3] == want_sig[:3], (got_sig, want_sig)
            per_tag = sorted(len(o.Shape.Faces) for o in re_doc.Objects if hasattr(o, "Shape"))
            assert per_tag == [19, 25, 25], per_tag
        finally:
            App.closeDocument(re_doc.Name)
        print("check_export: OK (4 definitions, 12 instances, 7 loose faces, 69 placed faces across Closed/Layer0/Open)")
    finally:
        App.closeDocument(doc.Name)
        if os.path.exists(out_path):
            os.remove(out_path)


def check_fixture_component_round_trip():
    """Round-trips nested components and real component fixtures through
    import -> export -> re-parse, checking definitions, instance counts,
    placements, tags, and geometry equivalence with the flat export (#28)."""
    import openskp

    # 1. Three-level nested component model (Bolt -> Bracket -> Frame) with
    #    non-uniform scale, mirroring, and tags on both nested and root links.
    nested_src = os.path.join(FIXTURES_DIR, "_nested_roundtrip_src.skp")
    nested_out = os.path.join(FIXTURES_DIR, "_nested_roundtrip_out.skp")
    b = openskp.create()
    tag_hw = b.add_layer("Hardware")
    tag_frame = b.add_layer("Framing")
    with b.add_component_definition("Bolt") as bolt:
        bolt.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
    with b.add_component_definition("Bracket") as bracket:
        bracket.add_face([(0, 0, 0), (6, 0, 0), (6, 4, 0), (0, 4, 0)])
        bracket.add_instance(bolt, name="B1", translation=(1, 1, 0), layer=tag_hw)
        bracket.add_instance(
            bolt, name="B2", matrix3x3=(0, -2, 0, 1.5, 0, 0, 0, 0, 1), translation=(5, 3, 0), layer=tag_hw
        )
    with b.add_component_definition("Frame") as frame:
        frame.add_face([(0, 0, 0), (40, 0, 0), (40, 2, 0), (0, 2, 0)])
        frame.add_instance(bracket, name="LeftBracket", translation=(0, 2, 0))
        frame.add_instance(
            bracket, name="RightBracket", matrix3x3=(-1, 0, 0, 0, 1, 0, 0, 0, 1), translation=(40, 2, 0)
        )
    b.add_instance(frame, name="Bay 1", translation=(0, 0, 0), layer=tag_frame)
    b.add_instance(frame, name="Bay 2", translation=(60, 0, 0), layer=tag_frame)
    with open(nested_src, "wb") as f:
        f.write(b.to_bytes())
    _save_export_sample(nested_src, "nested_original.skp")

    try:
        for src_path, sample_name in (
            (nested_src, "nested_roundtrip.skp"),
            (os.path.join(FIXTURES_DIR, "capilla_quiroz_v17.skp"), "capilla_quiroz_v17_roundtrip.skp"),
            (os.path.join(FIXTURES_DIR, "instance_scale_term_2021.skp"), "instance_scale_term_2021_roundtrip.skp"),
            (os.path.join(FIXTURES_DIR, "instance_scale_term_v6.skp"), "instance_scale_term_v6_roundtrip.skp"),
        ):
            orig = openskp.SkpFile.open(src_path).parse()
            orig_defs = {
                d.name: (len(d.faces), len(d.instances))
                for d in orig.definitions.values()
                if d is not orig.root and (d.faces or d.instances)
            }
            orig_root_insts = sorted(
                (
                    i.name,
                    orig.definitions[i.ref_idx].name,
                    i.layer or "Layer0",
                    _fold_m13(i.matrix),
                )
                for i in orig.root.instances
            )

            flat_out = os.path.join(FIXTURES_DIR, "_nested_flat_out.skp")
            doc = App.newDocument()
            try:
                importSKP.import_skp(src_path, doc, merge=False)
                _flat_export_file(list(doc.Objects), flat_out)
                importSKP.export(list(doc.Objects), nested_out)
                _save_export_sample(nested_out, sample_name)
            finally:
                App.closeDocument(doc.Name)

            rt = openskp.SkpFile.open(nested_out).parse()
            rt_defs = {
                d.name: (len(d.faces), len(d.instances))
                for d in rt.definitions.values()
                if d is not rt.root
            }
            assert rt_defs == orig_defs, f"{sample_name}: defs {rt_defs} != {orig_defs}"

            rt_root_insts = sorted(
                (
                    i.name,
                    rt.definitions[i.ref_idx].name,
                    i.layer or "Layer0",
                    _fold_m13(i.matrix),
                )
                for i in rt.root.instances
            )
            assert len(rt_root_insts) == len(orig_root_insts), (sample_name, len(rt_root_insts), len(orig_root_insts))
            for (rn, rd, rl, rm), (on, od, ol, om) in zip(rt_root_insts, orig_root_insts):
                assert (rn, rd, rl) == (on, od, ol), (sample_name, (rn, rd, rl), (on, od, ol))
                assert all(abs(a - b) < 1e-5 for a, b in zip(rm, om)), (sample_name, rm, om)

            # World-space face count, area, and vertices must match the flat export.
            flat_doc = App.newDocument()
            re_doc = App.newDocument()
            try:
                importSKP.import_skp(flat_out, flat_doc, merge=True)
                importSKP.import_skp(nested_out, re_doc, merge=True)
                flat_face_sig = _face_signature([o.Shape for o in flat_doc.Objects if hasattr(o, "Shape")])
                rt_face_sig = _face_signature([o.Shape for o in re_doc.Objects if hasattr(o, "Shape")])
                assert rt_face_sig[0] == flat_face_sig[0], (sample_name, rt_face_sig[0], flat_face_sig[0])
                assert abs(rt_face_sig[1] - flat_face_sig[1]) <= 1e-5 * max(1.0, flat_face_sig[1]), (
                    sample_name, rt_face_sig[1], flat_face_sig[1]
                )
                assert rt_face_sig[2] == flat_face_sig[2], (
                    sample_name,
                    sorted(set(rt_face_sig[2]) - set(flat_face_sig[2]))[:5],
                    sorted(set(flat_face_sig[2]) - set(rt_face_sig[2]))[:5],
                )
            finally:
                App.closeDocument(flat_doc.Name)
                App.closeDocument(re_doc.Name)
                if os.path.exists(flat_out):
                    os.remove(flat_out)
            print(f"check_fixture_component_round_trip({sample_name}): OK ({len(rt_defs)} defs, "
                  f"{len(rt_root_insts)} root instances, {rt_face_sig[0]} placed faces)")
    finally:
        for p in (nested_src, nested_out):
            if os.path.exists(p):
                os.remove(p)


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


def check_large_hierarchy_warning():
    """Models at or above _LARGE_HIERARCHY_DEFINITION_THRESHOLD (1,000
    definitions, #31) print a console warning suggesting MergeOnImport when
    imported in the hierarchy layout, and stay quiet below the threshold or
    when merge=True."""
    import tempfile

    import openskp

    threshold = importSKP._LARGE_HIERARCHY_DEFINITION_THRESHOLD
    assert threshold == 1000, threshold

    orig_print_warning = App.Console.PrintWarning
    printed = []

    def record_warning(msg):
        printed.append(msg)
        orig_print_warning(msg)

    App.Console.PrintWarning = record_warning
    try:
        with tempfile.TemporaryDirectory() as tmp:
            small_path = os.path.join(tmp, "small.skp")
            b_small = openskp.create()
            small_defs = []
            for i in range(10):
                with b_small.add_component_definition(f"P_{i}") as comp:
                    comp.add_face([(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)])
                small_defs.append(comp)
            for i, comp in enumerate(small_defs):
                b_small.add_instance(comp, translation=(i * 3.0, 0.0, 0.0))
            with open(small_path, "wb") as f:
                f.write(b_small.to_bytes())

            doc = App.newDocument()
            try:
                printed.clear()
                importSKP.import_skp(small_path, doc, merge=False)
                assert importSKP.last_import_stats["large_hierarchy_warning"] is False
                assert not any("MergeOnImport" in m for m in printed), printed
            finally:
                App.closeDocument(doc.Name)

            big_path = os.path.join(tmp, "thousand.skp")
            b_big = openskp.create()
            big_defs = []
            for i in range(threshold):
                with b_big.add_component_definition(f"D_{i}") as comp:
                    comp.add_face([(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)])
                big_defs.append(comp)
            for i, comp in enumerate(big_defs):
                b_big.add_instance(comp, translation=((i % 50) * 3.0, (i // 50) * 3.0, 0.0))
            with open(big_path, "wb") as f:
                f.write(b_big.to_bytes())

            # Hierarchy layout at threshold -> warns and suggests MergeOnImport.
            doc = App.newDocument()
            try:
                printed.clear()
                importSKP.import_skp(big_path, doc, merge=False)
                assert importSKP.last_import_stats["large_hierarchy_warning"] is True
                assert any(str(threshold) in m and "MergeOnImport" in m for m in printed), printed
            finally:
                App.closeDocument(doc.Name)

            # Merged layout on the same model -> no warning.
            doc = App.newDocument()
            try:
                printed.clear()
                importSKP.import_skp(big_path, doc, merge=True)
                assert importSKP.last_import_stats["large_hierarchy_warning"] is False
                assert not any("MergeOnImport" in m for m in printed), printed
            finally:
                App.closeDocument(doc.Name)
    finally:
        App.Console.PrintWarning = orig_print_warning
    print(f"check_large_hierarchy_warning: OK (warns at >= {threshold} defs in hierarchy, quiet when merged)")


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
    check_fixture_component_round_trip()
    check_large_hierarchy_warning()
    checked = 0
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if name.endswith(".skp") and not name.startswith("_"):
            check_same_geometry(os.path.join(FIXTURES_DIR, name))
            checked += 1
    assert checked >= 7, f"only {checked} fixtures checked"


# freecadcmd runs this file as a module named after it, not "__main__", and exits 0
# when it raises; see tests/_harness.py.
_harness.run(_main)
