# Vendored dependency: openskp

`vendor/openskp/` is a vendored copy of [openskp](https://github.com/iamahsanmehmood/openskp)
(PyPI: [`openskp`](https://pypi.org/project/openskp/)), pinned to
upstream `main` at commit
[`13ae161`](https://github.com/iamahsanmehmood/openskp/commit/13ae161fa0108647a04ad5730776120bb6fe8c83),
which is 1.3.0 plus unreleased fixes. It is pinned to a commit rather than
the 1.3.0 release because 1.3.0 predates two fixes this add-on needs (see
"Why this commit" below); move the pin to the next release once it is cut.

## Why vendored instead of a declared dependency

Per FreeCAD's own [dependency guidance](https://freecad.github.io/Addon-Academy/Topics/Dependencies),
a Python dependency should first be checked against the
[Python allow-list](https://freecad.github.io/Addon-Academy/Topics/Dependencies/Allow-List)
before vendoring. `openskp` is not on that list, and even a declared
(non-allow-listed) dependency isn't auto-installed by the Addon Manager -
users would have to `pip install openskp` into FreeCAD's own embedded
interpreter manually before the addon would even import. Vendoring avoids
that friction entirely, and openskp meets the guidance's own criteria for
"vendor with care": pure Python in everything this addon actually uses (the one
compiled dependency, `mapbox_earcut`, is only needed by openskp's scene/mesh
triangulation, which this importer doesn't call - see below), permissively (MIT) licensed - compatible with this addon's own
MIT license - small, and stable.

## What's different from upstream

Nothing. `vendor/openskp/` is byte-for-byte `packages/python/src/openskp/` at
the pinned commit (plus this add-on's own `LICENSE` copy, which upstream keeps
at the repository root).

Until 2026-10-17 this section described one local modification, making
`import mapbox_earcut` optional so `import openskp` works in FreeCAD's bundled
Python ([freecad-openskp#3](https://github.com/iamahsanmehmood/freecad-openskp/issues/3)).
That change is now upstream
([openskp#406](https://github.com/iamahsanmehmood/openskp/pull/406)), so the
local patch is gone.

## Why this commit

- **Dimensions anchored inside components
  ([openskp#384](https://github.com/iamahsanmehmood/openskp/pull/384)).** The
  1.3.0 legacy reader lost its place at the first such dimension and silently
  dropped every root entity after it. On a real SketchUp 2017 model (96 placed
  components) it read 75, so about a fifth of the model was missing from the
  import with no error. See
  [openskp#412](https://github.com/iamahsanmehmood/openskp/issues/412).
- **Optional `mapbox_earcut`
  ([openskp#406](https://github.com/iamahsanmehmood/openskp/pull/406)).** See
  above.
- Also new in this copy's legacy (pre-2021) reader: SketchUp 6 files with
  attribute dictionaries
  ([openskp#385](https://github.com/iamahsanmehmood/openskp/pull/385)), and
  SketchUp 3 files, including single-material ones
  ([openskp#407](https://github.com/iamahsanmehmood/openskp/pull/407),
  [#419](https://github.com/iamahsanmehmood/openskp/pull/419)).

## Updating this vendored copy

Export `packages/python/src/openskp/` from the
[openskp repo](https://github.com/iamahsanmehmood/openskp) at the commit you
are pinning, using `git archive` so no `__pycache__/` or untracked files come
along:

```
git -C path/to/openskp archive <commit> packages/python/src/openskp | tar -x -C /tmp/rv
cp -r /tmp/rv/packages/python/src/openskp/. vendor/openskp/
```

Update the pin above, check that `diff -r -x LICENSE vendor/openskp
/tmp/rv/packages/python/src/openskp` is empty, and re-run the tests (`freecadcmd
tests/test_import.py`, `freecadcmd tests/test_export.py`, and
`python tests/test_vendored_without_earcut.py`).
