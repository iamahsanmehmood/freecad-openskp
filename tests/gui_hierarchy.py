"""Checks the component hierarchy (#5) in FreeCAD's real GUI, which the headless tests
cannot: whether the links draw their hidden definitions, whether the hidden tag and the
definitions themselves stay out of the view, and what the result looks like.

Run by CI under a virtual display (see the "gui" job in .github/workflows/ci.yml):

    GUI_OUT=gui-out GUI_TESTS_DIR=$PWD/tests xvfb-run FreeCAD tests/gui_hierarchy.py

It imports tests/_synthetic.py's model, measures the bounding box of what the 3D view's
scene graph actually draws, compares it with what SketchUp shows (VISIBLE_BOUNDS_IN), and
does the same for the merged import. Writes PASS or the failure to $GUI_OUT/result.txt and
screenshots of the view and the window (with the model tree) next to it, then exits.
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


def _check_bounds(label, got, want_in, mm):
    want = tuple((a * mm, b * mm) for a, b in want_in)
    for axis, (g, w) in zip("xyz", zip(got, want)):
        if abs(g[0] - w[0]) > 1e-2 or abs(g[1] - w[1]) > 1e-2:
            raise AssertionError(f"{label}: the view draws {axis} from {g[0]:.2f} to {g[1]:.2f} mm, "
                                 f"expected {w[0]:.2f} to {w[1]:.2f} (all drawn: {got})")


def _show(doc, name):
    import FreeCADGui as Gui

    gui_doc = Gui.getDocument(doc.Name)
    Gui.ActiveDocument = gui_doc
    view = gui_doc.activeView()
    view.viewIsometric()
    view.fitAll()
    Gui.updateGui()
    view.saveImage(os.path.join(OUT, f"{name}.png"), 1400, 900, "White")
    return view


def main():
    try:
        import FreeCAD as App
        import FreeCADGui as Gui

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
        # The window with the model tree expanded, for a person to look at.
        tree = Gui.getMainWindow()
        for obj in (model_part, *tags.values()):
            Gui.Selection.addSelection(obj)
        try:
            Gui.runCommand("Std_TreeExpand")
        except Exception:
            pass  # only for the screenshot
        Gui.Selection.clearSelection()
        Gui.updateGui()
        tree.grab().save(os.path.join(OUT, "hierarchy-window.png"))

        merged = App.newDocument("Merged")
        importSKP.import_skp(path, merged, merge=True)
        view = _show(merged, "merged")
        drawn = _drawn_bounds(view)
        _check_bounds("merged", drawn, VISIBLE_BOUNDS_IN, mm)
        lines.append(f"merged: the view draws {drawn} mm, the same")

        _finish("PASS\n" + "\n".join(lines), 0)
    except BaseException:
        _finish("FAIL\n" + traceback.format_exc(), 1)


from PySide import QtCore  # noqa: E402

# Let the main window finish starting before touching views.
QtCore.QTimer.singleShot(3000, main)
# A hard stop if anything hangs.
QtCore.QTimer.singleShot(240000, lambda: _finish("FAIL\ntimed out", 1))
