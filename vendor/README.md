# Vendored dependency: openskp

`vendor/openskp/` is a vendored copy of [openskp](https://github.com/iamahsanmehmood/openskp)
(PyPI: [`openskp`](https://pypi.org/project/openskp/)), pinned to
**v1.3.0** (commit
[`15ccc0f`](https://github.com/iamahsanmehmood/openskp/commit/15ccc0f2c4fe045adb5bee85520bf09286425352)).

## Why vendored instead of a declared dependency

Per FreeCAD's own [dependency guidance](https://freecad.github.io/Addon-Academy/Topics/Dependencies),
a Python dependency should first be checked against the
[Python allow-list](https://freecad.github.io/Addon-Academy/Topics/Dependencies/Allow-List)
before vendoring. `openskp` is not on that list, and even a declared
(non-allow-listed) dependency isn't auto-installed by the Addon Manager -
users would have to `pip install openskp` into FreeCAD's own embedded
interpreter manually before the addon would even import. Vendoring avoids
that friction entirely, and openskp meets the guidance's own criteria for
"vendor with care": pure Python (no compiled extensions to rebuild per
platform), permissively (MIT) licensed - compatible with this addon's own
MIT license - small, and stable.

## What's different from upstream

Nothing beyond the pin itself. No local modifications have been made to
the vendored copy.

## Updating this vendored copy

Copy `packages/python/src/openskp/` from the
[openskp repo](https://github.com/iamahsanmehmood/openskp) over
`vendor/openskp/` here (excluding `__pycache__/`), update the version and
commit SHA above, and re-run the test suite (`freecadcmd
tests/test_import.py` and `tests/test_export.py`).
