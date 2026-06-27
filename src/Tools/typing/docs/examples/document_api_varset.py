# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD


doc = FreeCAD.newDocument("DocumentApiVarSet")
params = doc.addObject("App::VarSet", "Parameters")
params.Label = "Shared Parameters"
params.addProperty("App::PropertyFloat", "PanelWidth", "Variables", "Panel width")
params.addProperty("App::PropertyFloat", "PanelHeight", "Variables", "Panel height")
params.PanelWidth = 24.0
params.PanelHeight = 12.0

panel = doc.addObject("Part::Box", "DrivenPanel")
panel.setExpression("Length", "Parameters.PanelWidth")
panel.setExpression("Height", "Parameters.PanelHeight")
panel.Width = 2.0

doc.recompute()

assert params.Name == "Parameters"
assert params.Label == "Shared Parameters"
assert params.PanelWidth == 24.0
assert params.PanelHeight == 12.0
assert panel.Name == "DrivenPanel"
assert panel.Length == params.PanelWidth
assert panel.Height == params.PanelHeight
assert dict(panel.ExpressionEngine)["Length"] == "Parameters.PanelWidth"
assert dict(panel.ExpressionEngine)["Height"] == "Parameters.PanelHeight"
