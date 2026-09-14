"""Run with FreeCAD's own headless interpreter, e.g.:

    freecadcmd tests/test_import.py

Verifies importSKP.import_skp() against real fixture files, checking
face count, validity, and absence of degenerate (zero-area/NaN) faces -
not just "it didn't crash."
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

# Expected (faces, invalid, nan_area) per fixture - from the README's own
# "Verification status" table. A real regression (a suddenly-degenerate
# import) must fail this test, not just print a different number - that's
# the whole point of pinning these rather than only checking "didn't crash."
EXPECTED = {
    "SU_File.skp": (32, 0, 0),
    "capilla_quiroz_v17.skp": (192, 0, 0),
}


def check(fixture_name):
    path = os.path.join(FIXTURES_DIR, fixture_name)
    doc = App.newDocument()
    try:
        importSKP.import_skp(path, doc)
        objs = [o for o in doc.Objects if hasattr(o, "Shape")]
        assert objs, f"{fixture_name}: no shape object created"
        faces = objs[0].Shape.Faces
        invalid = [f for f in faces if not f.isValid()]
        nan_area = [f for f in faces if math.isnan(f.Area)]
        got = (len(faces), len(invalid), len(nan_area))
        print(f"{fixture_name}: {got[0]} faces, {got[1]} invalid, {got[2]} NaN-area")
        expected = EXPECTED.get(fixture_name)
        if expected is not None:
            assert got == expected, (
                f"{fixture_name}: expected {expected} (faces, invalid, nan_area), got {got}"
            )
        return got
    finally:
        App.closeDocument(doc.Name)


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
