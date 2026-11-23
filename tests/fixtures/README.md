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

`instance_scale_term_2021.skp` and `instance_scale_term_v6.skp` come from the
openskp repository (`packages/python/tests/fixtures/`), where they were built
and saved through SketchUp's own library as SketchUp 2021 and SketchUp 6 files:
one 100 in cube definition placed twice, once scaled through the 3x3 (0.01) and
once through the matrix's scale term (index 12 = 0.01, translation 2000 in;
openskp#444). SketchUp reports both cubes as 1 x 1 x 1 in, at x = 0 and
x = 20 in, which `check_scale_term` in `tests/test_import.py` checks. No
private content.

`plate_with_hole_2017.skp` and `plate_with_hole_2021.skp` were built and saved
through SketchUp's own C library (`SketchUpAPI.dll`, `SUModelSaveToFileWithVersion`)
as SketchUp 2017 and SketchUp 2021 files: a 20 x 20 x 2 in rectangular plate
centred at the origin (`z = 0..2` in) with a radius 4 in, 24-segment circular
through-hole (`SUArcCurve` full circles on the top and bottom perforated faces,
24 soft and smooth vertical bore edges, 30 faces total). Verified by
`tests/test_curved_surfaces.py`. No private content.
