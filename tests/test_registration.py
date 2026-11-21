"""Run with FreeCAD's own headless interpreter, in a FRESH process whose user data
directory holds an installed copy of this add-on:

    FREECAD_USER_DATA=/some/dir freecadcmd tests/test_registration.py

(with the add-on's files copied to /some/dir/Mod/OpenSKP; the CI step does exactly
that, see .github/workflows/ci.yml).

The other test files call the importer directly, so they cannot tell whether FreeCAD
registers the SketchUp import/export types by itself at startup. That registration is
FreeCAD's job, done while it starts and from the files of the installed add-on, so it
can only be checked by starting FreeCAD with the add-on installed and asking what it
registered. This is the check that was missing when package.xml changed from
``<workbench>`` to ``<other>``: FreeCAD runs a top-level ``Init.py`` only for
``<workbench>`` entries, so the types silently stopped being registered (#14) while
every other test stayed green.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402  - freecadcmd exits 0 on a failing script; see tests/_harness.py


def main():
    import FreeCAD

    user_data = os.environ.get("FREECAD_USER_DATA")
    assert user_data, (
        "set FREECAD_USER_DATA to a scratch directory that holds the add-on in Mod/<name>: "
        "without it this would test whatever happens to be installed on this machine"
    )
    mod = os.path.join(os.path.abspath(FreeCAD.getUserAppDataDir()), "Mod")
    assert os.path.abspath(user_data) == os.path.abspath(FreeCAD.getUserAppDataDir()), (
        f"FreeCAD is using {FreeCAD.getUserAppDataDir()!r}, not FREECAD_USER_DATA={user_data!r}"
    )
    assert os.path.isfile(os.path.join(mod, "OpenSKP", "package.xml")), (
        f"the add-on is not installed in {mod}"
    )

    for kind, registered in (
        ("import", FreeCAD.getImportType("skp")),
        ("export", FreeCAD.getExportType("skp")),
    ):
        assert "importSKP" in registered, (
            f".skp {kind} is not registered at startup: FreeCAD offers {registered!r}"
        )

    # What FreeCAD registered must be the installed add-on's module, importable as is.
    import importSKP

    installed = os.path.abspath(os.path.join(mod, "OpenSKP", "importSKP.py"))
    assert os.path.abspath(importSKP.__file__) == installed, (
        f"importSKP comes from {importSKP.__file__!r}, not from the installed add-on {installed!r}"
    )
    assert callable(importSKP.open) and callable(importSKP.export)
    print("registration: .skp import and export registered at startup by the installed add-on - OK")


_harness.run(main)
