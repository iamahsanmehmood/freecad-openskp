# Import fixtures

`Test_Component.skp` is a small user-provided SketchUp 2017 reproduction of
missing swept-profile surfaces. The parsed model root contains 106 polygons.
Fourteen cannot be constructed as planar FreeCAD faces: thirteen quadrilaterals
and one concave eight-vertex outline have slightly non-coplanar vertices.

The original importer produced 92 faces and skipped 14. Triangulating only the
rejected simple polygons produces 124 valid CAD faces (92 unchanged faces plus
32 triangles), retaining all original boundary coordinates. The result sews
into one closed solid and passes the detailed BOP check. These are verified
by `tests/test_nonplanar.py`, including with optional triangulation dependencies
unavailable. The fixture demonstrates the importer's handling of the parsed
geometry; it does not establish whether non-planarity originated in SketchUp
or in the underlying parser.
