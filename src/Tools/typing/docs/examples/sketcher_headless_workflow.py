# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import Part
import Sketcher


doc = FreeCAD.newDocument("SketcherHeadlessWorkflow")
sketch = doc.addObject("Sketcher::SketchObject", "HeadlessSketch")

line_index = sketch.addGeometry(
    Part.LineSegment(
        FreeCAD.Vector(0.0, 0.0, 0.0),
        FreeCAD.Vector(40.0, 0.0, 0.0),
    ),
    False,
)
circle_index = sketch.addGeometry(
    Part.Circle(
        FreeCAD.Vector(20.0, 15.0, 0.0),
        FreeCAD.Vector(0.0, 0.0, 1.0),
        5.0,
    ),
    False,
)

sketch.addConstraint(Sketcher.Constraint("Horizontal", line_index))
sketch.addConstraint(Sketcher.Constraint("Distance", line_index, 40.0))
sketch.addConstraint(Sketcher.Constraint("Radius", circle_index, 5.0))

doc.recompute()

assert sketch.Name == "HeadlessSketch"
assert doc.getObject("HeadlessSketch") is sketch
assert line_index == 0
assert circle_index == 1
assert len(sketch.Geometry) == 2
assert len(sketch.Constraints) == 3
assert sketch.Constraints[0].Type == "Horizontal"
assert sketch.Constraints[1].Value == 40.0
assert sketch.Constraints[2].Value == 5.0
