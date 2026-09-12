"""Console-mode init - registers the .skp import/export types whether or
not the FreeCAD GUI is running. See importSKP.py for the actual
importer/exporter."""
import FreeCAD

FreeCAD.addImportType("SketchUp (*.skp)", "importSKP")
FreeCAD.addExportType("SketchUp (*.skp)", "importSKP")
