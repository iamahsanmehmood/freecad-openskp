"""The synthetic SketchUp model the hierarchy tests import (tests/test_hierarchy.py,
tests/gui_hierarchy.py), written with openskp's own writer so every expected object
follows from how it is built here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importSKP  # noqa: E402,F401  - registers the vendored openskp

# The synthetic model's instances that sit at the top level (not inside Table):
# name -> (3x3 row-major, translation in inches). Leg is a 2 x 2 x 28 in box.
TOP_LEVEL_LEGS = {
    "Scaled leg": ((0, -3, 0, 2, 0, 0, 0, 0, 0.5), (0, 100, 0)),   # rotated 90 deg, scaled 2 / 3 / 0.5
    "Mirrored leg": ((-1, 0, 0, 0, 1, 0, 0, 0, 1), (50, 100, 0)),  # mirrored in x
}
SHEARED = ((1, 0.5, 0, 0, 1, 0, 0, 0, 1), (100, 100, 0))
LEG_SIZE = (2, 2, 28)

# What a viewer should draw, in inches: everything placed except the table on the hidden
# tag "Open" (x 100 to 140) and the unplaced "Spare" (x and y -50 to -45). From the closed
# table (0 to 40, 0 to 20, 0 to 30), the loose face (y -20), the scaled leg (x -6, y 104)
# and the sheared leg (x 103).
VISIBLE_BOUNDS_IN = ((-6, 103), (-20, 104), (0, 30))


def _box(cb, x, y, z):
    cb.add_face([(0, 0, 0), (0, y, 0), (x, y, 0), (x, 0, 0)])
    cb.add_face([(0, 0, z), (x, 0, z), (x, y, z), (0, y, z)])
    cb.add_face([(0, 0, 0), (x, 0, 0), (x, 0, z), (0, 0, z)])
    cb.add_face([(0, y, 0), (0, y, z), (x, y, z), (x, y, 0)])
    cb.add_face([(0, 0, 0), (0, 0, z), (0, y, z), (0, y, 0)])
    cb.add_face([(x, 0, 0), (x, y, 0), (x, y, z), (x, 0, z)])


def write_synthetic(path):
    """A table (a blue top and four unpainted legs) placed twice: on tag
    "Closed", and on tag "Open" (hidden) painted red. Three legs at the top
    level: one rotated and scaled non-uniformly, one mirrored, one sheared.
    A component "Spare" that is never placed, and one loose face at the top."""
    import openskp

    b = openskp.create()
    red = b.add_material("Red", (200, 30, 30, 255))
    blue = b.add_material("Blue", (30, 30, 200, 255))
    open_tag = b.add_layer("Open", hidden=True)
    closed_tag = b.add_layer("Closed")
    with b.add_component_definition("Leg") as leg:
        _box(leg, *LEG_SIZE)
    with b.add_component_definition("Table") as table:
        table.add_face([(0, 0, 30), (40, 0, 30), (40, 20, 30), (0, 20, 30)], material=blue)
        for i, (x, y) in enumerate([(0, 0), (38, 0), (0, 18), (38, 18)]):
            table.add_instance(leg, name=f"Leg {i + 1}", translation=(x, y, 0))
    with b.add_component_definition("Spare") as spare:
        # away from everything placed, so it shows if definitions are drawn where they are defined
        spare.add_face([(-50, -50, 0), (-45, -50, 0), (-45, -45, 0), (-50, -45, 0)])
    b.add_instance(table, name="Table closed", layer=closed_tag)
    b.add_instance(table, name="Table open", translation=(100, 0, 0), material=red, layer=open_tag)
    for name, (m, t) in TOP_LEVEL_LEGS.items():
        b.add_instance(leg, name=name, matrix3x3=m, translation=t)
    b.add_instance(leg, name="Sheared leg", matrix3x3=SHEARED[0], translation=SHEARED[1])
    b.add_face([(0, -20, 0), (10, -20, 0), (10, -10, 0), (0, -10, 0)])
    with open(path, "wb") as f:
        f.write(b.to_bytes())
