"""Makes a failing test fail the CI job.

``freecadcmd script.py`` does not: when the script raises (an ``assert`` that
fails, an import error, anything), FreeCAD prints the traceback to its own console and
exits 0, so a CI step that just runs the script is green whatever the script found. This
was checked directly by pushing an unconditional ``assert False`` at the top level of
tests/test_import.py: the job still passed. Until this existed, none of the FreeCAD tests
in this repo could fail in CI.

Use it like this, around everything that can raise::

    import _harness
    try:
        ...imports and the checks...
    except BaseException:
        _harness.die()

or ``_harness.run(main)`` for a function. ``die()`` reports the traceback on stderr and on
FreeCAD's console and ends the process with exit code 1 (``os._exit``, because a plain
``sys.exit`` is caught by FreeCAD's script runner just like the exception was).
"""
import os
import sys
import traceback


def die():
    """Report the exception being handled and end the process with a non-zero exit code."""
    text = traceback.format_exc()
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        import FreeCAD as App

        App.Console.PrintError(text + "\n")
    except Exception:
        pass
    os._exit(1)


def run(main):
    """Call ``main()``; any exception it raises ends the process non-zero."""
    try:
        main()
    except BaseException:
        die()
