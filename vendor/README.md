# Vendored dependency: openskp

`vendor/openskp/` is a vendored copy of [openskp](https://github.com/iamahsanmehmood/openskp)
(PyPI: [`openskp`](https://pypi.org/project/openskp/)), pinned to
upstream `main` at commit
[`4046d12`](https://github.com/iamahsanmehmood/openskp/commit/4046d12c6405f6ea5af45510f9b0c7a7e299f069),
which is 1.3.0 plus unreleased fixes. It is pinned to a commit rather than
the 1.3.0 release because 1.3.0 predates fixes this add-on needs (see
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

Moved on 2026-10-27 from `13ae161`. What the new copy adds for an import:

- **Instances scaled through the matrix's scale term
  ([openskp#444](https://github.com/iamahsanmehmood/openskp/issues/444)).**
  SketchUp can keep a uniform scale in an instance's transformation instead of
  in its 3x3. Every earlier reader ignored it, so such an instance came out 100x
  its size and 100x its distance from its parent in this add-on too.
- **More legacy (pre-2021) files read correctly:** SketchUp 4, 5 and 2019 files
  ([openskp#440](https://github.com/iamahsanmehmood/openskp/pull/440)) and files
  saved through SketchUp's C API as 2019 or 2020
  ([#459](https://github.com/iamahsanmehmood/openskp/pull/459)); a 2021+ scene
  that saves no camera is no longer dropped
  ([#458](https://github.com/iamahsanmehmood/openskp/pull/458)).
- **Faster 2021+ parsing:** the same model data, 2 to 4 times faster on a large
  file ([#439](https://github.com/iamahsanmehmood/openskp/pull/439),
  [#453](https://github.com/iamahsanmehmood/openskp/pull/453)).
- **OBJ and PLY export fixes** that keep material names unique and colours
  ([#421](https://github.com/iamahsanmehmood/openskp/pull/421),
  [#423](https://github.com/iamahsanmehmood/openskp/pull/423)); this add-on does
  not call those exporters.
- **New data this add-on can use later:** `Definition.is_group`
  ([#446](https://github.com/iamahsanmehmood/openskp/issues/446)), and the
  exact centre, radius and angles of every arc and circle in
  `Definition.arcs` ([#462](https://github.com/iamahsanmehmood/openskp/issues/462);
  see freecad-openskp#7). Nothing in the importer reads them yet, so this
  update changes no import beyond the fixes above.

What the previous pin (`13ae161`) already brought:

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
