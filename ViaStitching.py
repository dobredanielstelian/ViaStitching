#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# ViaStitching — KiCad 10 action plugin
#
# Original work: Copyright (C) JS Reynaud
#   https://github.com/jsreynaud/kicad-action-scripts
#
# Modifications: Copyright (C) 2026 Daniel Stelian Dobre
#   - KiCad 10 API compatibility (GetFilledPolysList, GetLocalClearance,
#     GetBoardPolygonOutlines, PCB_TEXT class name, plugin registration)
#   - Proper clearance-aware collision detection using SEG.Distance and
#     BOX2I geometry instead of HitTest point checks
#   - Via-vs-via collision check for same-net existing vias
#   - Square, Staggered and Hexagonal placement patterns
#   - Interactive net dropdown populated from the live board
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#

from __future__ import print_function

import sys
import os
import random
import time
import wx

import pcbnew
from pcbnew import *

try:
    xrange
except NameError:
    xrange = range


def wxPrint(msg):
    wx.LogMessage(str(msg))


class ViaStitchingDialog(wx.Dialog):
    def __init__(self, nets=None, board_clearance_mm=0.2):
        super().__init__(None, title="Via Stitching Parameters")

        self._nets = sorted(nets) if nets else []
        main = wx.BoxSizer(wx.VERTICAL)

        def add_row(label, ctrl):
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self, label=label), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            row.Add(ctrl, 1, wx.ALL | wx.EXPAND, 5)
            main.Add(row, 0, wx.EXPAND)

        self.diameter = wx.TextCtrl(self, value="0.46")
        add_row("Via Diameter (mm):", self.diameter)

        self.drill = wx.TextCtrl(self, value="0.20")
        add_row("Drill (mm):", self.drill)

        self.spacing = wx.TextCtrl(self, value="2.54")
        add_row("Spacing (mm):", self.spacing)

        self.clearance = wx.TextCtrl(self, value="{:.3f}".format(board_clearance_mm))
        add_row("Min Clearance (mm):", self.clearance)

        # Net selector: dropdown from board if available, text fallback
        if nets:
            default = "GND" if "GND" in nets else nets[0]
            self.netname = wx.ComboBox(self, value=default, choices=self._nets,
                                       style=wx.CB_DROPDOWN | wx.CB_SORT)
        else:
            self.netname = wx.TextCtrl(self, value="GND")
        add_row("Net Name:", self.netname)

        # Pattern selector
        patterns = ["Square", "Staggered", "Hexagonal"]
        self.pattern = wx.Choice(self, choices=patterns)
        self.pattern.SetSelection(0)
        add_row("Pattern:", self.pattern)

        # Grouping checkbox
        self.group_vias = wx.CheckBox(self, label="Group all vias (enables bulk delete, disables individual delete)")
        self.group_vias.SetValue(False)
        main.Add(self.group_vias, 0, wx.ALL, 8)

        # ── Extra per-net clearance rules ──────────────────────────────────
        box = wx.StaticBox(self, label="Extra clearance rules (nets with stricter spacing)")
        box_sz = wx.StaticBoxSizer(box, wx.VERTICAL)

        # Column header
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        hdr.Add(wx.StaticText(self, label="Net"), 1, wx.LEFT, 4)
        hdr.Add(wx.StaticText(self, label="Min clearance (mm)"), 0, wx.RIGHT, 36)
        box_sz.Add(hdr, 0, wx.EXPAND | wx.TOP, 2)

        self._rules_sizer = wx.BoxSizer(wx.VERTICAL)
        self._rule_rows = []          # list of (row_sizer, net_ctrl, clr_ctrl, del_btn)
        box_sz.Add(self._rules_sizer, 0, wx.EXPAND)

        add_btn = wx.Button(self, label="+ Add rule")
        add_btn.Bind(wx.EVT_BUTTON, lambda e: self._add_rule_row())
        box_sz.Add(add_btn, 0, wx.ALL, 4)

        main.Add(box_sz, 0, wx.EXPAND | wx.ALL, 5)
        # ──────────────────────────────────────────────────────────────────

        btns = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        main.Add(btns, 0, wx.ALL | wx.ALIGN_CENTER, 10)

        self.SetSizer(main)
        self.Fit()
        self.SetMinSize((440, 320))
        self.Centre()

    # ── Extra-rule row helpers ─────────────────────────────────────────────

    def _add_rule_row(self, net="", clearance_mm="0.200"):
        """Append one (net, clearance) row to the extra-rules section."""
        row_sz = wx.BoxSizer(wx.HORIZONTAL)

        net_ctrl = wx.ComboBox(self, value=net, choices=self._nets,
                               style=wx.CB_DROPDOWN | wx.CB_SORT, size=(170, -1))
        clr_ctrl = wx.TextCtrl(self, value=str(clearance_mm), size=(75, -1))
        del_btn  = wx.Button(self, label=u"✕", size=(28, -1))   # ✕

        row_sz.Add(net_ctrl, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 2)
        row_sz.Add(clr_ctrl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 2)
        row_sz.Add(del_btn,  0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 2)

        entry = (row_sz, net_ctrl, clr_ctrl, del_btn)
        self._rule_rows.append(entry)
        self._rules_sizer.Add(row_sz, 0, wx.EXPAND)

        del_btn.Bind(wx.EVT_BUTTON,
                     lambda e, ent=entry: self._remove_rule_row(ent))

        self.Layout()
        self.Fit()

    def _remove_rule_row(self, entry):
        """Remove an extra-rule row and refresh layout."""
        row_sz, net_ctrl, clr_ctrl, del_btn = entry
        self._rule_rows = [r for r in self._rule_rows if r is not entry]
        self._rules_sizer.Detach(row_sz)
        net_ctrl.Destroy()
        clr_ctrl.Destroy()
        del_btn.Destroy()
        self.Layout()
        self.Fit()

    def GetExtraRules(self):
        """Return [(net_name, clearance_mm), …] for every filled-in rule row."""
        rules = []
        for _, net_ctrl, clr_ctrl, _ in self._rule_rows:
            net = net_ctrl.GetValue().strip()
            if not net:
                continue
            try:
                rules.append((net, float(clr_ctrl.GetValue())))
            except ValueError:
                pass
        return rules


class ViaObject:
    """
    ViaObject holds all information of a single Via candidate in the grid
    """

    def __init__(self, x, y, pos_x, pos_y):
        self.X = x
        self.Y = y
        self.PosX = pos_x
        self.PosY = pos_y


class FillArea:
    """
    Automatically add vias on areas where there are no tracks/existing vias,
    pads and keepout areas, for a given net.
    """

    REASON_OK = 0
    REASON_NO_SIGNAL = 1
    REASON_OTHER_SIGNAL = 2
    REASON_KEEPOUT = 3
    REASON_TRACK = 4
    REASON_PAD = 5
    REASON_DRAWING = 6
    REASON_STEP = 7

    GRID_TYPE_BOARD_BOUNDS = "Board Bounds"
    GRID_TYPE_ABSOLUTE = "Absolute (0, 0)"
    GRID_TYPE_GRID_ORIGIN = "Grid Origin"

    FILL_TYPE_RECTANGULAR = "Rectangular"
    FILL_TYPE_STAR = "Star"
    FILL_TYPE_CONCENTRIC = "Concentric"
    FILL_TYPE_OUTLINE = "Outline"
    FILL_TYPE_OUTLINE_NO_HOLES = "Outline (No Holes)"

    PATTERN_SQUARE = "Square"
    PATTERN_STAGGERED = "Staggered"
    PATTERN_HEXAGONAL = "Hexagonal"

    def __init__(self, filename=None):
        self.filename = None
        self.clearance = 0
        self.pcb = None
        self.netname = None
        self.debug = False
        self.random = False
        self.grid_type = self.GRID_TYPE_BOARD_BOUNDS
        self.fill_type = self.FILL_TYPE_RECTANGULAR
        self.pattern = self.PATTERN_SQUARE
        self.only_selected_area = False
        self.delete_vias = False
        self.via_through_areas = False
        self.same_net_tracks = False
        self.group_vias = False
        self.extra_clearances = {}
        self.tmp_dir = None
        self.parent_area = None
        self.pcb_group = None
        self.target_net = None

        # Always use the live board when running inside KiCad
        self.SetPCB(GetBoard())
        self.SetFile(filename)

        self.SetStepMM(2.54)
        self.SetSizeMM(0.46)
        self.SetDrillMM(0.20)
        self.SetClearanceMM(0.2)

        if self.pcb is not None:
            for lnet in ["GND", "/GND"]:
                if self.pcb.FindNet(lnet) is not None:
                    self.SetNetname(lnet)
                    break

        if self.netname is None:
            self.SetNetname("GND")

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    def SetFile(self, filename):
        # For CLI usage only; in KiCad interactive mode we use GetBoard()
        self.filename = filename
        if self.filename and self.pcb is None:
            self.SetPCB(LoadBoard(self.filename))
        return self

    def SetDebug(self):
        wxPrint("Set debug")
        self.debug = True
        return self

    def SetRandom(self, r):
        random.seed()
        self.random = r
        return self

    def SetViaThroughAreas(self, r):
        self.via_through_areas = r
        return self

    def SetSameNetTracks(self, r):
        self.same_net_tracks = r
        return self

    def SetGridType(self, grid_type):
        self.grid_type = grid_type
        return self

    def GetGridOrig(self, lboard):
        if self.grid_type == self.GRID_TYPE_ABSOLUTE:
            return VECTOR2I(0, 0)
        elif self.grid_type == self.GRID_TYPE_GRID_ORIGIN:
            return self.pcb.GetDesignSettings().GetGridOrigin()
        else:
            return lboard.GetPosition()

    def SetFillType(self, fill_type):
        self.fill_type = fill_type
        return self

    def SetPattern(self, pattern):
        self.pattern = pattern
        return self

    def SetPCB(self, pcb):
        self.pcb = pcb
        if self.pcb is not None:
            self.pcb.BuildListOfNets()
        return self

    def SetNetname(self, netname):
        self.netname = netname
        return self

    def SetStepMM(self, s):
        self.step = float(FromMM(s))
        return self

    def SetSizeMM(self, s):
        self.size = float(FromMM(s))
        return self

    def SetDrillMM(self, s):
        self.drill = float(FromMM(s))
        return self

    def OnlyOnSelectedArea(self):
        self.only_selected_area = True
        return self

    def DeleteVias(self):
        self.delete_vias = True
        return self

    def SetGroupVias(self, v):
        self.group_vias = bool(v)
        return self

    def SetClearanceMM(self, s):
        self.clearance = float(FromMM(s))
        return self

    def _resolve_clearance(self):
        """Bump self.clearance up to the board-level minimum from design rules.

        Called at the start of Run() and ConcentricFillVias() so that every
        downstream check automatically uses the stricter of (a) what the user
        asked for and (b) what the board rules require.
        """
        if self.pcb is None:
            return
        try:
            board_min = self.pcb.GetDesignSettings().m_MinClearance
            if board_min > self.clearance:
                wxPrint(
                    "ViaStitching: clearance raised to board minimum {:.3f} mm".format(
                        ToMM(board_min)
                    )
                )
                self.clearance = float(board_min)
        except Exception:
            pass

    def SetExtraNetClearances(self, rules):
        """Set per-net clearance overrides from the dialog's extra-rules table.

        rules — list of (net_name: str, clearance_mm: float) tuples entered by
                the user.  Any net not listed falls back to self.clearance (the
                board minimum).  Values below the board minimum are ignored.
        """
        self.extra_clearances = {
            name: int(FromMM(mm))
            for name, mm in rules
            if name
        }
        return self

    def _build_net_clearance_map(self):
        """Return {net_name: effective_clearance_iu} from user-specified extra rules.

        Only the nets the user explicitly added appear in the map; every other net
        gets looked up with .get(name, min_gap) which falls back to the board minimum.
        The value stored is max(board_minimum, user_value) so the map can never make
        a net *less* strict than the board rules require.
        """
        base = int(self.clearance)
        return {
            name: max(base, iu)
            for name, iu in self.extra_clearances.items()
        }

    # -------------------------------------------------------------------------
    # Debug helpers
    # -------------------------------------------------------------------------

    def GetReasonSymbol(self, reason):
        if isinstance(reason, ViaObject):
            return "X"
        if reason == self.REASON_NO_SIGNAL:
            return " "
        if reason == self.REASON_OTHER_SIGNAL:
            return "O"
        if reason == self.REASON_KEEPOUT:
            return "K"
        if reason == self.REASON_TRACK:
            return "T"
        if reason == self.REASON_PAD:
            return "P"
        if reason == self.REASON_DRAWING:
            return "D"
        if reason == self.REASON_STEP:
            return "-"

        return str(reason)

    def PrintRect(self, rectangle):
        print("_" * (len(rectangle) + 2))
        for y in range(len(rectangle[0])):
            print("|", end="")
            for x in range(len(rectangle)):
                print("%s" % self.GetReasonSymbol(rectangle[x][y]), end="")
            print("|")
        print("_" * (len(rectangle) + 2))
        print(
            """
OK           = 'X'
NO_SIGNAL    = ' '
OTHER_SIGNAL = 'O'
KEEPOUT      = 'K'
TRACK        = 'T'
PAD          = 'P'
DRAWING      = 'D'
STEP         = '-'
"""
        )

    # -------------------------------------------------------------------------
    # Via creation / board refill
    # -------------------------------------------------------------------------

    def AddVia(self, position, x, y):
        if self.parent_area:
            m = PCB_VIA(self.pcb)
            m.SetPosition(position)

            if self.target_net is None:
                self.target_net = self.pcb.FindNet(self.netname)

            m.SetNet(self.target_net)
            m.SetViaType(VIATYPE_THROUGH)
            m.SetDrill(int(self.drill))
            m.SetWidth(int(self.size))
            m.SetIsFree(True)

            self.pcb.Add(m)
            if self.pcb_group is not None:
                self.pcb_group.AddItem(m)
            return m
        else:
            wxPrint("Unable to find a valid parent area (zone)")

    def RefillBoardAreas(self):
        for i in range(self.pcb.GetAreaCount()):
            area = self.pcb.GetArea(i)
            area.SetNeedRefill(True)

    # -------------------------------------------------------------------------
    # Collision checks
    # -------------------------------------------------------------------------

    def CheckViaInAllAreas(self, via, all_areas):
        """
        Returns a REASON_* code if placing a via at (via.PosX, via.PosY) would
        violate zone rules.  Uses a circle-vs-polygon collision check so that
        the full via footprint (radius + clearance) is tested against every
        zone boundary — not just four diagonal probe points.
        """
        p = VECTOR2I(int(via.PosX), int(via.PosY))

        for area in all_areas:
            area_layer = area.GetLayer()
            area_clearance = area.GetLocalClearance() or 0
            area_priority = area.GetAssignedPriority()
            is_rules_area = area.GetIsRuleArea()
            is_rule_exclude_via_area = area.GetIsRuleArea() and area.GetDoNotAllowVias()
            is_target_net = (area.GetNetname() == self.netname)

            if is_target_net and not is_rule_exclude_via_area:
                continue  # same net, not a keepout — no conflict

            # How close can the via centre get to this zone's boundary?
            # Use the stricter of: board minimum, zone local override, and the
            # zone net's net-class clearance (e.g. an 'Earth' net with 1 mm class).
            nc_clearance = 0
            if hasattr(self, '_net_clearance_map'):
                nc_clearance = self._net_clearance_map.get(area.GetNetname(), 0)
            required_gap = int(max(self.clearance, area_clearance, nc_clearance) + self.size / 2)

            outline = area.Outline()
            if outline is None:
                continue

            # Check the via circle (centre + required_gap as clearance) against
            # the zone outline using KiCad's own Collide machinery.
            # Collide(VECTOR2I, clearance) returns True when the point is within
            # clearance of the polygon boundary or inside it.
            zone_hit = outline.Collide(p, required_gap)
            # Also check if the centre is strictly inside the outline
            inside = False
            for i in range(outline.OutlineCount()):
                if outline.Outline(i).PointInside(p):
                    inside = True
                    break

            if not (zone_hit or inside):
                continue  # via is safely outside this zone

            if is_rule_exclude_via_area:
                return self.REASON_KEEPOUT

            if not self.via_through_areas and not is_rules_area:
                # Allow if a higher-priority same-net zone covers this point
                target_areas_on_same_layer = [
                    a for a in all_areas
                    if (a.GetAssignedPriority() > area_priority
                        and a.GetLayer() == area_layer
                        and a.GetNetname() == self.netname)
                ]
                if any(a.HitTest(p) for a in target_areas_on_same_layer):
                    continue
                return self.REASON_OTHER_SIGNAL

        return self.REASON_OK

    def ClearViaInStepSize(self, rectangle, x, y, distance):
        for x_pos in range(x - distance, x + distance + 1):
            if (x_pos >= 0) and (x_pos < len(rectangle)):
                distance_y = distance - abs(x - x_pos) if self.fill_type == self.FILL_TYPE_STAR else distance
                for y_pos in range(y - distance_y, y + distance_y + 1):
                    if (y_pos >= 0) and (y_pos < len(rectangle[0])):
                        if (x_pos == x) and (y_pos == y):
                            continue
                        rectangle[x_pos][y_pos] = self.REASON_STEP

    # -------------------------------------------------------------------------
    # Distance / outline helpers
    # -------------------------------------------------------------------------

    def CheckViaDistance(self, p, via, outline):
        p2 = VECTOR2I(via.GetPosition())
        dist = self.clearance + self.size / 2 + via.GetWidth() / 2

        if outline.Collide(p2):
            dist = int(max(dist, self.step * 0.6))

        return (p - p2).EuclideanNorm() >= dist

    def AddViasAlongOutline(self, outline, outline_parent, all_vias, offset=0):
        via_placed = 0
        step = max(self.step, self.size + self.clearance)
        length = int(outline.Length())
        steps = length // step
        steps = 1 if steps == 0 else steps
        stepsize = int(length // steps)

        for l in range(int(stepsize * offset), length, stepsize):
            p = outline.PointAlong(l)
            if all(self.CheckViaDistance(p, via, outline_parent) for via in all_vias):
                via = self.AddVia(p, 0, 0)
                all_vias.append(via)
                via_placed += 1

        return via_placed

    # -------------------------------------------------------------------------
    # Concentric / outline fill
    # -------------------------------------------------------------------------

    def ConcentricFillVias(self):
        self._resolve_clearance()
        self._net_clearance_map = self._build_net_clearance_map()
        wxPrint("Calculate placement areas")

        zones = [zone for zone in self.pcb.Zones() if zone.GetNetname() == self.netname]
        if not zones:
            wxPrint("No areas to fill")
            return

        self.parent_area = zones[0]

        existing_vias = [
            track for track in self.pcb.GetTracks()
            if (track.GetClass() == "PCB_VIA" and track.GetNetname() == self.netname)
        ]
        all_new_vias = []

        wxPrint("Generating via placement")
        off = 0
        via_placed = 0
        processed_any = False

        for zone in zones:
            if self.only_selected_area and not zone.IsSelected():
                continue

            filled = zone.GetFilledPolysList(zone.GetFirstLayer())
            if filled is None or filled.OutlineCount() == 0:
                if self.debug:
                    wxPrint("  Zone layer={} -> Skipped (empty fill)".format(zone.GetLayerName()))
                continue
            zone_poly = filled.CloneDropTriangulation()

            if self.debug:
                wxPrint(
                    "  Zone layer={} outline_count={}".format(
                        zone.GetLayerName(), zone_poly.OutlineCount()
                    )
                )

            processed_any = True

            inflate_amount = int(-(1 * self.clearance + 0.5 * self.size))
            zone_poly.Inflate(inflate_amount, CORNER_STRATEGY_ALLOW_ACUTE_CORNERS, FromMM(0.01))

            if self.debug:
                wxPrint("  -> After inflate: outline_count={}".format(zone_poly.OutlineCount()))

            if zone_poly.OutlineCount() == 0:
                if self.debug:
                    wxPrint("  -> Skipped (empty after inflate)")
                continue

            zone_vias = existing_vias + all_new_vias

            current_poly = zone_poly
            while current_poly.OutlineCount() > 0:
                for i in range(current_poly.OutlineCount()):
                    outline = current_poly.Outline(i)
                    n = self.AddViasAlongOutline(outline, outline, zone_vias, off)
                    via_placed += n

                    if self.fill_type != self.FILL_TYPE_OUTLINE_NO_HOLES:
                        for k in range(current_poly.HoleCount(i)):
                            hole = current_poly.Hole(i, k)
                            n = self.AddViasAlongOutline(hole, outline, zone_vias, off)
                            via_placed += n

                if self.fill_type == self.FILL_TYPE_CONCENTRIC:
                    current_poly.Inflate(
                        int(-max(self.step, self.size + self.clearance)),
                        CORNER_STRATEGY_CHAMFER_ALL_CORNERS,
                        FromMM(0.01),
                    )
                    off = 0.5 if off == 0 else 0
                else:
                    current_poly = SHAPE_POLY_SET()

            all_new_vias = zone_vias[len(existing_vias):]

        if not processed_any:
            wxPrint("No areas to fill")
            return

        self.RefillBoardAreas()
        msg = "Done. {:d} vias placed. You have to refill all your pcb's areas/zones !!!".format(via_placed)
        wxPrint(msg)
        pcbnew.Refresh()
        return via_placed

    # -------------------------------------------------------------------------
    # Main rectangular/star fill
    # -------------------------------------------------------------------------

    def Run(self):
        # Must be first: bumps self.clearance to board-rule minimum so every
        # downstream check automatically respects DRC.
        self._resolve_clearance()
        # Build per-net clearance map (net class clearances).  Stored on self so
        # CheckViaInAllAreas can use it without an extra argument.
        self._net_clearance_map = self._build_net_clearance_map()

        VIA_GROUP_NAME = "ViaStitching {}".format(self.netname)

        # Reset group handle each run; rebuilt below only when grouping is on.
        self.pcb_group = None

        if self.group_vias:
            if self.debug:
                print("Enumerate groups")
            for g in self.pcb.Groups():
                if g.GetName() == VIA_GROUP_NAME:
                    if self.debug:
                        print("Group {} Found !".format(VIA_GROUP_NAME))
                    self.pcb_group = g

            if self.delete_vias:
                wx.MessageBox(
                    "To delete vias:\n"
                    " - select one of the generated vias to select the group of vias named {}\n"
                    " - hit delete key\n"
                    " - That's all !".format(VIA_GROUP_NAME),
                    "Information",
                )
                return

            if self.pcb_group is None:
                self.pcb_group = PCB_GROUP(self.pcb)
                self.pcb_group.SetName(VIA_GROUP_NAME)
                self.pcb.Add(self.pcb_group)
        elif self.delete_vias:
            wx.MessageBox(
                "Grouping is disabled — vias are placed individually.\n"
                "Select and delete unwanted vias directly in the PCB editor.",
                "Information",
            )
            return

        if self.fill_type in (
            self.FILL_TYPE_CONCENTRIC,
            self.FILL_TYPE_OUTLINE,
            self.FILL_TYPE_OUTLINE_NO_HOLES,
        ):
            self.ConcentricFillVias()
            if self.filename:
                self.pcb.Save(self.filename)
            pcbnew.Refresh()
            return

        target_tracks = self.pcb.GetTracks()

        lboard = self.pcb.ComputeBoundingBox(False)
        origin = self.GetGridOrig(lboard)

        l_clearance = self.clearance + self.size
        if l_clearance < self.step:
            l_clearance = self.step

        board_min_x = lboard.GetPosition().x
        board_min_y = lboard.GetPosition().y
        board_max_x = board_min_x + lboard.GetWidth()
        board_max_y = board_min_y + lboard.GetHeight()

        from math import floor, ceil, sqrt

        # Y step depends on pattern:
        #   Square / Staggered → same spacing in X and Y
        #   Hexagonal          → rows are sqrt(3)/2 ≈ 0.866× closer, giving the
        #                        densest possible packing (true hex close-pack)
        if self.pattern == self.PATTERN_HEXAGONAL:
            y_step = max(1, int(l_clearance * sqrt(3) / 2))
        else:
            y_step = l_clearance

        x_min = int(floor((board_min_x - origin.x - l_clearance) / l_clearance))
        x_max = int(ceil((board_max_x - origin.x + l_clearance) / l_clearance))
        y_min = int(floor((board_min_y - origin.y - y_step) / y_step))
        y_max = int(ceil((board_max_y - origin.y + y_step) / y_step))

        x_limit = x_max - x_min + 1
        y_limit = y_max - y_min + 1

        if self.debug:
            print(
                "l_clearance : {}; step : {}; size: {}; clearance: {}; "
                "x/y_limit ({} {}), board size : {} {}".format(
                    l_clearance,
                    self.step,
                    self.size,
                    self.clearance,
                    x_limit,
                    y_limit,
                    lboard.GetWidth(),
                    lboard.GetHeight(),
                )
            )

        rectangle = [[self.REASON_NO_SIGNAL] * y_limit for _ in xrange(x_limit)]

        if self.debug:
            print("\nInitial rectangle:")
            self.PrintRect(rectangle)

        all_pads = self.pcb.GetPads()
        all_tracks = self.pcb.GetTracks()
        all_drawings = list(filter(
            lambda x: x.GetClass() == "PCB_TEXT"
            and self.pcb.GetLayerID(x.GetLayerName()) in (F_Cu, B_Cu),
            self.pcb.Drawings(),
        ))

        all_areas = [self.pcb.GetArea(i) for i in xrange(self.pcb.GetAreaCount())]
        target_areas = filter(lambda x: (x.GetNetname() == self.netname), all_areas)

        board_edge = SHAPE_POLY_SET()
        self.pcb.GetBoardPolygonOutlines(board_edge, True)
        b_clearance = max(self.pcb.GetDesignSettings().m_CopperEdgeClearance, self.clearance) + self.size
        board_edge.Deflate(int(b_clearance), CORNER_STRATEGY_ROUND_ALL_CORNERS, FromMM(0.01))

        via_list = []
        max_target_area_clearance = 0

        for area in target_areas:
            wxPrint("Processing Target Area: %s, LayerName: %s..." % (area.GetNetname(), area.GetLayerName()))
            if self.parent_area is None:
                self.parent_area = area

            is_selected_area = area.IsSelected()
            area_clearance = area.GetLocalClearance() or 0
            if max_target_area_clearance < area_clearance:
                max_target_area_clearance = area_clearance

            if (not self.only_selected_area) or (self.only_selected_area and is_selected_area):
                for x in xrange(len(rectangle)):
                    for y in xrange(len(rectangle[0])):
                        if rectangle[x][y] == self.REASON_NO_SIGNAL:
                            row = y + y_min
                            stagger = (l_clearance // 2) if (
                                self.pattern in (self.PATTERN_STAGGERED, self.PATTERN_HEXAGONAL)
                                and row % 2 == 1
                            ) else 0
                            current_x = origin.x + ((x + x_min) * l_clearance) + stagger
                            current_y = origin.y + (row * y_step)

                            point_to_test = VECTOR2I(int(current_x), int(current_y))

                            hit_area = False
                            outline = area.Outline()
                            if outline is not None:
                                for i in range(outline.OutlineCount()):
                                    chain = outline.Outline(i)
                                    if chain.PointInside(point_to_test):
                                        hit_area = True
                                        break

                            hit_edge = area.HitTest(point_to_test)
                            test_result = hit_area and not hit_edge
                            test_result = test_result and board_edge.Collide(point_to_test)

                            if test_result:
                                via_obj = ViaObject(
                                    x=x,
                                    y=y,
                                    pos_x=current_x,
                                    pos_y=current_y,
                                )
                                rectangle[x][y] = via_obj
                                via_list.append(via_obj)

        if self.debug:
            print("\nPost target areas:")
            self.PrintRect(rectangle)

        wxPrint("Processing all vias of target area...")
        for via in via_list:
            reason = self.CheckViaInAllAreas(via, all_areas)
            if reason != self.REASON_OK:
                rectangle[via.X][via.Y] = reason

        if self.debug:
            print("\nPost areas:")
            self.PrintRect(rectangle)

        # Build collision obstacles with proper geometry (via radius + clearance)
        wxPrint("Building collision obstacles...")
        via_r = int(self.size / 2)
        # self.clearance has already been resolved to max(user value, board minimum)
        # by _resolve_clearance() at the top of Run().
        min_gap = int(self.clearance)

        # Tracks and existing vias: use real segment-distance check.
        # Rules:
        #   same-net VIA   → enforce clearance so new vias don't crowd existing ones
        #   same-net TRACK → skip (no DRC violation; copper is same net)
        #   diff-net item  → enforce full clearance, using the stricter of board
        #                    minimum and the track's own net-class clearance
        track_obstacles = []
        for track in all_tracks:
            is_via = track.GetClass() == "PCB_VIA"
            same_net = track.GetNetname() == self.netname

            if same_net and not is_via:
                # Same-net trace: skip unless the user asked to treat them as obstacles
                if not self.same_net_tracks:
                    continue

            t_r = int(track.GetWidth() / 2)

            if same_net:
                if is_via:
                    # Same-net via: keep clearance so new vias don't stack on top
                    # of existing ones.  (DRC doesn't require this, but without it
                    # vias are placed with zero air-gap between copper rings.)
                    min_dist = via_r + t_r + min_gap
                else:
                    # Same-net trace treated as obstacle: physical overlap only
                    min_dist = via_r + t_r
            else:
                # Different net: use the pre-built per-net clearance (already
                # accounts for net-class rules like a 1 mm Earth clearance).
                track_gap = self._net_clearance_map.get(track.GetNetname(), min_gap)
                min_dist = via_r + t_r + track_gap

            seg = SEG(track.GetStart(), track.GetEnd())
            bbox = track.GetBoundingBox().GetInflated(int(min_dist))
            track_obstacles.append((seg, min_dist, bbox))

        # Hole-to-hole clearance from design settings (separate DRC rule from copper clearance).
        # Try the KiCad attribute name; fall back to copper clearance if not found.
        via_drill_r = int(self.drill / 2)
        try:
            hole_clearance = max(min_gap, self.pcb.GetDesignSettings().m_MinHoleSeparation)
        except AttributeError:
            try:
                hole_clearance = max(min_gap, self.pcb.GetDesignSettings().m_HoleClearance)
            except AttributeError:
                hole_clearance = min_gap

        # Pads: two independent checks per pad candidate position.
        #
        # 1. Copper-overlap check (rectangular bbox):
        #    - same-net pad  → margin = via_r  (via copper must not overlap pad copper;
        #      previously margin=0 allowed the via ring to overlap since only the center
        #      had to be inside the raw bbox)
        #    - diff-net pad  → margin = via_r + min_gap
        #
        # 2. Drill-to-drill check (circular, for TH/NPTH pads):
        #    Even when the copper check passes, the via drill can be too close to the
        #    pad drill hole — a distinct DRC rule (hole-to-hole separation).
        #    min_dist = pad_drill_r + via_drill_r + hole_clearance
        pad_obstacles = []
        for pad in all_pads:
            same = pad.GetNetname() == self.netname
            if same:
                margin = via_r
            else:
                pad_gap = self._net_clearance_map.get(pad.GetNetname(), min_gap)
                margin = via_r + pad_gap
            copper_bbox = pad.GetBoundingBox().GetInflated(int(margin))

            # Drill check: only TH / NPTH pads have a drill size
            drill_min_dist = 0
            try:
                d = pad.GetDrillSize()
                if d.x > 0:
                    pad_drill_r = int(max(d.x, d.y) / 2)
                    drill_min_dist = pad_drill_r + via_drill_r + hole_clearance
            except Exception:
                pass

            pad_obstacles.append((copper_bbox, drill_min_dist, VECTOR2I(pad.GetPosition())))

        # Copper-text drawings — treat like a track clearance obstacle
        drawing_obstacles = [
            d.GetBoundingBox().GetInflated(via_r + min_gap)
            for d in all_drawings
        ]

        # Check every remaining candidate position
        wxPrint("Checking tracks, pads, drawings...")
        for x in xrange(len(rectangle)):
            for y in xrange(len(rectangle[0])):
                cell = rectangle[x][y]
                if not isinstance(cell, ViaObject):
                    continue

                p = VECTOR2I(int(cell.PosX), int(cell.PosY))

                # Tracks: bbox pre-filter then precise segment distance
                for seg, min_dist, bbox in track_obstacles:
                    if bbox.Contains(p) and seg.Distance(p) < min_dist:
                        rectangle[x][y] = self.REASON_TRACK
                        break
                if rectangle[x][y] != cell:
                    continue

                # Pads: copper-overlap bbox + drill-to-drill distance
                for copper_bbox, drill_min_dist, pad_pos in pad_obstacles:
                    if copper_bbox.Contains(p):
                        rectangle[x][y] = self.REASON_PAD
                        break
                    if drill_min_dist > 0 and (p - pad_pos).EuclideanNorm() < drill_min_dist:
                        rectangle[x][y] = self.REASON_PAD
                        break
                if rectangle[x][y] != cell:
                    continue

                # Copper-text drawings
                for bbox in drawing_obstacles:
                    if bbox.Contains(p):
                        rectangle[x][y] = self.REASON_DRAWING
                        break

        if self.debug:
            print("\nPost tracks/pads/drawings:")
            self.PrintRect(rectangle)

        # Apply step / star pattern spacing
        wxPrint("Applying step/star spacing...")
        distance = int(self.step / l_clearance)
        for x in xrange(len(rectangle)):
            for y in xrange(len(rectangle[0])):
                if isinstance(rectangle[x][y], ViaObject):
                    self.ClearViaInStepSize(rectangle, x, y, distance)

        if self.debug:
            print("\nPost step spacing:")
            self.PrintRect(rectangle)

        # Finally place vias
        wxPrint("Placing vias...")
        placed = 0
        for x in xrange(len(rectangle)):
            for y in xrange(len(rectangle[0])):
                cell = rectangle[x][y]
                if isinstance(cell, ViaObject):
                    pos = VECTOR2I(int(cell.PosX), int(cell.PosY))
                    self.AddVia(pos, x, y)
                    placed += 1

        self.RefillBoardAreas()
        wxPrint("Done. {} vias placed. You have to refill all your pcb's areas/zones !!!".format(placed))

        pcbnew.Refresh()

        if self.filename:
            self.pcb.Save(self.filename)


# -------------------------------------------------------------------------
# Interactive wrapper for KiCad
# -------------------------------------------------------------------------

def RunViaStitchingInteractive():
    board_clearance_mm = 0.2
    try:
        board = pcbnew.GetBoard()
        nets = sorted([n for n in board.GetNetsByName().keys() if n])
        board_min = board.GetDesignSettings().m_MinClearance
        if board_min > 0:
            board_clearance_mm = pcbnew.ToMM(board_min)
    except Exception:
        nets = []

    dlg = ViaStitchingDialog(nets, board_clearance_mm=board_clearance_mm)
    if dlg.ShowModal() != wx.ID_OK:
        dlg.Destroy()
        return

    try:
        via_size = float(dlg.diameter.GetValue())
        via_drill = float(dlg.drill.GetValue())
        spacing = float(dlg.spacing.GetValue())
        netname = dlg.netname.GetValue().strip()
        clearance = float(dlg.clearance.GetValue())
    except ValueError as e:
        wx.MessageBox("Invalid input: {}".format(e), "Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()
        return

    pattern_map = {0: FillArea.PATTERN_SQUARE,
                   1: FillArea.PATTERN_STAGGERED,
                   2: FillArea.PATTERN_HEXAGONAL}
    pattern     = pattern_map.get(dlg.pattern.GetSelection(), FillArea.PATTERN_SQUARE)
    group       = dlg.group_vias.GetValue()
    extra_rules = dlg.GetExtraRules()
    dlg.Destroy()

    fa = FillArea()
    fa.SetNetname(netname)
    fa.SetSizeMM(via_size)
    fa.SetDrillMM(via_drill)
    fa.SetStepMM(spacing)
    fa.SetClearanceMM(clearance)
    fa.SetPattern(pattern)
    fa.SetGroupVias(group)
    fa.SetExtraNetClearances(extra_rules)

    fa.Run()


# -------------------------------------------------------------------------
# CLI / direct usage
# -------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        fname = sys.argv[1]
    else:
        fname = None

    fa = FillArea(fname)
    fa.Run()
