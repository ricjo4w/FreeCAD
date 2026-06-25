# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import Part


doc = FreeCAD.newDocument("PartHeadlessWorkflow")

box = Part.makeBox(12.0, 8.0, 4.0)
cylinder = Part.makeCylinder(2.0, 4.0)
shape = box.fuse((cylinder,))

feature = doc.addObject("Part::Feature", "PartWorkflowShape")
feature.Label = "Part Workflow Shape"
feature.Shape = shape

doc.recompute()

assert feature.Name == "PartWorkflowShape"
assert feature.Label == "Part Workflow Shape"
assert doc.getObject("PartWorkflowShape") is feature
assert feature.Shape.isValid()
assert feature.Shape.Volume > box.Volume
assert len(feature.Shape.Solids) >= 1
