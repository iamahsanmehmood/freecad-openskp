"""Checks the component hierarchy (#5) in FreeCAD's real GUI, which the headless tests
cannot: whether the links draw their hidden definitions, whether the hidden tag and the
definitions themselves stay out of the view, and what the result looks like.

Run by CI under a virtual display (see the "gui" job in .github/workflows/ci.yml):

    GUI_OUT=gui-out GUI_TESTS_DIR=$PWD/tests xvfb-run FreeCAD tests/gui_hierarchy.py

It imports tests/_synthetic.py's model, measures the bounding box of what the 3D view's
scene graph actually draws, compares it with what SketchUp shows (VISIBLE_BOUNDS_IN), and
does the same for the merged import. Writes PASS or the failure to $GUI_OUT/result.txt and
pictures of the window (view and model tree) next to it, then exits.
"""
import os
import sys
import traceback

OUT = os.path.abspath(os.environ.get("GUI_OUT", "gui-out"))
TESTS = os.environ["GUI_TESTS_DIR"]
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.dirname(TESTS))


def _finish(text, code):
    with open(os.path.join(OUT, "result.txt"), "w") as f:
        f.write(text + "\n")
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    os._exit(code)


def _drawn_bounds(view):
    from pivy import coin

    region = view.getViewer().getSoRenderManager().getViewportRegion()
    action = coin.SoGetBoundingBoxAction(region)
    action.apply(view.getViewer().getSceneGraph())
    box = action.getBoundingBox()
    lo, hi = box.getMin().getValue(), box.getMax().getValue()
    return tuple((lo[i], hi[i]) for i in range(3))


def _check_bounds(label, got, want_in, mm, tol=1e-2):
    want = tuple((a * mm, b * mm) for a, b in want_in)
    for axis, (g, w) in zip("xyz", zip(got, want)):
        if abs(g[0] - w[0]) > tol or abs(g[1] - w[1]) > tol:
            raise AssertionError(f"{label}: the view draws {axis} from {g[0]:.2f} to {g[1]:.2f} mm, "
                                 f"expected {w[0]:.2f} to {w[1]:.2f} (all drawn: {got})")


def _show(doc, name):
    """Shows ``doc`` fitted in an isometric view and saves a picture of the
    whole window, model tree expanded. (A grab of the window, not
    View.saveImage: under the virtual display the off-screen renderer that
    saveImage uses leaves most of either import out.)"""
    import FreeCADGui as Gui
    from PySide import QtGui

    gui_doc = Gui.getDocument(doc.Name)
    Gui.ActiveDocument = gui_doc
    view = gui_doc.activeView()
    view.viewIsometric()
    view.fitAll()
    window = Gui.getMainWindow()
    for tree in window.findChildren(QtGui.QTreeWidget):
        tree.expandAll()
    Gui.updateGui()
    window.grab().save(os.path.join(OUT, f"{name}.png"))
    return view


def main():
    try:
        import FreeCAD as App

        import importSKP
        from _synthetic import VISIBLE_BOUNDS_IN, write_synthetic

        mm = importSKP.INCH_TO_MM
        path = os.path.join(OUT, "synthetic.skp")
        write_synthetic(path)
        lines = []

        doc = App.newDocument("Hierarchy")
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats
        components = doc.getObject(stats["components_group"])
        assert not components.ViewObject.Visibility
        for part in components.Group:
            assert not part.ViewObject.Visibility, f"definition {part.Label} is shown"
        model_part = doc.getObject(stats["model_part"])
        tags = {o.Label: o for o in model_part.Group if o.TypeId == "App::DocumentObjectGroup"}
        assert not tags["Open"].ViewObject.Visibility and tags["Closed"].ViewObject.Visibility
        for name, colors in stats["object_face_colors"].items():
            obj = doc.getObject(name)
            assert len(obj.ViewObject.DiffuseColor) == len(obj.Shape.Faces), name
        view = _show(doc, "hierarchy")
        drawn = _drawn_bounds(view)
        _check_bounds("hierarchy", drawn, VISIBLE_BOUNDS_IN, mm)
        lines.append(f"hierarchy: the view draws {drawn} mm, as SketchUp shows it")

        merged = App.newDocument("Merged")
        importSKP.import_skp(path, merged, merge=True)
        view = _show(merged, "merged")
        drawn = _drawn_bounds(view)
        _check_bounds("merged", drawn, VISIBLE_BOUNDS_IN, mm)
        lines.append(f"merged: the view draws {drawn} mm, the same")

        # MakeSolids (#21): the same model with each closed component a solid draws the
        # same, and every solid's faces still carry one colour each.
        solid_doc = App.newDocument("Solids")
        importSKP.import_skp(path, solid_doc, merge=False, solids=True)
        stats = importSKP.last_import_stats
        assert stats["solids_built"] == 2, stats["solids_built"]
        for name, colors in stats["object_face_colors"].items():
            obj = solid_doc.getObject(name)
            assert len(obj.ViewObject.DiffuseColor) == len(obj.Shape.Faces), name
        view = _show(solid_doc, "solids")
        drawn = _drawn_bounds(view)
        _check_bounds("solids", drawn, VISIBLE_BOUNDS_IN, mm)
        lines.append(f"solids: the view draws {drawn} mm, the same")

        # ImportAssemblies (#22): Table and Table (Red) become Assembly::AssemblyObject,
        # leaf parts stay App::Part, the 3D view draws the same extent, and the Assembly
        # workbench activates the imported assembly cleanly.
        import FreeCADGui as Gui
        import UtilsAssembly

        asm_doc = App.newDocument("Assemblies")
        importSKP.import_skp(path, asm_doc, merge=False, assemblies=True)
        stats = importSKP.last_import_stats
        assert stats["assemblies"] == 2, stats
        components = asm_doc.getObject(stats["components_group"])
        by_label = {o.Label: o for o in components.Group}
        for label in ("Table", "Table (Red)"):
            assert by_label[label].TypeId == "Assembly::AssemblyObject", by_label[label].TypeId
            assert not by_label[label].ViewObject.Visibility
        for label in ("Leg", "Leg (Red)", "Spare"):
            assert by_label[label].TypeId == "App::Part", by_label[label].TypeId
            assert not by_label[label].ViewObject.Visibility
        for name in stats["object_face_colors"]:
            obj = asm_doc.getObject(name)
            assert len(obj.ViewObject.DiffuseColor) == len(obj.Shape.Faces), name
        view = _show(asm_doc, "assemblies")
        drawn = _drawn_bounds(view)
        _check_bounds("assemblies", drawn, VISIBLE_BOUNDS_IN, mm)

        App.setActiveDocument(asm_doc.Name)
        Gui.activateWorkbench("AssemblyWorkbench")
        Gui.updateGui()
        table = by_label["Table"]
        assert Gui.getDocument(asm_doc.Name).setEdit(table.Name), "setEdit(Table) returned False"
        Gui.updateGui()
        assert UtilsAssembly.activeAssembly() == table, UtilsAssembly.activeAssembly()
        assert UtilsAssembly.number_of_components_in(table) == 5, UtilsAssembly.number_of_components_in(table)
        _show(asm_doc, "assembly_workbench")
        Gui.getDocument(asm_doc.Name).resetEdit()
        Gui.updateGui()
        lines.append(f"assemblies: the view draws {drawn} mm, AssemblyWorkbench activates Table (5 components)")

        # FitCurvedSurfaces (#18, parts 1 & 2): circular holes, cylindrical bores,
        # cylinders and frustums render with exact bounds and 1:1 DiffuseColor.
        plate_path = os.path.join(TESTS, "fixtures", "plate_with_hole_2021.skp")
        plate_doc = App.newDocument("PlateWithHole")
        importSKP.import_skp(plate_path, plate_doc, merge=False, solids=True, curved_surfaces=True)
        stats = importSKP.last_import_stats
        assert stats["cylinders_built"] == 1 and stats["face_arcs_built"] == 2 and stats["solids_built"] == 1, stats
        for name in stats["object_face_colors"]:
            obj = plate_doc.getObject(name)
            assert len(obj.ViewObject.DiffuseColor) == len(obj.Shape.Faces), name
        view = _show(plate_doc, "plate_with_hole")
        plate_drawn = _drawn_bounds(view)
        _check_bounds("plate_with_hole", plate_drawn, ((-10, 10), (-10, 10), (0, 2)), mm)
        lines.append(f"plate_with_hole: the view draws {plate_drawn} mm, solid with true cylindrical bore")

        frustum_path = os.path.join(TESTS, "fixtures", "frustum_2021.skp")
        frustum_doc = App.newDocument("Frustum")
        importSKP.import_skp(frustum_path, frustum_doc, merge=False, solids=True, curved_surfaces=True)
        stats = importSKP.last_import_stats
        assert stats["cones_built"] == 1 and stats["solids_built"] == 1, stats
        view = _show(frustum_doc, "frustum")
        frustum_drawn = _drawn_bounds(view)
        # Coin3D tessellates a curved outer rim into display chords (~0.1 mm sagitta).
        _check_bounds("frustum", frustum_drawn, ((-6, 6), (-6, 6), (0, 4)), mm, tol=0.5)
        lines.append(f"frustum: the view draws {frustum_drawn} mm, 3-face conical solid")

        sphere_path = os.path.join(TESTS, "fixtures", "sphere_2021.skp")
        sphere_doc = App.newDocument("Sphere")
        importSKP.import_skp(sphere_path, sphere_doc, merge=False, solids=True, curved_surfaces=True)
        stats = importSKP.last_import_stats
        assert stats["spheres_built"] == 1 and stats["solids_built"] == 1, stats
        for name in stats["object_face_colors"]:
            obj = sphere_doc.getObject(name)
            assert len(obj.ViewObject.DiffuseColor) == len(obj.Shape.Faces), name
        view = _show(sphere_doc, "sphere")
        sphere_drawn = _drawn_bounds(view)
        _check_bounds("sphere", sphere_drawn, ((-5, 5), (-5, 5), (-5, 5)), mm, tol=0.5)
        lines.append(f"sphere: the view draws {sphere_drawn} mm, 1-face spherical solid")

        # ImportAnnotations (#29): positioned leader text, root dimension (36 in = 914.4 mm),
        # and component dimension inside Beam (24 in = 609.6 mm) in FreeCAD's real GUI,
        # skipping the positionless screen text, preserving selection in other open documents,
        # and leaving zero orphan objects when no 3D view is active.
        annot_path = os.path.join(TESTS, "fixtures", "annotations_2017.skp")
        sel_keep_obj = sphere_doc.Objects[0]
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(sphere_doc.Name, sel_keep_obj.Name)
        assert [o.Name for o in Gui.Selection.getSelection(sphere_doc.Name)] == [sel_keep_obj.Name]

        for label_mode, merge_mode in (("annotations_hierarchy", False), ("annotations_merged", True)):
            annot_doc = App.newDocument(label_mode)
            importSKP.import_skp(annot_path, annot_doc, merge=merge_mode)
            stats = importSKP.last_import_stats
            assert stats["texts_built"] == 1 and stats["dimensions_built"] == 2, (label_mode, stats)
            assert stats["texts_skipped_no_position"] == 1, (label_mode, stats)
            assert not Gui.Selection.getSelection(annot_doc.Name), (label_mode, "selection not cleared")
            assert [o.Name for o in Gui.Selection.getSelection(sphere_doc.Name)] == [sel_keep_obj.Name], (
                label_mode, "other document selection not preserved"
            )
            txts = [
                o for o in annot_doc.Objects
                if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
            ]
            assert len(txts) == 1 and list(txts[0].Text) == ["Apex label"]
            assert txts[0].ViewObject.Visibility
            dim_objs = [
                o for o in annot_doc.Objects
                if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
            ]
            for dobj in dim_objs:
                seg_u = (dobj.End - dobj.Start).normalize()
                assert abs(dobj.Normal.Length - 1.0) <= 1e-5 and abs(dobj.Normal.dot(seg_u)) <= 1e-5
            dims = sorted((o.End - o.Start).Length for o in dim_objs)
            assert len(dims) == 2, dims
            assert abs(dims[0] - 24.0 * mm) <= 1e-2 and abs(dims[1] - 36.0 * mm) <= 1e-2, dims
            _show(annot_doc, label_mode)

        # Import with a Spreadsheet tab in front (non-3D active view; FreeCAD 1.1.3 raises
        # AttributeError on getViewDirection after Draft adds its object) AND into a hidden
        # document with no MDI view: geometry must import cleanly and zero orphan Draft
        # objects may remain.
        sp_doc = App.newDocument("annotations_spreadsheet_front")
        sheet = sp_doc.addObject("Spreadsheet::Sheet", "Sheet")
        sheet.ViewObject.doubleClicked()
        Gui.updateGui()
        importSKP.import_skp(annot_path, sp_doc, merge=False)
        sp_stats = importSKP.last_import_stats
        assert sp_stats["faces_built"] == 2, sp_stats
        sp_roots = {
            sheet.Name,
            sp_doc.getObject(sp_stats["model_part"]).Name,
            sp_doc.getObject(sp_stats["components_group"]).Name,
        }
        sp_orphans = [o.Name for o in sp_doc.Objects if not o.InList and o.Name not in sp_roots]
        assert not sp_orphans, f"orphan objects left behind with Spreadsheet in front: {sp_orphans}"
        if sp_stats["dimensions_built"] == 0:
            assert sp_stats["annotation_failures"] == 2, sp_stats
            assert not [o.Name for o in sp_doc.Objects if "Dimension" in o.Name], [o.Name for o in sp_doc.Objects]

        noview_doc = App.newDocument("annotations_no_3d_view", hidden=True)
        importSKP.import_skp(annot_path, noview_doc, merge=False)
        nv_stats = importSKP.last_import_stats
        assert nv_stats["faces_built"] == 2, nv_stats
        top_roots = {
            noview_doc.getObject(nv_stats["model_part"]).Name,
            noview_doc.getObject(nv_stats["components_group"]).Name,
        }
        orphans = [o.Name for o in noview_doc.Objects if not o.InList and o.Name not in top_roots]
        assert not orphans, f"orphan objects left behind in hidden document: {orphans}"
        lines.append(
            "annotations: 1 Draft leader text + 2 Draft linear dimensions (609.6 mm, 914.4 mm) shown in GUI; "
            "other-doc selection preserved; Spreadsheet-in-front and hidden-doc imports clean (0 orphans)"
        )

        _finish("PASS\n" + "\n".join(lines), 0)
    except BaseException:
        _finish("FAIL\n" + traceback.format_exc(), 1)


from PySide import QtCore  # noqa: E402

# Let the main window finish starting before touching views.
QtCore.QTimer.singleShot(3000, main)
# A hard stop if anything hangs.
QtCore.QTimer.singleShot(240000, lambda: _finish("FAIL\ntimed out", 1))
