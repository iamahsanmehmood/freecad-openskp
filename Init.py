"""Console-mode init - registers the .skp import type whether or not the
FreeCAD GUI is running. See importSKP.py for the actual importer."""
import FreeCAD

FreeCAD.addImportType("SketchUp (*.skp)", "importSKP")
