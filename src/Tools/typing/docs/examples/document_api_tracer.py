# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD


doc = FreeCAD.newDocument("DocumentApiTracer")
box = doc.addObject("Part::Box", "TracerBox")
box.Label = "Tracer Box"
box.Length = 10.0

doc.recompute()

assert box.Name == "TracerBox"
assert box.Label == "Tracer Box"
assert box.Length == 10.0
assert doc.getObject("TracerBox") is box
