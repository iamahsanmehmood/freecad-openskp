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


def _show(doc, name, view_setup=None, fit=True):
    """Shows ``doc`` fitted in an isometric view (or custom ``view_setup(view)``)
    and saves a picture of the whole window, model tree expanded. (A grab of the
    window, not View.saveImage: under the virtual display the off-screen renderer
    that saveImage uses leaves most of either import out.)"""
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtGui

    App.setActiveDocument(doc.Name)
    gui_doc = Gui.getDocument(doc.Name)
    Gui.ActiveDocument = gui_doc
    view = gui_doc.activeView()
    if hasattr(view, "setAnimationEnabled"):
        view.setAnimationEnabled(False)
    if view_setup is None:
        view.viewIsometric()
    else:
        view_setup(view)
    if fit:
        view.fitAll()
    window = Gui.getMainWindow()
    for tree in window.findChildren(QtGui.QTreeWidget):
        tree.expandAll()
    Gui.updateGui()
    Gui.updateGui()
    window.grab().save(os.path.join(OUT, f"{name}.png"))
    return view


def _extract_coin_texture_state(node):
    """Extracts ``(tex_digests, uv_lists)`` from populated Coin3D ``SoTexture2``
    and ``SoTextureCoordinate2`` nodes under ``OpenSKPTexturedRoot``."""
    import hashlib
    from pivy import coin

    tex_digests = []
    uv_lists = []

    def walk(n, inside_tex_root):
        if n is None:
            return
        sb_name = n.getTypeId().getName()
        tname = sb_name.getString() if hasattr(sb_name, "getString") else str(sb_name)
        n_obj_name = n.getName().getString() if hasattr(n.getName(), "getString") else str(n.getName())
        inside = inside_tex_root or (n_obj_name == "OpenSKPTexturedRoot")
        if inside and tname == "Texture2":
            val = n.image.get()
            txt = (
                val.getString()
                if hasattr(val, "getString")
                else (val.decode("utf-8", errors="surrogateescape") if isinstance(val, bytes) else str(val))
            )
            parts = txt.split(None, 3)
            if len(parts) >= 3:
                w, h, nc = int(parts[0]), int(parts[1]), int(parts[2])
                if w > 0 and h > 0:
                    tex_digests.append(((w, h), nc, len(txt), hashlib.sha256(txt.encode("utf-8", errors="surrogateescape")).hexdigest()))
        elif inside and tname == "TextureCoordinate2":
            cnt = n.point.getNum()
            pts = []
            for idx in range(cnt):
                v = n.point[idx]
                xy = v.getValue() if hasattr(v, "getValue") else tuple(v)
                pts.append((round(float(xy[0]), 5), round(float(xy[1]), 5)))
            uv_lists.append(pts)
        if hasattr(n, "getNumChildren"):
            for i in range(n.getNumChildren()):
                walk(n.getChild(i), inside)

    walk(node, False)
    return tex_digests, uv_lists


def _sample_viewport_color_buckets(png_path, x_range=(0.28, 0.94), y_range=(0.18, 0.86)):
    """Classifies saturated pixels in the central 3D viewport (or sub-region) of
    a saved window screenshot so tests verify what OpenGL actually drew."""
    from PySide import QtGui

    img = QtGui.QImage(png_path)
    assert not img.isNull(), f"failed to read screenshot {png_path}"
    img = img.convertToFormat(QtGui.QImage.Format_RGB888)
    w, h = img.width(), img.height()
    buckets = set()
    for y in range(int(h * y_range[0]), int(h * y_range[1]), 3):
        for x in range(int(w * x_range[0]), int(w * x_range[1]), 3):
            c = QtGui.QColor(img.pixel(x, y))
            r, g, b = c.red(), c.green(), c.blue()
            sat = max(r, g, b) - min(r, g, b)
            if sat < 35:
                continue
            if g > 140 and b > 160 and r < 55:
                buckets.add("bright_cyan_stripe")
            if r > 140 and b > 100 and g < 100:
                buckets.add("magenta")
            elif r > 150 and g > 130 and b < 95:
                buckets.add("yellow_or_chartreuse")
            elif b > 95 and r < 80:
                buckets.add("cyan_or_blue")
            elif g > 140 and r < 90 and b < 110:
                buckets.add("green")
            elif r > 135 and g < 85 and b < 85:
                buckets.add("red")
            elif r > 150 and 75 <= g <= 145 and 60 <= b <= 130:
                buckets.add("salmon_coral")
    return sorted(buckets)


def _collect_coin_tex2_nodes(node):
    """Returns the Coin3D ``SoTexture2`` nodes under ``OpenSKPTexturedRoot``."""
    nodes = []

    def walk(n, inside):
        if n is None:
            return
        sb_name = n.getTypeId().getName()
        tname = sb_name.getString() if hasattr(sb_name, "getString") else str(sb_name)
        n_obj_name = n.getName().getString() if hasattr(n.getName(), "getString") else str(n.getName())
        inside = inside or (n_obj_name == "OpenSKPTexturedRoot")
        if inside and tname == "Texture2":
            nodes.append(n)
        if hasattr(n, "getNumChildren"):
            for i in range(n.getNumChildren()):
                walk(n.getChild(i), inside)

    walk(node, False)
    return nodes


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

        # Close earlier test documents before #30 textured-material checks so MDI focus and memory stay clean.
        for prev_dname in list(App.listDocuments()):
            App.closeDocument(prev_dname)
        Gui.updateGui()

        # ImportTextures (#30): selective Part::FeaturePython promotion, Coin3D "Textured"
        # display mode through App::Link (including mirrored links), front/back culling,
        # positioned & projected UVs, HLS colorize (modes 0 & 1), opacity, .FCStd save/reopen,
        # copy/paste, undo/redo, Spreadsheet-in-front, hidden doc, and clean reopen without
        # the add-on installed.
        import json
        import Part
        import shutil
        import subprocess
        import openskp
        from pivy import coin

        tex_uv_2017 = os.path.join(TESTS, "fixtures", "textures_uv_2017.skp")
        tex_uv_2021 = os.path.join(TESTS, "fixtures", "textures_uv_2021.skp")
        # Hierarchy App::Part includes its Origin at z=0; merged compound spans the faces' own z=[1, 13] in.
        tex_uv_hier_bounds_in = ((0.0, 62.0), (0.0, 32.0), (0.0, 13.0))
        tex_uv_merged_bounds_in = ((0.0, 62.0), (0.0, 32.0), (1.0, 13.0))

        for ver_tag, uv_path in (("2017", tex_uv_2017), ("2021", tex_uv_2021)):
            tdoc = App.newDocument(f"TexturesUV_{ver_tag}")
            importSKP.import_skp(uv_path, tdoc, merge=False, textures=True)
            tstats = importSKP.last_import_stats
            assert tstats["textures_loaded"] == 2 and tstats["textured_materials"] == 3, (ver_tag, tstats)
            assert tstats["textured_objects"] == 3 and tstats["textures_failed"] == 0, (ver_tag, tstats)

            t_comps = tdoc.getObject(tstats["components_group"])
            assert not t_comps.ViewObject.Visibility
            for part in t_comps.Group:
                assert not part.ViewObject.Visibility

            geom_by_lbl = {
                o.Label: o for o in tdoc.Objects if o.TypeId in ("Part::Feature", "Part::FeaturePython")
            }
            assert geom_by_lbl["Panel geometry"].TypeId == "Part::Feature"
            for tex_lbl in (
                "Panel (MatChecker) geometry",
                "Panel (MatStripesCutout) geometry",
                f"textures_uv_{ver_tag} geometry",
            ):
                tobj = geom_by_lbl[tex_lbl]
                assert tobj.TypeId == "Part::FeaturePython", (ver_tag, tex_lbl, tobj.TypeId)
                assert tobj.ViewObject.DisplayMode == "Textured", tobj.ViewObject.DisplayMode
                # Review point 3: rebuilt exactly once during import (not ~5 times)!
                assert tobj.ViewObject.Proxy._rebuild_count == 1, (
                    ver_tag, tex_lbl, tobj.ViewObject.Proxy._rebuild_count
                )
                assert len(tobj.ViewObject.DiffuseColor) == len(tobj.Shape.Faces), tex_lbl
                tdigests, tuv_lists = _extract_coin_texture_state(tobj.ViewObject.RootNode)
                assert tdigests and tuv_lists, (ver_tag, tex_lbl, tdigests, len(tuv_lists))

            # Review point 3: Panel (MatChecker) geometry and root geometry share the EXACT same SoTexture2 node!
            panel_chk_nodes = _collect_coin_tex2_nodes(geom_by_lbl["Panel (MatChecker) geometry"].ViewObject.RootNode)
            root_chk_nodes = _collect_coin_tex2_nodes(geom_by_lbl[f"textures_uv_{ver_tag} geometry"].ViewObject.RootNode)
            assert len(panel_chk_nodes) == 1 and any(panel_chk_nodes[0] == rn for rn in root_chk_nodes), (
                len(panel_chk_nodes), len(root_chk_nodes)
            )

            if ver_tag == "2017":
                view = _show(tdoc, "textures_uv_hierarchy_iso")
                _check_bounds("textures_uv_2017_hierarchy", _drawn_bounds(view), tex_uv_hier_bounds_in, mm, tol=0.5)

                # Review point 4: SoRayPickAction -> getElementPicked maps Coin triangles back to exact Face1..Face3!
                root_tex_obj = geom_by_lbl["textures_uv_2017 geometry"]
                vp_reg = view.getViewer().getSoRenderManager().getViewportRegion()
                picked_faces = []
                for px_in in (6.0, 26.0, 46.0):
                    rpa = coin.SoRayPickAction(vp_reg)
                    rpa.setRay(coin.SbVec3f(px_in * mm, 26.0 * mm, 50.0 * mm), coin.SbVec3f(0.0, 0.0, -1.0))
                    rpa.apply(root_tex_obj.ViewObject.RootNode)
                    pp = rpa.getPickedPoint()
                    assert pp is not None, f"ray at x={px_in} in missed root textured faces"
                    sub_el = root_tex_obj.ViewObject.Proxy.getElementPicked(pp)
                    picked_faces.append(sub_el)
                assert set(picked_faces) == {"Face1", "Face2", "Face3"}, f"expected Face1..3, got {picked_faces}"
                # Review 2 point 3: outside "Textured" mode, Proxy.getElementPicked raises NotImplementedError
                # so C++ ViewProviderPartExt handles picking in Flat Lines, Shaded, and through App::Link!
                root_tex_obj.ViewObject.DisplayMode = "Flat Lines"
                Gui.updateGui()
                try:
                    root_tex_obj.ViewObject.Proxy.getElementPicked(pp)
                    raise AssertionError("expected NotImplementedError outside Textured mode")
                except NotImplementedError:
                    pass
                rpa_flat = coin.SoRayPickAction(vp_reg)
                rpa_flat.setRay(coin.SbVec3f(6.0 * mm, 26.0 * mm, 50.0 * mm), coin.SbVec3f(0.0, 0.0, -1.0))
                rpa_flat.apply(root_tex_obj.ViewObject.RootNode)
                pp_flat = rpa_flat.getPickedPoint()
                assert pp_flat is not None
                assert root_tex_obj.ViewObject.getElementPicked(pp_flat) == "Face1"
                root_tex_obj.ViewObject.DisplayMode = "Textured"
                Gui.updateGui()
                assert root_tex_obj.ViewObject.getElementPicked(pp) in ("Face1", "Face2", "Face3")

                t_mp_pick = tdoc.getObject(tstats["model_part"])
                t_l0_pick = [o for o in t_mp_pick.Group if o.Label == "Layer0"][0]
                chk_lnk = [o for o in t_l0_pick.Group if o.TypeId == "App::Link" and "Checker" in o.Label][0]
                lc_bb = chk_lnk.Shape.BoundBox
                for dm_lnk in ("Textured", "Flat Lines"):
                    chk_lnk.LinkedObject.Group[0].ViewObject.DisplayMode = dm_lnk
                    Gui.updateGui()
                    rpa_lnk = coin.SoRayPickAction(vp_reg)
                    rpa_lnk.setRay(
                        coin.SbVec3f(lc_bb.Center.x, lc_bb.Center.y, lc_bb.ZMax + 50.0),
                        coin.SbVec3f(0.0, 0.0, -1.0),
                    )
                    rpa_lnk.apply(chk_lnk.ViewObject.RootNode)
                    pp_lnk = rpa_lnk.getPickedPoint()
                    assert pp_lnk is not None, dm_lnk
                    picked_lnk = chk_lnk.ViewObject.getElementPicked(pp_lnk)
                    assert picked_lnk and "Face1" in picked_lnk, (dm_lnk, picked_lnk)
                chk_lnk.LinkedObject.Group[0].ViewObject.DisplayMode = "Textured"
                Gui.updateGui()

                _show(tdoc, "textures_uv_top_front", lambda v: v.viewTop())
                top_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textures_uv_top_front.png"))
                assert (
                    "magenta" in top_hues
                    and "yellow_or_chartreuse" in top_hues
                    and "bright_cyan_stripe" in top_hues
                ), f"top view missing expected checker/stripe hues: {top_hues}"

                # Isolate the double-sided root quad (front=MatChecker, back=MatStripesCutout) and check
                # bottom view (-Z): front magenta/yellow checker must be culled and back cyan/navy shown!
                t_model_part = tdoc.getObject(tstats["model_part"])
                t_layer0 = [o for o in t_model_part.Group if o.Label == "Layer0"][0]
                t_links = [o for o in t_layer0.Group if o.TypeId == "App::Link"]
                for lnk in t_links:
                    lnk.ViewObject.Visibility = False
                _show(tdoc, "textures_uv_bottom_back", lambda v: v.viewBottom())
                bot_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textures_uv_bottom_back.png"))
                assert "cyan_or_blue" in bot_hues, f"bottom view missing back-face stripes: {bot_hues}"
                for lnk in t_links:
                    lnk.ViewObject.Visibility = True
                Gui.updateGui()

                # Review point 10: dumps() is pure, failed saveAs restores "Textured", and user-chosen
                # "Shaded" mode is preserved across save & reopen alongside "Textured"!
                assert root_tex_obj.ViewObject.Proxy.dumps() == {"mode": "Textured"}
                assert root_tex_obj.ViewObject.DisplayMode == "Textured"
                try:
                    tdoc.saveAs(os.path.join(OUT, "nonexistent_subdir_xyz", "cannot_write.FCStd"))
                except Exception:
                    pass
                Gui.updateGui()
                assert root_tex_obj.ViewObject.DisplayMode == "Textured", (
                    f"failed saveAs left DisplayMode={root_tex_obj.ViewObject.DisplayMode!r}"
                )

                stripes_def_obj = geom_by_lbl["Panel (MatStripesCutout) geometry"]
                stripes_def_obj.ViewObject.DisplayMode = "Shaded"
                pre_coin_state = _extract_coin_texture_state(root_tex_obj.ViewObject.RootNode)
                saved_fcstd = os.path.join(OUT, "textures_gui_saved.FCStd")
                tdoc.saveAs(saved_fcstd)
                assert root_tex_obj.ViewObject.DisplayMode == "Textured"
                assert stripes_def_obj.ViewObject.DisplayMode == "Shaded"
                App.closeDocument(tdoc.Name)
                # Review point 2: closing the document must clear its cached texture entries automatically!
                assert importSKP._COIN_TEX_NODE_CACHE.total_bytes == 0, importSKP._COIN_TEX_NODE_CACHE.total_bytes
                assert importSKP._STORE_UNPACK_CACHE.total_bytes == 0, importSKP._STORE_UNPACK_CACHE.total_bytes

                re_doc = App.openDocument(saved_fcstd)
                re_doc.recompute()
                re_root = [o for o in re_doc.Objects if o.Label == "textures_uv_2017 geometry"][0]
                re_stripes = [o for o in re_doc.Objects if o.Label == "Panel (MatStripesCutout) geometry"][0]
                assert re_root.ViewObject.DisplayMode == "Textured", re_root.ViewObject.DisplayMode
                assert re_stripes.ViewObject.DisplayMode == "Shaded", (
                    f"saved Shaded mode was overwritten on reopen: {re_stripes.ViewObject.DisplayMode!r}"
                )
                re_stripes.ViewObject.DisplayMode = "Textured"
                Gui.updateGui()
                # Resave with all textured objects in "Textured" mode for the no-addon reopen check below
                re_doc.saveAs(saved_fcstd)
                post_coin_state = _extract_coin_texture_state(re_root.ViewObject.RootNode)
                assert post_coin_state == pre_coin_state, (post_coin_state[0], pre_coin_state[0])
                re_view = _show(re_doc, "textures_reopened", lambda v: v.viewTop())
                _check_bounds("textures_reopened", _drawn_bounds(re_view), tex_uv_hier_bounds_in, mm, tol=0.5)
                re_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textures_reopened.png"))
                assert (
                    "magenta" in re_hues
                    and "yellow_or_chartreuse" in re_hues
                    and "bright_cyan_stripe" in re_hues
                ), re_hues

                # GUI copy/paste + undo/redo + recompute + live user colour/transparency edits (point 12)
                re_doc.UndoMode = 1
                re_doc.openTransaction("Copy & delete")
                copied_gui = re_doc.copyObject(re_root, False)
                re_doc.recompute()
                assert re_root.ViewObject.DisplayMode == "Textured"
                assert _extract_coin_texture_state(copied_gui.ViewObject.RootNode)[0] == pre_coin_state[0]
                re_doc.removeObject(copied_gui.Name)
                re_doc.commitTransaction()
                re_doc.undo()
                Gui.updateGui()
                re_doc.redo()
                Gui.updateGui()

                # Review 2 point 4:
                # (a) First face at 50% alpha (Transparency=50) + second face at 100% alpha keeps 0.5 and 1.0
                #     (never double-multiplying by 1 - Transparency/100)!
                dc_orig = list(re_root.ViewObject.DiffuseColor)
                dc_half_first = list(dc_orig)
                dc_half_first[0] = (dc_orig[0][0], dc_orig[0][1], dc_orig[0][2], 0.5)
                dc_half_first[1] = (dc_orig[1][0], dc_orig[1][1], dc_orig[1][2], 1.0)
                re_root.ViewObject.DiffuseColor = dc_half_first
                Gui.updateGui()
                assert len(_collect_coin_tex2_nodes(re_root.ViewObject.RootNode)) >= 2

                def _coin_tex_batch_transparencies(node):
                    vals = []
                    if not hasattr(node, "getNumChildren"):
                        return vals
                    n_ch = node.getNumChildren()
                    has_t2 = any(
                        node.getChild(j).getTypeId().getName().getString() == "Texture2"
                        for j in range(n_ch)
                    )
                    if has_t2:
                        for j in range(n_ch):
                            sub = node.getChild(j)
                            if sub.getTypeId().getName().getString() == "Material":
                                vals.append(round(float(sub.transparency[0]), 3) if sub.transparency.getNum() else 0.0)
                        return vals
                    for i in range(n_ch):
                        vals.extend(_coin_tex_batch_transparencies(node.getChild(i)))
                    return sorted(vals)

                tr_vals = _coin_tex_batch_transparencies(re_root.ViewObject.Proxy.tex_root)
                assert 0.0 in tr_vals and 0.5 in tr_vals and 0.75 not in tr_vals, tr_vals

                # (b) Setting Transparency=60 keeps faces TEXTURED at 40% opacity in both Coin3D and export!
                re_root.ViewObject.DiffuseColor = dc_orig
                re_root.ViewObject.Transparency = 60
                Gui.updateGui()
                assert len(_collect_coin_tex2_nodes(re_root.ViewObject.RootNode)) >= 2
                tr_60 = _coin_tex_batch_transparencies(re_root.ViewObject.Proxy.tex_root)
                assert all(abs(v - 0.6) < 0.02 for v in tr_60), tr_60
                tr60_skp = os.path.join(OUT, "transp_60_export.skp")
                importSKP.export([re_root], tr60_skp)
                tr60_rt = openskp.SkpFile.open(tr60_skp).parse()
                tr60_tmats = [m for m in tr60_rt.materials if m.texture is not None]
                assert len(tr60_tmats) >= 2 and all(abs(m.transparency - 0.4) < 0.03 for m in tr60_tmats), [
                    (m.name, m.transparency) for m in tr60_rt.materials
                ]

                # Review 2 point 5: recolouring a whole multi-face object (len(DiffuseColor) == 1)
                # survives recompute() and .FCStd save/reopen without being overwritten!
                rb_before = re_root.ViewObject.Proxy._rebuild_count
                re_root.ViewObject.LineColor = (1.0, 0.0, 0.0)
                re_root.ViewObject.DiffuseColor = [(0.1, 0.8, 0.2, 1.0)]
                Gui.updateGui()
                assert re_root.ViewObject.Proxy._rebuild_count > rb_before
                re_doc.recompute()
                assert len(re_root.ViewObject.DiffuseColor) == 1
                assert importSKP._rgb_key(re_root.ViewObject.DiffuseColor[0]) == (26, 204, 51)
                whole_col_fcstd = os.path.join(OUT, "whole_recolored.FCStd")
                re_doc.saveAs(whole_col_fcstd)
                App.closeDocument(re_doc.Name)

                wc_doc = App.openDocument(whole_col_fcstd)
                wc_doc.recompute()
                wc_root = [o for o in wc_doc.Objects if o.Label == "textures_uv_2017 geometry"][0]
                assert len(wc_root.ViewObject.DiffuseColor) == 1, len(wc_root.ViewObject.DiffuseColor)
                assert importSKP._rgb_key(wc_root.ViewObject.DiffuseColor[0]) == (26, 204, 51)
                App.closeDocument(wc_doc.Name)
            else:
                view_21 = _show(tdoc, "textures_uv_2021_iso")
                _check_bounds("textures_uv_2021_hierarchy", _drawn_bounds(view_21), tex_uv_hier_bounds_in, mm, tol=0.5)
                App.closeDocument(tdoc.Name)

        # Merged layout GUI check + Review point 1 (move & rotate textured object: 1x placement, 0 UV swim)
        mdoc = App.newDocument("TexturesMerged")
        importSKP.import_skp(tex_uv_2017, mdoc, merge=True, textures=True)
        mview = _show(mdoc, "textures_uv_merged_top", lambda v: v.viewTop())
        _check_bounds("textures_uv_merged", _drawn_bounds(mview), tex_uv_hier_bounds_in, mm, tol=0.5)
        mobj = [o for o in mdoc.Objects if o.TypeId == "Part::FeaturePython"][0]
        uvs_before_move = _extract_coin_texture_state(mobj.ViewObject.RootNode)[1]
        mobj.Placement = App.Placement(
            App.Vector(20.0 * mm, 10.0 * mm, 5.0 * mm),
            App.Rotation(App.Vector(0.0, 0.0, 1.0), 90.0),
        )
        Gui.updateGui()
        _show(mdoc, "textures_uv_merged_moved_rotated", lambda v: v.viewTop())
        bb_act = coin.SoGetBoundingBoxAction(mview.getViewer().getSoRenderManager().getViewportRegion())
        bb_act.apply(mobj.ViewObject.RootNode)
        m_box = bb_act.getBoundingBox()
        m_lo, m_hi = m_box.getMin().getValue(), m_box.getMax().getValue()
        m_node_bounds = tuple((m_lo[i], m_hi[i]) for i in range(3))
        _check_bounds(
            "textures_merged_moved_rotated",
            m_node_bounds,
            ((-12.0, 20.0), (10.0, 72.0), (1.0 + 5.0, 13.0 + 5.0)),
            mm,
            tol=0.5,
        )
        uvs_after_move = _extract_coin_texture_state(mobj.ViewObject.RootNode)[1]
        assert uvs_after_move == uvs_before_move, "moving/rotating textured object swam its local UVs!"
        assert importSKP._unplaced_shape(mobj).isPartner(mobj.Shape), "unplaced shape must satisfy isPartner in real FreeCAD"

        # Review 3 Must 1 & late-store GUI checks in real Coin3D:
        # (1) Single-face NaN in UV matrix keeps remaining faces textured while drawing the bad face solid:
        import json
        import Part

        def _count_coin_fsets(node):
            cnt = 0
            if not hasattr(node, "getNumChildren"):
                return 0
            for i in range(node.getNumChildren()):
                ch = node.getChild(i)
                if ch.getTypeId().getName().getString() == "IndexedFaceSet":
                    cnt += 1
                cnt += _count_coin_fsets(ch)
            return cnt

        orig_m_uv = mobj.SketchUpFaceUVsJSON
        m_uv_nan = json.loads(orig_m_uv)
        m_uv_nan["faces"][0][0][3] = [float("nan")] + [0.0] * 8
        mobj.SketchUpFaceUVsJSON = json.dumps(m_uv_nan, allow_nan=True)
        Gui.updateGui()
        assert len(_collect_coin_tex2_nodes(mobj.ViewObject.Proxy.tex_root)) >= 1, "good faces must stay textured when one face has NaN UV"
        assert _count_coin_fsets(mobj.ViewObject.Proxy.tex_root) >= 2, "both solid fallback batch and textured batch must exist"

        # (2) Refused/corrupt SketchUpFaceUVsJSON draws solid from DiffuseColor instead of disappearing:
        mobj.SketchUpFaceUVsJSON = "{refused_corrupt_json"
        Gui.updateGui()
        assert _count_coin_fsets(mobj.ViewObject.Proxy.tex_root) >= 1, "object disappeared on refused UV JSON!"

        # (3) Changing Shape face count (e.g. Refine / replacing with a 6-face box) draws all 6 faces solid:
        mobj.SketchUpFaceUVsJSON = orig_m_uv
        Gui.updateGui()
        mobj.Shape = Part.makeBox(50.0, 50.0, 50.0)
        Gui.updateGui()
        assert _count_coin_fsets(mobj.ViewObject.Proxy.tex_root) >= 1, "object disappeared when Shape face count changed!"

        # (4) Unlinked pasted object whose material was unknown at first rebuilds to textured when store links later:
        m_store = [o for o in mdoc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
        late_doc = App.newDocument("LateStoreGui")
        try:
            importSKP._PROCESS_MAT_CACHE.clear()
            importSKP._PROCESS_TEX_CACHE.clear()
            fp_late = late_doc.addObject("Part::FeaturePython", "LatePasteObj")
            fp_late.addProperty("App::PropertyLink", "SketchUpTextureStore", "SketchUp", "")
            fp_late.addProperty("App::PropertyString", "SketchUpFaceUVsJSON", "SketchUp", "")
            fp_late.Shape = Part.makeFace(
                Part.makePolygon([App.Vector(0, 0, 0), App.Vector(100, 0, 0), App.Vector(100, 100, 0), App.Vector(0, 100, 0), App.Vector(0, 0, 0)]),
                "Part::FaceMakerSimple",
            )
            one_rec = json.loads(orig_m_uv)["faces"][0]
            fp_late.SketchUpFaceUVsJSON = json.dumps({"v": 1, "faces": [one_rec]})
            fp_late.Proxy = importSKP._TexturedObjectProxy(fp_late)
            fp_late.ViewObject.DiffuseColor = [(0.9, 0.16, 0.75, 1.0)]
            fp_late.Proxy._ensure_gui_view_provider(fp_late)
            Gui.updateGui()
            assert fp_late.ViewObject.Proxy._had_unresolved_textures is True
            assert _count_coin_fsets(fp_late.ViewObject.Proxy.tex_root) >= 1
            assert len(_collect_coin_tex2_nodes(fp_late.ViewObject.Proxy.tex_root)) == 0
            # Now supply the store pack into late_doc -> must rebuild into textured!
            s_mats, s_blobs = importSKP._read_store_pack(m_store)
            late_s = importSKP._ensure_document_texture_store(late_doc, dict(s_blobs.items()), s_mats)
            importSKP._notify_texture_store_updated(late_s)
            Gui.updateGui()
            assert fp_late.SketchUpTextureStore == late_s
            assert len(_collect_coin_tex2_nodes(fp_late.ViewObject.Proxy.tex_root)) >= 1
        finally:
            App.closeDocument(late_doc.Name)

        App.closeDocument(mdoc.Name)

        # Review point 5: Mirrored two-sided textured components (textures_mirrored_2017.skp)
        mir_path = os.path.join(TESTS, "fixtures", "textures_mirrored_2017.skp")
        mir_doc = App.newDocument("TexturesMirrored")
        importSKP.import_skp(mir_path, mir_doc, merge=False, textures=True)
        mir_stats = importSKP.last_import_stats
        assert mir_stats.get("mirrored_copies") == 2 and mir_stats["links"] == 2, mir_stats
        _show(mir_doc, "textures_mirrored_top", lambda v: v.viewTop())
        _show(mir_doc, "textures_mirrored_bottom", lambda v: v.viewBottom())
        mir_top_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textures_mirrored_top.png"))
        mir_bot_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textures_mirrored_bottom.png"))
        # Top view shows front Red/Green/Yellow/Magenta on Card_Normal & Card_MirrorX AND back cyan stripes on Z-mirrored Card_MirrorZ!
        assert {"red", "green", "magenta", "yellow_or_chartreuse", "bright_cyan_stripe"}.issubset(set(mir_top_hues)), (
            f"textures_mirrored_top.png missing expected front+Z-mirrored-back hues: {mir_top_hues}"
        )
        assert {"red", "green", "magenta", "yellow_or_chartreuse", "bright_cyan_stripe"}.issubset(set(mir_bot_hues)), (
            f"textures_mirrored_bottom.png missing expected back+Z-mirrored-front hues: {mir_bot_hues}"
        )
        # Review 2 point 10c: user colour set on a flipped face (Card_MirrorZ) exports to .skp!
        mz_gui = [o for o in mir_doc.Objects if o.Label == "Card_MirrorZ (mirrored copy)"][0]
        mz_gui.ViewObject.DiffuseColor = [(0.9, 0.1, 0.2, 1.0)]
        Gui.updateGui()
        mz_exp = os.path.join(OUT, "flipped_recolor_rt.skp")
        importSKP.export([mz_gui], mz_exp)
        mz_rt = openskp.SkpFile.open(mz_exp).parse()
        assert any(
            m.texture is None and max(abs(a - b) for a, b in zip(m.color[:3], (230, 26, 51))) <= 2
            for m in mz_rt.materials
        ), [(m.name, m.color, m.texture is not None) for m in mz_rt.materials]
        App.closeDocument(mir_doc.Name)

        # Review point 6: Open fitted curved cylinder (Part.Cylinder) with distinct front & back textures
        import struct
        import zlib
        from openskp.model import Material, Texture

        def _gui_png(w, h, fn):
            raw = b"".join(
                bytes([0] + [ch for x in range(w) for ch in fn(x, y)])
                for y in range(h)
            )
            def _ck(t, p):
                return struct.pack(">I", len(p)) + t + p + struct.pack(">I", zlib.crc32(t + p) & 0xFFFFFFFF)
            return (
                b"\x89PNG\r\n\x1a\n"
                + _ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + _ck(b"IDAT", zlib.compress(raw, 6))
                + _ck(b"IEND", b"")
            )

        cyl_front_png = _gui_png(
            32, 32, lambda x, y: (230, 35, 195, 255) if ((x // 8) + (y // 8)) % 2 == 0 else (245, 225, 25, 255)
        )
        cyl_back_png = _gui_png(
            32, 32, lambda x, y: (15, 220, 240, 255) if (y // 6) % 2 == 0 else (20, 35, 115, 255)
        )
        orig_skp_parse = openskp.SkpFile.parse

        def _open_cyl_parse(self):
            m = orig_skp_parse(self)
            mf = Material(
                id=201, name="CylFront", color=(230, 35, 195, 255), transparency=1.0,
                texture=Texture(filename="cf.png", width=8.0, height=6.0, data=cyl_front_png),
            )
            mb = Material(
                id=202, name="CylBack", color=(15, 220, 240, 255), transparency=1.0,
                texture=Texture(filename="cb.png", width=5.0, height=5.0, data=cyl_back_png),
            )
            m.materials.extend([mf, mb])
            m.materials_by_id[201] = mf
            m.materials_by_id[202] = mb
            for d in [m.root, *m.definitions.values()]:
                for cid in [fid for fid, f in d.faces.items() if f.normal and abs(f.normal[2]) > 0.9]:
                    d.faces.pop(cid, None)
                for f in d.faces.values():
                    f.material_id = 201
                    f.back_material_id = 202
            return m

        openskp.SkpFile.parse = _open_cyl_parse
        cyl_doc = App.newDocument("TexturedOpenCylinder")
        try:
            importSKP.import_skp(
                os.path.join(TESTS, "fixtures", "cylinder_2017.skp"),
                cyl_doc,
                merge=True,
                curved_surfaces=True,
                textures=True,
            )
        finally:
            openskp.SkpFile.parse = orig_skp_parse
        cyl_stats = importSKP.last_import_stats
        assert cyl_stats["cylinders_built"] == 1 and cyl_stats["textured_objects"] == 1, cyl_stats
        _show(cyl_doc, "textured_cylinder_front_back")
        cyl_hues = _sample_viewport_color_buckets(os.path.join(OUT, "textured_cylinder_front_back.png"))
        assert {"magenta", "yellow_or_chartreuse", "bright_cyan_stripe"}.issubset(set(cyl_hues)), (
            f"textured_cylinder_front_back.png missing outer checker or inner back stripes: {cyl_hues}"
        )
        App.closeDocument(cyl_doc.Name)

        # Colorized textures (legacy_textured_colorize_2017.skp: plain, tint, shift, tint_half)
        import struct

        col_fixture = os.path.join(TESTS, "fixtures", "legacy_textured_colorize_2017.skp")
        orig_col = openskp.SkpFile.open(col_fixture).parse()
        cb = openskp.open_existing(col_fixture)[0]
        pat_head = bytes([255, 255, 255, 255, 0, 255, 255, 255, 255]) + b"\xff\xfe\xff\x00" + struct.pack("<II", 1, 0)
        mbuf = bytes(cb._material_writer.buf)
        pos = 0
        patched = bytearray()
        for src_m in orig_col.materials:
            j = mbuf.index(pat_head, pos)
            patched.extend(mbuf[pos:j])
            cr, cg, cb_v, ca = src_m.color
            use_op = 1 if src_m.transparency < 1.0 - 1e-6 else 0
            stored_op = (1.0 - src_m.transparency) if use_op else 1.0
            patched.extend(
                bytes([120, 40, 120, 255, 0, cr, cg, cb_v, ca])
                + b"\xff\xfe\xff\x00"
                + struct.pack("<IIdB", 2 if src_m.colorized else 1, int(src_m.colorize_type or 0), stored_op, use_op)
            )
            pos = j + len(pat_head) + 9
        patched.extend(mbuf[pos:])
        cb._material_writer.buf = patched
        for idx, mname in enumerate(("plain", "tint", "shift", "tint_half")):
            x0 = idx * 1.5
            cb.add_face(
                [(x0, 0.0, 0.1), (x0 + 1.0, 0.0, 0.1), (x0 + 1.0, 1.0, 0.1), (x0, 1.0, 0.1)],
                material=cb.materials_by_name[mname],
            )
        col_skp_out = os.path.join(OUT, "colorize_quads.skp")
        with open(col_skp_out, "wb") as fh:
            fh.write(cb.to_bytes())
        col_doc = App.newDocument("Colorize2017")
        importSKP.import_skp(col_skp_out, col_doc, merge=True, textures=True)
        col_stats = importSKP.last_import_stats
        assert col_stats["textures_loaded"] == 1 and col_stats["textured_materials"] == 4, col_stats
        _show(col_doc, "colorize_2017_top", lambda v: v.viewTop())
        col_hues = _sample_viewport_color_buckets(os.path.join(OUT, "colorize_2017_top.png"))
        assert {"red", "cyan_or_blue", "green", "yellow_or_chartreuse", "magenta"}.issubset(set(col_hues)), (
            f"colorize_2017_top.png missing expected plain/tint/shift hues: {col_hues}"
        )
        App.closeDocument(col_doc.Name)

        # Real-world architectural model: capilla_quiroz_v17.skp (Spanish roof tile texture,
        # translucent textured safety glass, solid blue stained glass, and back-painted concrete
        # steps 'grada'), rendered from isometric + front-right camera + standalone 'grada' steps
        # to compare directly against SketchUp's own embedded thumbnails!
        cap_path = os.path.join(TESTS, "fixtures", "capilla_quiroz_v17.skp")
        cap_doc = App.newDocument("CapillaTextured")
        importSKP.import_skp(cap_path, cap_doc, merge=False, textures=True)
        cap_stats = importSKP.last_import_stats
        assert cap_stats["textures_loaded"] >= 3 and cap_stats["textures_failed"] == 0, cap_stats
        cap_geom = {o.Label: o for o in cap_doc.Objects if o.TypeId in ("Part::Feature", "Part::FeaturePython")}
        assert cap_geom["grada geometry"].TypeId == "Part::FeaturePython"
        assert cap_geom["puerta geometry"].TypeId == "Part::FeaturePython"
        assert cap_geom["capilla_quiroz_v17 geometry"].TypeId == "Part::FeaturePython"
        _show(cap_doc, "capilla_textured_iso")

        def _capilla_front_cam(v):
            cam = v.getCameraNode()
            # Frame the right chapel facade + roof + steps through stained glass (matching SketchUp thumb 0)
            eye = coin.SbVec3f(335.0 * mm, -40.0 * mm, 130.0 * mm)
            target = coin.SbVec3f(255.0 * mm, 240.0 * mm, 80.0 * mm)
            cam.position.setValue(eye)
            cam.pointAt(target, coin.SbVec3f(0.0, 0.0, 1.0))
            if hasattr(cam, "height"):
                cam.height.setValue(5200.0)

        _show(cap_doc, "capilla_textured_front", _capilla_front_cam, fit=False)

        # Show 'grada' concrete steps definition alone in its own view to compare with SketchUp thumb 2
        cap_comps_grp = cap_doc.getObject(cap_stats["components_group"])
        cap_model_part = cap_doc.getObject(cap_stats["model_part"])
        cap_model_part.ViewObject.Visibility = False
        cap_comps_grp.ViewObject.Visibility = True
        for cpart in cap_comps_grp.Group:
            cpart.ViewObject.Visibility = (cpart.Label == "grada")
        _show(cap_doc, "capilla_grada_steps", lambda v: v.viewFront())
        _show(cap_doc, "capilla_grada_steps_iso")
        App.closeDocument(cap_doc.Name)

        # Spreadsheet-in-front and hidden no-3D-view imports with textures=True: zero errors!
        sp_tex_doc = App.newDocument("textures_spreadsheet_front")
        sp_sheet = sp_tex_doc.addObject("Spreadsheet::Sheet", "Sheet")
        sp_sheet.ViewObject.doubleClicked()
        Gui.updateGui()
        importSKP.import_skp(tex_uv_2017, sp_tex_doc, merge=False, textures=True)
        assert importSKP.last_import_stats["textured_objects"] == 3
        App.closeDocument(sp_tex_doc.Name)

        nv_tex_doc = App.newDocument("textures_no_3d_view", hidden=True)
        importSKP.import_skp(tex_uv_2017, nv_tex_doc, merge=False, textures=True)
        assert importSKP.last_import_stats["textured_objects"] == 3
        App.closeDocument(nv_tex_doc.Name)

        # Subprocess GUI check WITHOUT the add-on installed: open saved .FCStd in a clean
        # FreeCAD GUI process (no importSKP on sys.path / Mod), verify shapes AND solid
        # DiffuseColor render visibly, and save textures_opened_without_addon.png!
        repo_root = os.path.dirname(TESTS)
        apprun_cand = os.path.join(repo_root, "squashfs-root", "AppRun")
        if os.path.isfile(apprun_cand):
            no_addon_dir = os.path.join(OUT, "no_addon_gui")
            no_addon_user = os.path.join(no_addon_dir, "user")
            os.makedirs(no_addon_user, exist_ok=True)
            no_addon_png = os.path.join(OUT, "textures_opened_without_addon.png")
            no_addon_json = os.path.join(no_addon_dir, "result.json")
            no_addon_script = os.path.join(no_addon_dir, "open_without_addon.py")
            with open(no_addon_script, "w", encoding="utf-8") as fh:
                fh.write(
                    "import json, os, sys, traceback\n"
                    f"repo = {repo_root!r}\n"
                    "sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != os.path.abspath(repo)]\n"
                    "from PySide import QtCore, QtGui\n"
                    "def run_check():\n"
                    "    try:\n"
                    "        try:\n"
                    "            import importSKP\n"
                    "            addon_loaded = True\n"
                    "        except ImportError:\n"
                    "            addon_loaded = False\n"
                    "        import FreeCAD as App, FreeCADGui as Gui\n"
                    "        from pivy import coin\n"
                    f"        doc = App.openDocument({saved_fcstd!r})\n"
                    "        App.setActiveDocument(doc.Name)\n"
                    "        gdoc = Gui.getDocument(doc.Name)\n"
                    "        Gui.ActiveDocument = gdoc\n"
                    "        view = gdoc.activeView()\n"
                    "        if hasattr(view, 'setAnimationEnabled'):\n"
                    "            view.setAnimationEnabled(False)\n"
                    "        view.viewTop()\n"
                    "        view.fitAll()\n"
                    "        win = Gui.getMainWindow()\n"
                    "        for tree in win.findChildren(QtGui.QTreeWidget):\n"
                    "            tree.expandAll()\n"
                    "        Gui.updateGui()\n"
                    f"        win.grab().save({no_addon_png!r})\n"
                    "        reg = view.getViewer().getSoRenderManager().getViewportRegion()\n"
                    "        act = coin.SoGetBoundingBoxAction(reg)\n"
                    "        act.apply(view.getViewer().getSceneGraph())\n"
                    "        box = act.getBoundingBox()\n"
                    "        lo, hi = box.getMin().getValue(), box.getMax().getValue()\n"
                    "        bounds = [[float(lo[i]), float(hi[i])] for i in range(3)]\n"
                    f"        open({no_addon_json!r}, 'w').write(json.dumps({{'ok': True, 'addon_loaded': addon_loaded, 'bounds': bounds}}))\n"
                    "        os._exit(0)\n"
                    "    except BaseException:\n"
                    f"        open({no_addon_json!r}, 'w').write(json.dumps({{'ok': False, 'err': traceback.format_exc()}}))\n"
                    "        os._exit(1)\n"
                    "QtCore.QTimer.singleShot(2500, run_check)\n"
                    "QtCore.QTimer.singleShot(60000, lambda: os._exit(2))\n"
                )
            na_env = dict(os.environ)
            na_env.pop("PYTHONPATH", None)
            na_env["FREECAD_USER_DATA"] = no_addon_user
            na_proc = subprocess.run(
                [apprun_cand, no_addon_script],
                cwd=no_addon_dir,
                env=na_env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert os.path.isfile(no_addon_json), (
                f"no-addon GUI subprocess wrote no result (rc={na_proc.returncode}):\n"
                f"{na_proc.stdout}\n{na_proc.stderr}"
            )
            na_res = json.loads(open(no_addon_json, encoding="utf-8").read())
            assert na_res.get("ok"), na_res
            assert na_res["addon_loaded"] is False, na_res
            na_bounds = tuple((b[0], b[1]) for b in na_res["bounds"])
            _check_bounds("opened_without_addon", na_bounds, tex_uv_hier_bounds_in, mm, tol=0.5)
            na_hues = _sample_viewport_color_buckets(no_addon_png)
            assert "salmon_coral" in na_hues and "cyan_or_blue" in na_hues, (
                f"saved .FCStd opened without add-on did not render solid DiffuseColor hues: {na_hues}"
            )
            shutil.rmtree(no_addon_dir, ignore_errors=True)

        lines.append(
            "textures (#30): selective Part::FeaturePython + Coin3D SoTexture2 verified on 2017/2021 UV/projected, "
            "colorize (plain/tint/shift/tint_half), capilla_quiroz_v17, .FCStd reopen, copy/undo, and without add-on"
        )

        _finish("PASS\n" + "\n".join(lines), 0)
    except BaseException:
        _finish("FAIL\n" + traceback.format_exc(), 1)


from PySide import QtCore  # noqa: E402

# Let the main window finish starting before touching views.
QtCore.QTimer.singleShot(3000, main)
# A hard stop if anything hangs.
QtCore.QTimer.singleShot(240000, lambda: _finish("FAIL\ntimed out", 1))
