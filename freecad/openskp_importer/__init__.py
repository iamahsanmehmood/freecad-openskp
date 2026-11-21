"""FreeCAD's startup package for the OpenSKP importer/exporter.

FreeCAD imports ``freecad.<package>.init`` from every add-on that ships a ``freecad``
namespace directory. That is how this add-on gets its .skp import/export types
registered when package.xml declares the ``<other>`` content type: FreeCAD runs a
top-level ``Init.py`` only for ``<workbench>`` entries (#14). The importer and exporter
themselves are in ``importSKP.py``, next to package.xml.
"""
