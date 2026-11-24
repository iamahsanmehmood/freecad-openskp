"""Empirical feasibility probe for textured materials on import (#30).

Exercises FreeCAD 1.1's material, appearance, and Coin3D view-provider APIs
both headlessly (freecadcmd) and under xvfb in FreeCADGui (called from
tests/gui_hierarchy.py) to establish ground truth before building #30:

1. ``App.Material`` and ``Part::Feature`` / ``ViewProviderPartExt.ShapeAppearance``.
2. ``Materials`` workbench (``Materials.MaterialManager``, appearance models,
   texture properties, and whether ``ShapeAppearance`` / ``ViewProviderPartExt``
   creates any Coin3D texture nodes from them).
3. Direct ``pivy.coin`` scenegraph injection (``SoTexture2`` +
   ``SoTextureCoordinate2``) onto a C++ ``Part::Feature``'s ``RootNode``,
   checking single-texture scope, ``recompute()``, and ``.FCStd`` save/reopen.
4. Custom ``Part::FeaturePython`` + Python ``ViewProvider`` proxy storing
   embedded texture bytes + ``openskp`` ``compute_face_uv`` coordinates in
   ``.FCStd``-persisted properties, building per-material ``SoTexture2`` +
   ``SoTextureCoordinate2`` + ``SoIndexedFaceSet`` display subgraphs while
   retaining the exact B-rep ``Part.Shape``, tested across:
   - multi-texture + untextured faces on one compound,
   - ``App::Link`` component instancing inside ``App::Part`` (hierarchy mode),
   - ``.FCStd`` ``saveAs`` -> ``closeDocument`` -> ``openDocument`` reload,
   - image format decoding via FreeCAD's bundled ``PySide.QtGui.QImage``.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _harness  # noqa: E402


def _make_png_rgba(width, height, pixel_fn):
    """Writes a tiny valid RGBA PNG in pure stdlib for deterministic probes."""
    raw_rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            r, g, b, a = pixel_fn(x, y)
            row.extend((r & 0xFF, g & 0xFF, b & 0xFF, a & 0xFF))
        raw_rows.append(bytes(row))
    raw = b"".join(raw_rows)

    def chunk(tag, payload):
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _checker_png_a():
    # High-contrast magenta / yellow 64x64 2x2 checker
    return _make_png_rgba(
        64,
        64,
        lambda x, y: (230, 30, 180, 255) if ((x // 32 + y // 32) % 2 == 0) else (250, 220, 30, 255),
    )


def _stripes_png_b():
    # High-contrast cyan / navy horizontal stripes
    return _make_png_rgba(
        64,
        64,
        lambda x, y: (20, 210, 230, 255) if ((y // 16) % 2 == 0) else (20, 40, 160, 255),
    )


def _coin_node_types(node, out=None):
    """Collects all Coin3D node type names reachable under ``node``."""
    if out is None:
        out = []
    if node is None:
        return out
    tname = node.getTypeId().getName()
    out.append(tname)
    if hasattr(node, "getNumChildren"):
        try:
            n = node.getNumChildren()
            for i in range(n):
                _coin_node_types(node.getChild(i), out)
        except Exception:
            pass
    return out


class _TexturedObjectProxy:
    """Minimal Part::FeaturePython object proxy preserving exact B-rep Shape."""

    def execute(self, fp):
        pass

    def dumps(self):
        return {}

    def loads(self, state):
        return None


class _TexturedViewProviderProxy:
    """Minimal Python ViewProvider proxy building a Coin3D 'Textured' display
    mode with one SoSeparator per textured material (SoTexture2 + custom UVs)
    plus untextured solid-colour faces, rebuilt automatically on .FCStd load."""

    def __init__(self, vobj=None):
        if vobj is not None:
            vobj.Proxy = self

    def attach(self, vobj):
        from pivy import coin

        self.vobj = vobj
        self.tex_root = coin.SoSeparator()
        self.tex_root.setName("OpenSKPTexturedRoot")
        vobj.addDisplayMode(self.tex_root, "Textured")
        self._rebuild(vobj.Object)

    def getDisplayModes(self, vobj):
        return ["Textured"]

    def getDefaultDisplayMode(self):
        return "Textured"

    def setDisplayMode(self, mode):
        return mode

    def updateData(self, fp, prop):
        if prop in ("Shape", "TexturePayloadJSON"):
            self._rebuild(fp)

    def onChanged(self, vobj, prop):
        pass

    def dumps(self):
        return {}

    def loads(self, state):
        return None

    def _decode_png_to_coin_image(self, tex_node, png_bytes):
        from pivy import coin
        from PySide import QtGui

        qimg = QtGui.QImage.fromData(png_bytes)
        if not qimg.isNull():
            qimg = qimg.convertToFormat(QtGui.QImage.Format_RGBA8888)
            qimg = qimg.mirrored(False, True)
            w, h = qimg.width(), qimg.height()
            try:
                buf = qimg.constBits()
                raw = buf.tobytes() if hasattr(buf, "tobytes") else bytes(buf)
                tex_node.image.setValue(coin.SbVec2s(w, h), 4, raw[: w * h * 4])
                tex_node.model = coin.SoTexture2.MODULATE
                self.used_in_memory_image = True
                return True
            except Exception as exc:
                self.in_memory_error = repr(exc)
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png_bytes)
            tex_node.filename.setValue(tmp_path)
            tex_node.model = coin.SoTexture2.MODULATE
            self.used_in_memory_image = False
            return True
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _rebuild(self, fp):
        from pivy import coin

        if not hasattr(self, "tex_root") or self.tex_root is None:
            return
        self.tex_root.removeAllChildren()
        raw_json = getattr(fp, "TexturePayloadJSON", "") or ""
        if not raw_json:
            return
        payload = json.loads(raw_json)

        hints = coin.SoShapeHints()
        hints.vertexOrdering = coin.SoShapeHints.COUNTERCLOCKWISE
        hints.shapeType = coin.SoShapeHints.UNKNOWN_SHAPE_TYPE
        self.tex_root.addChild(hints)

        # 1. Textured face groups (one SoSeparator + SoTexture2 per distinct image)
        textures_b64 = payload.get("textures_b64", {})
        for grp in payload.get("textured_groups", []):
            sep = coin.SoSeparator()
            png_bytes = base64.b64decode(textures_b64[grp["tex_id"]])
            tex = coin.SoTexture2()
            self._decode_png_to_coin_image(tex, png_bytes)
            sep.addChild(tex)

            mat = coin.SoMaterial()
            r, g, b, a = grp.get("tint_rgba", [1.0, 1.0, 1.0, 1.0])
            mat.diffuseColor.setValue(r, g, b)
            if a < 1.0:
                mat.transparency.setValue(1.0 - a)
            sep.addChild(mat)

            coords = coin.SoCoordinate3()
            coords.point.setValues(0, len(grp["positions"]), grp["positions"])
            sep.addChild(coords)

            tcoords = coin.SoTextureCoordinate2()
            tcoords.point.setValues(0, len(grp["uvs"]), grp["uvs"])
            sep.addChild(tcoords)

            tbind = coin.SoTextureCoordinateBinding()
            tbind.value = coin.SoTextureCoordinateBinding.PER_VERTEX_INDEXED
            sep.addChild(tbind)

            fset = coin.SoIndexedFaceSet()
            flat_idx = []
            for tri in grp["triangles"]:
                flat_idx.extend([tri[0], tri[1], tri[2], -1])
            fset.coordIndex.setValues(0, len(flat_idx), flat_idx)
            fset.textureCoordIndex.setValues(0, len(flat_idx), flat_idx)
            sep.addChild(fset)
            self.tex_root.addChild(sep)

        # 2. Untextured solid-colour faces in the same object
        untex = payload.get("untextured_faces", [])
        if untex:
            sep = coin.SoSeparator()
            mat = coin.SoMaterial()
            colors = [u["rgba"][:3] for u in untex]
            transp = [1.0 - u["rgba"][3] for u in untex]
            mat.diffuseColor.setValues(0, len(colors), colors)
            mat.transparency.setValues(0, len(transp), transp)
            sep.addChild(mat)

            mbind = coin.SoMaterialBinding()
            mbind.value = coin.SoMaterialBinding.PER_FACE
            sep.addChild(mbind)

            all_pts = []
            flat_idx = []
            for u in untex:
                base = len(all_pts)
                all_pts.extend(u["positions"])
                for tri in u["triangles"]:
                    flat_idx.extend([base + tri[0], base + tri[1], base + tri[2], -1])

            coords = coin.SoCoordinate3()
            coords.point.setValues(0, len(all_pts), all_pts)
            sep.addChild(coords)

            fset = coin.SoIndexedFaceSet()
            fset.coordIndex.setValues(0, len(flat_idx), flat_idx)
            sep.addChild(fset)
            self.tex_root.addChild(sep)


def probe_headless():
    """Runs headless feasibility checks (freecadcmd + GUI)."""
    import FreeCAD as App
    import Part
    import importSKP

    from openskp._face_groups import compute_face_uv, face_uv_basis

    report = {
        "freecad_version": App.Version()[:4],
        "gui_up": bool(App.GuiUp),
    }

    # 1. App.Material capabilities
    mat = App.Material()
    mat_attrs = sorted(a for a in dir(mat) if not a.startswith("__"))
    report["app_material_attrs"] = mat_attrs
    report["app_material_has_texture_attr"] = any("tex" in a.lower() or "img" in a.lower() for a in mat_attrs)

    doc = App.newDocument("ProbeHeadless")
    try:
        f0 = Part.makePlane(100.0, 100.0, App.Vector(0, 0, 0))
        f1 = Part.makePlane(100.0, 100.0, App.Vector(120, 0, 0))
        pf = doc.addObject("Part::Feature", "TwoFaces")
        pf.Shape = Part.makeCompound([f0, f1])
        report["part_feature_props"] = sorted(pf.PropertiesList)

        # 2. Materials module (FreeCAD 1.1 Material/Appearance system)
        mat_mod_info = {"available": False}
        try:
            import Materials

            mgr = Materials.MaterialManager()
            mat_mod_info["available"] = True
            mat_mod_info["materials_dir"] = sorted(a for a in dir(Materials) if not a.startswith("_"))
            mat_mod_info["manager_attrs"] = sorted(a for a in dir(mgr) if not a.startswith("_"))
            tex_models = {}
            all_mats = mgr.Materials
            mat_mod_info["bundled_material_count"] = len(all_mats)
            for uuid_key, m_obj in list(all_mats.items()):
                app_props = list(getattr(m_obj, "AppearanceProperties", {}).keys())
                tex_keys = [k for k in app_props if "tex" in k.lower() or "image" in k.lower() or "url" in k.lower()]
                if tex_keys:
                    tex_models[m_obj.Name] = {
                        "uuid": uuid_key,
                        "tex_keys": tex_keys,
                        "all_appearance_keys": sorted(app_props),
                    }
                    if len(tex_models) >= 3:
                        break
            mat_mod_info["sample_textured_materials"] = tex_models
            if hasattr(Materials, "ModelManager"):
                mmgr = Materials.ModelManager()
                model_hits = {}
                for muuid, mod in getattr(mmgr, "Models", {}).items():
                    mname = getattr(mod, "Name", str(muuid))
                    props = sorted(getattr(mod, "Properties", {}).keys()) if hasattr(mod, "Properties") else []
                    if any("tex" in p.lower() or "image" in p.lower() for p in props) or "tex" in mname.lower() or "render" in mname.lower():
                        model_hits[mname] = props
                mat_mod_info["texture_or_render_models"] = model_hits
        except Exception as exc:
            mat_mod_info["error"] = repr(exc)
        report["materials_module"] = mat_mod_info

        # 3. Verify openskp texture + UV extraction on capilla_quiroz_v17.skp
        cap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "capilla_quiroz_v17.skp")
        skp = importSKP.openskp.SkpFile.open(cap_path)
        skp_model = skp.parse()
        tex_mats = [m for m in skp_model.materials if m.texture is not None and m.texture.data]
        uv_checked = 0
        for defn in [skp_model.root, *skp_model.definitions.values()]:
            for face in defn.faces.values():
                m = skp_model.materials_by_id.get(face.material_id) if face.material_id is not None else None
                if m is not None and m.texture is not None and m.texture.data and face.normal:
                    xr, yr = face_uv_basis(face.normal)
                    tw = m.texture.width or 10.0
                    th = m.texture.height or 10.0
                    for edge_id, sign in face.loops[0]:
                        edge = defn.edges[edge_id]
                        vid = edge.v1_id if sign >= 0 else edge.v2_id
                        v = defn.vertices[vid]
                        u, vv = compute_face_uv((v.x, v.y, v.z), xr, yr, face.uv_transform, tw, th)
                        assert isinstance(u, float) and isinstance(vv, float)
                        uv_checked += 1
                    if uv_checked >= 12:
                        break
            if uv_checked >= 12:
                break
        report["capilla_textured_materials"] = [
            {
                "name": m.name,
                "filename": m.texture.filename,
                "bytes": len(m.texture.data),
                "tile_in": [m.texture.width, m.texture.height],
                "colorized": m.colorized,
            }
            for m in tex_mats
        ]
        report["capilla_uv_vertices_checked"] = uv_checked
        assert len(tex_mats) == 4 and uv_checked >= 4, report

        # 4. Check QtGui.QImage format support in headless AppImage too
        qimage_info = {"available": False}
        try:
            from PySide import QtGui

            fmts = sorted(bytes(f).decode("ascii", "ignore") for f in QtGui.QImageReader.supportedImageFormats())
            qimg = QtGui.QImage.fromData(_checker_png_a())
            qimage_info = {
                "available": True,
                "supported_formats": fmts,
                "decoded_png_size": [qimg.width(), qimg.height(), qimg.isNull()],
            }
        except Exception as exc:
            qimage_info["error"] = repr(exc)
        report["qimage"] = qimage_info
    finally:
        App.closeDocument(doc.Name)

    return report


def probe_gui(out_dir, show_fn):
    """Runs GUI-only Coin3D, ShapeAppearance, App::Link, and .FCStd save/reopen
    experiments under xvfb-run FreeCAD, saving report JSON + screenshots."""
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from pivy import coin
    from openskp._face_groups import compute_face_uv, face_uv_basis

    report = probe_headless()
    gui_findings = {}

    # --- Experiment A & B: ViewProviderPartExt + ShapeAppearance + Materials ---
    doc_ab = App.newDocument("TexProbeAB")
    f0 = Part.makePlane(100.0, 100.0, App.Vector(0, 0, 0))
    f1 = Part.makePlane(100.0, 100.0, App.Vector(120, 0, 0))
    pf = doc_ab.addObject("Part::Feature", "StandardPartFeature")
    pf.Shape = Part.makeCompound([f0, f1])
    doc_ab.recompute()
    Gui.updateGui()

    vo = pf.ViewObject
    gui_findings["viewprovider_part_props"] = sorted(vo.PropertiesList)
    gui_findings["viewprovider_display_modes"] = list(vo.listDisplayModes())
    gui_findings["default_root_coin_nodes"] = _coin_node_types(vo.RootNode)

    # Check if assigning a textured Materials.Material via ShapeAppearance adds any SoTexture2
    shape_app_tex_nodes = []
    shape_app_error = None
    try:
        import Materials

        mgr = Materials.MaterialManager()
        textured_uuid = None
        for uuid_key, m_obj in mgr.Materials.items():
            app_props = getattr(m_obj, "AppearanceProperties", {})
            if any("tex" in k.lower() or "image" in k.lower() for k in app_props):
                textured_uuid = uuid_key
                break
        m0 = App.Material()
        m0.DiffuseColor = (0.9, 0.2, 0.2, 0.0)
        if textured_uuid and hasattr(mgr.getMaterial(textured_uuid), "getMaterialAppearance"):
            m0 = mgr.getMaterial(textured_uuid).getMaterialAppearance()
        m1 = App.Material()
        m1.DiffuseColor = (0.2, 0.8, 0.3, 0.0)
        vo.ShapeAppearance = [m0, m1]
        Gui.updateGui()
        shape_app_tex_nodes = [n for n in _coin_node_types(vo.RootNode) if "Texture" in n]
    except Exception as exc:
        shape_app_error = repr(exc)
    gui_findings["shape_appearance_texture_coin_nodes"] = shape_app_tex_nodes
    gui_findings["shape_appearance_error"] = shape_app_error

    # --- Experiment C: Direct Coin3D injection on C++ Part::Feature + save/reopen ---
    doc_c = App.newDocument("TexProbeCoinRaw")
    pfc = doc_c.addObject("Part::Feature", "RawCoinBox")
    pfc.Shape = Part.makeCompound([f0, f1])
    doc_c.recompute()
    Gui.updateGui()

    png_c_path = os.path.join(out_dir, "_probe_checker.png")
    with open(png_c_path, "wb") as fh:
        fh.write(_checker_png_a())

    tex_c = coin.SoTexture2()
    tex_c.filename.setValue(png_c_path)
    pfc.ViewObject.RootNode.insertChild(tex_c, 0)
    Gui.updateGui()
    before_types_c = [n for n in _coin_node_types(pfc.ViewObject.RootNode) if "Texture" in n]
    show_fn(doc_c, "tex_probe_raw_coin_before_save")

    fcstd_c = os.path.join(out_dir, "probe_raw_coin.FCStd")
    doc_c.saveAs(fcstd_c)
    App.closeDocument(doc_c.Name)
    doc_c2 = App.openDocument(fcstd_c)
    Gui.updateGui()
    pfc2 = doc_c2.getObject("RawCoinBox")
    after_types_c = [n for n in _coin_node_types(pfc2.ViewObject.RootNode) if "Texture" in n]
    show_fn(doc_c2, "tex_probe_raw_coin_after_reopen")
    gui_findings["raw_coin_on_part_feature"] = {
        "texture_nodes_before_save": before_types_c,
        "texture_nodes_after_reopen": after_types_c,
        "survives_save_reopen": bool(after_types_c),
    }

    # --- Experiment D: Part::FeaturePython + Python ViewProvider + App::Link + save/reopen ---
    doc_d = App.newDocument("TexProbeFeaturePython")
    face_a = Part.makePlane(100.0, 100.0, App.Vector(0, 0, 0))
    face_b = Part.makePlane(100.0, 100.0, App.Vector(115, 0, 0))
    face_c = Part.makePlane(100.0, 100.0, App.Vector(230, 0, 0))
    compound = Part.makeCompound([face_a, face_b, face_c])

    def build_group_from_brep_face(brep_face, tex_id, uv_xform, tile_in):
        verts_mm, tris = brep_face.tessellate(0.5)
        xr, yr = face_uv_basis((0.0, 0.0, 1.0))
        positions = [(v.x, v.y, v.z) for v in verts_mm]
        uvs = [
            compute_face_uv((v.x / 25.4, v.y / 25.4, v.z / 25.4), xr, yr, uv_xform, tile_in[0], tile_in[1])
            for v in verts_mm
        ]
        return {
            "tex_id": tex_id,
            "tint_rgba": [1.0, 1.0, 1.0, 1.0],
            "positions": positions,
            "uvs": uvs,
            "triangles": [list(t) for t in tris],
        }

    verts_c, tris_c = face_c.tessellate(0.5)
    # 45-degree rotated UV transform for face_b
    c45 = 0.70710678
    rot_uv = (c45, -c45, 0.0, c45, c45, 0.0, 0.0, 0.0, 1.0)
    payload = {
        "textures_b64": {
            "checker": base64.b64encode(_checker_png_a()).decode("ascii"),
            "stripes": base64.b64encode(_stripes_png_b()).decode("ascii"),
        },
        "textured_groups": [
            build_group_from_brep_face(face_a, "checker", None, (1.5, 1.5)),
            build_group_from_brep_face(face_b, "stripes", rot_uv, (2.0, 2.0)),
        ],
        "untextured_faces": [
            {
                "rgba": [0.15, 0.80, 0.25, 1.0],
                "positions": [(v.x, v.y, v.z) for v in verts_c],
                "triangles": [list(t) for t in tris_c],
            }
        ],
    }

    # Put inside an App::Part definition + App::Link placement to test hierarchy mode too!
    comp_part = doc_d.addObject("App::Part", "DefPart")
    comp_part.Visibility = False
    fp_obj = doc_d.addObject("Part::FeaturePython", "TexturedGeom")
    fp_obj.addProperty("App::PropertyString", "TexturePayloadJSON", "SketchUp", "Embedded textures + UVs")
    fp_obj.addProperty("App::PropertyFileIncluded", "EmbeddedSampleTex", "SketchUp", "Binary texture packed in FCStd")
    sample_tex_tmp = os.path.join(out_dir, "_sample_pack.png")
    with open(sample_tex_tmp, "wb") as fh:
        fh.write(_checker_png_a())
    fp_obj.EmbeddedSampleTex = sample_tex_tmp
    os.remove(sample_tex_tmp)
    fp_obj.Shape = compound
    fp_obj.TexturePayloadJSON = json.dumps(payload)
    fp_obj.Proxy = _TexturedObjectProxy()
    vp_proxy = _TexturedViewProviderProxy(fp_obj.ViewObject)
    fp_obj.ViewObject.DisplayMode = "Textured"
    comp_part.addObject(fp_obj)

    link1 = doc_d.addObject("App::Link", "PlacedInstance1")
    link1.LinkedObject = comp_part
    link1.Placement = App.Placement(App.Vector(0, 0, 0), App.Rotation())

    link2 = doc_d.addObject("App::Link", "PlacedInstance2")
    link2.LinkedObject = comp_part
    link2.Placement = App.Placement(App.Vector(0, 130, 0), App.Rotation(App.Vector(0, 0, 1), 15.0))

    doc_d.recompute()
    Gui.updateGui()
    before_d_nodes = [n for n in _coin_node_types(fp_obj.ViewObject.RootNode) if "Texture" in n]
    show_fn(doc_d, "tex_probe_featurepython_before_save")

    fcstd_d = os.path.join(out_dir, "probe_featurepython.FCStd")
    doc_d.saveAs(fcstd_d)
    fcstd_d_size = os.path.getsize(fcstd_d)
    import zipfile

    with zipfile.ZipFile(fcstd_d, "r") as zf:
        fcstd_entries = sorted(zf.namelist())
    # Remove any external temp png to prove zero external file dependency on reopen
    if os.path.exists(png_c_path):
        os.remove(png_c_path)
    App.closeDocument(doc_d.Name)

    doc_d2 = App.openDocument(fcstd_d)
    Gui.updateGui()
    fp_obj2 = doc_d2.getObject("TexturedGeom")
    after_d_nodes = [n for n in _coin_node_types(fp_obj2.ViewObject.RootNode) if "Texture" in n]
    after_d_mode = fp_obj2.ViewObject.DisplayMode
    after_d_faces = len(fp_obj2.Shape.Faces)
    reopened_inc_file = getattr(fp_obj2, "EmbeddedSampleTex", "")
    reopened_inc_exists = bool(reopened_inc_file and os.path.isfile(reopened_inc_file))
    show_fn(doc_d2, "tex_probe_featurepython_after_reopen")

    gui_findings["feature_python_viewprovider"] = {
        "display_modes": list(fp_obj2.ViewObject.listDisplayModes()),
        "active_display_mode_after_reopen": after_d_mode,
        "brep_face_count_after_reopen": after_d_faces,
        "used_in_memory_sosfimage": getattr(vp_proxy, "used_in_memory_image", None),
        "in_memory_error": getattr(vp_proxy, "in_memory_error", None),
        "property_file_included_reopened_ok": reopened_inc_exists,
        "fcstd_zip_entries": fcstd_entries,
        "texture_nodes_before_save": before_d_nodes,
        "texture_nodes_after_reopen": after_d_nodes,
        "fcstd_bytes": fcstd_d_size,
        "survives_save_reopen": (
            after_d_mode == "Textured"
            and after_d_faces == 3
            and reopened_inc_exists
            and before_d_nodes.count("SoTexture2") == 2
            and after_d_nodes.count("SoTexture2") == 2
        ),
    }

    for d in (doc_ab, doc_c2, doc_d2):
        try:
            App.closeDocument(d.Name)
        except Exception:
            pass

    report["gui_findings"] = gui_findings
    report_path = os.path.join(out_dir, "texture_feasibility_report.json")
    with open(report_path, "w") as fh:
        json.dump(report, fh, indent=2)

    assert gui_findings["feature_python_viewprovider"]["survives_save_reopen"], gui_findings
    return report


def main():
    report = probe_headless()
    print("texture_feasibility headless:", json.dumps(report, indent=2))
    print("test_texture_feasibility: OK")


if __name__ == "__main__" or __name__ == "test_texture_feasibility":
    import FreeCAD as _App

    if not _App.GuiUp:
        _harness.run(main)
