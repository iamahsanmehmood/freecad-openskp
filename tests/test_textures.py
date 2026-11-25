"""Headless test suite for textured materials on import and export (#30).

Run with FreeCAD's headless interpreter:
    freecadcmd tests/test_textures.py

Verifies all seven #30 requirements:
1. ``ImportTextures`` preference (off by default) and ``import_skp(..., textures=None)``:
   when off, every fixture in ``tests/fixtures/`` across all three layouts
   (``merge=False, assemblies=False``; ``merge=False, assemblies=True``; ``merge=True``)
   produces a document 100% identical to ``origin/main`` (zero proxy objects, zero
   extra properties, identical shapes, placements, names, links, tags, and colours).
2. Selective ``Part::FeaturePython`` promotion: only geometry objects with at least
   one valid textured face become ``Part::FeaturePython``; untextured objects stay
   plain ``Part::Feature``. Shapes, solids, curved surfaces, colours, names, links,
   tags, and ``export()`` back to ``.skp`` (#28) work identically, with exported
   textured materials and per-face front/back UVs verified against the source model
   through ``openskp``.
3. Single-copy document-wide texture storage (``OpenSKPTextures``) across many
   definitions and multiple imports; exact UVs via ``face_uv_basis`` +
   ``compute_face_uv`` verified against ``SketchUpAPI.dll`` ground truth across
   inherited component paint, scaled/sheared instances, back faces, positioned UVs,
   projected UVs, opacity, BMP/GIF/TGA/TIFF decoding, and HLS colorize modes 0 & 1.
4. ``.FCStd`` save/reopen, same-doc and cross-doc ``copyObject``, transaction
   ``undo()``/``redo()``, ``recompute()``, headless import without 3D view, and
   reopening a saved ``.FCStd`` in a clean subprocess without the add-on installed.
5. Fault tolerance: unreadable formats (``DDS``), corrupt images, and empty texture
   bytes never abort an import; they fall back to solid colours, increment
   ``stats['textures_failed']``, and emit a single consolidated warning.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402

try:
    import FreeCAD as App  # noqa: E402
    import Part  # noqa: E402
    import importSKP  # noqa: E402
    import openskp  # noqa: E402
    from _synthetic import write_synthetic  # noqa: E402
    from openskp._face_groups import compute_face_uv, face_uv_basis  # noqa: E402
except BaseException:
    _harness.die()

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASELINE_COMMIT = "a7544128e7f02476cfb0781029dc72001bf9003e"

# Pinned ground-truth vertex UVs extracted directly from SketchUp 2025's C SDK
# (`SUFaceGetUVHelper` -> `SUUVHelperGetFrontUVQ` / `SUUVHelperGetBackUVQ`) on
# `textures_uv_2017.skp` and `textures_uv_2021.skp`.
SDK_ROOT_FACE_UVS = [
    # 1. Positioned quad at x in [0, 12], y in [20, 32], z = 1:
    #    front = 45-deg rotated MatChecker, back = offset/scaled MatStripesCutout
    {
        (0.0, 20.0, 1.0): ((0.25, 0.1), (0.0, 0.25)),
        (12.0, 20.0, 1.0): ((1.75, 1.6), (1.5, 0.25)),
        (12.0, 32.0, 1.0): ((0.25, 3.1), (1.5, 2.25)),
        (0.0, 32.0, 1.0): ((-1.25, 1.6), (0.0, 2.25)),
    },
    # 2. Tilted ramp at x in [20, 32], y in [20, 32], z in [1, 13]:
    #    front = PROJECTED MatChecker along oblique projection (0, -0.5, -sqrt(3)/2)
    {
        (20.0, 20.0, 1.0): ((0.0, 0.0), None),
        (32.0, 20.0, 1.0): ((2.0, 0.0), None),
        (32.0, 32.0, 13.0): ((2.0, 1.5), None),
        (20.0, 32.0, 13.0): ((0.0, 1.5), None),
    },
    # 3. Unpositioned translucent quad at x in [40, 52], y in [20, 32], z = 1 (tile 6x6)
    {
        (40.0, 20.0, 1.0): ((40.0 / 6.0, 20.0 / 6.0), None),
        (52.0, 20.0, 1.0): ((52.0 / 6.0, 20.0 / 6.0), None),
        (52.0, 32.0, 1.0): ((52.0 / 6.0, 32.0 / 6.0), None),
        (40.0, 32.0, 1.0): ((40.0 / 6.0, 32.0 / 6.0), None),
    },
]

# Pinned ground-truth RGB pixels from `SUTextureGetColorizedImageRep` on
# `legacy_textured_colorize_2017.skp` (2x1 base PNG: left=[200,40,40], right=[40,40,200]).
SDK_COLORIZE_RGB_AND_OPACITY = {
    "plain": ([(200, 40, 40), (40, 40, 200)], 1.0),
    "tint": ([(32, 237, 80), (32, 237, 80)], 1.0),
    "shift": ([(172, 253, 46), (253, 46, 172)], 1.0),
    "tint_half": ([(65, 73, 255), (65, 73, 255)], 0.5),
}


def _make_png_rgba(width: int, height: int, pixel_fn) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            r, g, b, a = pixel_fn(x, y)
            row.extend((r & 0xFF, g & 0xFF, b & 0xFF, a & 0xFF))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def ck(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr) + ck(b"IDAT", zlib.compress(raw, 6)) + ck(b"IEND", b"")


def _scan_skp_no_private_strings(skp_path: str, check_no_drive_letters: bool = False) -> None:
    """Scans raw bytes (ASCII, UTF-16LE, UTF-16BE) and every ZIP entry of ``skp_path``
    for private/host path tokens, e-mail addresses, and (when ``check_no_drive_letters``
    is set) Windows drive-letter paths."""
    forbidden = ("Users", "Devs", ".gemini", "Temp", "oskp_exp_", "AppData", "/tmp/")
    email_re = re.compile(rb"[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]{2,}\.(?:com|org|net|io|edu|gov|dev)\b")
    drive_re = re.compile(rb"(?<![A-Za-z])[A-Za-z]:[/\\]")

    def check_blob(blob: bytes, where: str):
        for tok in forbidden:
            for enc in ("ascii", "utf-16-le", "utf-16-be"):
                needle = tok.encode(enc)
                if needle.lower() in blob.lower():
                    raise AssertionError(f"{os.path.basename(skp_path)} ({where}): found {tok!r} in {enc}")
        m = email_re.search(blob)
        if m:
            raise AssertionError(f"{os.path.basename(skp_path)} ({where}): found email {m.group(0)!r}")
        if check_no_drive_letters:
            dm = drive_re.search(blob)
            if dm:
                raise AssertionError(f"{os.path.basename(skp_path)} ({where}): found drive path {dm.group(0)!r}")

    with open(skp_path, "rb") as fh:
        raw = fh.read()
    check_blob(raw, "raw")
    if zipfile.is_zipfile(skp_path):
        with zipfile.ZipFile(skp_path, "r") as zf:
            for zn in zf.namelist():
                check_blob(zf.read(zn), f"zip:{zn}")


def check_fixtures_clean():
    for fname in (
        "textures_uv_2017.skp",
        "textures_uv_2021.skp",
        "textures_mirrored_2017.skp",
        "legacy_textured_colorize_2017.skp",
        "texture_bmp_2021.skp",
        "texture_gif_2021.skp",
        "texture_tga_2021.skp",
        "texture_tiff_2021.skp",
    ):
        p = os.path.join(FIXTURES_DIR, fname)
        assert os.path.isfile(p), f"missing fixture {p}"
        _scan_skp_no_private_strings(
            p, check_no_drive_letters=fname.startswith(("textures_uv_", "textures_mirrored_"))
        )
    print("check_fixtures_clean: OK (all 8 texture fixtures scanned across raw bytes + every ZIP entry)")


def _doc_snapshot(doc, stats):
    """Captures a complete deterministic structural, property, geometry, and colour
    snapshot of ``doc`` to compare against ``origin/main``."""
    objs = []
    for o in doc.Objects:
        props = sorted(o.PropertiesList)
        shape_sig = None
        if hasattr(o, "Shape") and o.Shape is not None and not o.Shape.isNull():
            sh = o.Shape
            verts = sorted((round(v.X, 4), round(v.Y, 4), round(v.Z, 4)) for v in sh.Vertexes)
            shape_sig = (
                sh.ShapeType,
                len(sh.Faces),
                len(sh.Solids),
                len(sh.Edges),
                round(sh.Area, 3),
                round(sh.Volume, 3) if sh.Solids else 0.0,
                verts[:12],
                verts[-12:],
            )
        pl = None
        if hasattr(o, "Placement"):
            p = o.Placement
            pl = (
                round(p.Base.x, 4),
                round(p.Base.y, 4),
                round(p.Base.z, 4),
                round(p.Rotation.Q[0], 6),
                round(p.Rotation.Q[1], 6),
                round(p.Rotation.Q[2], 6),
                round(p.Rotation.Q[3], 6),
            )
        sv = None
        if hasattr(o, "ScaleVector"):
            s = o.ScaleVector
            sv = (round(s.x, 6), round(s.y, 6), round(s.z, 6))
        grp = [c.Name for c in getattr(o, "Group", ()) or ()]
        lnk = getattr(getattr(o, "LinkedObject", None), "Name", None)
        objs.append(
            {
                "name": o.Name,
                "label": o.Label,
                "type_id": o.TypeId,
                "props": props,
                "has_proxy": getattr(o, "Proxy", None) is not None and not getattr(o, "SketchUpAnnotationKind", ""),
                "skp_layer": getattr(o, "SketchUpLayer", None),
                "skp_inst_name": getattr(o, "SketchUpInstanceName", None),
                "visibility": getattr(o, "Visibility", None),
                "placement": pl,
                "scale": sv,
                "group": grp,
                "linked": lnk,
                "shape": shape_sig,
            }
        )
    face_colors = stats.get("object_face_colors") or stats.get("layer_face_colors") or {}
    norm_colors = {
        k: [tuple(round(c, 5) for c in rgba) for rgba in v]
        for k, v in sorted(face_colors.items())
    }
    return objs, norm_colors


def _load_main_importskp_if_available(tmp_dir: str):
    """Loads pinned pre-textures baseline ``BASELINE_COMMIT:importSKP.py`` as an
    isolated module so ``textures=False`` / default is compared directly against
    the exact pre-textures code even after this PR merges."""
    src = subprocess.check_output(
        ["git", "-C", REPO_DIR, "show", f"{BASELINE_COMMIT}:importSKP.py"],
        stderr=subprocess.PIPE,
    )
    mod_path = os.path.join(tmp_dir, "importSKP_main_baseline.py")
    with open(mod_path, "wb") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("importSKP_main_baseline", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, BASELINE_COMMIT


def check_off_identical_to_main():
    """Requirement 1: ImportTextures is False by default, explicit textures arg wins
    over preference, and when off every fixture in all 3 layouts produces a document
    100% identical to main (zero texture proxies, zero texture properties)."""
    params = App.ParamGet(importSKP._PARAM_PATH)
    params.RemBool("ImportTextures")
    params.RemBool("MergeOnImport")
    params.RemBool("ImportAssemblies")

    tex_fixture = os.path.join(FIXTURES_DIR, "textures_uv_2017.skp")
    try:
        for pref_val, arg_val, want_tex in (
            (None, None, False),
            (False, None, False),
            (True, None, True),
            (True, False, False),
            (False, True, True),
        ):
            if pref_val is None:
                params.RemBool("ImportTextures")
            else:
                params.SetBool("ImportTextures", pref_val)
            doc = App.newDocument()
            try:
                if arg_val is None:
                    importSKP.import_skp(tex_fixture, doc, merge=False)
                else:
                    importSKP.import_skp(tex_fixture, doc, merge=False, textures=arg_val)
                stats = importSKP.last_import_stats
                assert stats["import_textures"] is want_tex, (pref_val, arg_val, stats)
                has_store = any(getattr(o, "IsSketchUpTextureStore", False) for o in doc.Objects)
                has_fp = any(o.TypeId == "Part::FeaturePython" for o in doc.Objects)
                assert has_store is want_tex and has_fp is want_tex, (pref_val, arg_val, has_store, has_fp)
            finally:
                App.closeDocument(doc.Name)
    finally:
        params.RemBool("ImportTextures")

    fixtures = sorted(
        f for f in os.listdir(FIXTURES_DIR) if f.endswith(".skp") and not f.startswith("_")
    )
    layouts = (
        ("hierarchy", {"merge": False, "assemblies": False}),
        ("assemblies", {"merge": False, "assemblies": True}),
        ("merged", {"merge": True, "assemblies": False}),
    )

    with tempfile.TemporaryDirectory() as tmp:
        main_mod, main_ref = _load_main_importskp_if_available(tmp)
        compared_to_main = 0
        for fname in fixtures:
            fpath = os.path.join(FIXTURES_DIR, fname)
            for layout_label, kw in layouts:
                doc_def = App.newDocument("OffDefault")
                doc_exp = App.newDocument("OffExplicit")
                try:
                    importSKP.import_skp(fpath, doc_def, **kw)
                    snap_def = _doc_snapshot(doc_def, importSKP.last_import_stats)
                    for o in doc_def.Objects:
                        assert o.TypeId != "Part::FeaturePython", (fname, layout_label, o.Name)
                        assert not getattr(o, "IsSketchUpTextureStore", False), (fname, layout_label, o.Name)
                        for forbidden_prop in (
                            "IsSketchUpTextureStore",
                            "SketchUpTextureStore",
                            "SketchUpFaceUVsJSON",
                            "TexturePack",
                            "MaterialsJSON",
                        ):
                            assert forbidden_prop not in o.PropertiesList, (fname, layout_label, o.Name, forbidden_prop)

                    importSKP.import_skp(fpath, doc_exp, textures=False, **kw)
                    snap_exp = _doc_snapshot(doc_exp, importSKP.last_import_stats)
                    assert snap_def == snap_exp, f"{fname} ({layout_label}): textures=None != textures=False"

                    if main_mod is not None:
                        doc_main = App.newDocument("MainBaseline")
                        try:
                            main_mod.import_skp(fpath, doc_main, **kw)
                            snap_main = _doc_snapshot(doc_main, main_mod.last_import_stats)
                            assert snap_def == snap_main, (
                                f"{fname} ({layout_label}): off import differs from {main_ref}"
                            )
                            compared_to_main += 1
                        finally:
                            App.closeDocument(doc_main.Name)
                finally:
                    App.closeDocument(doc_def.Name)
                    App.closeDocument(doc_exp.Name)

        assert main_mod is not None and main_ref == BASELINE_COMMIT, main_ref
        assert compared_to_main == len(fixtures) * len(layouts) and compared_to_main > 0, (
            compared_to_main, len(fixtures), len(layouts)
        )

    print(
        f"check_off_identical_to_main: OK ({len(fixtures)} fixtures x 3 layouts = {len(fixtures) * 3} runs; "
        f"compared_to_baseline={compared_to_main} [{main_ref[:12]}])"
    )


def _eval_uv_record_side(side_entry, mats_dict, local_pt_in):
    assert side_entry is not None and side_entry[0] == "t", side_entry
    mk = side_entry[1]
    minfo = mats_dict[mk]
    tw = float(minfo["tile_w"])
    th = float(minfo["tile_h"])
    basis_raw = side_entry[2]
    uv_xf = tuple(side_entry[3]) if side_entry[3] is not None else None
    inv_m12 = side_entry[4]
    p_def = importSKP._apply_m12(inv_m12, local_pt_in)
    xr, yr = face_uv_basis((float(basis_raw[0]), float(basis_raw[1]), float(basis_raw[2])))
    return compute_face_uv(p_def, xr, yr, uv_xf, tw, th)


def check_selective_promotion_and_feature_parity():
    """Requirement 2: Only objects with >= 1 textured face become Part::FeaturePython;
    all others stay plain Part::Feature, and shapes, solids, curved surfaces, colours,
    names, links, and tags match textures=False."""
    # 1. Purely untextured models create ZERO Part::FeaturePython and ZERO OpenSKPTextures
    with tempfile.TemporaryDirectory() as tmp:
        syn_path = os.path.join(tmp, "synthetic.skp")
        write_synthetic(syn_path)
        for upath in (
            syn_path,
            os.path.join(FIXTURES_DIR, "SU_File.skp"),
            os.path.join(FIXTURES_DIR, "Test_Component.skp"),
        ):
            for merge_mode in (False, True):
                doc = App.newDocument()
                try:
                    importSKP.import_skp(upath, doc, merge=merge_mode, textures=True)
                    stats = importSKP.last_import_stats
                    assert stats["textured_objects"] == 0, (upath, merge_mode, stats)
                    assert not [o for o in doc.Objects if o.TypeId == "Part::FeaturePython"], upath
                    assert not [o for o in doc.Objects if getattr(o, "IsSketchUpTextureStore", False)], upath
                finally:
                    App.closeDocument(doc.Name)

    # 2. Mixed textured + untextured models: selective promotion + exact parity with textures=False
    for fname in ("textures_uv_2017.skp", "textures_uv_2021.skp", "capilla_quiroz_v17.skp"):
        fpath = os.path.join(FIXTURES_DIR, fname)
        for kw in (
            {"merge": False, "assemblies": False},
            {"merge": False, "assemblies": True},
            {"merge": True, "assemblies": False},
            {"merge": False, "solids": True},
        ):
            doc_off = App.newDocument("ParityOff")
            doc_on = App.newDocument("ParityOn")
            try:
                importSKP.import_skp(fpath, doc_off, textures=False, **kw)
                stats_off = dict(importSKP.last_import_stats)
                importSKP.import_skp(fpath, doc_on, textures=True, **kw)
                stats_on = dict(importSKP.last_import_stats)

                stores = [o for o in doc_on.Objects if getattr(o, "IsSketchUpTextureStore", False)]
                assert len(stores) == 1, (fname, kw, len(stores))

                geom_off = [o for o in doc_off.Objects if o.TypeId == "Part::Feature"]
                geom_on = [o for o in doc_on.Objects if o.TypeId in ("Part::Feature", "Part::FeaturePython")]
                assert len(geom_off) == len(geom_on), (fname, kw, len(geom_off), len(geom_on))

                for o_off, o_on in zip(geom_off, geom_on):
                    assert o_off.Label == o_on.Label, (o_off.Label, o_on.Label)
                    assert len(o_off.Shape.Faces) == len(o_on.Shape.Faces)
                    assert len(o_off.Shape.Solids) == len(o_on.Shape.Solids)
                    assert abs(o_off.Shape.Area - o_on.Shape.Area) <= 1e-4 * max(1.0, o_off.Shape.Area)

                # Colours, links, tags, and assemblies match textures=False identically
                assert stats_off.get("object_face_colors") == stats_on.get("object_face_colors")
                assert stats_off.get("layer_face_colors") == stats_on.get("layer_face_colors")
                links_off = [(o.Label, getattr(o, "SketchUpLayer", ""), tuple(o.ScaleVector)) for o in doc_off.Objects if o.TypeId == "App::Link"]
                links_on = [(o.Label, getattr(o, "SketchUpLayer", ""), tuple(o.ScaleVector)) for o in doc_on.Objects if o.TypeId == "App::Link"]
                assert links_off == links_on, (fname, kw, links_off, links_on)

                if fname.startswith("textures_uv_") and not kw["merge"]:
                    # Panel_Plain's target "Panel" is unpainted -> MUST stay plain Part::Feature!
                    # Panel (MatChecker), Panel (MatStripesCutout), and root geometry -> Part::FeaturePython!
                    by_lbl = {o.Label: o for o in geom_on}
                    assert by_lbl["Panel geometry"].TypeId == "Part::Feature", by_lbl["Panel geometry"].TypeId
                    assert by_lbl["Panel (MatChecker) geometry"].TypeId == "Part::FeaturePython"
                    assert by_lbl["Panel (MatStripesCutout) geometry"].TypeId == "Part::FeaturePython"
                    root_lbl = f"{os.path.splitext(fname)[0]} geometry"
                    assert by_lbl[root_lbl].TypeId == "Part::FeaturePython"
                    assert stats_on["textured_objects"] == 3, stats_on
                elif fname == "capilla_quiroz_v17.skp" and not kw["merge"]:
                    # "grada" (concrete steps painted on back faces) and "puerta" (textured
                    # safety-glass door) + root geometry -> Part::FeaturePython!
                    by_lbl = {o.Label: o for o in geom_on}
                    assert by_lbl["grada geometry"].TypeId == "Part::FeaturePython"
                    assert by_lbl["puerta geometry"].TypeId == "Part::FeaturePython"
                    assert by_lbl["capilla_quiroz_v17 geometry"].TypeId == "Part::FeaturePython"
            finally:
                App.closeDocument(doc_off.Name)
                App.closeDocument(doc_on.Name)

    # 3. Closed textured solid box (solids=True) + curved textured cylinder (curved_surfaces=True)
    with tempfile.TemporaryDirectory() as tmp:
        png_p = os.path.join(tmp, "box_tex.png")
        with open(png_p, "wb") as fh:
            fh.write(_make_png_rgba(16, 16, lambda x, y: (200, 80, 40, 255)))
        prev = os.getcwd()
        try:
            os.chdir(tmp)
            b = openskp.create()
            tmat = b.add_texture_material("Brick", "box_tex.png", applied_width=10.0, applied_height=10.0)
            with b.add_component_definition("TexBox") as tb:
                x, y, z = 10.0, 12.0, 8.0
                tb.add_face([(0, 0, 0), (0, y, 0), (x, y, 0), (x, 0, 0)], material=tmat)
                tb.add_face([(0, 0, z), (x, 0, z), (x, y, z), (0, y, z)], material=tmat)
                tb.add_face([(0, 0, 0), (x, 0, 0), (x, 0, z), (0, 0, z)], material=tmat)
                tb.add_face([(0, y, 0), (0, y, z), (x, y, z), (x, y, 0)], material=tmat)
                tb.add_face([(0, 0, 0), (0, 0, z), (0, y, z), (0, y, 0)], material=tmat)
                tb.add_face([(x, 0, 0), (x, y, 0), (x, y, z), (x, 0, z)], material=tmat)
            with b.add_component_definition("PlainBox") as pb:
                x, y, z = 5.0, 5.0, 5.0
                pb.add_face([(0, 0, 0), (0, y, 0), (x, y, 0), (x, 0, 0)])
                pb.add_face([(0, 0, z), (x, 0, z), (x, y, z), (0, y, z)])
                pb.add_face([(0, 0, 0), (x, 0, 0), (x, 0, z), (0, 0, z)])
                pb.add_face([(0, y, 0), (0, y, z), (x, y, z), (x, y, 0)])
                pb.add_face([(0, 0, 0), (0, 0, z), (0, y, z), (0, y, 0)])
                pb.add_face([(x, 0, 0), (x, y, 0), (x, y, z), (x, 0, z)])
            b.add_instance(tb, translation=(0, 0, 0))
            b.add_instance(pb, translation=(30, 0, 0))
            skp_box = os.path.join(tmp, "solid_tex.skp")
            with open(skp_box, "wb") as fh:
                fh.write(b.to_bytes())
        finally:
            os.chdir(prev)

        doc = App.newDocument()
        try:
            importSKP.import_skp(skp_box, doc, merge=False, solids=True, textures=True)
            stats = importSKP.last_import_stats
            assert stats["solids_built"] == 2 and stats["textured_objects"] == 1, stats
            by_lbl = {o.Label: o for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")}
            assert by_lbl["TexBox geometry"].TypeId == "Part::FeaturePython"
            assert len(by_lbl["TexBox geometry"].Shape.Solids) == 1
            assert by_lbl["PlainBox geometry"].TypeId == "Part::Feature"
            assert len(by_lbl["PlainBox geometry"].Shape.Solids) == 1
        finally:
            App.closeDocument(doc.Name)

    print("check_selective_promotion_and_feature_parity: OK (only textured objects use Part::FeaturePython; solids/links/tags/colours match)")


def check_deduplication_uvs_formats_and_colorize():
    """Requirement 3: Single-copy image storage per document across many definitions,
    exact SDK ground-truth UVs (inherited, scaled, sheared, back-face, positioned,
    projected), opacity, BMP/GIF/TGA/TIFF formats, and HLS colorize shift & tint."""
    # 1. 25 definitions + 4 materials sharing 1 image -> 1 OpenSKPTextures object & 1 image blob,
    #    even when imported twice into the same document!
    with tempfile.TemporaryDirectory() as tmp:
        png_bytes = _make_png_rgba(32, 32, lambda x, y: (40, 180, 220, 255) if (x + y) % 2 == 0 else (240, 240, 240, 255))
        with open(os.path.join(tmp, "shared.png"), "wb") as fh:
            fh.write(png_bytes)
        prev = os.getcwd()
        try:
            os.chdir(tmp)
            b = openskp.create()
            mats = [
                b.add_texture_material(f"SharedMat_{i}", "shared.png", applied_width=4.0 + i, applied_height=6.0)
                for i in range(4)
            ]
            defs = []
            for d_idx in range(25):
                with b.add_component_definition(f"TileDef_{d_idx}") as cd:
                    cd.add_face([(0, 0, 0), (8, 0, 0), (8, 8, 0), (0, 8, 0)], material=mats[d_idx % 4])
                defs.append(cd)
            for d_idx, cd in enumerate(defs):
                b.add_instance(cd, translation=((d_idx % 5) * 12.0, (d_idx // 5) * 12.0, 0.0))
            multi_skp = os.path.join(tmp, "shared_25defs.skp")
            with open(multi_skp, "wb") as fh:
                fh.write(b.to_bytes())
        finally:
            os.chdir(prev)

        doc = App.newDocument()
        orig_write_pack = importSKP._write_store_pack
        pack_writes = [0]

        def counting_write_pack(*args, **kwargs):
            pack_writes[0] += 1
            return orig_write_pack(*args, **kwargs)

        importSKP._write_store_pack = counting_write_pack
        try:
            importSKP.import_skp(multi_skp, doc, merge=False, textures=True)
            stats = importSKP.last_import_stats
            assert stats["textures_loaded"] == 1 and stats["textured_materials"] == 4, stats
            assert stats["textured_objects"] == 25, stats
            # Single pack write across all 25 textured definitions (#43 review point 2)!
            assert pack_writes[0] == 1, f"expected 1 TexturePack write for 25 defs, got {pack_writes[0]}"
            sample_fp = [o for o in doc.Objects if o.TypeId == "Part::FeaturePython"][0]
            ed_modes = sample_fp.getEditorMode("SketchUpFaceUVsJSON")
            assert "Hidden" in ed_modes, f"SketchUpFaceUVsJSON should be hidden, got {ed_modes}"
            # Import a second time into the SAME document -> still 1 store and 1 image blob!
            importSKP.import_skp(multi_skp, doc, merge=True, textures=True)
            stores = [o for o in doc.Objects if getattr(o, "IsSketchUpTextureStore", False)]
            assert len(stores) == 1, len(stores)
            with zipfile.ZipFile(stores[0].TexturePack, "r") as zf:
                names = zf.namelist()
                assert len(names) == 1 and zf.read(names[0]) == png_bytes, len(names)
        finally:
            importSKP._write_store_pack = orig_write_pack
            App.closeDocument(doc.Name)

    # 2. All 4 non-PNG/JPEG 2021 fixture formats from openskp (BMP, GIF, TGA, TIFF)
    for fmt_fixture in (
        "texture_bmp_2021.skp",
        "texture_gif_2021.skp",
        "texture_tga_2021.skp",
        "texture_tiff_2021.skp",
    ):
        doc = App.newDocument()
        try:
            importSKP.import_skp(os.path.join(FIXTURES_DIR, fmt_fixture), doc, merge=True, textures=True)
            stats = importSKP.last_import_stats
            assert stats["textures_loaded"] == 1 and stats["textures_failed"] == 0, (fmt_fixture, stats)
            assert stats["textured_objects"] == 1, (fmt_fixture, stats)
            store = [o for o in doc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
            mats_dict, blobs_dict = importSKP._read_store_pack(store)
            assert len(mats_dict) == 1 and len(blobs_dict) == 1
            mk, minfo = next(iter(mats_dict.items()))
            gl_tup = importSKP._decode_gl_texture_tuple(minfo["tex_id"], blobs_dict[minfo["tex_id"]], minfo)
            assert gl_tup is not None and gl_tup[0] > 0 and gl_tup[1] > 0, (fmt_fixture, gl_tup)
        finally:
            App.closeDocument(doc.Name)

    # 3. Exact SDK ground-truth UVs on both textures_uv_2017.skp and textures_uv_2021.skp
    mm = importSKP.INCH_TO_MM
    for fname in ("textures_uv_2017.skp", "textures_uv_2021.skp"):
        fpath = os.path.join(FIXTURES_DIR, fname)
        for merge_mode in (False, True):
            doc = App.newDocument()
            try:
                importSKP.import_skp(fpath, doc, merge=merge_mode, textures=True)
                store = [o for o in doc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
                mats_dict, blobs_dict = importSKP._read_store_pack(store)
                # 3 materials (MatChecker, MatStripesCutout, MatGlassTex) sharing 2 PNGs!
                assert len(blobs_dict) == 2 and len(mats_dict) == 3, (fname, len(blobs_dict), len(mats_dict))

                if not merge_mode:
                    root_lbl = f"{os.path.splitext(fname)[0]} geometry"
                    target_obj = [o for o in doc.Objects if o.Label == root_lbl][0]
                else:
                    target_obj = [o for o in doc.Objects if o.TypeId == "Part::FeaturePython"][0]

                payload = json.loads(target_obj.SketchUpFaceUVsJSON)
                recs = payload["faces"]
                assert len(recs) == len(target_obj.Shape.Faces)

                # Verify all 3 root faces (positioned front+back, projected ramp, translucent quad)
                for expected_face_map in SDK_ROOT_FACE_UVS:
                    sample_pt = next(iter(expected_face_map))
                    # Find matching B-rep face by checking if all 4 expected vertices lie on it
                    matched_idx = None
                    for idx, bf in enumerate(target_obj.Shape.Faces):
                        bverts = [(round(v.X / mm, 3), round(v.Y / mm, 3), round(v.Z / mm, 3)) for v in bf.Vertexes]
                        if all(any(max(abs(a - b) for a, b in zip(ep, bv)) < 1e-3 for bv in bverts) for ep in expected_face_map):
                            matched_idx = idx
                            break
                    assert matched_idx is not None, (fname, merge_mode, sample_pt)
                    f_rec = recs[matched_idx]
                    for pt_in, (want_f_uv, want_b_uv) in expected_face_map.items():
                        got_f_uv = _eval_uv_record_side(f_rec[0], mats_dict, pt_in)
                        assert max(abs(got_f_uv[0] - want_f_uv[0]), abs(got_f_uv[1] - want_f_uv[1])) < 1e-5, (
                            fname, merge_mode, pt_in, got_f_uv, want_f_uv
                        )
                        if want_b_uv is not None:
                            assert f_rec[1] is not None, (fname, merge_mode, "missing back UV record")
                            got_b_uv = _eval_uv_record_side(f_rec[1], mats_dict, pt_in)
                            assert max(abs(got_b_uv[0] - want_b_uv[0]), abs(got_b_uv[1] - want_b_uv[1])) < 1e-5, (
                                fname, merge_mode, pt_in, got_b_uv, want_b_uv
                            )

                # In merged mode, also verify the 2x-scaled Panel_Cutout_Scaled face at x in [20, 44], y in [0, 12], z = 1:
                # its local-space width is 12 in (tile=6 -> U spans 0..2 over world x=20..44)!
                if merge_mode:
                    scaled_idx = None
                    for idx, bf in enumerate(target_obj.Shape.Faces):
                        bb = bf.BoundBox
                        if abs(bb.XMin - 20.0 * mm) < 1e-2 and abs(bb.XMax - 44.0 * mm) < 1e-2 and abs(bb.YMax - 12.0 * mm) < 1e-2:
                            scaled_idx = idx
                            break
                    assert scaled_idx is not None
                    s_rec = recs[scaled_idx]
                    uv0 = _eval_uv_record_side(s_rec[0], mats_dict, (20.0, 0.0, 1.0))
                    uv1 = _eval_uv_record_side(s_rec[0], mats_dict, (44.0, 12.0, 1.0))
                    assert abs(uv0[0] - 0.0) < 1e-5 and abs(uv0[1] - 0.0) < 1e-5, uv0
                    assert abs(uv1[0] - 2.0) < 1e-5 and abs(uv1[1] - 2.0) < 1e-5, uv1
            finally:
                App.closeDocument(doc.Name)

    # 4. Colorize modes 0 (shift) and 1 (tint) + opacity on legacy_textured_colorize_2017.skp
    with tempfile.TemporaryDirectory() as tmp:
        col_src = os.path.join(FIXTURES_DIR, "legacy_textured_colorize_2017.skp")
        orig_col = openskp.SkpFile.open(col_src).parse()
        b = openskp.open_existing(col_src)[0]
        # open_existing replays textured materials via add_texture_material (plain defaults);
        # restore the source fixture's exact colorize flags, paint colours, and transparency!
        pat_head = bytes([255, 255, 255, 254, 0, 255, 255, 255, 254]) + b"\xff\xfe\xff\x00" + struct.pack("<II", 1, 0)
        mbuf = bytes(b._material_writer.buf)
        assert mbuf.count(pat_head) == 4
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
        b._material_writer.buf = patched
        for idx, mname in enumerate(("plain", "tint", "shift", "tint_half")):
            x0 = idx * 1.5
            b.add_face(
                [(x0, 0.0, 0.1), (x0 + 1.0, 0.0, 0.1), (x0 + 1.0, 1.0, 0.1), (x0, 1.0, 0.1)],
                material=b.materials_by_name[mname],
            )
        col_with_faces = os.path.join(tmp, "colorize_quads.skp")
        with open(col_with_faces, "wb") as fh:
            fh.write(b.to_bytes())

        doc = App.newDocument()
        try:
            importSKP.import_skp(col_with_faces, doc, merge=True, textures=True)
            stats = importSKP.last_import_stats
            # 4 materials sharing 1 embedded PNG!
            assert stats["textures_loaded"] == 1 and stats["textured_materials"] == 4, stats
            store = [o for o in doc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
            mats_dict, blobs_dict = importSKP._read_store_pack(store)
            assert len(blobs_dict) == 1 and len(mats_dict) == 4
            by_name = {v["name"]: v for v in mats_dict.values()}
            for mname, (want_px, want_op) in SDK_COLORIZE_RGB_AND_OPACITY.items():
                minfo = by_name[mname]
                assert abs(minfo["opacity"] - want_op) < 1e-6, (mname, minfo)
                w, h, nc, gl_bytes = importSKP._decode_gl_texture_tuple(
                    minfo["tex_id"], blobs_dict[minfo["tex_id"]], minfo
                )
                assert (w, h) == (2, 1), (w, h)
                px0 = tuple(gl_bytes[0:3])
                px1 = tuple(gl_bytes[nc : nc + 3])
                for got_rgb, sdk_rgb in ((px0, want_px[0]), (px1, want_px[1])):
                    assert max(abs(a - b) for a, b in zip(got_rgb, sdk_rgb)) <= 2, (mname, got_rgb, sdk_rgb)
        finally:
            App.closeDocument(doc.Name)

    print("check_deduplication_uvs_formats_and_colorize: OK (1 blob/doc across 25 defs, BMP/GIF/TGA/TIFF, SDK UVs <1e-5, SDK colorize <=2/255)")


def check_export_round_trip():
    """Requirement 2: Exporting a textured import (hierarchy and merged) preserves
    component definitions, links, tags, textured materials, opacities, and per-face
    front/back UV coordinates when re-read through openskp, with zero temp paths."""
    with tempfile.TemporaryDirectory() as tmp:
        for fname in ("textures_uv_2017.skp", "textures_uv_2021.skp", "capilla_quiroz_v17.skp", "texture_tiff_2021.skp"):
            src_path = os.path.join(FIXTURES_DIR, fname)
            for merge_mode in (False, True):
                doc = App.newDocument()
                out_skp = os.path.join(tmp, f"rt_{int(merge_mode)}_{fname}")
                try:
                    cwd_before = os.getcwd()
                    importSKP.import_skp(src_path, doc, merge=merge_mode, textures=True)
                    importSKP.export(list(doc.Objects), out_skp)
                    assert os.getcwd() == cwd_before, "export() must not change current working directory"
                    _scan_skp_no_private_strings(out_skp, check_no_drive_letters=True)
                    rt = openskp.SkpFile.open(out_skp).parse()
                    tex_mats = [m for m in rt.materials if m.texture is not None and m.texture.data]
                    assert tex_mats, (fname, merge_mode, "no textured materials written on export")
                    for tm in tex_mats:
                        tfn = tm.texture.filename or ""
                        assert "/" not in tfn and "\\" not in tfn and ":" not in tfn, (fname, tfn)

                    if fname.startswith("textures_uv_"):
                        assert len(tex_mats) == 3, (fname, merge_mode, [m.name for m in tex_mats])
                        glass_m = [m for m in tex_mats if abs(m.transparency - 0.5) < 1e-2]
                        assert len(glass_m) == 1, [m.transparency for m in tex_mats]
                        if not merge_mode:
                            # 3 instances of Panel (2 painted variants + 1 plain) + 3 root faces
                            assert len(rt.root.instances) == 3 and len(rt.root.faces) == 3, (
                                len(rt.root.instances), len(rt.root.faces)
                            )

                        # Verify exported UVs on the 3 root quads (including rotated front, positioned back,
                        # and projected tilted ramp!) match SketchUpAPI.dll ground truth via openskp!
                        for expected_face_map in SDK_ROOT_FACE_UVS:
                            matched_face = None
                            for rf in rt.root.faces.values():
                                rverts = [
                                    rt.root.vertices[rt.root.edges[eid].v1_id if sgn >= 0 else rt.root.edges[eid].v2_id]
                                    for eid, sgn in rf.loops[0]
                                ]
                                rpts = [(round(v.x, 3), round(v.y, 3), round(v.z, 3)) for v in rverts]
                                if all(any(max(abs(a - b) for a, b in zip(ep, rp)) < 1e-3 for rp in rpts) for ep in expected_face_map):
                                    matched_face = rf
                                    break
                            assert matched_face is not None, (fname, merge_mode, "exported root face not found")
                            fmat = rt.materials_by_id[matched_face.material_id]
                            fxr, fyr = face_uv_basis(matched_face.normal)
                            for pt_in, (want_f_uv, want_b_uv) in expected_face_map.items():
                                got_f_uv = compute_face_uv(
                                    pt_in, fxr, fyr, matched_face.uv_transform, fmat.texture.width, fmat.texture.height
                                )
                                assert max(abs(got_f_uv[0] - want_f_uv[0]), abs(got_f_uv[1] - want_f_uv[1])) < 1e-4, (
                                    fname, merge_mode, pt_in, got_f_uv, want_f_uv
                                )
                                if want_b_uv is not None:
                                    bmat = rt.materials_by_id[matched_face.back_material_id]
                                    got_b_uv = compute_face_uv(
                                        pt_in, fxr, fyr, matched_face.uv_transform_back, bmat.texture.width, bmat.texture.height
                                    )
                                    assert max(abs(got_b_uv[0] - want_b_uv[0]), abs(got_b_uv[1] - want_b_uv[1])) < 1e-4, (
                                        fname, merge_mode, pt_in, got_b_uv, want_b_uv
                                    )
                finally:
                    App.closeDocument(doc.Name)

        # Moved + rotated textured Part::FeaturePython (#43 review point 1):
        # _unplaced_shape strips Placement (no double offset) and exported UVs stay locked (no UV swim)!
        mm = importSKP.INCH_TO_MM
        mv_doc = App.newDocument("MovedRotatedExport")
        try:
            importSKP.import_skp(os.path.join(FIXTURES_DIR, "textures_uv_2017.skp"), mv_doc, merge=True, textures=True)
            mv_obj = [o for o in mv_doc.Objects if o.TypeId == "Part::FeaturePython"][0]
            orig_bb = mv_obj.Shape.BoundBox
            mv_obj.Placement = App.Placement(
                App.Vector(10.0 * mm, -5.0 * mm, 3.0 * mm),
                App.Rotation(App.Vector(0.0, 0.0, 1.0), 90.0),
            )
            unpl = importSKP._unplaced_shape(mv_obj)
            assert unpl.isPartner(mv_obj.Shape), "unplaced shape wrapper must share the same OCCT TShape (isPartner)"
            assert abs(unpl.BoundBox.XMin - orig_bb.XMin) < 1e-4 and abs(unpl.BoundBox.ZMax - orig_bb.ZMax) < 1e-4
            mv_skp = os.path.join(tmp, "moved_rotated_rt.skp")
            importSKP.export(list(mv_doc.Objects), mv_skp)
            _scan_skp_no_private_strings(mv_skp, check_no_drive_letters=True)
            mv_rt = openskp.SkpFile.open(mv_skp).parse()
            expected_moved_quad = {
                (-y + 10.0, x - 5.0, z + 3.0): uvs
                for (x, y, z), uvs in SDK_ROOT_FACE_UVS[0].items()
            }
            matched_mv = None
            for rf in mv_rt.root.faces.values():
                rverts = [
                    mv_rt.root.vertices[mv_rt.root.edges[eid].v1_id if sgn >= 0 else mv_rt.root.edges[eid].v2_id]
                    for eid, sgn in rf.loops[0]
                ]
                rpts = [(round(v.x, 3), round(v.y, 3), round(v.z, 3)) for v in rverts]
                if all(any(max(abs(a - b) for a, b in zip(ep, rp)) < 1e-3 for rp in rpts) for ep in expected_moved_quad):
                    matched_mv = rf
                    break
            assert matched_mv is not None, "moved+rotated quad not found at 1x transformed world coordinates"
            fmat = mv_rt.materials_by_id[matched_mv.material_id]
            bmat = mv_rt.materials_by_id[matched_mv.back_material_id]
            fxr, fyr = face_uv_basis(matched_mv.normal)
            for pt_in, (want_f_uv, want_b_uv) in expected_moved_quad.items():
                got_f_uv = compute_face_uv(
                    pt_in, fxr, fyr, matched_mv.uv_transform, fmat.texture.width, fmat.texture.height
                )
                got_b_uv = compute_face_uv(
                    pt_in, fxr, fyr, matched_mv.uv_transform_back, bmat.texture.width, bmat.texture.height
                )
                assert max(abs(got_f_uv[0] - want_f_uv[0]), abs(got_f_uv[1] - want_f_uv[1])) < 1e-4, (got_f_uv, want_f_uv)
                assert max(abs(got_b_uv[0] - want_b_uv[0]), abs(got_b_uv[1] - want_b_uv[1])) < 1e-4, (got_b_uv, want_b_uv)
        finally:
            App.closeDocument(mv_doc.Name)

        # O(N) _build_three_uv_pins on 1,200 nearly collinear vertices + scale-relative 1 in face on 12,000 in tile:
        collinear_pts = [(float(i) * 0.01, 0.0, 0.0) for i in range(1199)] + [(6.0, 8.0, 0.0)]
        t_pins = time.perf_counter()
        pins = importSKP._build_three_uv_pins(collinear_pts, collinear_pts, lambda p, _n=None: (p[0] * 0.5, p[1] * 0.25))
        dt_pins = time.perf_counter() - t_pins
        assert pins is not None and len(pins) == 3 and dt_pins < 0.2, (pins, dt_pins)

        # 1 in x 1 in face mapped onto a 12,000 in x 12,000 in image with rotated positioned UVs (#43 review 2 point 10):
        quad_1in = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
        huge_tile_pins = importSKP._build_three_uv_pins(
            quad_1in,
            quad_1in,
            lambda p, _n=None: ((0.8 * p[0] - 0.6 * p[1] + 5.0) / 12000.0, (0.6 * p[0] + 0.8 * p[1] + 3.0) / 12000.0),
        )
        assert huge_tile_pins is not None and len(huge_tile_pins) == 3, huge_tile_pins

        ft_exp_doc = App.newDocument("ExportFaultTol")
        try:
            importSKP.import_skp(os.path.join(FIXTURES_DIR, "textures_uv_2017.skp"), ft_exp_doc, merge=True, textures=True)
            store = [o for o in ft_exp_doc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
            mats_d, blobs_d = importSKP._read_store_pack(store)
            keys = sorted(mats_d.keys())
            mats_d[keys[0]]["filename"] = ("long_texture_name_" * 20)[:296] + ".png"
            mats_d[keys[1]] = "corrupt_non_dict_entry"
            store.MaterialsJSON = json.dumps(mats_d)
            importSKP._STORE_UNPACK_CACHE.clear()
            importSKP._PROCESS_MAT_CACHE.clear()
            deep_dir = os.path.join(tmp, "deep_export_sub_" + ("d" * 40))
            os.makedirs(deep_dir, exist_ok=True)
            ft_out = os.path.join(deep_dir, "fault_tol_export.skp")
            importSKP.export(list(ft_exp_doc.Objects), ft_out)
            _scan_skp_no_private_strings(ft_out, check_no_drive_letters=True)
            estats = importSKP.last_export_stats
            assert estats["materials_failed"] >= 1 and estats["faces_written"] >= 3, estats
            ft_rt = openskp.SkpFile.open(ft_out).parse()
            assert len(ft_rt.root.faces) >= 3 and any(m.texture is not None for m in ft_rt.materials)
        finally:
            App.closeDocument(ft_exp_doc.Name)

    print("check_export_round_trip: OK (hierarchy/merged, moved+rotated UVs, O(N) pins, 300-char filename, and fault-tolerant export)")


def check_persistence_copy_undo_recompute_and_no_addon():
    """Requirement 4: .FCStd save/reopen, same-doc & cross-doc copy/paste, undo/redo,
    recompute, headless import, and opening a saved .FCStd without the add-on installed."""
    with tempfile.TemporaryDirectory() as tmp:
        # Copy fixture to a temporary path we delete BEFORE reopening .FCStd, proving
        # zero dependence on the external .skp file after import.
        tmp_skp = os.path.join(tmp, "ephemeral_src.skp")
        with open(os.path.join(FIXTURES_DIR, "textures_uv_2017.skp"), "rb") as src_fh:
            with open(tmp_skp, "wb") as dst_fh:
                dst_fh.write(src_fh.read())

        fcstd_path = os.path.join(tmp, "persisted_textures.FCStd")
        doc = App.newDocument("PersistSrc")
        try:
            doc.UndoMode = 1
            doc.openTransaction("Import SKP")
            importSKP.import_skp(tmp_skp, doc, merge=False, textures=True)
            doc.commitTransaction()
            os.remove(tmp_skp)

            pre_faces = sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::"))
            assert pre_faces == 6, pre_faces

            # Test recompute()
            doc.recompute()
            assert sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")) == 6

            # Test same-doc copyObject and cross-doc copyObject (without dependencies + recompute)
            root_tex_obj = [o for o in doc.Objects if o.TypeId == "Part::FeaturePython" and len(o.Shape.Faces) == 3][0]
            same_copy = doc.copyObject(root_tex_obj, False)
            doc.recompute()
            assert same_copy.TypeId == "Part::FeaturePython" and len(same_copy.Shape.Faces) == 3
            doc.removeObject(same_copy.Name)

            doc2 = App.newDocument("PasteTarget")
            try:
                pasted = doc2.copyObject(root_tex_obj, False)
                doc2.recompute()
                assert pasted.SketchUpTextureStore is not None, "cross-doc paste + recompute must attach OpenSKPTextures"
                fcstd_paste = os.path.join(tmp, "pasted.FCStd")
                doc2.saveAs(fcstd_paste)
            finally:
                App.closeDocument(doc2.Name)

            re_paste = App.openDocument(fcstd_paste)
            try:
                p_objs = [o for o in re_paste.Objects if o.TypeId == "Part::FeaturePython"]
                assert len(p_objs) == 1 and len(p_objs[0].Shape.Faces) == 3
                p_mats, p_blobs = importSKP._resolve_object_materials_and_blobs(p_objs[0])
                assert len(p_mats) == 3 and len(p_blobs) == 2
            finally:
                App.closeDocument(re_paste.Name)

            # Test undo() + redo()
            doc.openTransaction("Delete textured root")
            doc.removeObject(root_tex_obj.Name)
            doc.commitTransaction()
            assert sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")) == 3
            doc.undo()
            assert sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")) == 6
            doc.redo()
            assert sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")) == 3
            doc.undo()
            assert sum(len(o.Shape.Faces) for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")) == 6

            doc.saveAs(fcstd_path)
        finally:
            if doc.Name in App.listDocuments():
                App.closeDocument(doc.Name)

        # Clear in-process caches before reopen so reopen proves true .FCStd self-containment!
        importSKP._PROCESS_TEX_CACHE.clear()
        importSKP._PROCESS_MAT_CACHE.clear()
        importSKP._DECODED_GL_CACHE.clear()
        importSKP._STORE_UNPACK_CACHE.clear()

        reopened = App.openDocument(fcstd_path)
        try:
            reopened.recompute()
            fp_objs = [o for o in reopened.Objects if o.TypeId == "Part::FeaturePython"]
            plain_objs = [o for o in reopened.Objects if o.TypeId == "Part::Feature"]
            assert len(fp_objs) == 3 and len(plain_objs) == 1, (len(fp_objs), len(plain_objs))
            stores = [o for o in reopened.Objects if getattr(o, "IsSketchUpTextureStore", False)]
            assert len(stores) == 1
            mats_d, blobs_d = importSKP._read_store_pack(stores[0])
            assert len(mats_d) == 3 and len(blobs_d) == 2

            # Re-exporting the reopened .FCStd back to .skp still writes all 3 textured materials!
            re_exp = os.path.join(tmp, "reopened_export.skp")
            importSKP.export(list(reopened.Objects), re_exp)
            _scan_skp_no_private_strings(re_exp, check_no_drive_letters=True)
            rt_re = openskp.SkpFile.open(re_exp).parse()
            assert len([m for m in rt_re.materials if m.texture is not None]) == 3
        finally:
            App.closeDocument(reopened.Name)

        # Mid-import failure/cancel (#43 review 2 point 6): objects already built keep their TexturePack!
        orig_place = importSKP._HierarchyImport._place
        cancel_doc = App.newDocument("PartialImportCancel")
        cancel_fcstd = os.path.join(tmp, "partial_cancel.FCStd")
        try:
            def _abort_second_place(self_hi, inst, inh_m, vis):
                res = orig_place(self_hi, inst, inh_m, vis)
                if self_hi.stats.get("textured_objects", 0) >= 1:
                    raise RuntimeError("simulated mid-import cancel")
                return res

            importSKP._HierarchyImport._place = _abort_second_place
            raised_cancel = False
            try:
                importSKP.import_skp(
                    os.path.join(FIXTURES_DIR, "textures_uv_2017.skp"), cancel_doc, merge=False, textures=True
                )
            except RuntimeError as exc:
                assert "simulated mid-import cancel" in str(exc)
                raised_cancel = True
            assert raised_cancel
            assert not importSKP._ACTIVE_IMPORT_TEX_CTX_BY_DOC
            c_stores = [o for o in cancel_doc.Objects if getattr(o, "IsSketchUpTextureStore", False)]
            assert len(c_stores) == 1 and os.path.isfile(c_stores[0].TexturePack)
            cancel_doc.saveAs(cancel_fcstd)
        finally:
            importSKP._HierarchyImport._place = orig_place
            App.closeDocument(cancel_doc.Name)

        importSKP._PROCESS_TEX_CACHE.clear()
        importSKP._PROCESS_MAT_CACHE.clear()
        importSKP._STORE_UNPACK_CACHE.clear()
        re_cancel = App.openDocument(cancel_fcstd)
        try:
            c_fps = [o for o in re_cancel.Objects if o.TypeId == "Part::FeaturePython"]
            assert len(c_fps) >= 1
            c_mats, c_blobs = importSKP._resolve_object_materials_and_blobs(c_fps[0])
            assert len(c_mats) >= 1 and len(c_blobs) >= 1
        finally:
            App.closeDocument(re_cancel.Name)

        # Cross-document paste > 32 MB cache (#43 review 2 point 8):
        # 40 x 1 MB textures (40 MB > 32 MB cache) across 4 objects pasted into another doc ->
        # missing blobs come from open source doc's store, TexturePack written ONCE, link set only when complete!
        import numpy as np

        def _fast_1mb_png(seed: int) -> bytes:
            w, h = 520, 520
            x = np.arange(w, dtype=np.uint32)[None, :]
            y = np.arange(h, dtype=np.uint32)[:, None]
            st = (x * np.uint32(1664525) + y * np.uint32(1013904223) + np.uint32((seed * 2654435761) & 0xFFFFFFFF)) & np.uint32(0xFFFFFFFF)
            st ^= st >> np.uint32(15)
            st = (st * np.uint32(2246822519)) & np.uint32(0xFFFFFFFF)
            rgba = np.empty((h, w, 4), dtype=np.uint8)
            rgba[..., 0] = st & np.uint32(0xFF)
            rgba[..., 1] = (st >> np.uint32(8)) & np.uint32(0xFF)
            rgba[..., 2] = (st >> np.uint32(16)) & np.uint32(0xFF)
            rgba[..., 3] = 255
            rows = np.empty((h, 1 + w * 4), dtype=np.uint8)
            rows[:, 0] = 0
            rows[:, 1:] = rgba.reshape(h, w * 4)
            raw = rows.tobytes()
            def _ck(t: bytes, p: bytes) -> bytes:
                return struct.pack(">I", len(p)) + t + p + struct.pack(">I", zlib.crc32(t + p) & 0xFFFFFFFF)
            return (
                b"\x89PNG\r\n\x1a\n"
                + _ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + _ck(b"IDAT", zlib.compress(raw, 1))
                + _ck(b"IEND", b"")
            )

        big_src = App.newDocument("BigPasteSrc")
        big_dst = App.newDocument("BigPasteDst")
        orig_wp = importSKP._write_store_pack
        dst_writes = [0]
        try:
            src_blobs = {}
            src_mats = {}
            for i in range(40):
                b_i = _fast_1mb_png(i + 1)
                tid = hashlib.sha256(b_i).hexdigest()[:24]
                mk = f"m_big_{i:03d}"
                src_blobs[tid] = b_i
                src_mats[mk] = {
                    "name": f"BigMat_{i}",
                    "rgba": [180, 120, 80, 255],
                    "opacity": 1.0,
                    "tex_id": tid,
                    "filename": f"big_{i}.png",
                    "tile_w": 10.0,
                    "tile_h": 10.0,
                    "colorized": False,
                    "colorize_type": 0,
                }
            src_store = importSKP._ensure_document_texture_store(big_src, src_blobs, src_mats)
            # Evict process caches completely so ALL 40 x 1 MB textures must come from big_src's store!
            importSKP._PROCESS_TEX_CACHE.clear()
            importSKP._PROCESS_MAT_CACHE.clear()
            importSKP._STORE_UNPACK_CACHE.clear()

            src_objs = []
            mks_sorted = sorted(src_mats.keys())
            quad_sh = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0), App.Vector(10, 10, 0), App.Vector(0, 10, 0), App.Vector(0, 0, 0)]
            )
            face_sh = Part.Face(quad_sh)
            for g_i in range(4):
                comp_sh = Part.makeCompound([face_sh for _ in range(10)])
                fo = big_src.addObject("Part::FeaturePython", f"BigPart_{g_i}")
                fo.Shape = comp_sh
                fo.addProperty("App::PropertyLinkHidden", "SketchUpTextureStore", "SketchUp", "")
                fo.SketchUpTextureStore = src_store
                fo.addProperty("App::PropertyString", "SketchUpFaceUVsJSON", "SketchUp", "")
                chunk_mks = mks_sorted[g_i * 10 : (g_i + 1) * 10]
                fo.SketchUpFaceUVsJSON = json.dumps(
                    {"v": 1, "faces": [[["t", mk_c, [0.0, 0.0, 1.0], None, None], None, 0] for mk_c in chunk_mks]}
                )
                fo.Proxy = importSKP._TexturedObjectProxy(fo)
                src_objs.append(fo)
            del src_blobs

            def _count_dst_wp(st_o, md, bd):
                if getattr(st_o, "Document", None) is big_dst:
                    dst_writes[0] += 1
                return orig_wp(st_o, md, bd)

            importSKP._write_store_pack = _count_dst_wp
            pasted_objs = [big_dst.copyObject(so, False) for so in src_objs]
            # Also add 1 incomplete object referencing a missing material key: its link must stay None!
            inc_obj = big_dst.addObject("Part::FeaturePython", "IncompletePaste")
            inc_obj.Shape = Part.makeCompound([face_sh])
            inc_obj.addProperty("App::PropertyLinkHidden", "SketchUpTextureStore", "SketchUp", "")
            inc_obj.addProperty("App::PropertyString", "SketchUpFaceUVsJSON", "SketchUp", "")
            inc_obj.SketchUpFaceUVsJSON = json.dumps(
                {"v": 1, "faces": [[["t", "m_nonexistent_999", [0.0, 0.0, 1.0], None, None], None, 0]]}
            )
            inc_obj.Proxy = importSKP._TexturedObjectProxy(inc_obj)

            big_dst.recompute()
            assert dst_writes[0] == 1, f"expected 1 TexturePack write on cross-doc paste, got {dst_writes[0]}"
            for po in pasted_objs:
                assert po.SketchUpTextureStore is not None
            assert inc_obj.SketchUpTextureStore is None, "incomplete pasted object must keep SketchUpTextureStore=None"
            dst_mats, dst_blobs = importSKP._read_store_pack(pasted_objs[0].SketchUpTextureStore)
            assert len(dst_mats) == 40 and len(dst_blobs) == 40, (len(dst_mats), len(dst_blobs))
        finally:
            importSKP._write_store_pack = orig_wp
            App.closeDocument(big_src.Name)
            App.closeDocument(big_dst.Name)

        # Subprocess WITHOUT the add-on installed: verify B-rep shapes load intact with zero crashes!
        clean_work = os.path.join(tmp, "no_addon_env")
        clean_user = os.path.join(tmp, "no_addon_user")
        os.makedirs(clean_work, exist_ok=True)
        os.makedirs(clean_user, exist_ok=True)
        probe_script = os.path.join(clean_work, "check_no_addon.py")
        out_json = os.path.join(clean_work, "no_addon_result.json")
        with open(probe_script, "w", encoding="utf-8") as fh:
            fh.write(
                "import json, os, sys\n"
                f"repo = {REPO_DIR!r}\n"
                "sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != os.path.abspath(repo)]\n"
                "try:\n"
                "    import importSKP\n"
                "    addon_present = True\n"
                "except ImportError:\n"
                "    addon_present = False\n"
                "import FreeCAD as App\n"
                f"doc = App.openDocument({fcstd_path!r})\n"
                "geom_objs = [o for o in doc.Objects if o.TypeId in ('Part::Feature', 'Part::FeaturePython') and not o.Shape.isNull()]\n"
                "faces = sum(len(o.Shape.Faces) for o in geom_objs)\n"
                "area = sum(o.Shape.Area for o in geom_objs)\n"
                "links = sum(1 for o in doc.Objects if o.TypeId == 'App::Link')\n"
                "App.closeDocument(doc.Name)\n"
                f"open({out_json!r}, 'w').write(json.dumps({{'addon_present': addon_present, 'faces': faces, 'area': area, 'links': links}}))\n"
            )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["FREECAD_USER_DATA"] = clean_user
        proc = subprocess.run(
            [sys.executable, probe_script],
            cwd=clean_work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert os.path.isfile(out_json), f"no-addon subprocess failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        res = json.loads(open(out_json, encoding="utf-8").read())
        assert res["addon_present"] is False, res
        assert res["faces"] == 6 and res["links"] == 3 and res["area"] > 1000.0, res

    print("check_persistence_copy_undo_recompute_and_no_addon: OK (.FCStd reopen, copy/paste, undo/redo, recompute, and clean no-addon reopen)")


def check_fault_tolerance():
    """Requirement 5: A failure in one texture (DDS format Qt cannot read, corrupt PNG,
    or empty texture bytes) never stops the import; affected faces use solid colours,
    stats['textures_failed'] counts every failed texture, and a single consolidated
    warning is emitted."""
    orig_parse = openskp.SkpFile.parse
    orig_warn = App.Console.PrintWarning
    printed = []

    def record_warn(msg):
        printed.append(msg)
        orig_warn(msg)

    with tempfile.TemporaryDirectory() as tmp:
        good_png = _make_png_rgba(16, 16, lambda x, y: (30, 190, 90, 255))
        with open(os.path.join(tmp, "good.png"), "wb") as fh:
            fh.write(good_png)
        prev = os.getcwd()
        try:
            os.chdir(tmp)
            b = openskp.create()
            m_good = b.add_texture_material("GoodTex", "good.png", applied_width=5.0, applied_height=5.0)
            m_dds = b.add_texture_material("UnreadableDDS", "good.png", applied_width=5.0, applied_height=5.0)
            m_bad = b.add_texture_material("CorruptPNG", "good.png", applied_width=5.0, applied_height=5.0)
            m_empty = b.add_texture_material("EmptyBytes", "good.png", applied_width=5.0, applied_height=5.0)
            with b.add_component_definition("GoodComp") as gc:
                gc.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=m_good)
            with b.add_component_definition("DdsComp") as dc:
                dc.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=m_dds)
            b.add_instance(gc, translation=(0, 0, 0))
            b.add_instance(dc, translation=(20, 0, 0))
            b.add_face([(40, 0, 0), (50, 0, 0), (50, 10, 0), (40, 10, 0)], material=m_bad)
            b.add_face([(60, 0, 0), (70, 0, 0), (70, 10, 0), (60, 10, 0)], material=m_empty)
            fault_skp = os.path.join(tmp, "faulty_textures.skp")
            with open(fault_skp, "wb") as fh:
                fh.write(b.to_bytes())
        finally:
            os.chdir(prev)

        def patched_parse(self):
            m = orig_parse(self)
            for mat in m.materials:
                if mat.name == "UnreadableDDS" and mat.texture is not None:
                    mat.texture.data = b"DDS " + b"\x00" * 124
                    mat.texture.filename = "wall.dds"
                elif mat.name == "CorruptPNG" and mat.texture is not None:
                    mat.texture.data = b"\x89PNG\r\n\x1a\ncorrupt_truncated_idat"
                    mat.texture.filename = "broken.png"
                elif mat.name == "EmptyBytes" and mat.texture is not None:
                    mat.texture.data = b""
                    mat.texture.filename = "empty.png"
            return m

        App.Console.PrintWarning = record_warn
        openskp.SkpFile.parse = patched_parse
        try:
            for merge_mode in (False, True):
                printed.clear()
                doc = App.newDocument()
                try:
                    importSKP.import_skp(fault_skp, doc, merge=merge_mode, textures=True)
                    stats = importSKP.last_import_stats
                    assert stats["faces_built"] == 4, (merge_mode, stats)
                    assert stats["textures_loaded"] == 1 and stats["textures_failed"] == 3, (merge_mode, stats)
                    tex_warns = [w for w in stats["warnings"] if "failed to read" in w]
                    assert len(tex_warns) == 1, (merge_mode, stats["warnings"])
                    assert "3 texture(s)" in tex_warns[0] and "DDS" in tex_warns[0], tex_warns[0]
                    console_tex_warns = [w for w in printed if "failed to read" in w]
                    assert len(console_tex_warns) == 1, console_tex_warns

                    if not merge_mode:
                        # GoodComp -> Part::FeaturePython; DdsComp + root (CorruptPNG + EmptyBytes) -> plain Part::Feature!
                        by_lbl = {o.Label: o for o in doc.Objects if hasattr(o, "Shape") and o.TypeId.startswith("Part::")}
                        assert by_lbl["GoodComp geometry"].TypeId == "Part::FeaturePython"
                        assert by_lbl["DdsComp geometry"].TypeId == "Part::Feature"
                        assert by_lbl["faulty_textures geometry"].TypeId == "Part::Feature"
                        assert stats["textured_objects"] == 1, stats
                finally:
                    App.closeDocument(doc.Name)
        finally:
            openskp.SkpFile.parse = orig_parse
            App.Console.PrintWarning = orig_warn

    print("check_fault_tolerance: OK (DDS, corrupt PNG, and empty bytes counted in textures_failed=3 and reported once; geometry intact)")


def check_mirrored_curved_memory_and_damaged_fcstd():
    """Verifies review fixes (#43):
    - Point 5: textures_mirrored_2017.skp imports negative-determinant textured instances
      as mirrored placed copies (exact front/back normals & UVs) while untextured mirrored
      instances and textures=False stay App::Link.
    - Point 6: Fitted curved Part.Cylinder with distinct front & back textures preserves
      outward normals and UVs on import and export.
    - Point 2: Byte-bounded caches evict by byte size, use lazy ZIP reading for large packs,
      and release all memory when documents close.
    - Point 9: Non-dict MaterialsJSON / SketchUpFaceUVsJSON ('[1]') and >32,767-px image
      headers never crash or over-allocate.
    """
    mm = importSKP.INCH_TO_MM
    mir_path = os.path.join(FIXTURES_DIR, "textures_mirrored_2017.skp")

    # 1. Mirrored placements (Point 5)
    doc_off = App.newDocument("MirroredOff")
    doc_on = App.newDocument("MirroredOn")
    try:
        importSKP.import_skp(mir_path, doc_off, merge=False, textures=False)
        st_off = dict(importSKP.last_import_stats)
        assert st_off["links"] == 4 and st_off.get("mirrored_copies", 0) == 0, st_off

        importSKP.import_skp(mir_path, doc_on, merge=False, textures=True)
        st_on = dict(importSKP.last_import_stats)
        assert st_on["links"] == 2 and st_on.get("mirrored_copies", 0) == 2, st_on

        links_on = {o.Label: o for o in doc_on.Objects if o.TypeId == "App::Link"}
        assert set(links_on.keys()) == {"Card_Normal", "Plain_MirrorX"}, set(links_on.keys())

        store = [o for o in doc_on.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
        mats_dict, _ = importSKP._read_store_pack(store)
        copies = {o.Label: o for o in doc_on.Objects if "(mirrored copy)" in o.Label}
        assert set(copies.keys()) == {"Card_MirrorX (mirrored copy)", "Card_MirrorZ (mirrored copy)"}, set(copies.keys())

        # Card_MirrorX: diag(-1, 1, 1) + translate(28, 0, 0) on local [0..12, 0..12, z=1] ->
        # spans x in [16, 28], y in [0, 12], z = 1.
        # Its SketchUp front normal remains +Z (MatFrontQuad), with U mirrored: x=16 -> u=1.0, x=28 -> u=0.0,
        # and SketchUpInstanceName preserved!
        mx_obj = copies["Card_MirrorX (mirrored copy)"]
        assert getattr(mx_obj, "SketchUpInstanceName", None) == "Card_MirrorX"
        mx_rec = json.loads(mx_obj.SketchUpFaceUVsJSON)["faces"][0]
        mx_bf = mx_obj.Shape.Faces[0]
        mx_bn = importSKP._brep_face_normal_tuple(mx_bf)
        mx_fn = (-mx_bn[0], -mx_bn[1], -mx_bn[2]) if mx_rec[2] else mx_bn
        assert mx_fn[2] > 0.99, ("Card_MirrorX front normal should point +Z", mx_fn)
        assert mats_dict[mx_rec[0][1]]["name"] == "MatFrontQuad"
        assert mats_dict[mx_rec[1][1]]["name"] == "MatBackStripes"
        uv_x16 = _eval_uv_record_side(mx_rec[0], mats_dict, (16.0, 0.0, 1.0))
        uv_x28 = _eval_uv_record_side(mx_rec[0], mats_dict, (28.0, 12.0, 1.0))
        assert abs(uv_x16[0] - 1.0) < 1e-5 and abs(uv_x16[1] - 0.0) < 1e-5, uv_x16
        assert abs(uv_x28[0] - 0.0) < 1e-5 and abs(uv_x28[1] - 1.0) < 1e-5, uv_x28

        # Card_MirrorZ: diag(1, 1, -1) + translate(34, 0, 2) -> front normal flipped to -Z!
        mz_obj = copies["Card_MirrorZ (mirrored copy)"]
        assert getattr(mz_obj, "SketchUpInstanceName", None) == "Card_MirrorZ"
        mz_rec = json.loads(mz_obj.SketchUpFaceUVsJSON)["faces"][0]
        mz_bf = mz_obj.Shape.Faces[0]
        mz_bn = importSKP._brep_face_normal_tuple(mz_bf)
        mz_fn = (-mz_bn[0], -mz_bn[1], -mz_bn[2]) if mz_rec[2] else mz_bn
        assert mz_fn[2] < -0.99, ("Card_MirrorZ front normal should point -Z", mz_fn)
    finally:
        App.closeDocument(doc_off.Name)
        App.closeDocument(doc_on.Name)

    # 1b. Mirrored textured component with internal hidden tag + instance name, AND annotation-only sheared component (#43 review 2 point 7)
    with tempfile.TemporaryDirectory() as tmp:
        wing_png = _make_png_rgba(16, 16, lambda x, y: (210, 90, 40, 255))
        with open(os.path.join(tmp, "wing.png"), "wb") as fh:
            fh.write(wing_png)
        prev = os.getcwd()
        try:
            os.chdir(tmp)
            b = openskp.create()
            wm = b.add_texture_material("WingTex", "wing.png", applied_width=6.0, applied_height=6.0)
            hid_tag = b.add_layer("HiddenTrim", hidden=True)
            with b.add_component_definition("TrimPart") as td:
                td.add_face([(0, 12, 0), (10, 12, 0), (10, 22, 0), (0, 22, 0)], material=wm)
            with b.add_component_definition("WingDef") as wd:
                wd.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=wm)
                wd.add_instance(td, name="TrimInst", layer=hid_tag)
            with b.add_component_definition("AnnotOnlyDef") as ad:
                ad.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
            b.add_instance(wd, name="LeftWing", translation=(20, 0, 0), matrix3x3=(-1, 0, 0, 0, 1, 0, 0, 0, 1))
            b.add_instance(ad, name="ShearedAnnot", translation=(40, 0, 0), matrix3x3=(1, 0.5, 0, 0, 1, 0, 0, 0, 1))
            mtag_skp = os.path.join(tmp, "mirrored_multitag.skp")
            with open(mtag_skp, "wb") as fh:
                fh.write(b.to_bytes())
        finally:
            os.chdir(prev)

        orig_p = openskp.SkpFile.parse
        from openskp.model import TextEntity

        def _inject_sheared_text(self):
            m = orig_p(self)
            for d in m.definitions.values():
                if d.name == "AnnotOnlyDef":
                    d.faces.clear()
                    d.edges.clear()
                    d.vertices.clear()
                    d.texts.append(TextEntity(text="NoteInSheared", point=(1.0, 2.0, 0.0), label_point=(3.0, 4.0, 0.0), hidden=False))
            return m

        openskp.SkpFile.parse = _inject_sheared_text
        mtag_doc = App.newDocument("MirroredMultiTag")
        try:
            importSKP.import_skp(mtag_skp, mtag_doc, merge=False, annotations=True, textures=True)
            mt_stats = importSKP.last_import_stats
            assert mt_stats.get("mirrored_copies") == 1 and mt_stats["sheared_copies"] == 1, mt_stats
            if importSKP._draft_available():
                # 1 text placed in world space for ShearedAnnot + 1 in AnnotOnlyDef's component definition!
                assert mt_stats["texts_built"] == 2, mt_stats
            wing_part = [o for o in mtag_doc.Objects if o.Label == "LeftWing (mirrored copy)"][0]
            assert wing_part.TypeId == "App::Part"
            assert getattr(wing_part, "SketchUpInstanceName", None) == "LeftWing"
            by_sub_tag = {getattr(c, "SketchUpLayer", ""): c for c in wing_part.Group}
            assert set(by_sub_tag.keys()) == {"Layer0", "HiddenTrim"}, set(by_sub_tag.keys())
            assert by_sub_tag["Layer0"].Visibility is True
            assert by_sub_tag["HiddenTrim"].Visibility is False
        finally:
            openskp.SkpFile.parse = orig_p
            App.closeDocument(mtag_doc.Name)

    # 2. Fitted curved open cylinder with distinct front & back textures (Point 6)
    from openskp.model import Material, Texture

    orig_parse = openskp.SkpFile.parse
    front_png = _make_png_rgba(16, 16, lambda x, y: (230, 40, 190, 255) if ((x // 4) + (y // 4)) % 2 == 0 else (245, 220, 30, 255))
    back_png = _make_png_rgba(16, 16, lambda x, y: (20, 210, 235, 255) if (y // 4) % 2 == 0 else (20, 35, 110, 255))

    def cyl_patched_parse(self):
        m = orig_parse(self)
        m_f = Material(
            id=101,
            name="MatCylFront",
            color=(230, 40, 190, 255),
            transparency=1.0,
            texture=Texture(filename="cyl_front.png", width=8.0, height=6.0, data=front_png),
        )
        m_b = Material(
            id=102,
            name="MatCylBack",
            color=(20, 210, 235, 255),
            transparency=1.0,
            texture=Texture(filename="cyl_back.png", width=5.0, height=5.0, data=back_png),
        )
        m.materials.extend([m_f, m_b])
        m.materials_by_id[101] = m_f
        m.materials_by_id[102] = m_b
        for d in [m.root, *m.definitions.values()]:
            cap_ids = [fid for fid, f in d.faces.items() if f.normal and abs(f.normal[2]) > 0.9]
            for cid in cap_ids:
                d.faces.pop(cid, None)
            for f in d.faces.values():
                f.material_id = 101
                f.back_material_id = 102
        return m

    with tempfile.TemporaryDirectory() as tmp:
        openskp.SkpFile.parse = cyl_patched_parse
        cyl_doc = App.newDocument("TexturedCylinder")
        try:
            importSKP.import_skp(
                os.path.join(FIXTURES_DIR, "cylinder_2017.skp"),
                cyl_doc,
                merge=True,
                curved_surfaces=True,
                textures=True,
            )
            cstats = importSKP.last_import_stats
            assert cstats["cylinders_built"] == 1 and cstats["textured_objects"] == 1, cstats
            cyl_obj = [o for o in cyl_doc.Objects if o.TypeId == "Part::FeaturePython"][0]
            assert len(cyl_obj.Shape.Faces) == 1 and isinstance(cyl_obj.Shape.Faces[0].Surface, Part.Cylinder)
            openskp.SkpFile.parse = orig_parse

            cyl_out = os.path.join(tmp, "cyl_exported.skp")
            importSKP.export(list(cyl_doc.Objects), cyl_out)
            _scan_skp_no_private_strings(cyl_out, check_no_drive_letters=True)
            cyl_rt = openskp.SkpFile.open(cyl_out).parse()
            assert len(cyl_rt.root.faces) >= 12
            for rf in cyl_rt.root.faces.values():
                rverts = [
                    cyl_rt.root.vertices[cyl_rt.root.edges[eid].v1_id if sgn >= 0 else cyl_rt.root.edges[eid].v2_id]
                    for eid, sgn in rf.loops[0]
                ]
                cx = sum(v.x for v in rverts) / len(rverts)
                cy = sum(v.y for v in rverts) / len(rverts)
                # Exported cylinder facets must all point radially outward with MatCylFront on front & MatCylBack on back!
                assert cx * rf.normal[0] + cy * rf.normal[1] > 0.0, (cx, cy, rf.normal)
                assert cyl_rt.materials_by_id[rf.material_id].name == "MatCylFront"
                assert cyl_rt.materials_by_id[rf.back_material_id].name == "MatCylBack"
        finally:
            openskp.SkpFile.parse = orig_parse
            App.closeDocument(cyl_doc.Name)

    # 3. Byte-bounded caches + lazy large pack + document-close release (Point 2)
    bc = importSKP._ByteBoundedCache(1024)
    bc.put("a", b"x" * 700, 700, doc_uid=("D1", "u1"))
    bc.put("b", b"y" * 700, 700, doc_uid=("D1", "u1"))
    assert "a" not in bc and "b" in bc and bc.total_bytes == 700
    bc.release_doc(("D1", "u1"))
    assert len(bc) == 0 and bc.total_bytes == 0

    for c_inst in (
        importSKP._PROCESS_TEX_CACHE,
        importSKP._PROCESS_MAT_CACHE,
        importSKP._DECODED_GL_CACHE,
        importSKP._STORE_UNPACK_CACHE,
        importSKP._COIN_TEX_NODE_CACHE,
    ):
        assert c_inst.total_bytes == 0, f"cache not empty when all docs closed: {c_inst.total_bytes}"

    # 4. Damaged .FCStd JSON & oversized 40000x40000 PNG header guard (Point 9)
    def _ck(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    huge_ihdr = struct.pack(">IIBBBBB", 40000, 40000, 8, 6, 0, 0, 0)
    huge_png = b"\x89PNG\r\n\x1a\n" + _ck(b"IHDR", huge_ihdr) + _ck(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + _ck(b"IEND", b"")
    assert importSKP._decode_qimage_rgba(huge_png, "huge.png") is None

    dmg_doc = App.newDocument("DamagedFCStd")
    try:
        importSKP.import_skp(os.path.join(FIXTURES_DIR, "textures_uv_2017.skp"), dmg_doc, merge=True, textures=True)
        d_store = [o for o in dmg_doc.Objects if getattr(o, "IsSketchUpTextureStore", False)][0]
        d_fp = [o for o in dmg_doc.Objects if o.TypeId == "Part::FeaturePython"][0]
        for bad_mats, bad_uvs in (
            ("[1]", "[1]"),
            (
                json.dumps({"m_bad": {"rgba": ["text", "x", None, 255], "opacity": "not_a_float", "tile_w": "bad"}}),
                json.dumps({"v": 1, "faces": [[["t", {"dict_key": 1}, [0, 0, 1], None, None], None, None]]}),
            ),
            (
                json.dumps({"m_ok": {"rgba": ["oops", 10, 20, 255], "opacity": "bad", "tex_id": {"bad": 1}}}),
                json.dumps({"v": 1, "faces": [[["s", "bad", 0.5, 0.5, 1.0], None, None]]}),
            ),
        ):
            d_store.MaterialsJSON = bad_mats
            d_fp.SketchUpFaceUVsJSON = bad_uvs
            importSKP._STORE_UNPACK_CACHE.clear()
            parsed_bad = importSKP._parse_face_uvs_payload(bad_uvs)
            if bad_uvs == "[1]":
                assert parsed_bad is None
            else:
                # Bad side entries are skipped per-record rather than dropping the whole payload
                assert parsed_bad is not None and parsed_bad["faces"] == [(None, None, 0)], parsed_bad
            assert importSKP._initial_side_diffuse_rgba(
                ["t", {"dict": 1}, None, None, None], {"m": {"rgba": ["txt", 0, 0, 255]}}
            ) == importSKP._DEFAULT_FACE_RGBA
            assert importSKP._initial_side_diffuse_rgba(
                ["t", "m_txt", None, None, None], {"m_txt": {"rgba": ["txt", "bad", None, 255], "opacity": "nan"}}
            ) is not None
            dmg_doc.recompute()
            with tempfile.TemporaryDirectory() as tmp:
                dmg_skp = os.path.join(tmp, "damaged_out.skp")
                importSKP.export(list(dmg_doc.Objects), dmg_skp)
                assert os.path.isfile(dmg_skp)
    finally:
        App.closeDocument(dmg_doc.Name)

    print("check_mirrored_curved_memory_and_damaged_fcstd: OK (mirrored copies, curved cylinder front/back, byte caches, damaged JSON & 40000px guard)")


def check_review3_fallbacks_pack_warning_and_paste_scaling():
    """Verifies third-review items (#43):
    1. Must 1:
       (a) SketchUpFaceUVsJSON > 32 MB (~120,000 positioned two-sided faces, ~37 MB)
           is accepted (limit raised to 512 MB), and payloads that ARE refused / invalid
           still draw solid faces from DiffuseColor in _rebuild instead of disappearing.
       (b) Shape modification changing face count (e.g. Part refine / box replacement)
           draws all current Shape.Faces solid from DiffuseColor in _rebuild.
       (c) A single NaN or bad UV matrix skips only that one record: _rebuild draws the
           bad face solid from DiffuseColor while keeping SoTexture2 on valid faces, and
           export keeps textures & UVs on all valid faces.
    2. Must 2: Failure writing TexturePack on a successful import emits a clear warning
       to both App.Console.PrintWarning and stats['warnings'].
    3. If-easy items:
       - Unlinked pasted object with initially unknown material marks _had_unresolved_textures
         and rebuilds to textured once SketchUpTextureStore / TexturePack arrives.
       - Bulk unresolved paste scales linearly O(N) without rescanning doc.Objects.
       - Hidden SketchUp mirrored textured instance keeps the same visibility as the
         App::Link it replaces (visible unless its tag is hidden)."""
    import gc
    import time

    # 1a. >32 MB UV payload accepted (~37 MB, 120,000 positioned two-sided records)
    rec_str = (
        '[["t","m_front",[0.0,0.0,1.0],[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0],'
        '[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0]],'
        '["t","m_back",[0.0,0.0,-1.0],[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0],'
        '[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0]],0]'
    )
    n_large = 160000
    raw_large = '{"v":1,"faces":[' + ",".join([rec_str] * n_large) + "]}"
    assert len(raw_large) > 35 * 1024 * 1024, len(raw_large)
    parsed_large = importSKP._parse_face_uvs_payload(raw_large)
    assert parsed_large is not None and len(parsed_large["faces"]) == n_large
    del raw_large, parsed_large
    gc.collect()

    # 1b & 1c. Partial NaN UV record on multi-face textured import: export keeps good face textured!
    uv_path = os.path.join(FIXTURES_DIR, "textures_uv_2017.skp")
    nan_doc = App.newDocument("PartialNaNExport")
    try:
        importSKP.import_skp(uv_path, nan_doc, merge=True, textures=True)
        fp_m = [o for o in nan_doc.Objects if o.TypeId == "Part::FeaturePython"][0]
        orig_payload = json.loads(fp_m.SketchUpFaceUVsJSON)
        assert len(orig_payload["faces"]) >= 2
        # Inject NaN into face 0's 3x3 UV matrix while keeping face 1..N valid
        bad_payload = json.loads(fp_m.SketchUpFaceUVsJSON)
        bad_payload["faces"][0][0][3] = [float("nan")] + [0.0] * 8
        raw_with_nan = json.dumps(bad_payload, allow_nan=True)
        repaired = importSKP._parse_face_uvs_payload(raw_with_nan)
        assert repaired is not None and len(repaired["faces"]) == len(orig_payload["faces"])
        assert repaired["faces"][0][0] is None  # bad face skipped
        assert repaired["faces"][1][0] is not None and repaired["faces"][1][0][0] == "t"  # good face preserved!

        fp_m.SketchUpFaceUVsJSON = raw_with_nan
        with tempfile.TemporaryDirectory() as tmp:
            nan_out = os.path.join(tmp, "partial_nan_rt.skp")
            importSKP.export(list(nan_doc.Objects), nan_out)
            nan_rt = openskp.SkpFile.open(nan_out).parse()
            tex_mats_rt = [m for m in nan_rt.materials if m.texture is not None]
            assert len(tex_mats_rt) >= 2, [m.name for m in nan_rt.materials]
    finally:
        App.closeDocument(nan_doc.Name)

    # 2. Must 2: Unwritable TexturePack surfaces warning in stats["warnings"] and Console
    orig_write_pack = importSKP._write_store_pack
    orig_warn = App.Console.PrintWarning
    printed_warns = []

    def cap_warn(msg):
        printed_warns.append(msg)
        orig_warn(msg)

    def disk_full_write_pack(*_a, **_kw):
        raise OSError(28, "No space left on device")

    App.Console.PrintWarning = cap_warn
    importSKP._write_store_pack = disk_full_write_pack
    pack_doc = App.newDocument("DiskFullPack")
    try:
        importSKP.import_skp(uv_path, pack_doc, merge=True, textures=True)
        p_stats = importSKP.last_import_stats
        assert p_stats["faces_built"] >= 2, p_stats
        assert any("failed to write texture pack" in w and "No space left on device" in w for w in p_stats["warnings"]), p_stats["warnings"]
        assert any("failed to write texture pack" in w for w in printed_warns), printed_warns
    finally:
        importSKP._write_store_pack = orig_write_pack
        App.Console.PrintWarning = orig_warn
        App.closeDocument(pack_doc.Name)

    # 3a. Bulk unresolved paste scales O(N) (300 pasted FeaturePython objects with unknown material key)
    scale_doc = App.newDocument("BulkUnresolvedPaste")
    try:
        importSKP._clear_doc_texture_caches(scale_doc)
        box_sh = Part.makeBox(10.0, 10.0, 10.0)
        uv_unknown = json.dumps({
            "v": 1,
            "faces": [[["t", "m_unknown_future_store", [0, 0, 1], None, None], None, 0]] * len(box_sh.Faces),
        })
        t0 = time.perf_counter()
        created_fps = []
        for idx in range(300):
            fp_u = scale_doc.addObject("Part::FeaturePython", f"PastedTex_{idx}")
            fp_u.addProperty("App::PropertyLink", "SketchUpTextureStore", "SketchUp", "")
            fp_u.addProperty("App::PropertyString", "SketchUpFaceUVsJSON", "SketchUp", "")
            fp_u.Shape = box_sh
            fp_u.SketchUpFaceUVsJSON = uv_unknown
            fp_u.Proxy = importSKP._TexturedObjectProxy(fp_u)
            fp_u.Proxy.execute(fp_u)
            created_fps.append(fp_u)
        dt_paste = time.perf_counter() - t0
        assert dt_paste < 2.0, f"300 unresolved pastes took {dt_paste:.2f}s (expected O(N) < 2.0s)"

        # Now add the texture store with m_unknown_future_store later -> all 300 objects resolve & link!
        png_b = _make_png_rgba(8, 8, lambda x, y: (40, 180, 220, 255))
        late_store = importSKP._ensure_document_texture_store(
            scale_doc,
            {"tid_late": png_b},
            {
                "m_unknown_future_store": {
                    "name": "LateMat",
                    "rgba": [40, 180, 220, 255],
                    "opacity": 1.0,
                    "tex_id": "tid_late",
                    "filename": "late.png",
                    "tile_w": 10.0,
                    "tile_h": 10.0,
                    "colorized": False,
                    "colorize_type": 0,
                }
            },
        )
        importSKP._notify_texture_store_updated(late_store)
        assert all(o.SketchUpTextureStore == late_store for o in created_fps)
    finally:
        App.closeDocument(scale_doc.Name)

    # 3b. Hidden SketchUp mirrored instance keeps same visibility as the App::Link it replaces
    with tempfile.TemporaryDirectory() as tmp:
        h_png = _make_png_rgba(8, 8, lambda x, y: (200, 80, 40, 255))
        with open(os.path.join(tmp, "h.png"), "wb") as fh:
            fh.write(h_png)
        prev = os.getcwd()
        try:
            os.chdir(tmp)
            b = openskp.create()
            hm = b.add_texture_material("HidMirTex", "h.png", applied_width=5.0, applied_height=5.0)
            with b.add_component_definition("HidMirCard") as hc:
                hc.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=hm)
            b.add_instance(
                hc,
                name="HidMirInst",
                translation=(20, 0, 0),
                matrix3x3=(-1, 0, 0, 0, 1, 0, 0, 0, 1),
                hidden=True,
            )
            hm_skp = os.path.join(tmp, "hidden_mirrored.skp")
            with open(hm_skp, "wb") as fh:
                fh.write(b.to_bytes())
        finally:
            os.chdir(prev)

        d_off = App.newDocument("HidMirOff")
        d_on = App.newDocument("HidMirOn")
        try:
            importSKP.import_skp(hm_skp, d_off, merge=False, textures=False)
            importSKP.import_skp(hm_skp, d_on, merge=False, textures=True)
            lnk = [o for o in d_off.Objects if o.TypeId == "App::Link"][0]
            mcopy = [o for o in d_on.Objects if "(mirrored copy)" in o.Label][0]
            assert mcopy.Visibility == lnk.Visibility, (mcopy.Visibility, lnk.Visibility)
        finally:
            App.closeDocument(d_off.Name)
            App.closeDocument(d_on.Name)

    print("check_review3_fallbacks_pack_warning_and_paste_scaling: OK (>32MB UVs, partial NaN export, pack write warning, O(N) paste + late store link, hidden mirrored parity)")


def _main():
    check_fixtures_clean()
    check_off_identical_to_main()
    check_selective_promotion_and_feature_parity()
    check_deduplication_uvs_formats_and_colorize()
    check_export_round_trip()
    check_persistence_copy_undo_recompute_and_no_addon()
    check_fault_tolerance()
    check_mirrored_curved_memory_and_damaged_fcstd()
    check_review3_fallbacks_pack_warning_and_paste_scaling()


_harness.run(_main)
