# SPDX-License-Identifier: GPL-3.0-or-later
# Original work: Copyright (C) JS Reynaud — https://github.com/jsreynaud/kicad-action-scripts
# Modifications: Copyright (C) 2026 Daniel Stelian Dobre

import pcbnew
from .ViaStitching import RunViaStitchingInteractive

class ViaStitchingPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Via Stitching Tool"
        self.category = "Modify PCB"
        self.description = "Fill copper areas with stitching vias for a given net"
        self.show_toolbar_button = True

    def Run(self):
        RunViaStitchingInteractive()

ViaStitchingPlugin().register()
