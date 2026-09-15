"""Run with FreeCAD's own headless interpreter, e.g.:

    freecadcmd tests/test_import.py

Verifies importSKP.import_skp() against real fixture files, checking
face count, validity, absence of degenerate (zero-area/NaN) faces, AND
loose-edge (structural-framing/light-gauge-steel) run count - not just
"it didn't crash." The loose-edge count is the regression guard for a
real gap: a definition made entirely of loose edges contributed nothing
to the import at all before openskp.loose_edge_runs() existed - found
testing against a real structural-framing file (1,077 runs across 93 of
145 definitions, all silently missing beforehand), the same class of bug
already found and fixed in the Blender addon.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importSKP  # noqa: E402

import FreeCAD as App  # noqa: E402

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures"
)

# Expected (faces, invalid, nan_area, loose_edges) per fixture - from the
# README's own "Verification status" table. A real regression (a
# suddenly-degenerate import, or loose edges silently dropping again)
# must fail this test, not just print a different number - that's the
# whole point of pinning these rather than only checking "didn't crash."
EXPECTED = {
    "SU_File.skp": (32, 0, 0, 0),
    "capilla_quiroz_v17.skp": (192, 0, 0, 20),
}


def check(fixture_name):
    path = os.path.join(FIXTURES_DIR, fixture_name)
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc)
        objs = [o for o in doc.Objects if hasattr(o, "Shape")]
        assert objs, f"{fixture_name}: no shape object created"
        shape = objs[0].Shape
        faces = shape.Faces
        invalid = [f for f in faces if not f.isValid()]
        nan_area = [f for f in faces if math.isnan(f.Area)]

        face_edge_hashes = {e.hashCode() for f in faces for e in f.Edges}
        loose_edges = [e for e in shape.Edges if e.hashCode() not in face_edge_hashes]

        got = (len(faces), len(invalid), len(nan_area), len(loose_edges))
        print(
            f"{fixture_name}: {got[0]} faces, {got[1]} invalid, {got[2]} NaN-area, "
            f"{got[3]} loose edges"
        )
        expected = EXPECTED.get(fixture_name)
        if expected is not None:
            assert got == expected, (
                f"{fixture_name}: expected {expected} (faces, invalid, nan_area, loose_edges), got {got}"
            )
        check_face_colors(fixture_name, len(faces))
        return got
    finally:
        App.closeDocument(doc.Name)


# Per fixture: expected distinct RGBA colors present after material
# resolution (rounded to 3dp) - pinned from real, independently-verified
# facts about these exact source files: SU_File.skp carries no materials
# at all (every face falls back to the shared default), while
# capilla_quiroz_v17.skp's 0.5/0.7-alpha translucent glass materials were
# already cross-validated once, independently, against openskp's own
# build_instanced_scene() output while adding the equivalent Blender
# materials support (see blender-openskp/tests/test_import.py) - the SAME
# source file, so those two alpha values recurring here is a genuine
# cross-check, not a coincidence.
EXPECTED_DISTINCT_ALPHAS = {
    "SU_File.skp": {1.0},
    "capilla_quiroz_v17.skp": {0.5, 0.7, 1.0},
}


def check_face_colors(fixture_name, expected_face_count):
    """Cross-checks material -> per-face-color resolution directly
    (import_skp() itself only applies ViewObject.DiffuseColor when
    App.GuiUp, which freecadcmd never is - so this calls the same
    _get_local_shape() used in production directly, the only way to
    exercise this headlessly) against the pinned facts above, not just
    "some color got produced." """
    import openskp

    path = os.path.join(FIXTURES_DIR, fixture_name)
    model = openskp.SkpFile.open(path).parse()

    stats = {"faces_built": 0, "faces_skipped": 0, "placements": 0, "edge_runs_built": 0, "edge_runs_skipped": 0}
    shape, colors = importSKP._get_local_shape(model.root, model, stats, {}, frozenset())
    assert len(colors) == expected_face_count, (
        f"{fixture_name}: {len(colors)} face colors for {expected_face_count} faces - "
        "must be 1:1 with Shape.Faces, since ViewObject.DiffuseColor is applied positionally"
    )
    assert shape is not None and len(shape.Faces) == expected_face_count

    got_alphas = {round(c[3], 3) for c in colors}
    expected_alphas = EXPECTED_DISTINCT_ALPHAS.get(fixture_name)
    if expected_alphas is not None:
        assert got_alphas == expected_alphas, (
            f"{fixture_name}: expected alpha values {expected_alphas}, got {got_alphas}"
        )
    print(f"{fixture_name}: {len(set(colors))} distinct face colors, alphas {sorted(got_alphas)} - OK")


# Note: freecadcmd runs a script file as a module named after the file
# (e.g. "test_import"), not "__main__" like a normal Python interpreter -
# so this runs unconditionally at import time rather than behind an
# `if __name__ == "__main__"` guard, which would silently never fire here.
_checked = 0
for _name in os.listdir(FIXTURES_DIR):
    if _name.endswith(".skp"):
        check(_name)
        _checked += 1
assert _checked == len(EXPECTED), (
    f"expected to check {len(EXPECTED)} fixtures, found {_checked} .skp files in {FIXTURES_DIR}"
)
