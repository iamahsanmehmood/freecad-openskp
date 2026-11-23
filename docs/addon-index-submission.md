# FreeCAD Addon Index Submission Checklist & Issue Text

This document records the readiness check for listing `freecad-openskp` in the official [FreeCAD Addon Index](https://github.com/FreeCAD/Addons) (used by FreeCAD 1.1+ and Addon Manager 2026.2.19+) and provides the exact submission text for the maintainer.

## Background

The first submission ([FreeCAD/Addons#151](https://github.com/FreeCAD/Addons/issues/151), opened 2026-09-15) was closed on 2026-09-16 solely under the mandatory 30-day minimum repository age policy. The repository (`created_at: 2026-09-12`) now satisfies that requirement.

---

## Readiness Checklist

Checked against the [Addon Academy Quality Requirements](https://freecad.github.io/Addon-Academy/Topics/Addon-Index/Index/Qualities), [Addon Manifest specification](https://freecad.github.io/Addon-Academy/Topics/Structuring/Manifest), [Structuring guide](https://freecad.github.io/Addon-Academy/Topics/Structuring), [Python allow-list & Vendoring rules](https://freecad.github.io/Addon-Academy/Topics/Dependencies/Vendoring), and FreeCAD's automated [`Addon-Reports` static analyzer](https://github.com/FreeCAD/Addon-Reports):

### Governance & Repository Metadata
- [x] **Repository age >= 30 days**: Created `2026-09-12`.
- [x] **Active maintainer**: `<maintainer>` and `<author>` set in `package.xml`.
- [x] **GitHub repository topics**: Includes `freecad` and `addon` (`3d`, `addon`, `bim`, `cad`, `freecad`, `python`, `sketchup`, `skp`).
- [x] **Privacy / network / AI**: Offline file importer/exporter only; zero network calls, telemetry, or AI integration.

### Licensing
- [x] **Consistent SPDX identifier**: `<license file="LICENSE">MIT</license>` matches root `LICENSE` (`MIT`) and GitHub's detected repository license.
- [x] **Vendored license preserved**: `vendor/openskp/LICENSE` (`MIT`) and provenance documentation in `vendor/README.md` are included in the repository.

### Manifest (`package.xml`)
- [x] **RelaxNG schema (`package-schema.rng.xml`)**: Valid XML 1.0 document under `xmlns="https://wiki.freecad.org/Package_Metadata"`, `format="1"`.
- [x] **Filename-safe `<name>`**: Changed from `OpenSKP Import/Export` to `OpenSKP Import-Export` (`/` is disallowed by the Manifest `<name>` specification because `/\?%*:|"<>` are invalid in directory names).
- [x] **Raw Markdown `<url type="readme">`**: Changed from `/blob/main/README.md` (HTML page) to `https://github.com/iamahsanmehmood/freecad-openskp/raw/main/README.md` per the Manifest `<url>` specification so Addon Manager renders Markdown directly.
- [x] **Repository branch match**: `<url type="repository" branch="main">` matches the repository's default branch.
- [x] **Scalable SVG `<icon>` <= 16 KiB**: Added `icon.svg` (< 1 KiB) and referenced `<icon>icon.svg</icon>` (eliminating `Addon-Reports` `LOW` non-scalable `.png` icon warning; `icon.png` is retained alongside it).
- [x] **Version gating**: `<freecadmin>1.1.0</freecadmin>` (required for `<other>` content items) and `<pythonmin>3.10</pythonmin>`.
- [x] **Declared dependencies**:
  - `<depend type="internal">part</depend>`
  - `<depend type="internal" optional="true">assembly</depend>`
  - `<depend type="python">defusedxml</depend>` (present on `FreeCAD/Addons` `Data/Python/{3.10..3.14}/Allowed-Packages`)
  - `<depend type="python">numpy</depend>` (present on `FreeCAD/Addons` `Data/Python/{3.10..3.14}/Allowed-Packages`)

### Layout & Startup Registration (`<other>` Content Type)
- [x] **`<other>` content entry, not `<workbench>`**: This add-on adds `.skp` file import/export handlers rather than a toolbar/workbench (`InitGui` class).
- [x] **PEP 420 `freecad` namespace layout**: Startup registration lives in `freecad/openskp_importer/init.py`, with **no** `freecad/__init__.py` (`L002`), **no** root `__init__.py` (`L001`), and **no** top-level `Init.py` or `InitGui.py` (which FreeCAD skips for `<other>` entries, see #14).
- [x] **No `sys.path` mutation**: `importSKP._load_vendored_openskp()` loads `vendor/openskp` via `importlib.util.spec_from_file_location` only when `openskp` is not already in `sys.modules`, leaving `sys.path` untouched.
- [x] **Verified at startup in CI**: `tests/test_registration.py` installs `git archive HEAD` into `FREECAD_USER_DATA/Mod/OpenSKP` and verifies both `package.xml`/layout compliance and `FreeCAD.getImportType("skp")` / `FreeCAD.getExportType("skp")` on **FreeCAD 1.1.3** and **FreeCAD 1.1.4**.

### Dependencies, Vendoring & Static Analysis (`Addon-Reports` / Bandit)
- **Why `vendor/openskp` is vendored**:
  - `openskp` on PyPI declares mesh/CLI dependencies (`mapbox_earcut>=1.0`, `trimesh>=3.0`, `numpy>=2.2.6`) needed by its triangulated scene/GLB pipeline. `mapbox_earcut` is a compiled C++ extension not on FreeCAD's `Allowed-Packages` list, and `numpy>=2.2.6` can conflict with FreeCAD's bundled NumPy pin.
  - `freecad-openskp` calls only `openskp`'s pure-Python B-rep parser (`SkpFile.open().parse()`) and writer (`openskp.create()`), whose runtime imports are strictly `numpy` and `defusedxml` (both on `Allowed-Packages` and shipped in official FreeCAD builds). `vendor/openskp/` is an unmodified MIT-licensed pure-Python copy documented in `vendor/README.md`.
- **Bandit scan (`bandit -r` with `Addon-Reports` `skips: ["B101", "B110"]`)**:
  - `importSKP.py` and `freecad/`: **0 findings**.
  - `vendor/openskp/export/dxf.py:344`: 1 `B324` report (`hashlib.md5(...)` without `usedforsecurity=False`, used only to shorten long AutoCAD layer names in `openskp`'s unused DXF exporter). Because `vendor/openskp` is a byte-for-byte copy of upstream `openskp` 1.6.0, that `usedforsecurity=False` keyword belongs in upstream `openskp` prior to the next re-vendor.

### Pre-Submission Maintainer Steps
1. **Bump `<version>` and `<date>` in `package.xml` when cutting the next release tag** (the Addon Manager compares `<version>` on the tracked branch to offer updates; see [Updating an addon](https://freecad.github.io/Addon-Academy/Guides/Maintaining/Updating) if you later split a `release` branch from `main`).
2. Open a new **Addon - Addition** issue at `https://github.com/FreeCAD/Addons/issues/new?template=Addon-Addition.yml` using the exact fields below.

---

## Submission Issue Text (`FreeCAD/Addons` -> `Addon - Addition`)

**Title:**
```text
OpenSKP Import-Export
```

**🔌 Repository:**
```text
https://github.com/iamahsanmehmood/freecad-openskp
```

**📑 Notes:**
```text
Resubmission of #151 (closed on 2026-09-16 solely for the 30-day repository age rule; the repository was created on 2026-09-12).

Native SketchUp (.skp, versions 3 through 2026) import and export for FreeCAD 1.1+, built on OpenSKP (https://github.com/iamahsanmehmood/openskp, MIT) — no Trimble SDK and no SketchUp installation required.

What it supports:
- Import: exact B-rep planar faces/loops (Part.Face / Part.Compound rather than triangle soup), SketchUp component & group hierarchy via App::Part + App::Link (or optional FreeCAD 1.x Assembly::AssemblyObject containers, or per-tag merged compounds for very large models), optional watertight solid sewing (MakeSolids), optional analytic cylinder/cone/sphere & planar arc fitting (FitCurvedSurfaces), loose-edge/framing runs & true arcs/circles, per-face material colours, and SketchUp tag visibility.
- Export: writes FreeCAD objects, materials, and tags to native .skp files.
- Packaging & layout: uses `<freecadmin>1.1.0</freecadmin>`, `<other>` content type (file-format importer/exporter with no workbench GUI), and namespaced startup registration in `freecad/openskp_importer/init.py` (no top-level `Init.py` / `InitGui.py`, no `sys.path` modification). Runtime Python dependencies (`numpy`, `defusedxml`) are declared in `package.xml` and are on `Allowed-Packages`.
- Note on `vendor/openskp`: `openskp` is vendored as an unmodified, pure-Python MIT copy (documented with its `LICENSE` in `vendor/README.md`) because the PyPI `openskp` distribution pulls compiled mesh-triangulation dependencies (`mapbox_earcut`, `trimesh`, `numpy>=2.2.6`) for its GLB/scene pipeline that this B-rep importer never calls.
- CI: automated headless + Xvfb GUI test suite on official FreeCAD 1.1.3 and 1.1.4 Linux AppImages on every push/PR, including clean-room `Mod/` startup registration checks.
```

---

## Optional: `Package - Addition` Text (Only If Reviewers Prefer Unvendoring `openskp`)

If upstream `openskp` later moves `mapbox_earcut` / `trimesh` into an optional extra (e.g. `openskp[mesh]`) and the Addon Index reviewers prefer `openskp` on `Allowed-Packages` over `vendor/openskp/`, open `https://github.com/FreeCAD/Addons/issues/new?template=Package-Addition.yml` with:

**Title:**
```text
openskp
```

**📦 Package:**
```text
openskp
```

**📑 Reason:**
```text
My addon `OpenSKP Import-Export` (https://github.com/iamahsanmehmood/freecad-openskp) uses `openskp` (https://pypi.org/project/openskp/, MIT-licensed pure-Python SketchUp .skp binary parser and writer) to import and export native SketchUp (.skp) models in FreeCAD without requiring the proprietary Trimble C SDK. Its core parser/writer runtime dependencies (`numpy`, `defusedxml`, `shapely`) are already on FreeCAD's `Allowed-Packages` list.
```
