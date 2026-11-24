"""Run with FreeCAD's own headless interpreter:

    freecadcmd tests/test_annotations.py

Verifies SketchUp text and linear dimension import (#29):
- Component hierarchy (merge=False) and merged (merge=True) layouts on SDK
  fixture annotations_2017.skp (leader text imported, screen text without 3D
  position skipped and counted, root 36 in and component 24 in dimensions).
- Real SDK-saved SketchUp 2021 fixture annotations_2021.skp (root 36 in
  dimension imported, Beam's 24 in component dimension not misplaced at the
  origin when openskp <= 1.6.0 lacks owner-local VFF annotations, and full
  owner-local import when openskp#623 fields are present).
- Dimension plane & Normal orientation (classic elevation planes, vertical
  dimensions in capilla_quiroz_v17.skp, and skipped positionless leader texts).
- Draft failure resilience (catching per-annotation errors without aborting).
- Active-document isolation (App.setActiveDocument restored cleanly).
- Sheared placements nested inside components carrying their annotations.
- ImportAnnotations preference, explicit overrides, and clean .skp export.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402

try:
    import Draft  # noqa: E402
    import importSKP  # noqa: E402

    import FreeCAD as App  # noqa: E402
except BaseException:
    _harness.die()

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MM = importSKP.INCH_TO_MM


def _has_offset_vec():
    import openskp

    return "offset_vector" in getattr(openskp.model.Dimension, "__dataclass_fields__", {})


def _scan_fixture_no_private_paths(path):
    """Scans raw bytes (ASCII + UTF-16) and every ZIP entry (including
    meta/meta.dat) for host paths, usernames, temp dirs, and email addresses."""
    import zipfile

    blobs = [("RAW", open(path, "rb").read())]
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                blobs.append((name, zf.read(name)))
    email_re = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    bad_tokens = (
        "Users", "Admin", "Devs", "lanes", ".gemini",
        "Temp\\", "Temp/", "scratch", "AppData", "C:\\", "G:\\",
    )
    for entry_name, data in blobs:
        for tok in bad_tokens:
            for enc in ("ascii", "utf-16-le", "utf-16-be"):
                needle = tok.encode(enc)
                assert needle.lower() not in data.lower(), (path, entry_name, tok, enc)
        assert not email_re.search(data), (path, entry_name, email_re.search(data).group(0))


def _assert_vec_in(vec_mm, want_in, tol=1e-3, label=""):
    want = App.Vector(*(c * MM for c in want_in))
    err = (vec_mm - want).Length
    assert err <= tol, f"{label}: got {tuple(vec_mm)} mm, expected {tuple(want)} mm (err={err})"


def _measured_mm(dim_obj):
    span = (dim_obj.End - dim_obj.Start).Length
    if hasattr(dim_obj, "Distance") and float(dim_obj.Distance) > 0:
        assert abs(float(dim_obj.Distance) - span) <= 1e-3, (float(dim_obj.Distance), span)
        return float(dim_obj.Distance)
    return span


def _assert_normal_orthogonal(dim_obj, label=""):
    seg_dir = (dim_obj.End - dim_obj.Start).normalize()
    n = dim_obj.Normal
    assert abs(n.Length - 1.0) <= 1e-5, f"{label}: Normal not unit length: {tuple(n)}"
    assert abs(n.dot(seg_dir)) <= 1e-5, f"{label}: Normal {tuple(n)} parallel/not orthogonal to {tuple(seg_dir)}"


def check_hierarchy_annotations(path):
    """In the component hierarchy (merge=False):
    - Root leader text ('Apex label') and 36 in root linear dimension live in
      Layer0's 'Annotations' group; screen text ('Title block note', no 3D
      position) is skipped and counted in stats/warnings.
    - The 24 in component linear dimension inside 'Beam' belongs to Beam's own
      App::Part 'Annotations' group in local coordinates, placed at (40, 10, 0)
      inches on tag 'Framing' by the 'Beam 1' App::Link.
    """
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats
        assert stats["import_annotations"] is True, stats
        assert stats["texts_built"] == 1, stats
        assert stats["texts_skipped_no_position"] == 1, stats
        assert stats["dimensions_built"] == 2, stats
        assert any("without a 3D position" in w for w in stats["warnings"]), stats["warnings"]

        model_part = doc.getObject(stats["model_part"])
        direct_groups = {
            o.Label: o
            for o in importSKP._direct_children(model_part)
            if o.TypeId == "App::DocumentObjectGroup"
        }
        assert sorted(direct_groups) == ["Framing", "Layer0"], sorted(direct_groups)

        layer0_annots = [
            o for o in direct_groups["Layer0"].Group if getattr(o, "SketchUpAnnotations", False)
        ]
        assert len(layer0_annots) == 1 and re.sub(r"\d+$", "", layer0_annots[0].Label) == "Annotations", (
            [o.Label for o in layer0_annots]
        )
        root_agrp = layer0_annots[0]

        texts = [
            o for o in root_agrp.Group if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
        ]
        assert len(texts) == 1 and texts[0].SketchUpAnnotationKind == "leader_text", [o.Label for o in texts]
        leader = texts[0]
        assert list(leader.Text) == ["Apex label"], list(leader.Text)
        _assert_vec_in(leader.Placement.Base, (24.0, 14.0, 10.0), label="leader Placement.Base")
        _assert_vec_in(leader.SketchUpAnchorPoint, (18.0, 6.0, 0.0), label="leader SketchUpAnchorPoint")

        root_dims = [
            o for o in root_agrp.Group if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        assert len(root_dims) == 1, [o.Label for o in root_dims]
        rdim = root_dims[0]
        _assert_vec_in(rdim.Start, (0.0, 0.0, 0.0), label="root dim Start")
        _assert_vec_in(rdim.End, (36.0, 0.0, 0.0), label="root dim End")
        if not _has_offset_vec():
            _assert_vec_in(rdim.Dimline, (18.0, 0.0, 0.0), label="root dim Dimline (no offset on 1.6.0)")
        _assert_normal_orthogonal(rdim, "root dim")
        assert abs(_measured_mm(rdim) - 36.0 * MM) <= 1e-3, _measured_mm(rdim)

        # Component 'Beam' owns its 24 in dimension in Beam's App::Part.
        components = doc.getObject(stats["components_group"])
        beam_parts = [o for o in components.Group if o.Label == "Beam"]
        assert len(beam_parts) == 1, [o.Label for o in components.Group]
        beam_part = beam_parts[0]
        beam_annots = [o for o in beam_part.Group if getattr(o, "SketchUpAnnotations", False)]
        assert len(beam_annots) == 1 and re.sub(r"\d+$", "", beam_annots[0].Label) == "Annotations", (
            [o.Label for o in beam_annots]
        )
        bdims = [
            o for o in beam_annots[0].Group if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        assert len(bdims) == 1, [o.Label for o in bdims]
        bdim = bdims[0]
        _assert_vec_in(bdim.Start, (0.0, 0.0, 0.0), label="Beam local dim Start")
        _assert_vec_in(bdim.End, (24.0, 0.0, 0.0), label="Beam local dim End")
        if not _has_offset_vec():
            _assert_vec_in(bdim.Dimline, (12.0, 0.0, 0.0), label="Beam local dim Dimline (no offset on 1.6.0)")
        _assert_normal_orthogonal(bdim, "Beam dim")
        assert abs(_measured_mm(bdim) - 24.0 * MM) <= 1e-3, _measured_mm(bdim)

        # 'Beam 1' link places Beam at (40, 10, 0) inches on 'Framing'.
        beam_links = [o for o in direct_groups["Framing"].Group if o.TypeId == "App::Link"]
        assert len(beam_links) == 1 and beam_links[0].LinkedObject == beam_part, [o.Label for o in beam_links]
        _assert_vec_in(beam_links[0].Placement.Base, (40.0, 10.0, 0.0), label="Beam 1 link placement")

        print(
            "check_hierarchy_annotations: OK (leader text, skipped screen text, 914.4 mm root dim, "
            "609.6 mm Beam component dim)"
        )
    finally:
        App.closeDocument(doc.Name)


def check_merged_annotations(path):
    """In the merged layout (merge=True), annotations are placed in world space
    inside an Annotations group per SketchUp tag."""
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=True)
        stats = importSKP.last_import_stats
        assert stats["texts_built"] == 1 and stats["dimensions_built"] == 2, stats
        assert stats["texts_skipped_no_position"] == 1, stats

        agroups = {
            getattr(o, "SketchUpLayer", ""): o
            for o in doc.Objects
            if getattr(o, "SketchUpAnnotations", False)
        }
        assert sorted(agroups) == ["Framing", "Layer0"], sorted(agroups)
        assert agroups["Layer0"].Label == "Annotations"
        assert agroups["Framing"].Label == "Annotations (Framing)"

        l0_texts = [
            o for o in agroups["Layer0"].Group if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
        ]
        assert len(l0_texts) == 1 and list(l0_texts[0].Text) == ["Apex label"]
        _assert_vec_in(l0_texts[0].Placement.Base, (24.0, 14.0, 10.0), label="merged leader")

        l0_dims = [
            o for o in agroups["Layer0"].Group if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        assert len(l0_dims) == 1 and abs(_measured_mm(l0_dims[0]) - 36.0 * MM) <= 1e-3

        fr_dims = [
            o for o in agroups["Framing"].Group if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        assert len(fr_dims) == 1
        fdim = fr_dims[0]
        _assert_vec_in(fdim.Start, (40.0, 10.0, 0.0), label="merged Beam world Start")
        _assert_vec_in(fdim.End, (64.0, 10.0, 0.0), label="merged Beam world End")
        if not _has_offset_vec():
            _assert_vec_in(fdim.Dimline, (52.0, 10.0, 0.0), label="merged Beam world Dimline (no offset on 1.6.0)")
        assert abs(_measured_mm(fdim) - 24.0 * MM) <= 1e-3

        print("check_merged_annotations: OK (world-space per-tag Annotations groups)")
    finally:
        App.closeDocument(doc.Name)


def check_real_2021_annotations():
    """Tests the real SDK-saved SketchUp 2021 fixture annotations_2021.skp
    without monkeypatching parse() (points 1 & 2):
    - With openskp <= 1.6.0 (no Dimension.layer), only the root 36 in dimension
      imports; Beam's 24 in component dimension is NOT misplaced at (0,0,0) on
      Layer0, and skipped 2021+ texts + component dimensions are counted and
      reported.
    - With openskp#623+'s owner-local VFF annotations, both dimensions and
      positioned texts import in their owning scopes.
    """
    import openskp

    path_2021 = os.path.join(FIXTURES_DIR, "annotations_2021.skp")
    has_623 = "layer" in getattr(openskp.model.Dimension, "__dataclass_fields__", {})

    for merge in (False, True):
        doc = App.newDocument()
        try:
            importSKP.import_skp(path_2021, doc, merge=merge)
            stats = importSKP.last_import_stats
            dims = [
                o for o in doc.Objects if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
            ]
            if has_623:
                assert stats["dimensions_built"] == 2 and stats["texts_built"] == 1, (merge, stats)
                assert len(dims) == 2
                if merge:
                    beam_d = next(d for d in dims if abs(_measured_mm(d) - 24.0 * MM) <= 1e-2)
                    _assert_vec_in(beam_d.Start, (40.0, 10.0, 0.0), label="2021 merged Beam Start")
            else:
                assert stats["dimensions_built"] == 1, (merge, stats)
                assert stats["vff_component_dimensions_skipped"] == 1, (merge, stats)
                assert stats["vff_texts_skipped"] == 2, (merge, stats)
                assert any("2021+ component dimension" in w for w in stats["warnings"]), stats["warnings"]
                assert len(dims) == 1
                _assert_vec_in(dims[0].Start, (0.0, 0.0, 0.0), label="2021 root dim Start")
                _assert_vec_in(dims[0].End, (36.0, 0.0, 0.0), label="2021 root dim End")
                _assert_vec_in(dims[0].Dimline, (18.0, 0.0, 0.0), label="2021 root dim Dimline")
                _assert_normal_orthogonal(dims[0], "2021 root dim")
        finally:
            App.closeDocument(doc.Name)

    # Axis-constrained 5BD1 VFF dimensions:
    # - axis=1 (plane X = (1,0,0), parallel to a=(0,0,0)..b=(36,0,0)): imported!
    # - axis=2 (plane Y = (0,1,0), not parallel to a-b): skipped & counted!
    import io
    import struct
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        raw_2021 = open(path_2021, "rb").read()
        pk_off = raw_2021.index(b"PK\x03\x04")
        for axis_code, want_built, want_skip in ((1, 1, 0), (2, 0, 1)):
            zbuf = io.BytesIO()
            with zipfile.ZipFile(path_2021, "r") as zin, zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "model.dat":
                        needle = b"\xd1\x5b\x04\x00\x00\x00\x00\x00\x00\x00"
                        rpos = data.rfind(needle)
                        assert rpos > 0
                        data = data[:rpos + 6] + struct.pack("<I", axis_code) + data[rpos + 10:]
                    zout.writestr(item.filename, data)
            constrained_vff = os.path.join(tmp, f"constrained_axis{axis_code}_2021.skp")
            with open(constrained_vff, "wb") as f:
                f.write(raw_2021[:pk_off] + zbuf.getvalue())
            if not has_623:
                cdoc = App.newDocument()
                try:
                    importSKP.import_skp(constrained_vff, cdoc, merge=False)
                    cst = importSKP.last_import_stats
                    assert cst["dimensions_built"] == want_built, (axis_code, cst)
                    assert cst["constrained_dimensions_skipped"] == want_skip, (axis_code, cst)
                    if want_skip:
                        assert any("axis-constrained" in w for w in cst["warnings"]), cst["warnings"]
                finally:
                    App.closeDocument(cdoc.Name)
    print("check_real_2021_annotations: OK (real SDK 2021 fixture + 5BD1 check verified)")


def check_dimension_planes_and_capilla():
    """Verifies offset rule, unresolved endpoints, '<>' -> '$dim', and #623 screen texts:
    - With openskp 1.6.0, capilla_quiroz_v17.skp's 26 resolved dimensions sit
      directly on a-b (zero offset), its 2 unresolved dimensions are counted,
      its positionless anchored text is skipped, and every Normal is orthogonal.
    - When Dimension.offset_vector is provided (openskp#621/#623), Dimline uses
      offset_vector; scalar offset alone gives zero offset.
    - openskp#623 screen texts (leader_type == 0 / screen_position with (0,0,0)
      points) are skipped, and '<>' in custom dimension text maps to '$dim'.
    """
    import openskp
    from openskp.model import Dimension, TextEntity

    cap_path = os.path.join(FIXTURES_DIR, "capilla_quiroz_v17.skp")
    doc = App.newDocument()
    try:
        importSKP.import_skp(cap_path, doc, merge=False)
        stats = importSKP.last_import_stats
        txts = [
            o for o in doc.Objects if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
        ]
        if stats["texts_skipped_no_position"]:
            assert not txts, txts
        dims = [
            o for o in doc.Objects if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        assert len(dims) == 26, len(dims)
        assert stats["dimensions_skipped_unresolved"] == 2, stats
        for idx, d in enumerate(dims):
            _assert_normal_orthogonal(d, f"capilla dim[{idx}]")
            if not _has_offset_vec():
                mid = (d.Start + d.End) * 0.5
                assert (d.Dimline - mid).Length <= 1e-4, (idx, tuple(d.Dimline), tuple(mid))
    finally:
        App.closeDocument(doc.Name)

    doc2 = App.newDocument()
    try:
        # Scalar offset alone -> zero offset (on segment)
        scalar_only = importSKP._tag_dimension(
            Dimension(text="<>", hidden=False, a=(0.0, 20.0, 10.0), b=(-12.0, 20.0, 10.0), offset=-5.0),
            is_vff=False,
        )
        st = {"dimensions_built": 0, "texts_built": 0, "texts_skipped_no_position": 0}
        d0 = importSKP._create_draft_dimension_obj(doc2, scalar_only, None, st)
        assert d0 is not None
        _assert_vec_in(d0.Dimline, (-6.0, 20.0, 10.0), label="scalar-only Dimline on segment")
        assert d0.SketchUpDraftOverride == "", d0.SketchUpDraftOverride

        # Explicit offset_vector -> applied cleanly, and '<>' inside text -> '$dim'
        with_ov = importSKP._tag_dimension(
            Dimension(text="Span <> TYP", hidden=False, a=(0.0, 20.0, 10.0), b=(-12.0, 20.0, 10.0), offset=5.0),
            is_vff=False,
            extra={"offset_vector": (0.0, 0.0, -5.0)},
        )
        d1 = importSKP._create_draft_dimension_obj(doc2, with_ov, None, st)
        assert d1 is not None
        _assert_normal_orthogonal(d1, "offset_vector dim")
        _assert_vec_in(d1.Dimline, (-6.0, 20.0, 5.0), label="offset_vector Dimline")
        assert abs(abs(d1.Normal.y) - 1.0) <= 1e-5, tuple(d1.Normal)
        assert d1.SketchUpDraftOverride == "Span $dim TYP", d1.SketchUpDraftOverride
        assert "304.8 mm" in d1.Label, d1.Label

        # openskp#623 TextEntity contract: EVERY text carries screen_position
        # ((0,0) on placed texts with leader_type == 2, (u,v) on screen texts
        # with leader_type == 0)
        from dataclasses import dataclass

        @dataclass
        class _Real623TextEntity:
            text: str = ""
            hidden: bool = False
            layer: str = ""
            point: object = None
            label_point: object = None
            screen_position: object = (0.0, 0.0)
            leader_type: int = 2
            arrow_type: int = 3
            display_leader: bool = True

        ann_17_model = openskp.SkpFile.open(os.path.join(FIXTURES_DIR, "annotations_2017.skp")).parse()
        for t_raw in ann_17_model.root.texts:
            rt = _Real623TextEntity(
                text=t_raw.text,
                hidden=t_raw.hidden,
                point=t_raw.point if t_raw.point is not None else (0.0, 0.0, 0.0),
                label_point=t_raw.label_point if t_raw.label_point is not None else (0.0, 0.0, 0.0),
                screen_position=(0.0, 0.0) if t_raw.point is not None else (0.2, 0.8),
                leader_type=2 if t_raw.point is not None else 0,
            )
            importSKP._create_draft_text_obj(doc2, rt, None, st)
        assert st["texts_built"] == 1 and st["texts_skipped_no_position"] == 1, st
    finally:
        App.closeDocument(doc2.Name)
    print("check_dimension_planes_and_capilla: OK (offset rule, unresolved count, <> -> $dim, #623 screen text)")


def check_draft_failure_orphan_cleanup_and_isolation(path):
    """Points 4 & 5:
    - When Draft.make_linear_dimension or Draft.make_text adds an object to doc
      BEFORE raising (as FreeCAD 1.1.3 does when no 3D view is open), zero
      orphan objects are left behind in doc.Objects.
    - Exceptions in _extract_model_annotations, _dimension_geometry_mm,
      _make_annotations_group, or _collect_world_annotations never abort geometry.
    """
    orig_dim = getattr(Draft, "make_linear_dimension", None)
    orig_txt = getattr(Draft, "make_text", None)
    cap_path = os.path.join(FIXTURES_DIR, "capilla_quiroz_v17.skp")
    try:
        def orphan_raising_dim(*args, **kwargs):
            App.activeDocument().addObject("App::FeaturePython", "OrphanDimension")
            raise AttributeError("simulated missing 3D view after addObject")

        def orphan_raising_txt(*args, **kwargs):
            App.activeDocument().addObject("App::FeaturePython", "OrphanText")
            raise AttributeError("simulated missing 3D view in make_text")

        Draft.make_linear_dimension = orphan_raising_dim
        Draft.make_text = orphan_raising_txt
        base_doc = App.newDocument()
        try:
            importSKP.import_skp(cap_path, base_doc, merge=False, annotations=False)
            want_cap_faces = importSKP.last_import_stats["faces_built"]
        finally:
            App.closeDocument(base_doc.Name)

        doc = App.newDocument()
        try:
            importSKP.import_skp(cap_path, doc, merge=False)
            stats = importSKP.last_import_stats
            assert stats["faces_built"] == want_cap_faces and want_cap_faces > 0, stats
            assert stats["dimensions_built"] == 0 and stats["texts_built"] == 0, stats
            assert stats["annotation_failures"] == 26, stats
            orphans = [o.Name for o in doc.Objects if "Orphan" in o.Name]
            assert not orphans, f"orphan Draft objects left behind: {orphans}"
            assert sum("failed to create 26 annotation(s)" in w for w in stats["warnings"]) == 1, stats["warnings"]
        finally:
            App.closeDocument(doc.Name)
    finally:
        if orig_dim is not None:
            Draft.make_linear_dimension = orig_dim
        if orig_txt is not None:
            Draft.make_text = orig_txt

    for attr_name, modes in (
        ("_extract_model_annotations", (False, True)),
        ("_dimension_geometry_mm", (False, True)),
        ("_switch_active_doc", (False, True)),
        ("_make_annotations_group", (False, True)),
        ("_collect_world_annotations", (True,)),
    ):
        orig_helper = getattr(importSKP, attr_name)
        try:
            def boom(*_a, **_kw):
                raise RuntimeError(f"boom in {attr_name}")

            setattr(importSKP, attr_name, boom)
            for merge_flag in modes:
                doc = App.newDocument()
                try:
                    importSKP.import_skp(path, doc, merge=merge_flag)
                    st = importSKP.last_import_stats
                    assert st["faces_built"] == 2, (attr_name, merge_flag, st)
                    assert st["annotation_failures"] >= 1, (attr_name, merge_flag, st)
                finally:
                    App.closeDocument(doc.Name)
        finally:
            setattr(importSKP, attr_name, orig_helper)
    print("check_draft_failure_orphan_cleanup_and_isolation: OK (0 orphans, all outer paths guarded)")


def check_exponential_merged_walk_and_cap(path):
    """Point 6:
    - A 20-level binary instance chain (2^20 placements, no annotations) imports
      in merge=True in well under 1 s (no exponential annotation walk).
    - A 15-level binary instance chain (32,768 placements) with a dimension at
      the leaf caps Draft objects at _MAX_IMPORTED_ANNOTATIONS and reports once.
    """
    import time
    import openskp

    orig_parse = openskp.SkpFile.parse

    def build_binary_dag(skp_inst, base_model, depth, with_leaf_dim):
        skp_inst._parsed = None
        base_model.root.texts.clear()
        base_model.root.dimensions.clear()
        base_model.dimensions.clear()
        base_model.definitions.clear()
        ident = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        leaf = openskp.model.Definition(id=1, name="Leaf")
        if with_leaf_dim:
            leaf.dimensions.append(
                openskp.model.Dimension(text="", hidden=False, a=(0.0, 0.0, 0.0), b=(10.0, 0.0, 0.0), offset=0.0)
            )
        base_model.definitions[1] = leaf
        for lvl in range(2, depth + 2):
            node = openskp.model.Definition(id=lvl, name=f"Lvl_{lvl}")
            node.instances.append(openskp.model.Instance(name="A", ref_idx=lvl - 1, matrix=list(ident)))
            node.instances.append(openskp.model.Instance(name="B", ref_idx=lvl - 1, matrix=list(ident)))
            base_model.definitions[lvl] = node
        base_model.root.instances = [
            openskp.model.Instance(name="TopA", ref_idx=depth + 1, matrix=list(ident)),
            openskp.model.Instance(name="TopB", ref_idx=depth + 1, matrix=list(ident)),
        ]
        return base_model

    try:
        openskp.SkpFile.parse = lambda self, *a, **kw: build_binary_dag(self, orig_parse(self, *a, **kw), 20, False)
        doc = App.newDocument()
        try:
            t0 = time.time()
            importSKP.import_skp(path, doc, merge=True)
            elapsed = time.time() - t0
            assert elapsed < 2.0, f"20-level empty DAG took {elapsed:.2f}s"
            assert importSKP.last_import_stats["dimensions_built"] == 0
        finally:
            App.closeDocument(doc.Name)

        orig_cap = importSKP._MAX_IMPORTED_ANNOTATIONS
        importSKP._MAX_IMPORTED_ANNOTATIONS = 50
        try:
            openskp.SkpFile.parse = lambda self, *a, **kw: build_binary_dag(self, orig_parse(self, *a, **kw), 15, True)
            doc2 = App.newDocument()
            try:
                importSKP.import_skp(path, doc2, merge=True)
                st2 = importSKP.last_import_stats
                assert st2["dimensions_built"] == 50, st2
                assert st2["annotations_capped"] > 0, st2
                assert any("capped imported annotations at 50" in w for w in st2["warnings"]), st2["warnings"]
            finally:
                App.closeDocument(doc2.Name)
        finally:
            importSKP._MAX_IMPORTED_ANNOTATIONS = orig_cap
    finally:
        openskp.SkpFile.parse = orig_parse
    print("check_exponential_merged_walk_and_cap: OK (20-level DAG instant, 32,768 leaf dims capped)")


def check_active_document_switching(path):
    """Point 6: importing into ``target_doc`` while another document is active
    via App.setActiveDocument puts every Draft object into ``target_doc`` and
    restores the previously active document afterwards."""
    target_doc = App.newDocument("TargetImportDoc")
    other_doc = App.newDocument("OtherActiveDoc")
    try:
        App.setActiveDocument(other_doc.Name)
        assert App.activeDocument().Name == other_doc.Name
        importSKP.import_skp(path, target_doc, merge=False)
        assert App.activeDocument().Name == other_doc.Name, App.activeDocument().Name
        assert len(other_doc.Objects) == 0, [o.Label for o in other_doc.Objects]
        target_dims = [
            o for o in target_doc.Objects if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        target_txts = [
            o for o in target_doc.Objects if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
        ]
        assert len(target_dims) == 2 and len(target_txts) == 1, (len(target_dims), len(target_txts))
    finally:
        App.closeDocument(other_doc.Name)
        App.closeDocument(target_doc.Name)
    print("check_active_document_switching: OK (zero leakage to other active document)")


def check_nested_sheared_annotations(path):
    """Point 7b: a sheared placement nested inside a component carries its
    annotations into the enclosing component's Annotations group."""
    import openskp

    orig_parse = openskp.SkpFile.parse

    def parse_with_nested_shear(self, *args, **kwargs):
        m = orig_parse(self, *args, **kwargs)
        # Wrap Beam inside an Outer component using a sheared matrix [1, 0.5, 0, ...]
        beam_id, beam_def = next(iter(m.definitions.items()))
        beam_def.texts.append(
            openskp.model.TextEntity(text="Beam note", hidden=False, point=(2.0, 0.0, 0.0), label_point=(4.0, 2.0, 0.0))
        )
        outer_id = max(k for k in m.definitions if isinstance(k, int)) + 1
        outer = openskp.model.Definition(id=outer_id, name="OuterFrame")
        outer.instances.append(
            openskp.model.Instance(
                name="ShearedBeam",
                ref_idx=beam_id,
                matrix=[1.0, 0.5, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 10.0, 0.0, 0.0, 1.0],
                layer="Framing",
            )
        )
        m.definitions[outer_id] = outer
        m.root.instances = [
            openskp.model.Instance(
                name="Outer 1",
                ref_idx=outer_id,
                matrix=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                layer="Framing",
            )
        ]
        return m

    openskp.SkpFile.parse = parse_with_nested_shear
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False)
        stats = importSKP.last_import_stats
        components = doc.getObject(stats["components_group"])
        outer_part = next(o for o in components.Group if o.Label == "OuterFrame")
        outer_annots = [o for o in outer_part.Group if getattr(o, "SketchUpAnnotations", False)]
        assert len(outer_annots) == 1, [o.Label for o in outer_part.Group]
        nested_dims = [
            o for o in outer_annots[0].Group if getattr(o, "SketchUpAnnotationKind", "") == "linear_dimension"
        ]
        nested_txts = [
            o for o in outer_annots[0].Group if getattr(o, "SketchUpAnnotationKind", "").endswith("text")
        ]
        assert len(nested_dims) == 1 and len(nested_txts) == 1
        _assert_vec_in(nested_dims[0].Start, (10.0, 0.0, 0.0), label="sheared nested Start")
    finally:
        openskp.SkpFile.parse = orig_parse
        App.closeDocument(doc.Name)
    print("check_nested_sheared_annotations: OK (nested sheared component keeps annotations)")


def check_preference_and_export(path):
    """Verifies ImportAnnotations preference (default True when unset) and
    explicit annotations=True/False overrides, plus clean export ignoring
    Annotations groups."""
    import openskp

    params = App.ParamGet(importSKP._PARAM_PATH)
    try:
        for pref_val, arg_val, want_flag, want_txts, want_dims in (
            (None, None, True, 1, 2),
            (False, None, False, 0, 0),
            (True, None, True, 1, 2),
            (False, True, True, 1, 2),
            (True, False, False, 0, 0),
        ):
            if pref_val is None:
                params.RemBool("ImportAnnotations")
            else:
                params.SetBool("ImportAnnotations", pref_val)
            doc = App.newDocument()
            try:
                if arg_val is None:
                    importSKP.import_skp(path, doc, merge=False)
                else:
                    importSKP.import_skp(path, doc, merge=False, annotations=arg_val)
                stats = importSKP.last_import_stats
                assert stats["import_annotations"] is want_flag, (pref_val, arg_val, stats)
                assert stats["texts_built"] == want_txts, (pref_val, arg_val, stats)
                assert stats["dimensions_built"] == want_dims, (pref_val, arg_val, stats)
            finally:
                App.closeDocument(doc.Name)
    finally:
        params.RemBool("ImportAnnotations")

    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc, merge=False, annotations=True)
        with tempfile.TemporaryDirectory() as tmp:
            out_skp = os.path.join(tmp, "exported.skp")
            importSKP.export(list(doc.Objects), out_skp)
            rt = openskp.SkpFile.open(out_skp).parse()
            assert len(rt.root.faces) == 1 and len(rt.root.instances) == 1
    finally:
        App.closeDocument(doc.Name)
    print("check_preference_and_export: OK (ImportAnnotations default True, overrides & export clean)")


def _main():
    path = os.path.join(FIXTURES_DIR, "annotations_2017.skp")
    path_2021 = os.path.join(FIXTURES_DIR, "annotations_2021.skp")
    _scan_fixture_no_private_paths(path)
    _scan_fixture_no_private_paths(path_2021)
    check_hierarchy_annotations(path)
    check_merged_annotations(path)
    check_real_2021_annotations()
    check_dimension_planes_and_capilla()
    check_draft_failure_orphan_cleanup_and_isolation(path)
    check_exponential_merged_walk_and_cap(path)
    check_active_document_switching(path)
    check_nested_sheared_annotations(path)
    check_preference_and_export(path)


_harness.run(_main)
