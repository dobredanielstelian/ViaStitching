# SPDX-License-Identifier: MIT
# Original work: Copyright (c) weirdgyn — https://github.com/weirdgyn/viastitching
# Modifications: Copyright (c) 2025 Daniel Stelian Dobre

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
