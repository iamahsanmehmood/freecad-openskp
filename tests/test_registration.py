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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402  - freecadcmd exits 0 on a failing script; see tests/_harness.py

_MANIFEST_NS = "https://wiki.freecad.org/Package_Metadata"
_DISALLOWED_NAME_CHARS = set('/\\?%*:|"<>')


def check_manifest_and_layout(addon_dir: str) -> None:
    """Validate package.xml and directory layout against FreeCAD Addon Index rules (#32)."""
    import defusedxml.ElementTree as ET

    manifest_path = os.path.join(addon_dir, "package.xml")
    assert os.path.isfile(manifest_path), f"missing {manifest_path}"
    root = ET.parse(manifest_path).getroot()
    assert root.tag == f"{{{_MANIFEST_NS}}}package", f"unexpected root tag {root.tag!r}"
    assert root.attrib.get("format") == "1", f"unexpected format={root.attrib.get('format')!r}"

    def find_one(tag: str):
        el = root.find(f"{{{_MANIFEST_NS}}}{tag}")
        assert el is not None and (el.text or "").strip(), f"missing <{tag}> in package.xml"
        return el

    name = find_one("name").text.strip()
    bad_chars = _DISALLOWED_NAME_CHARS.intersection(name)
    assert not bad_chars, f"<name>{name!r}</name> contains disallowed filename chars {sorted(bad_chars)}"

    version = find_one("version").text.strip()
    assert re.match(r"^\d+\.\d+\.\d+", version), f"invalid semver <version>{version!r}</version>"
    date_str = find_one("date").text.strip()
    assert re.match(r"^\d{4}[-.]\d{2}[-.]\d{2}$", date_str), f"invalid <date>{date_str!r}</date>"
    find_one("description")

    maint = find_one("maintainer")
    assert "@" in maint.attrib.get("email", ""), "missing maintainer email attribute"
    find_one("author")

    lic = find_one("license")
    assert lic.text.strip() == "MIT", f"expected SPDX 'MIT', got {lic.text!r}"
    lic_file = lic.attrib.get("file", "")
    assert lic_file and os.path.isfile(os.path.join(addon_dir, lic_file)), (
        f"missing license file {lic_file!r}"
    )
    assert os.path.isfile(os.path.join(addon_dir, "vendor", "openskp", "LICENSE")), (
        "vendored openskp copy must preserve upstream LICENSE"
    )

    urls = {u.attrib.get("type"): u for u in root.findall(f"{{{_MANIFEST_NS}}}url")}
    assert "repository" in urls and urls["repository"].attrib.get("branch") == "main", (
        "missing <url type='repository' branch='main'>"
    )
    assert "bugtracker" in urls, "missing <url type='bugtracker'>"
    assert "readme" in urls, "missing <url type='readme'>"
    readme_url = (urls["readme"].text or "").strip()
    assert "/raw/" in readme_url and "/blob/" not in readme_url, (
        f"<url type='readme'> must point to raw Markdown, got {readme_url!r}"
    )

    icon_rel = find_one("icon").text.strip()
    icon_path = os.path.join(addon_dir, *icon_rel.split("/"))
    assert os.path.isfile(icon_path), f"missing icon {icon_rel!r}"
    assert icon_path.lower().endswith((".svg", ".svgz")), (
        f"Addon Index static analyzer expects a scalable SVG icon, got {icon_rel!r}"
    )
    assert os.path.getsize(icon_path) <= 16 * 1024, f"icon {icon_rel!r} exceeds 16 KiB"

    assert find_one("freecadmin").text.strip() == "1.1.0"
    assert re.match(r"^3\.\d+", find_one("pythonmin").text.strip())

    deps = {
        (d.attrib.get("type", "automatic"), (d.text or "").strip()): d.attrib.get("optional", "false")
        for d in root.findall(f"{{{_MANIFEST_NS}}}depend")
    }
    assert ("internal", "part") in deps, f"missing internal part dependency: {deps}"
    assert ("python", "defusedxml") in deps, f"missing defusedxml dependency: {deps}"
    assert ("python", "numpy") in deps, f"missing numpy dependency: {deps}"

    content = root.find(f"{{{_MANIFEST_NS}}}content")
    assert content is not None, "missing <content> element"
    assert content.find(f"{{{_MANIFEST_NS}}}other") is not None, "expected <other> content item"
    assert content.find(f"{{{_MANIFEST_NS}}}workbench") is None, "unexpected <workbench> content item"

    # Layout rules (FreeCAD/Addon-Reports check_layout.py + #14):
    # - freecad/ must be a PEP 420 namespace package (no freecad/__init__.py: L002)
    # - no __init__.py at repository root (L001)
    # - no top-level Init.py / InitGui.py dependency
    assert os.path.isfile(os.path.join(addon_dir, "freecad", "openskp_importer", "init.py"))
    assert not os.path.exists(os.path.join(addon_dir, "freecad", "__init__.py")), (
        "freecad/ namespace root must not contain __init__.py (L002)"
    )
    assert not os.path.exists(os.path.join(addon_dir, "__init__.py")), (
        "addon root must not contain __init__.py (L001)"
    )
    assert not os.path.exists(os.path.join(addon_dir, "Init.py")), (
        "addon must not rely on a top-level Init.py (#14)"
    )
    assert not os.path.exists(os.path.join(addon_dir, "InitGui.py")), (
        "addon has no workbench GUI and must not carry a top-level InitGui.py"
    )
    print("registration: package.xml manifest and Addon Index layout checks - OK")


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
    addon_dir = os.path.join(mod, "OpenSKP")
    assert os.path.isfile(os.path.join(addon_dir, "package.xml")), (
        f"the add-on is not installed in {mod}"
    )
    check_manifest_and_layout(addon_dir)

    for kind, registered in (
        ("import", FreeCAD.getImportType("skp")),
        ("export", FreeCAD.getExportType("skp")),
    ):
        assert "importSKP" in registered, (
            f".skp {kind} is not registered at startup: FreeCAD offers {registered!r}"
        )

    # What FreeCAD registered must be the installed add-on's module, importable as is.
    import importSKP

    installed = os.path.abspath(os.path.join(addon_dir, "importSKP.py"))
    assert os.path.abspath(importSKP.__file__) == installed, (
        f"importSKP comes from {importSKP.__file__!r}, not from the installed add-on {installed!r}"
    )
    assert callable(importSKP.open) and callable(importSKP.export)
    print("registration: .skp import and export registered at startup by the installed add-on - OK")


_harness.run(main)
