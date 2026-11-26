# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-26

- fix: fold the instance matrix's scale term into placements (#19)
- feat: import SketchUp's component hierarchy (#20)
- feat: option to make closed components solid (#23)
- feat: option to build composite components as Assemblies (#24)
- feat: true circles, arcs, cylinders and cones on import (#18, parts 1 and 2) (#25)
- feat: fit spherical surfaces as Part.Sphere on import (#18, part 3) (#27)
- feat(import): warn when a classic .skp file was read only in part (#33)
- perf(import): measure large-model hierarchy imports and warn at 1,000+ definitions (#34)
- chore: align package.xml and layout with FreeCAD Addon Index rules (#35)
- feat(export): preserve SketchUp component hierarchy on export (#36)
- feat(import): import large models in the merged layout automatically (#39)
- feat(import): import SketchUp texts and dimensions as Draft annotations (#40)
- feat(materials): import and export textured materials via selective FeaturePython and Coin3D (#30) (#43)
- chore: vendor openskp 1.7.0, enabling 2021+ texts and component dimensions, dimension offset vectors, classic leaderless texts, canonical component GUIDs, and public texture filename export
