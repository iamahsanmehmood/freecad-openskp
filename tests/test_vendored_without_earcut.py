"""Run with any Python (no FreeCAD needed):

    python tests/test_vendored_without_earcut.py

Regression test for freecad-openskp#3: FreeCAD's bundled Python has no
``mapbox_earcut``, and the vendored openskp used to hard-import it, so every
``.skp`` open died with ``ModuleNotFoundError`` before parsing anything. This
blocks that module the way a missing install would, then imports the vendored
package and runs the same entry points importSKP.py uses (parse + loose edge
runs) over the real fixtures.
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "vendor"))

sys.modules["mapbox_earcut"] = None  # makes `import mapbox_earcut` raise ImportError

import openskp  # noqa: E402


def main():
    assert openskp._core.mapbox_earcut is None, "mapbox_earcut should be unavailable"
    fixtures = sorted(glob.glob(os.path.join(HERE, "fixtures", "*.skp")))
    assert fixtures, "no fixtures found"
    for path in fixtures:
        model = openskp.SkpFile.open(path).parse()
        for definition in list(model.definitions.values()) + [model.root]:
            list(openskp.loose_edge_runs(definition))
        print(f"OK {os.path.basename(path)}: {len(model.definitions)} definitions")
    print("PASS: vendored openskp imports and parses without mapbox_earcut")


if __name__ == "__main__":
    main()
