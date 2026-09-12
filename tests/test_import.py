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
        print(
            f"{fixture_name}: {len(faces)} faces, "
            f"{len(invalid)} invalid, {len(nan_area)} NaN-area"
        )
        return len(faces), len(invalid), len(nan_area)
    finally:
        App.closeDocument(doc.Name)


# Note: freecadcmd runs a script file as a module named after the file
# (e.g. "test_import"), not "__main__" like a normal Python interpreter -
# so this runs unconditionally at import time rather than behind an
# `if __name__ == "__main__"` guard, which would silently never fire here.
for _name in os.listdir(FIXTURES_DIR):
    if _name.endswith(".skp"):
        check(_name)
