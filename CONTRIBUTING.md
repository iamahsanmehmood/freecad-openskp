# Contributing to OpenSKP Import/Export for FreeCAD

Thanks for considering a contribution. This is a small, single-file addon —
the barrier to contributing should be low, and this doc is scoped to match.

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md).

## Getting set up

You need a real FreeCAD install (not just the `openskp` Python package) —
the importer/exporter is built against FreeCAD's own `Part`/`App` APIs and
can only be exercised inside FreeCAD's own Python environment.

```bash
git clone https://github.com/iamahsanmehmood/freecad-openskp.git
```

Copy (or symlink) the cloned folder into FreeCAD's `Mod/` directory as
`OpenSKPImporter` so FreeCAD picks it up (`Help → About → "Report an
issue"` shows your `Mod/` path). Restart FreeCAD.

The `vendor/openskp/` directory is a vendored copy of the pure-Python
[OpenSKP](https://github.com/iamahsanmehmood/openskp) package — no
separate `pip install` needed, and no network access required to run the
addon or its tests.

## Running tests

Both test files run under FreeCAD's own headless interpreter:

```bash
freecadcmd tests/test_import.py
freecadcmd tests/test_export.py
```

Note: `freecadcmd` sets `__name__` to the *filename*, not `"__main__"` —
both test files run unconditionally at module level for this reason
rather than relying on an `if __name__ == "__main__":` guard, which would
silently never fire.

`tests/fixtures/` holds small, real `.skp` files (borrowed from OpenSKP's
own public test fixtures). If you add a new one, keep it small and make
sure you have the right to distribute it.

## Making changes

### Branch naming

| Type | Format | Example |
|:-----|:-------|:--------|
| Feature | `feat/short-description` | `feat/carry-over-materials` |
| Bug fix | `fix/short-description` | `fix/hole-winding-fallback` |
| Docs | `docs/short-description` | `docs/clarify-mod-folder-path` |

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): short description`, e.g. `fix: cache unique-definition
shapes instead of rebuilding per placement`.

## Pull requests

1. Fork the repo, branch from `main`.
2. Run both test files against the fixtures and, where the change touches
   the import or export path, against a real `.skp` file of your own —
   numbers (face counts, timings) in the PR description are far more
   useful than "works for me."
3. Update the README if the change affects what's supported or verified —
   this project is deliberately honest about what's *not* yet done
   (materials/layers aren't carried over either direction, for instance);
   don't let that drift out of date.
4. Open a PR using the [template](.github/PULL_REQUEST_TEMPLATE.md).

## Reporting bugs / suggesting features

Use the [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) or
[Feature Request](.github/ISSUE_TEMPLATE/feature_request.md) templates.
For a bug, attaching the `.skp` file that triggers it (or a minimal one
that reproduces it) is the single biggest time-saver.

## Scope

Import/export of geometry, built on OpenSKP. Anything about the
underlying `.skp` format itself (a parsing gap, a new attribute type, a
new export format) belongs in the
[OpenSKP repo](https://github.com/iamahsanmehmood/openskp), not here —
this addon is a thin FreeCAD-specific bridge on top of it, kept
intentionally independent so it stays usable outside of any one BIM
workflow (see the [discussion](https://github.com/IfcOpenShell/IfcOpenShell/issues/9481)
that shaped that call).

Thanks for helping make this better.
