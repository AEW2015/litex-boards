#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2020-2022 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# Build/Use:
# python3 -m litex_boards.targets.sqrl_fk33 --with-hbm --sys-clk-freq=250e6 --csr-csv=csr.csv --build --load
# litex_server --jtag --jtag-config=openocd_xc7_ft2232.cfg --jtag-chain=2
# litex_term crossover

import os
import math
from pathlib import Path

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex_boards.platforms import sqrl_fk33
from litex.soc.cores.clock import *
from litex.soc.cores.ram.xilinx_usp_hbm2 import (
    USPHBM2,
    USPHBM2_DEFAULT_BASE,
    USPHBM2_HIGH_BASE,
    add_usphbm2_pseudochannels,
    parse_usphbm2_channels,
    usphbm2_channel_origins,
    usphbm2_window_end,
)
from litex.soc.cores.ram.xilinx_usp_hbm2_runtime import (
    HBMClockMailbox,
    HBMRuntimeQuiesce,
    WishboneAdmissionGate,
)
from litex.soc.integration.soc import *
from litex.soc.integration.builder import *
from litex.soc.integration.export import MockCSR
from litex.soc.interconnect import axi, csr_bus, stream, wishbone
from litex.soc.interconnect.csr import CSR, CSRStatus, CSRStorage
from litex.soc.cores.led import LedChaser

from litepcie.phy.usppciephy import USPHBMPCIEPHY
from litepcie.software import generate_litepcie_software

# HBM XCI ------------------------------------------------------------------------------------------

def ensure_hbm_xci(url, hbm_xci=os.path.join("ip", "hbm", "hbm_0.xci")):
    if not os.path.exists(hbm_xci):
        os.makedirs(os.path.dirname(hbm_xci), exist_ok=True)
        os.system(f"wget -O {hbm_xci} {url}")

# CRG ----------------------------------------------------------------------------------------------

class _HBMResetRequest(LiteXModule):
    """Bound one SYS request before the MMCM's independent-input reset delay."""
    def __init__(self, request, sys_clk_freq):
        if not math.isfinite(sys_clk_freq) or sys_clk_freq <= 0:
            raise ValueError("Reset request requires a finite positive SYS frequency")
        self.history_cycles = math.ceil(sys_clk_freq / 100e6) + 1
        self.maximum_apb_pulse_cycles = math.ceil(self.history_cycles * 100e6 / sys_clk_freq)
        if self.maximum_apb_pulse_cycles > 3:
            raise ValueError("SYS reset pulse cannot clear before the eight CLK200 delay stages")
        self.seen = Signal()
        self.history = Signal(self.history_cycles)
        self.output = Signal()
        # A held/repeated request cannot reload the history before SYS reset.
        # More than one APB period covers capture at every phase. Bound the
        # APB pulse to30ns, below the eight CLK200 stages' earliest35ns stop.
        self.sync += [
            self.history.eq(Cat(request & ~self.seen, self.history[:self.history_cycles - 1])),
            If(request, self.seen.eq(1)),
        ]
        self.sync.apb += self.output.eq(self.history != 0)


class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_hbm, hbm_ref_clk_freq=100e6,
            strict_timing=False, hbm_axi_domain=False, hbm_axi_clk_freq=250e6,
            hbm_axi_reset_buf=False):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()
        if with_hbm:
            self.cd_hbm_ref = ClockDomain()
            self.cd_apb     = ClockDomain()
            if hbm_axi_domain:
                self.cd_axi = ClockDomain()

        # # #

        self.pll = pll = USPMMCM(speedgrade=-2)
        if strict_timing:
            self.reset_request = _HBMResetRequest(self.rst, sys_clk_freq)
            self.comb += pll.reset.eq(self.reset_request.output)
        else:
            self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(platform.request("clk200"), 200e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        if not strict_timing:
            platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin) # Ignore sys_clk to pll.clkin path created by SoC's rst.

        if with_hbm:
            pll.create_clkout(self.cd_hbm_ref, hbm_ref_clk_freq)
            pll.create_clkout(self.cd_apb,     100e6)
            if hbm_axi_domain:
                # Preserve SYS/ref/APB CLKOUT0/1/2 assignments. This output is
                # independently reset-synchronized by USPMMCM.create_clkout.
                # Buffer the experimental high-fanout reset after its existing
                # two release stages, without adding a release cycle.
                pll.create_clkout(self.cd_axi, hbm_axi_clk_freq,
                    reset_buf="bufg" if hbm_axi_reset_buf else None)

def _add_hbm_reset_constraints(platform):
    """Preserve buffered reset distribution and time native reset release."""
    prepare = r"""
proc fk33_reset_one {objects label} {
    if {[llength $objects] != 1} {error "$label requires one object: $objects"}
    return [lindex $objects 0]
}
proc fk33_reset_pin {cell name} {
    return [fk33_reset_one [get_pins -of_objects $cell -filter "REF_PIN_NAME == $name"] "$cell/$name"]
}
proc fk33_reset_driver {pin} {
    return [fk33_reset_one [get_pins -leaf -of_objects [get_nets -segments -of_objects $pin] -filter {DIRECTION == OUT}] "Driver of $pin"]
}
proc fk33_reset_constant {pin reference} {
    set driver [fk33_reset_driver $pin]
    set cell [fk33_reset_one [get_cells -of_objects $driver] "Constant driver"]
    if {[get_property REF_NAME $cell] ne $reference} {error "$pin must be driven by $reference"}
}
proc fk33_reset_clock {pin} {
    set clock [fk33_reset_one [get_clocks -of_objects $pin] "Clock of $pin"]
    if {$clock ne "main_clkout3" || abs([get_property PERIOD $clock] - 2.0) > 0.001 ||
        [get_property IS_INVERTED $pin]} {error "$pin requires the rising-edge 500 MHz AXI clock"}
    return $clock
}
proc fk33_reset_ff {cell reference initial} {
    if {[get_property REF_NAME $cell] ne $reference ||
        [string toupper [get_property ASYNC_REG $cell]] ni {TRUE 1}} {error "$cell is not a reset synchronizer FF"}
    set value [string tolower [get_property INIT $cell]]
    if {$initial == 0 && $value ni {0 1'b0 1'h0 1'd0 0x0}} {error "$cell must initialize to zero"}
    if {$initial == 1 && $value ni {1 1'b1 1'h1 1'd1 0x1}} {error "$cell must initialize to one"}
    fk33_reset_clock [fk33_reset_pin $cell C]
    fk33_reset_constant [fk33_reset_pin $cell CE] VCC
}
set fk33_axi_reset_candidates {}
foreach cell [get_cells -hierarchical -filter {ars_ff2 == TRUE}] {
    set clocks [get_clocks -of_objects [fk33_reset_pin $cell C]]
    if {[llength $clocks] == 1 && [lindex $clocks 0] eq "main_clkout3"} {
        lappend fk33_axi_reset_candidates $cell
    }
}
set fk33_axi_reset_stage1 [fk33_reset_one $fk33_axi_reset_candidates "AXI reset release stage"]
set fk33_axi_reset_q1 [fk33_reset_pin $fk33_axi_reset_stage1 Q]
set fk33_axi_reset_q0 [fk33_reset_driver [fk33_reset_pin $fk33_axi_reset_stage1 D]]
set fk33_axi_reset_stage0 [fk33_reset_one [get_cells -of_objects $fk33_axi_reset_q0] "AXI reset first stage"]
foreach cell [list $fk33_axi_reset_stage0 $fk33_axi_reset_stage1] {fk33_reset_ff $cell FDPE 1}
if {[string toupper [get_property ars_ff1 $fk33_axi_reset_stage0]] ni {TRUE 1}} {error "Missing AXI reset first-stage tag"}
fk33_reset_constant [fk33_reset_pin $fk33_axi_reset_stage0 D] GND
if {[get_nets -of_objects [fk33_reset_pin $fk33_axi_reset_stage0 PRE]] ne
    [get_nets -of_objects [fk33_reset_pin $fk33_axi_reset_stage1 PRE]]} {error "AXI reset stages must share PRE"}
set fk33_axi_reset_load [fk33_reset_one [get_pins -leaf -of_objects [get_nets -segments -of_objects $fk33_axi_reset_q1] -filter {DIRECTION == IN}] "AXI reset prebuffer load"]
set fk33_axi_reset_buf [fk33_reset_one [get_cells -of_objects $fk33_axi_reset_load] "AXI reset BUFG"]
if {[get_property REF_NAME $fk33_axi_reset_buf] ne "BUFGCE" ||
    [get_property REF_PIN_NAME $fk33_axi_reset_load] ne "I" ||
    [get_property IS_INVERTED $fk33_axi_reset_load]} {error "AXI reset requires its noninverted BUFGCE"}
fk33_reset_constant [fk33_reset_pin $fk33_axi_reset_buf CE] VCC
# Protect the small input nets, not the output reset net: downstream FFs may
# still be replicated, while reset loads cannot move around the BUFG.
foreach cell [list $fk33_axi_reset_stage0 $fk33_axi_reset_stage1 $fk33_axi_reset_buf] {
    set_property DONT_TOUCH TRUE $cell
}
foreach pin [list $fk33_axi_reset_q0 $fk33_axi_reset_q1] {
    set_property DONT_TOUCH TRUE [get_nets -segments -of_objects $pin]
}
proc fk33_check_reset_buffer {} {
    global fk33_axi_reset_q1 fk33_axi_reset_load fk33_axi_reset_buf
    set loads [get_pins -leaf -of_objects [get_nets -segments -of_objects $fk33_axi_reset_q1] -filter {DIRECTION == IN}]
    if {[llength $loads] != 1 || [lindex $loads 0] ne $fk33_axi_reset_load} {error "AXI reset BUFG bypass: $loads"}
    if {![get_property DONT_TOUCH $fk33_axi_reset_buf]} {error "AXI reset BUFG lost protection"}
    puts "FK33_AXI_RESET_BUFFER_PROTECTED source_loads=1"
}
fk33_check_reset_buffer
"""
    release = r"""
fk33_check_reset_buffer
set fk33_native_reset_cells [get_cells -hierarchical -filter {REF_NAME == HBM_SNGLBLI_INTF_AXI}]
if {[llength $fk33_native_reset_cells] != 32} {error "Expected 32 native HBM reset endpoints"}
set fk33_native_reset_syncs {}
foreach native $fk33_native_reset_cells {
    fk33_reset_clock [fk33_reset_pin $native ACLK]
    set reset_pin [fk33_reset_pin $native ARESET_N]
    set q1 [fk33_reset_driver $reset_pin]
    set stage1 [fk33_reset_one [get_cells -of_objects $q1] "Native reset release FF"]
    set q0 [fk33_reset_driver [fk33_reset_pin $stage1 D]]
    set stage0 [fk33_reset_one [get_cells -of_objects $q0] "Native reset first FF"]
    foreach cell [list $stage0 $stage1] {fk33_reset_ff $cell FDCE 0}
    if {$stage0 eq $stage1} {error "Native reset requires two stages"}
    fk33_reset_constant [fk33_reset_pin $stage0 D] VCC
    set clr0 [fk33_reset_pin $stage0 CLR]
    set clr1 [fk33_reset_pin $stage1 CLR]
    if {[get_nets -of_objects $clr0] ne [get_nets -of_objects $clr1]} {error "Native reset stages must share CLR"}
    # These are asynchronous assertion inputs of a validated two-FF reset
    # synchronizer. Interstage data and the hard ARESET_N output remain timed.
    set_false_path -to [list $clr0 $clr1]
    set_max_delay 1.8 -from [fk33_reset_pin $stage1 C] -to $reset_pin
    lappend fk33_native_reset_syncs $stage0 $stage1
}
if {[llength [lsort -unique $fk33_native_reset_syncs]] != 64} {error "Expected 32 independent two-FF native reset synchronizers"}
puts "FK33_NATIVE_RESET_CONSTRAINTS async_clear_pins=64 release_budget_ns=1.8"
"""
    # Vivado command templates use Python format braces.
    escape = lambda command: command.replace("{", "{{").replace("}", "}}")
    platform.toolchain.pre_optimize_commands += [escape(prepare)]
    platform.toolchain.pre_placement_commands += [escape(release)]
    platform.toolchain.pre_routing_commands += ["fk33_check_reset_buffer"]
    platform.toolchain.bitstream_commands += ["fk33_check_reset_buffer"]


def _hbm_cpu_locality_commands():
    """Select native CPU FIFO storage from its Gray tags and RAM wiring."""
    return r"""
# Return additions for the existing native-region pblocks; no pblock policy here.
proc fk33_cpu_one {objects label} {
    if {[llength $objects] != 1} {error "$label expectedone:$objects"}
    return [lindex $objects 0]
}
proc fk33_cpu_cell {pin} {return [fk33_cpu_one [get_cells -of_objects $pin] "Typedleafcellof$pin"]}
proc fk33_cpu_pin {cell role} {return [fk33_cpu_one [get_pins -of_objects $cell -filter "REF_PIN_NAME == $role"] "$cell/$role"]}
proc fk33_cpu_outputs {pins} {
    if {![llength $pins]} {return {}}
    return [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments -of_objects $pins] -filter {DIRECTION == OUT}]
}
proc fk33_cpu_inputs {pins} {
    if {![llength $pins]} {return {}}
    return [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments -of_objects $pins] -filter {DIRECTION == IN}]
}
proc fk33_cpu_eligible {cell} {
    global fk33_cpu_ff_cache fk33_cpu_clock_pins
    if {[dict exists $fk33_cpu_ff_cache $cell]} {return [dict get $fk33_cpu_ff_cache $cell]}
    set reason ORDINARY_NATIVE
    set ref [get_property REF_NAME [get_cells $cell]]
    if {$ref ni {FDRE FDSE}} {set reason NOT_ORDINARY_SYNC_FF}
    if {$reason eq "ORDINARY_NATIVE" && ![dict exists $fk33_cpu_clock_pins [fk33_cpu_pin $cell C]]} {set reason NOT_NATIVE_2NS}
    foreach property {ASYNC_REG HBM_GRAY_SRC HBM_GRAY_DST ars_ff1 ars_ff2} {
        set value [get_property -quiet $property $cell]
        if {$property in {HBM_GRAY_SRC HBM_GRAY_DST}} {
            if {$value ne ""} {set reason CDC_PUBLICATION_OR_SYNCHRONIZER}
        } elseif {[string toupper $value] in {TRUE 1 1'B1}} {set reason CDC_OR_RESET_SYNCHRONIZER}
    }
    if {[regexp -nocase {quiesce|mailbox|hbmruntime|hbm_control|permit_|trip_hold|reset_sync} $cell]} {set reason GLOBAL_OR_RESET_CONTROL}
    set ok [expr {$reason eq "ORDINARY_NATIVE"}]
    dict set fk33_cpu_ff_cache $cell $ok
    return $ok
}
proc fk33_cpu_claim {cell pc region role} {
    global fk33_cpu_claims
    dict lappend fk33_cpu_claims $cell [list $pc $region $role]
}
proc fk33_cpu_locality_select {pc_regions native_aclk reserve_pc31} {
    global fk33_cpu_clock_pins fk33_cpu_ff_cache fk33_cpu_claims fk33_cpu_macro_children
    set fk33_cpu_ff_cache [dict create]
    set fk33_cpu_claims [dict create]
    set fk33_cpu_macro_children [dict create]
    if {[dict size $pc_regions] != 32} {error "CPU locality requires32validated hard-port regions"}
    set hard_regions $pc_regions
    set clock [fk33_cpu_one [get_clocks -of_objects $native_aclk] "CPU native clock"]
    if {[get_property PERIOD $clock] != 2.0} {error "CPU locality requires native500MHz"}
    set fk33_cpu_clock_pins [dict create]
    foreach pin [fk33_cpu_inputs $native_aclk] {dict set fk33_cpu_clock_pins $pin 1}
    set gray_groups [dict create]
    set cells [get_cells -hier -filter {HBM_GRAY_SRC =~ hbm_cpu_pc*}]
    foreach cell $cells tag [get_property HBM_GRAY_SRC $cells] {dict lappend gray_groups $tag $cell}
    set binary_groups [dict create]
    foreach cell [get_cells -hier -filter {REF_NAME =~ FD* && NAME =~ *q_binary_reg*}] {
        if {[regexp {^(.+_q_binary)_reg} $cell -> prefix]} {dict lappend binary_groups $prefix $cell}
    }
    # Index actual primitive-parent relationships once, including unrequested siblings.
    set ram_leaves [get_cells -hier -filter {REF_NAME == RAMD32 || REF_NAME == RAMS32}]
    set leaf_parents [dict create]
    set all_children [dict create]
    foreach leaf $ram_leaves parent [get_property PARENT $ram_leaves] {
        dict set leaf_parents $leaf $parent
        if {$parent ne ""} {dict lappend all_children $parent $leaf}
    }
    set typed_macros [dict create]
    set parents [get_cells -quiet [dict keys $all_children]]
    foreach parent $parents ref [get_property REF_NAME $parents] {
        if {$ref in {RAM32M RAM32M16}} {dict set typed_macros $parent $parent}
    }
    set pruned 0
    set macro_regions [dict create]
    set bank_count 0
    foreach tag [lsort [dict keys $gray_groups]] {
        if {![regexp {^hbm_cpu_pc([0-9][0-9])_(aw|w|ar|b|r)_(produce|consume)$} $tag -> pc channel pointer]} {error "UnknownCPUgraytag:$tag"}
        scan $pc %d pc
        set read [expr {$channel in {aw w ar} && $pointer eq "consume"}]
        set write [expr {$channel in {b r} && $pointer eq "produce"}]
        if {!$read && !$write} {continue}
        incr bank_count
        set role [expr {$read ? "read" : "write"}]
        set sources [dict get $gray_groups $tag]
        if {[llength $sources] != 3} {error "Not3Graybits:$tag"}
        set stems {}
        foreach source $sources {
            if {![regexp {^(.+)_q_reg\[[012]\]$} $source -> stem]} {error "UnexpectedGrayFFname:$source"}
            lappend stems $stem
            if {![dict exists $fk33_cpu_clock_pins [fk33_cpu_pin $source C]]} {error "Tagnotnative:$tag/$source"}
        }
        set stem [fk33_cpu_one [lsort -unique $stems] Graycounterstem]
        set binary ${stem}_q_binary
        if {![dict exists $binary_groups $binary]} {
            if {$pc != 31 || !$reserve_pc31} {error "Missingbinarypointerwithretained/unreservedbank:$tag"}
            incr pruned
            puts "FK33_CPU_RESERVED_PRUNE tag=$tag"
            continue
        }
        set pointers [dict get $binary_groups $binary]
        foreach ff $pointers {if {![fk33_cpu_eligible $ff]} {error "Binarypointernotordinarynative:$ff"}}
        set addr_pins [fk33_cpu_inputs [get_pins -of_objects $pointers -filter {REF_PIN_NAME == Q}]]
        set ram_bits [dict create]
        foreach pin $addr_pins refpin [get_property REF_PIN_NAME $addr_pins] {
            if {![regexp {^(RADR|WADR|ADR|A)([01])$} $refpin -> kind bit]} {continue}
            if {$read && $kind eq "WADR" || $write && $kind eq "RADR"} {continue}
            set cell [fk33_cpu_cell $pin]
            if {![string match RAM* [get_property REF_NAME [get_cells $cell]]]} {continue}
            dict lappend ram_bits $cell $bit
        }
        set rams [dict keys $ram_bits]
        set region [dict get $hard_regions $pc]
        if {![llength $rams]} {
            if {$pc != 31 || !$reserve_pc31} {error "NoactualRAMforunreservedbank:$tag"}
            incr pruned
            puts "FK33_CPU_RESERVED_PRUNE tag=$tag"
            continue
        }
        foreach ram $rams {
            if {[lsort -unique [dict get $ram_bits $ram]] ne {0 1}} {error "RAMnotdirectlyboundtobothcurrentaddressbits:$tag/$ram"}
        }
        # Vivado promotes an internal RAM leaf assignment to its entire macro.
        foreach ram $rams {
            if {![dict exists $leaf_parents $ram]} {continue}
            set parent [dict get $leaf_parents $ram]
            if {![dict exists $typed_macros $parent]} {continue}
            dict lappend macro_regions $parent $region
            if {[llength [lsort -unique [dict get $macro_regions $parent]]] != 1} {error "RAM macro crosses CPU regions:$parent"}
            dict set fk33_cpu_macro_children $parent [dict get $all_children $parent]
        }
        set selected $pointers
        if {$read} {
            set sinks [fk33_cpu_inputs [get_pins -of_objects $rams -filter {DIRECTION == OUT}]]
            foreach pin $sinks rolepin [get_property REF_PIN_NAME $sinks] {
                if {$rolepin ne "D"} {continue}
                set ff [fk33_cpu_cell $pin]
                if {[string match FD* [get_property REF_NAME [get_cells $ff]]] && [fk33_cpu_eligible $ff]} {lappend selected $ff}
            }
        } else {
            set data [get_pins -of_objects $rams -filter {REF_PIN_NAME == I}]
            if {![llength $data]} {error "RAMwriteinputIabsent:$tag"}
            set drivers [fk33_cpu_outputs $data]
            set luts {}
            foreach cell [get_cells -of_objects $drivers] {
                set ref [get_property REF_NAME [get_cells $cell]]
                if {[string match FD* $ref] && [fk33_cpu_eligible $cell]} {lappend selected $cell}
                if {[string match LUT* $ref]} {lappend luts $cell}
            }
            if {[llength $luts]} {
                set pins [get_pins -of_objects $luts -filter {DIRECTION == IN}]
                foreach cell [get_cells -of_objects [fk33_cpu_outputs $pins]] {
                    if {[string match FD* [get_property REF_NAME [get_cells $cell]]] && [fk33_cpu_eligible $cell]} {lappend selected $cell}
                }
            }
        }
        set selected [lsort -unique $selected]
        if {[llength $selected] <= [llength $pointers]} {error "NoRAMdataFFsselected:$tag"}
        foreach cell [concat $rams $selected] {
            if {[dict exists $leaf_parents $cell] && [dict exists $typed_macros [dict get $leaf_parents $cell]]} {
                set cell [dict get $typed_macros [dict get $leaf_parents $cell]]
            }
            fk33_cpu_claim $cell $pc $region $role
        }
        puts "FK33_CPU_BANK tag=$tag region=$region RAMs=[llength $rams] FFs=[llength $selected]"
    }
    if {$bank_count != 160} {error "Not160tagboundbanks:$bank_count"}
    if {$pruned != ($reserve_pc31 ? 5 : 0)} {error "Unexpected CPU reserved-bank count:$pruned"}
    set additions [dict create]
    foreach cell [lsort [dict keys $fk33_cpu_claims]] {
        set claims [lsort -unique [dict get $fk33_cpu_claims $cell]]
        set regions {}
        foreach claim $claims {lappend regions [lindex $claim 1]}
        set regions [lsort -unique $regions]
        if {[llength $regions] != 1} {
            puts "FK33_CPU_SHARED_EXCLUDED cell=$cell claims=$claims"
            continue
        }
        set region [lindex $regions 0]
        dict lappend additions $region $cell
    }
    puts "FK33_CPU_LOCALITY banks=$bank_count pruned=$pruned regions=[dict size $additions]"
    return $additions
}
"""


def _add_hbm_native_floorplan(platform, reserve_pc31=False):
    """Guide native channel storage towards its fixed HBM clock region."""
    commands = r"""
proc fk33_floorplan_one {objects label} {
    if {[llength $objects] != 1} {error "$label requires one object: $objects"}
    return [lindex $objects 0]
}
proc fk33_floorplan_driver {pin} {
    set nets [get_nets -segments -of_objects $pin]
    return [fk33_floorplan_one [get_pins -leaf -of_objects $nets -filter {DIRECTION == OUT}] "$pin driver"]
}
proc fk33_check_native_floorplan_anchors {} {
    global fk33_native_anchors
    dict for {native intended} $fk33_native_anchors {
        set actual [fk33_floorplan_one [get_sites -of_objects [get_cells $native]] "$native placed site"]
        if {$actual ne $intended} {error "$native placed at $actual instead of architectural site $intended"}
    }
    puts "FK33_NATIVE_FLOORPLAN_ANCHORS ports=[dict size $fk33_native_anchors]"
}
set native_cells [get_cells -hierarchical -filter {REF_NAME == HBM_SNGLBLI_INTF_AXI}]
if {[llength $native_cells] != 32} {error "Native floorplan requires 32 hard ports"}
set boundary_ffs [get_cells -hierarchical -filter {REF_NAME =~ FD* && NAME =~ *hbmnativeelasticboundary*}]
set stem_cells [dict create]
set stem_buffers [dict create]
foreach cell $boundary_ffs {
    if {[regexp {([^/]*hbmnativeelasticboundary[0-9]+)(_buffer[0-9]+)_pipe_(valid_source|ready)_} $cell -> stem buffer]} {
        dict lappend stem_cells $stem $cell
        dict lappend stem_buffers $stem $buffer
    }
}
set region_members [dict create]
set cell_regions [dict create]
set boundary_stems {}
set permit_cells {}
set fk33_native_anchors [dict create]
set fk33_hard_regions [dict create]
set boundary_ports [dict create]
set fk33_native_aclk [fk33_floorplan_one [get_pins -of_objects [lindex $native_cells 0] -filter {REF_PIN_NAME == ACLK}] "Native ACLK"]
foreach native $native_cells {
    # FK33's vendor-expanded HBM stack/port identity fixes its BLI site even
    # before placement, when the leaf primitive has no LOC property yet.
    if {![regexp {/HBM_ONE_STACK_INTF<([01])>_INST/HBM_SNGLBLI_INTF_AXI<([0-9]+)>_INST$} $native -> stack local] || $local > 15} {
        error "Unknown FK33 native architectural identity: $native"
    }
    set pc [expr {16 * $stack + $local}]
    if {[dict exists $fk33_hard_regions $pc]} {error "Duplicate native architectural port $pc"}
    set site [fk33_floorplan_one [get_sites -quiet BLI_HBM_AXI_INTF_X${pc}Y0] "$native architectural site"]
    if {[get_property SITE_TYPE $site] ne "BLI_HBM_AXI_INTF"} {error "$site is not a native HBM site"}
    set existing [get_sites -quiet -of_objects $native]
    if {[llength $existing] && $existing ne $site} {error "$native existing site $existing differs from $site"}
    set loc [get_property LOC $native]
    if {$loc ne "" && $loc ne $site} {error "$native existing LOC $loc differs from $site"}
    set region [fk33_floorplan_one [get_clock_regions -of_objects $site] "$native clock region"]
    dict set fk33_native_anchors $native $site
    dict set fk33_hard_regions $pc $region
    set data_pin [get_pins -of_objects $native -filter {REF_PIN_NAME == "WDATA[0]" || REF_PIN_NAME == WDATA0}]
    set driver [fk33_floorplan_driver [fk33_floorplan_one $data_pin "$native WDATA0"]]
    set source [fk33_floorplan_one [get_cells -of_objects $driver] "$native WDATA0 source"]
    if {![string match FD* [get_property REF_NAME $source]] || [get_property REF_PIN_NAME $driver] ne "Q"} {
        error "$native WDATA0 must have a direct FF/Q driver"
    }
    if {![regexp {([^/]*hbmnativeelasticboundary[0-9]+)_buffer[0-9]+_pipe_valid_source_payload_data_reg} $source -> stem]} {
        error "$native WDATA0 source has no boundary stem: $source"
    }
    if {$stem in $boundary_stems} {error "Duplicate native boundary stem $stem"}
    lappend boundary_stems $stem
    dict set boundary_ports $stem $pc
    if {![dict exists $stem_cells $stem]} {error "Missing native boundary $stem"}
    set members [dict get $stem_cells $stem]
    set buffers [dict get $stem_buffers $stem]
    if {[llength [lsort -unique $buffers]] != 5} {error "$stem requires five retained channel buffers"}
    set reset [fk33_floorplan_one [get_pins -of_objects $native -filter {REF_PIN_NAME == ARESET_N}] "$native reset"]
    set stage1 [fk33_floorplan_one [get_cells -of_objects [fk33_floorplan_driver $reset]] "$native reset stage1"]
    set d1 [fk33_floorplan_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == D}] "$stage1 D"]
    set stage0 [fk33_floorplan_one [get_cells -of_objects [fk33_floorplan_driver $d1]] "$native reset stage0"]
    foreach stage [list $stage0 $stage1] {
        if {[get_property REF_NAME $stage] ne "FDCE"} {error "$stage must remain FDCE"}
        lappend members $stage
        lappend permit_cells $stage
    }
    foreach cell $members {
        if {[dict exists $cell_regions $cell]} {error "Duplicate native floorplan member $cell"}
        dict set cell_regions $cell $region
        dict lappend region_members $region $cell
    }
    puts "FK33_NATIVE_REGION_BINDING hard=$native site=$site region=$region source=$source stem=$stem registers=[llength $members]"
}
if {[llength [lsort -unique $permit_cells]] != 64} {error "Native floorplan requires 64 independent permit FFs"}
if {[llength [lsort -unique [dict values $fk33_native_anchors]]] != 32} {error "Expected 32 distinct native architectural sites"}
dict for {region members} [fk33_cpu_locality_select $fk33_hard_regions $fk33_native_aclk $fk33_reserve_pc31] {
    foreach cell $members {
        if {[dict exists $cell_regions $cell] && [dict get $cell_regions $cell] ne $region} {error "CPU storage crosses native regions: $cell"}
        dict set cell_regions $cell $region
        dict lappend region_members $region $cell
    }
}
# Reuse the CPU selector's exact native-clock pin cache.
set native_clock_pins [get_pins [dict keys $fk33_cpu_clock_pins] -filter {REF_PIN_NAME == C}]
if {$fk33_reserve_pc31} {
    set endpoint [get_cells -of_objects $native_clock_pins -filter {REF_NAME =~ FD* && NAME =~ *hbmwindowfourendpoint*}]
    if {![llength $endpoint]} {error "Reserved PC31 endpoint has no native-clock storage"}
    foreach cell $endpoint {
        dict set cell_regions $cell [dict get $fk33_hard_regions 31]
        dict lappend region_members [dict get $fk33_hard_regions 31] $cell
    }
    puts "FK33_NATIVE_ENDPOINT_LOCALITY pc=31 registers=[llength $endpoint]"
}
# Tracker instance numbering is allocation-dependent. Its native AW/AR ready
# inputs instead identify the first skid state, whose boundary has a known PC.
set tracker_predicates {}
set tracker_ports {}
set trackers [get_cells -hierarchical -filter {REF_NAME =~ fk33_hbm_quiesce* && IS_PRIMITIVE == 0}]
foreach tracker $trackers {
    set pcs {}
    set observed_roles 0
    foreach role {m_awready m_arready} {
        set pin [get_pins -quiet ${tracker}/$role]
        if {[llength $pin] != 1} {continue}
        if {[get_cells -of_objects $pin] ne $tracker} {error "$pin is not owned by $tracker"}
        incr observed_roles
        set outputs [get_pins -leaf -of_objects [get_nets -segments -of_objects $pin] -filter {DIRECTION == OUT}]
        set drivers [get_cells -of_objects $outputs]
        set luts [get_cells -of_objects $outputs -filter {REF_NAME =~ LUT*}]
        if {[llength $luts]} {
            set inputs [get_pins -of_objects $luts -filter {DIRECTION == IN}]
            set sources [get_pins -leaf -of_objects [get_nets -segments -of_objects $inputs] -filter {DIRECTION == OUT}]
            set drivers [concat $drivers [get_cells -of_objects $sources]]
        }
        set stems {}
        foreach driver $drivers {
            if {![string match FD* [get_property REF_NAME [get_cells $driver]]]} {continue}
            if {[regexp {([^/]*hbmnativeelasticboundary[0-9]+)_buffer[0-9]+_pipe_ready_valid_reg[^/]*$} $driver -> stem] && [dict exists $boundary_ports $stem] && [dict exists $cell_regions $driver]} {
                lappend stems $stem
            }
        }
        if {[llength [lsort -unique $stems]] == 1} {lappend pcs [dict get $boundary_ports [lindex $stems 0]]}
    }
    if {!$observed_roles} {puts "FK33_NATIVE_TRACKER_LOCALITY_PENDING tracker=$tracker reason=hierarchy_pins_unavailable"; continue}
    set pcs [lsort -unique $pcs]
    if {[llength $pcs] != 1} {error "$tracker AW/AR readiness does not identify one native boundary"}
    set pc [lindex $pcs 0]
    if {$pc in $tracker_ports} {error "Duplicate native tracker for PC$pc"}
    lappend tracker_ports $pc
    set region [dict get $fk33_hard_regions $pc]
    set members [get_cells -hierarchical -filter "REF_NAME =~ FD* && NAME =~ $tracker/*"]
    if {![llength $members]} {error "$tracker has no retained state"}
    foreach cell $members {
        set clock_pin [fk33_cpu_pin $cell C]
        if {![dict exists $fk33_cpu_clock_pins $clock_pin]} {error "$tracker state is not native-clocked: $cell"}
        dict set cell_regions $cell $region
        dict lappend region_members $region $cell
        lappend tracker_predicates $cell
    }
    puts "FK33_NATIVE_TRACKER_LOCALITY tracker=$tracker pc=$pc registers=[llength $members]"
}
if {[llength $tracker_ports] != 32} {puts "FK33_NATIVE_TRACKER_LOCALITY_PENDING retained_hierarchies=[llength $trackers] bound_ports=[llength $tracker_ports]"}
# Guide the measured READY-to-CE cones. Data muxes remain placer-controlled;
# both payload banks, including direct incoming RDATA FFs, are already grouped.
# Shared global, cross-region and top-port drivers remain unbound.
set data_pins [get_pins -of_objects [get_cells [dict keys $cell_regions]] -filter {REF_PIN_NAME == CE}]
if {[llength $tracker_predicates]} {
    set data_pins [concat $data_pins [get_pins -of_objects [get_cells $tracker_predicates] -filter {REF_PIN_NAME == D}]]
}
set data_nets [get_nets -segments -of_objects $data_pins]
set data_outputs [get_pins -leaf -of_objects $data_nets -filter {DIRECTION == OUT}]
set driver_luts [get_cells -of_objects $data_outputs -filter {REF_NAME =~ LUT*}]
set port_nets [dict create]
foreach net [get_nets -segments -of_objects [get_ports]] {dict set port_nets $net 1}
puts "FK33_NATIVE_CE_DRIVERS candidates=[llength $driver_luts]"
foreach lut $driver_luts {
    set claims {}
    set outputs [get_pins -of_objects $lut -filter {DIRECTION == OUT}]
    set nets [get_nets -segments -of_objects $outputs]
    set port_connected 0
    foreach net $nets {
        if {[dict exists $port_nets $net]} {set port_connected 1; break}
    }
    if {$port_connected} {continue}
    set loads [get_pins -leaf -of_objects $nets -filter {DIRECTION == IN}]
    if {![llength $loads]} {continue}
    set owners [get_cells -of_objects $loads]
    if {![llength $owners]} {continue}
    set safe 1
    foreach owner $owners {
        if {![dict exists $cell_regions $owner]} {
            set safe 0
            break
        }
        lappend claims [dict get $cell_regions $owner]
    }
    set claims [lsort -unique $claims]
    if {$safe && [llength $claims] == 1} {dict lappend region_members [lindex $claims 0] $lut}
}
dict for {region members} $region_members {
    set members [lsort -unique $members]
    set slices [get_sites -of_objects [get_clock_regions $region] -filter {SITE_TYPE == SLICEL || SITE_TYPE == SLICEM}]
    if {![llength $slices]} {error "$region has no SLICE sites"}
    set ys {}
    foreach slice $slices {
        if {![regexp {SLICE_X([0-9]+)Y([0-9]+)$} $slice -> x y]} {error "Unexpected SLICE name $slice"}
        lappend ys $y
    }
    set ys [lsort -integer -unique $ys]
    set middle [lindex $ys [expr {([llength $ys] - 1) / 2}]]
    set lower {}
    foreach slice $slices {
        regexp {Y([0-9]+)$} $slice -> y
        if {$y <= $middle} {lappend lower $slice}
    }
    set name fk33_native_$region
    if {[llength [get_pblocks -quiet $name]]} {error "Native pblock already exists: $name"}
    create_pblock $name
    resize_pblock [get_pblocks $name] -add $lower
    set_property IS_SOFT 1 [get_pblocks $name]
    foreach {property expected} {IS_SOFT 1 EXCLUDE_PLACEMENT 0 CONTAIN_ROUTING 0} {
        if {[get_property $property [get_pblocks $name]] != $expected} {error "$name $property did not retain $expected"}
    }
    add_cells_to_pblock [get_pblocks $name] [get_cells $members]
    set expected [lsort -unique [get_cells $members]]
    set actual [lsort -unique [get_cells -of_objects [get_pblocks $name]]]
    if {$actual ne $expected} {
        error "$name canonical membership differs: expected=[llength $expected] actual=[llength $actual]"
    }
    puts "FK33_NATIVE_REGION_PBLOCK region=$region slices=[llength $lower] cells=[llength $members] max_y=$middle soft=1"
}
# Vivado assigns a RAM32M/M16 atomically. Check its typed parent and the
# complete indexed sibling cohort together, once per macro, after assignment.
dict for {macro children} $fk33_cpu_macro_children {
    set expected [list fk33_native_[dict get $cell_regions $macro]]
    set objects [get_cells [concat [list $macro] $children]]
    set actual [lsort -unique [get_pblocks -quiet -of_objects $objects]]
    if {$actual ne $expected} {error "RAM macro effective membership differs: $macro/$actual/$expected"}
}
puts "FK33_NATIVE_FLOORPLAN ports=32 boundaries=32 permits=64"
"""
    commands = _hbm_cpu_locality_commands() + f"\nset fk33_reserve_pc31 {int(reserve_pc31)}\n" + commands
    commands = commands.replace("{", "{{").replace("}", "}}")
    platform.toolchain.pre_placement_commands.append(commands)
    platform.toolchain.pre_routing_commands.append("fk33_check_native_floorplan_anchors")


# BaseSoC ------------------------------------------------------------------------------------------

class HBMHostDrainBarrier(LiteXModule):
    """Acknowledge the current close generation after all SYS work retires.

    A synchronized idle level can remain high from the previous operation.
    The command token and runtime generation prevent a new owner or STOP
    request from consuming that old acknowledgment.
    """
    def __init__(self, admission_closed, host_idle, command_token, generation):
        self.idle_native = Signal()
        self.observed_idle_native = Signal()
        self.token_native = Signal()
        self.generation_native = Signal(32)
        level = Signal()
        self.sync += level.eq(admission_closed & host_idle)
        self.specials += MultiReg(level, self.idle_native, "axi")
        # Open-admission idle observation remains distinct from the tokenized
        # close acknowledgment used for ownership and STOP authorization.
        observed_idle = Signal()
        self.sync += observed_idle.eq(host_idle)
        self.specials += MultiReg(observed_idle, self.observed_idle_native, "axi")
        self.mailbox = mailbox = HBMClockMailbox(33, "sys", "axi")
        sent = Signal(33)
        valid = Signal()
        value = Cat(command_token, generation)
        self.comb += [
            mailbox.data.eq(value),
            mailbox.send.eq(admission_closed & host_idle &
                (~valid | (sent != value))),
            self.token_native.eq(mailbox.output[0]),
            self.generation_native.eq(mailbox.output[1:]),
        ]
        self.sync += If(mailbox.send & ~mailbox.busy,
            sent.eq(value), valid.eq(1))

class HBMCPUTransactionLatch(LiteXModule):
    """Finish an admitted CPU operation even if its Wishbone master times out.

    The AXI-Lite bridge reads Wishbone payload throughout independent AW/W
    acceptance. Hold that payload and CYC until its terminal response; an
    abandoned operation must never acknowledge a subsequent Wishbone cycle.
    """
    def __init__(self, master, slave):
        self.active = Signal()
        self.idle = Signal()
        abandoned = Signal()
        blocked = Signal()
        address = Signal.like(master.adr)
        data = Signal.like(master.dat_w)
        select = Signal.like(master.sel)
        write = Signal()
        self.comb += [
            slave.adr.eq(address), slave.dat_w.eq(data), slave.sel.eq(select),
            slave.we.eq(write), slave.cyc.eq(self.active), slave.stb.eq(self.active),
            master.dat_r.eq(slave.dat_r),
            master.ack.eq(slave.ack & self.active & ~abandoned & master.cyc),
            master.err.eq(slave.err & self.active & ~abandoned & master.cyc),
            self.idle.eq(~self.active),
        ]
        self.sync += [
            If(~master.cyc, blocked.eq(0)),
            If(self.active,
                If(~master.cyc, abandoned.eq(1)),
                If(slave.ack | slave.err,
                    self.active.eq(0),
                    # A newer cycle may already have arrived while the old
                    # response was outstanding. Require its withdrawal too.
                    If(abandoned & master.cyc, blocked.eq(1)),
                ),
            ).Elif(master.cyc & master.stb & ~blocked,
                self.active.eq(1), abandoned.eq(0),
                address.eq(master.adr), data.eq(master.dat_w),
                select.eq(master.sel), write.eq(master.we),
            ),
        ]


class HBMCPUWindowCDC(LiteXModule):
    """Narrow per-port CDC with SYS-side terminal-response accounting.

    Counts extend through the return FIFOs, not just native AXI completion.
    Separate AW/W counts retain half-writes across STOP and arbitrary channel
    skew. One CPU window permits at most one read and one write outstanding.
    """
    def __init__(self, master, slave, gray_bus_prefix=None, forward_read_pipeline=False,
            reverse_read_pipeline=False):
        self.idle = Signal()
        self.fault = Signal()
        if forward_read_pipeline or reverse_read_pipeline:
            from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint import HBMRegisteredReadCDC
            # Reuse the endpoint's registered-current-address FIFO. Keep the
            # empty/advance decision out of the RAM read data path.
            self.crossing = LiteXModule()
            for name in ("aw", "w", "ar", "b", "r"):
                forward = name in ("aw", "w", "ar")
                source = getattr(master if forward else slave, name)
                sink = getattr(slave if forward else master, name)
                prefix = None if gray_bus_prefix is None else f"{gray_bus_prefix}_{name}"
                if (forward and forward_read_pipeline) or (not forward and reverse_read_pipeline):
                    crossing = HBMRegisteredReadCDC(source.description,
                        cd_from="sys" if forward else "axi",
                        cd_to="axi" if forward else "sys", gray_bus_prefix=prefix,
                        read_address_max_fanout=16)
                else:
                    crossing = stream.ClockDomainCrossing(source.description,
                        cd_from="sys" if forward else "axi",
                        cd_to="axi" if forward else "sys", gray_bus_prefix=prefix)
                setattr(self.crossing, name, crossing)
                self.comb += [source.connect(crossing.sink), crossing.source.connect(sink)]
        else:
            self.crossing = axi.AXILiteClockDomainCrossing(master, slave,
                cd_from="sys", cd_to="axi", gray_bus_prefix=gray_bus_prefix)
        pending = [Signal(4, name=f"hbm_cdc_{name}_pending")
            for name in ("aw", "w", "ar")]
        self.pending = pending
        self.sync += [
            If((getattr(master, name).valid & getattr(master, name).ready) !=
                    (getattr(master, response).valid & getattr(master, response).ready),
                If(getattr(master, name).valid & getattr(master, name).ready,
                    If(count == 15, self.fault.eq(1)).Else(count.eq(count + 1)),
                ).Else(
                    If(count == 0, self.fault.eq(1)).Else(count.eq(count - 1)),
                ),
            ) for name, response, count in zip(("aw", "w", "ar"), ("b", "b", "r"), pending)
        ]
        self.comb += self.idle.eq(~self.fault &
            (Cat(*pending) == 0) & ~master.aw.valid & ~master.w.valid &
            ~master.ar.valid & ~master.b.valid & ~master.r.valid)


class HBMReservedPortError(LiteXModule):
    """Complete accidental accesses to an application-owned port with DECERR."""
    def __init__(self, port):
        address_seen = Signal()
        data_seen = Signal()
        read_pending = Signal()
        self.comb += [
            port.aw.ready.eq(~address_seen),
            port.w.ready.eq(~data_seen),
            port.b.valid.eq(address_seen & data_seen), port.b.resp.eq(3),
            port.ar.ready.eq(~read_pending),
            port.r.valid.eq(read_pending), port.r.resp.eq(3), port.r.data.eq(0),
        ]
        self.sync += [
            If(port.aw.valid & port.aw.ready, address_seen.eq(1)),
            If(port.w.valid & port.w.ready, data_seen.eq(1)),
            If(port.b.valid & port.b.ready, address_seen.eq(0), data_seen.eq(0)),
            If(port.ar.valid & port.ar.ready, read_pending.eq(1)),
            If(port.r.valid & port.r.ready, read_pending.eq(0)),
        ]


class HBMEndpointNativeWiring(LiteXModule):
    """Wire the endpoint's ordered AXI3 subset to the existing HBM record."""
    def __init__(self, endpoint, runtime):
        if endpoint.version != "axi3" or runtime.version != "axi4":
            raise ValueError("HBM endpoint wiring requires AXI3 and the existing AXI4-shaped record")
        for port in (endpoint, runtime):
            if (port.data_width, port.address_width, port.id_width) != (256, 33, 6):
                raise ValueError("HBM endpoint wiring preserves AXI256/address33/ID6")
        # The wrapper has no physical WID pin. The endpoint emits ordered W
        # bursts under one ID lock; AWID supplies the controller's write ID.
        self.comb += [
            endpoint.aw.connect(runtime.aw),
            endpoint.w.connect(runtime.w, omit={"id"}),
            endpoint.ar.connect(runtime.ar),
            runtime.b.connect(endpoint.b), runtime.r.connect(endpoint.r),
        ]

class HBMNativeElasticBoundary(LiteXModule):
    """Two-slot native AXI channels with registered payload and backpressure.

    The upstream runtime tracker owns requests accepted into these queues and
    retires responses only after their return to the upstream consumer. Queue
    occupancy additionally participates in drain, never in admission or reset.
    """
    def __init__(self, upstream, downstream):
        for interface in (upstream, downstream):
            if ((interface.data_width, interface.address_width, interface.id_width) != (256, 33, 6)
                    or interface.version != "axi4"):
                raise ValueError("Native elastic boundary preserves AXI256/address33/ID6")
        self.idle = Signal()
        self.levels = {}
        storage_valid = []
        for name in ("aw", "w", "ar", "b", "r"):
            source, sink = ((getattr(upstream, name), getattr(downstream, name))
                if name in ("aw", "w", "ar") else
                (getattr(downstream, name), getattr(upstream, name)))
            # Requests end in a register at the hard interface. Responses enter
            # a register first, keeping their native RAM data path unmuxed.
            forward = name in ("aw", "w", "ar")
            buffer = stream.Buffer(source.description, pipe_valid=True, pipe_ready=True,
                pipe_ready_capture_when_empty=True, pipe_ready_first=forward,
                pipe_ready_valid_max_fanout=64,
                pipe_valid_enable_max_fanout=32 if forward else None)
            self.submodules += buffer
            self.comb += [source.connect(buffer.sink), buffer.source.connect(sink)]
            level = Signal(max=3)
            self.levels[name] = level
            valid = buffer.pipe_valid.source.valid
            skid_valid = ~buffer.pipe_ready.sink.ready
            self.comb += level.eq(Cat(valid, Constant(0, 1)) + Cat(skid_valid, Constant(0, 1)))
            storage_valid += [valid, skid_valid]
        self.comb += self.idle.eq(Cat(*storage_valid) == 0)


class HBMCPUWindowPipeline(LiteXModule):
    """BIST-aware narrow AXI-Lite window boundary with idle accounting."""
    def __init__(self, cpu, native, bist=False, name="hbm_cpu_window",
            pipe_ready_capture_when_empty=False):
        self.idle = Signal(name=f"{name}_idle")
        converted = axi.AXILiteInterface(data_width=32, address_width=32)
        self.converted = converted
        self.native = native
        self.comb += [
            cpu.aw.connect(converted.aw), cpu.w.connect(converted.w),
            cpu.ar.connect(converted.ar),
        ]
        request_buffered = axi.AXILiteInterface(data_width=32, address_width=32)
        self.request_buffered = request_buffered
        offset_input = request_buffered if bist else converted
        request_buffers = {}
        self.request_buffers = request_buffers
        if bist:
            for channel in ("aw", "w", "ar"):
                request = getattr(converted, channel)
                buffer = stream.Buffer(request.description, pipe_valid=True, pipe_ready=True,
                    pipe_ready_capture_when_empty=pipe_ready_capture_when_empty,
                    pipe_ready_valid_max_fanout=64 if pipe_ready_capture_when_empty else None)
                request_buffers[channel] = buffer
                self.submodules += buffer
                self.comb += [request.connect(buffer.sink),
                    buffer.source.connect(getattr(request_buffered, channel))]

        write_buffer = stream.Buffer(offset_input.b.description,
            pipe_valid=True, pipe_ready=True,
            pipe_ready_capture_when_empty=pipe_ready_capture_when_empty,
            pipe_ready_valid_max_fanout=64 if pipe_ready_capture_when_empty else None)
        self.submodules += write_buffer
        self.write_buffer = write_buffer
        self.comb += [offset_input.b.connect(write_buffer.sink),
            write_buffer.source.connect(cpu.b)]

        read_buffer = stream.Buffer(offset_input.r.description,
            pipe_valid=True, pipe_ready=True,
            pipe_ready_capture_when_empty=pipe_ready_capture_when_empty,
            pipe_ready_valid_max_fanout=64 if pipe_ready_capture_when_empty else None)
        self.read_buffer = read_buffer
        self.submodules += read_buffer
        self.comb += [offset_input.r.connect(read_buffer.sink),
            read_buffer.source.connect(cpu.r)]
        self.submodules += axi.AXILiteOffset(offset_input, native, offset=0)

        if bist:
            write_pending = Signal(name=f"{name}_write_pending")
            read_pending = Signal(name=f"{name}_read_pending")
            self.write_pending = write_pending
            self.read_pending = read_pending
            write_request = ((converted.aw.valid & converted.aw.ready) |
                (converted.w.valid & converted.w.ready))
            read_request = converted.ar.valid & converted.ar.ready
            write_retire = write_buffer.source.valid & write_buffer.source.ready
            read_retire = read_buffer.source.valid & read_buffer.source.ready
            self.sync += [
                If(write_retire, write_pending.eq(0)
                ).Elif(write_request, write_pending.eq(1)),
                If(read_retire, read_pending.eq(0)
                ).Elif(read_request, read_pending.eq(1)),
            ]
            visible_request = (converted.aw.valid | converted.w.valid | converted.ar.valid |
                request_buffers["aw"].source.valid | request_buffers["w"].source.valid |
                request_buffers["ar"].source.valid)
            visible_response = (write_buffer.sink.valid | write_buffer.source.valid |
                read_buffer.sink.valid | read_buffer.source.valid)
            self.comb += self.idle.eq(~(write_pending | read_pending |
                visible_request | visible_response))
        else:
            self.comb += self.idle.eq(~(write_buffer.sink.valid | write_buffer.source.valid |
                read_buffer.sink.valid | read_buffer.source.valid))

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=125e6,
        with_led_chaser       = True,
        with_pcie             = False,
        pcie_lanes            = 4,
        pcie_ndmas            = 1,
        pcie_address_width    = 32,
        with_pcie_dma_status  = False,
        with_pcie_dma_monitor = False,
        with_hbm              = False,
        hbm_channels          = (0, 1, 2, 3),
        hbm_main_channel      = 0,
        hbm_base              = USPHBM2_DEFAULT_BASE,
        hbm_high_base         = USPHBM2_HIGH_BASE,
        hbm_strip_origin      = False,
        hbm_primitive         = False,
        hbm_primitive_rtl     = None,
        hbm_bist              = False,
        hbm_endpoint          = False,
        hbm_endpoint_traffic  = False,
        hbm_endpoint_two_port = False,
        hbm_axi_domain        = False,
        hbm_axi_clk_freq      = 250e6,
        hbm_experimental_500_1000 = False,
        **kwargs):
        self.hbm_primitive = hbm_primitive or hbm_primitive_rtl is not None
        self.hbm_axi_domain = bool(hbm_axi_domain)
        self.hbm_experimental_500_1000 = bool(hbm_experimental_500_1000)
        if hbm_experimental_500_1000 and not (
                self.hbm_primitive and hbm_endpoint and hbm_axi_domain and
                sys_clk_freq == 250e6 and hbm_axi_clk_freq == 500e6 and not hbm_endpoint_two_port):
            raise ValueError("Experimental 500/1000 requires a single primitive endpoint at SYS250/AXI500")
        if hbm_axi_clk_freq not in ((500e6,) if hbm_experimental_500_1000 else (250e6, 400e6)):
            raise ValueError("HBM AXI clock frequency must be 250 MHz or 400 MHz")
        if hbm_axi_clk_freq == 400e6 and (not hbm_axi_domain or sys_clk_freq != 200e6):
            raise ValueError("400 MHz HBM AXI requires a separate domain and 200 MHz SYS")
        self.hbm_axi_clk_freq = hbm_axi_clk_freq
        if hbm_axi_domain and not (self.hbm_primitive and (hbm_bist or hbm_endpoint)):
            raise ValueError("Separate HBM AXI domain requires primitive HBM and BIST or endpoint")
        if hbm_bist and not self.hbm_primitive:
            raise ValueError("HBM burst testing requires the primitive HBM profile")
        if hbm_endpoint_two_port and not (hbm_endpoint and hbm_endpoint_traffic and hbm_axi_domain):
            raise ValueError("Two-port HBM requires endpoint traffic and a separate AXI domain")
        if hbm_endpoint_traffic and not hbm_endpoint:
            raise ValueError("HBM endpoint traffic requires --hbm-endpoint")
        if hbm_endpoint and (not self.hbm_primitive or not hbm_axi_domain or
                hbm_axi_clk_freq != (500e6 if hbm_experimental_500_1000 else 250e6) or hbm_bist):
            raise ValueError("HBM endpoint requires primitive SYS250/AXI250 with a separate AXI domain and no BIST")
        if self.hbm_primitive:
            if not with_hbm:
                raise ValueError("Primitive HBM requires --with-hbm")
            required_sys_clk_freq = 200e6 if hbm_axi_clk_freq == 400e6 else 250e6
            if (sys_clk_freq != required_sys_clk_freq or with_pcie or not with_led_chaser or
                    tuple(parse_usphbm2_channels(hbm_channels)) != (0, 1, 2, 3) or
                    hbm_main_channel != 0 or hbm_base != USPHBM2_DEFAULT_BASE or
                    hbm_high_base != USPHBM2_HIGH_BASE or hbm_strip_origin):
                raise ValueError("Primitive HBM requires SYS250/AXI250 or separate SYS200/AXI400, PC0-3 without PCIe")
            if kwargs.get("cpu_type", "vexriscv") not in (None, "None", "vexriscv"):
                raise ValueError("Primitive HBM supports CPU-less or VexRiscv minimal operation")
            if kwargs.get("cpu_type", "vexriscv") not in (None, "None"):
                if kwargs.get("cpu_variant") not in (None, "minimal"):
                    raise ValueError("HBM firmware requires the uncached VexRiscv minimal variant")
                kwargs["cpu_variant"] = "minimal"
                if kwargs.get("cpu_reset_address") not in (None, 0x10000):
                    raise ValueError("HBM firmware ROM starts at 0x10000")
                kwargs["cpu_reset_address"] = 0x10000
                kwargs["integrated_rom_size"] = 0x10000
                kwargs["integrated_sram_size"] = 0x8000
                if kwargs.get("integrated_main_ram_size", 0):
                    raise ValueError("HBM firmware uses SRAM until external memory is calibrated")
            required = {"bus_standard": "wishbone", "bus_data_width": 32,
                "bus_address_width": 32, "bus_bursting": False, "bus_interconnect": "shared",
                "bus_timeout": int(1e6),
                "csr_data_width": 32, "csr_address_width": 14, "csr_paging": 0x800,
                "with_ctrl": True, "with_timer": True, "with_uart": True}
            for name, value in required.items():
                if kwargs.get(name, value) != value:
                    raise ValueError(f"Primitive HBM requires {name}={value!r}")
            if kwargs.get("bus_addressing") not in (None, "word"):
                raise ValueError("Primitive HBM requires word-addressed Wishbone")
            if kwargs.get("uart_name", "serial") not in ("serial", "crossover"):
                raise ValueError("Primitive HBM requires the crossover UART")
        platform = sqrl_fk33.Platform()
        if with_hbm:
            assert (self.hbm_primitive and hbm_axi_clk_freq == 400e6 and sys_clk_freq == 200e6) or 225e6 <= sys_clk_freq <= 450e6
            hbm_channels = parse_usphbm2_channels(hbm_channels)
            if hbm_main_channel not in hbm_channels:
                raise ValueError("HBM main channel must be one of the mapped HBM channels")
            hbm_origins = usphbm2_channel_origins(hbm_channels, hbm_base, hbm_high_base)
            hbm_end = usphbm2_window_end(hbm_origins)
            if hbm_end > 2**kwargs.get("bus_address_width", 32):
                kwargs["bus_address_width"] = 64
            if hbm_end > 2**pcie_address_width:
                pcie_address_width = 64
            if hbm_end > 2**33:
                hbm_strip_origin = True

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq, with_hbm,
            hbm_ref_clk_freq=200e6 if self.hbm_primitive else 100e6,
            strict_timing=self.hbm_primitive, hbm_axi_domain=hbm_axi_domain,
            hbm_axi_clk_freq=hbm_axi_clk_freq,
            hbm_axi_reset_buf=hbm_experimental_500_1000)
        if hbm_experimental_500_1000:
            _add_hbm_reset_constraints(platform)
            _add_hbm_native_floorplan(platform, reserve_pc31=hbm_endpoint)
        if self.hbm_primitive:
            platform.toolchain.pre_placement_commands += [
                "create_clock -name jtag_tck -period 66.666667 [get_pins BSCANE2/INTERNAL_TCK]",
                "set_clock_groups -asynchronous -group [get_clocks jtag_tck] -group [get_clocks -filter {{NAME != jtag_tck}}]",
            ]
            platform.toolchain.additional_commands += [
                "check_timing -verbose -file {build_name}_check_timing.rpt",
                "report_cdc -details -file {build_name}_cdc.rpt",
                "report_clock_interaction -file {build_name}_clock_interaction.rpt",
            ]
            if hbm_axi_domain:
                gray_filename = "gray-constraints-axi400.tcl" if hbm_axi_clk_freq == 400e6 else "gray-constraints.tcl"
                if hbm_experimental_500_1000:
                    gray_filename = "gray-constraints-axi500.tcl"
                gray_constraints = (Path(__file__).resolve().parents[1] / "software" /
                    "fk33_hbm" / "rebuild" / gray_filename).read_text()
                # Vivado command templates expand braces with str.format.
                platform.toolchain.pre_placement_commands.append(
                    gray_constraints.replace("{", "{{").replace("}", "}}"))
                if hbm_endpoint:
                    endpoint_gray_constraints = """
set endpoint_gray_sources [dict create]
set endpoint_gray_destinations [dict create]
foreach {attribute collection} {hbm_gray_src endpoint_gray_sources hbm_gray_dst endpoint_gray_destinations} {
    foreach cell [get_cells -hierarchical -filter "$attribute =~ hbm_endpoint_pc31_*"] {
        dict lappend $collection [get_property $attribute $cell] $cell
    }
}
if {[dict size $endpoint_gray_sources] != 10 || [dict size $endpoint_gray_destinations] != 10} {
    error "Expected ten endpoint Gray buses"
}
foreach channel {aw w b ar r} {
    foreach direction {produce consume} {
        set id hbm_endpoint_pc31_${channel}_${direction}
        set src [dict get $endpoint_gray_sources $id]
        set dst [dict get $endpoint_gray_destinations $id]
        if {[llength $src] != 3 || [llength $dst] != 3} {error "Incomplete endpoint Gray vector $id"}
        foreach cell $dst {
            if {[string equal -nocase [get_property -quiet mr_ff $cell] true]} {
                error "Endpoint Gray first stage retains generic false path: $id"
            }
        }
        set src_c [get_pins -of_objects $src -filter {REF_PIN_NAME == C}]
        set dst_d [get_pins -of_objects $dst -filter {REF_PIN_NAME == D}]
        if {[llength $src_c] != 3 || [llength $dst_d] != 3} {error "Missing endpoint Gray pins: $id"}
        set_max_delay -datapath_only 4.0 -from $src_c -to $dst_d
        set_bus_skew 4.0 -from $src_c -to $dst_d
    }
}
puts "HBM_ENDPOINT_GRAY_CONSTRAINTS_APPLIED groups=10 bound_ns=4.0"
"""
                    if hbm_endpoint_two_port:
                        endpoint_gray_constraints = endpoint_gray_constraints.replace(
                            '$attribute =~ hbm_endpoint_pc31_*',
                            '$attribute =~ hbm_endpoint_pc31_* || $attribute =~ hbm_endpoint_pc30_*')
                        endpoint_gray_constraints = endpoint_gray_constraints.replace(
                            '!= 10', '!= 20').replace('Expected ten', 'Expected twenty')
                        endpoint_gray_constraints = endpoint_gray_constraints.replace(
                            'foreach channel {aw w b ar r} {',
                            'foreach port {31 30} {\nforeach channel {aw w b ar r} {')
                        endpoint_gray_constraints = endpoint_gray_constraints.replace(
                            'set id hbm_endpoint_pc31_${channel}_${direction}',
                            'set id hbm_endpoint_pc${port}_${channel}_${direction}')
                        endpoint_gray_constraints = endpoint_gray_constraints.replace(
                            'puts "HBM_ENDPOINT_GRAY_CONSTRAINTS_APPLIED groups=10',
                            '}\nputs "HBM_ENDPOINT_GRAY_CONSTRAINTS_APPLIED groups=20')
                    if hbm_experimental_500_1000:
                        endpoint_gray_constraints = endpoint_gray_constraints.replace("4.0", "2.0")
                    platform.toolchain.pre_placement_commands.append(
                        endpoint_gray_constraints.replace("{", "{{").replace("}", "}}"))
                platform.toolchain.additional_commands.append(
                    "report_bus_skew -file {build_name}_bus_skew.rpt")

        # SoCCore ----------------------------------------------------------------------------------
        if kwargs.get("uart_name", "serial") == "serial":
            if kwargs.get("uart_name", "serial") == "serial": kwargs["uart_name"] = "crossover" # Defaults to Crossover-UART.
        kwargs["with_jtagbone"]  = True
        kwargs["jtagbone_chain"] = 2 # Chain 1 already used by HBM2 debug probes.
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on FK33", **kwargs)
        if hbm_experimental_500_1000:
            self.add_constant("HBM_EXPERIMENTAL_500_1000", 1)

        # HBM --------------------------------------------------------------------------------------
        if with_hbm:
            # Add HBM Core.
            if not self.hbm_primitive:
                self.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))
                ensure_hbm_xci("https://github.com/litex-hub/litex-boards/files/8178874/hbm_0.xci.txt")
                add_usphbm2_pseudochannels(
                    soc              = self,
                    hbm              = hbm,
                    channels         = hbm_channels,
                    main_channel     = hbm_main_channel,
                    origins          = hbm_origins,
                    hbm_high_base    = hbm_high_base,
                    hbm_strip_origin = hbm_strip_origin)
            else:
                from litex.soc.cores.ram.xilinx_usp_hbm2 import PrimitiveHBM2
                hbm_clock_options = {"axi_clk_freq": hbm_axi_clk_freq}
                if hbm_experimental_500_1000:
                    hbm_clock_options.update(hbm_clk_freq=1000e6, experimental_500_1000=True)
                if hbm_endpoint:
                    hbm_clock_options["with_native_ready"] = False
                if hbm_axi_clk_freq == 400e6:
                    hbm_clock_options["sys_clk_freq"] = sys_clk_freq
                hbm_core = PrimitiveHBM2(platform, rtl_dir=hbm_primitive_rtl,
                    **hbm_clock_options)
                self.hbm = hbm_core if hbm_axi_domain else ClockDomainsRenamer({"axi": "sys"})(hbm_core)
                axi_domain = "axi" if hbm_axi_domain else "sys"
                runtime_ports = self.hbm.axi
                downstream_idle = [1] * 32
                if hbm_experimental_500_1000:
                    runtime_ports = []
                    for native_channel in range(32):
                        native = axi.AXIInterface(data_width=256, address_width=33, id_width=6)
                        boundary = HBMNativeElasticBoundary(native, self.hbm.axi[native_channel])
                        self.submodules += ClockDomainsRenamer(axi_domain)(boundary)
                        runtime_ports.append(native)
                        downstream_idle[native_channel] = boundary.idle
                    self.add_constant("HBM_NATIVE_ELASTIC_DEPTH", 2)
                # Four RV32 windows select eight groups of four native ports.
                # AXILiteDecoder holds each route through its response, including
                # when a selector write arrives while a request is outstanding.
                # The runtime interposer gates each Wishbone root and drains all
                # 32 native ports before software may change bank or retune HBM.
                self.hbm_window = LiteXModule()
                self.hbm_window.bank = CSRStorage(3, name="bank")
                self.hbm_window.bank_effective = Signal(3, name="hbm_window_bank_effective")
                self.hbm_window.bank_fault = Signal(name="hbm_window_bank_fault")
                all_windows_idle = Signal(name="hbm_all_windows_idle")
                all_host_idle = Signal(name="hbm_all_host_idle") if hbm_endpoint else all_windows_idle
                self.hbm_runtime = HBMRuntimeQuiesce(platform,
                    registered_request=hbm_bist or hbm_endpoint,
                    registered_completions=hbm_bist or hbm_endpoint,
                    clock_domain=axi_domain,
                    sys_host_idle=all_host_idle if hbm_axi_domain else None,
                    registered_status=hbm_experimental_500_1000)
                host_idle_axi = Signal(name="hbm_host_admission_closed_and_idle_axi")
                observed_idle_axi = Signal(name="hbm_host_observed_idle_axi")
                endpoint_host_idle_axi = Signal(name="hbm_endpoint_host_idle_axi") if hbm_endpoint else observed_idle_axi
                if hbm_endpoint:
                    self.comb += endpoint_host_idle_axi.eq(observed_idle_axi &
                        (~self.hbm_runtime.native_quiesce_request | host_idle_axi))
                if hbm_bist:
                    from litex.soc.cores.ram.xilinx_usp_hbm2_bist import HBMBurstBIST
                    self.hbm_bist = HBMBurstBIST(platform,
                        ready=self.hbm.memory_ready_axi if hbm_axi_domain else self.hbm.memory_ready,
                        quiesce_request=self.hbm_runtime.native_quiesce_request,
                        all_drained=self.hbm_runtime.native_all_drained,
                        host_idle=host_idle_axi if hbm_axi_domain else all_windows_idle,
                        clock_domain=axi_domain)
                    self.add_constant("HBM_BIST_CHANNELS", 32)
                host_quiesce = self.hbm_runtime.quiesce_request
                if hbm_endpoint:
                    from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint import HBMWideEndpoint
                    from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint_csr import HBMEndpointCSR
                    self.hbm_transport = HBMWideEndpoint(app_domain="sys", native_domain="axi",
                        gray_bus_prefix="hbm_endpoint_pc31", max_outstanding=4,
                        native_pipe_ready=hbm_experimental_500_1000,
                        registered_quiesce_ack=hbm_experimental_500_1000)
                    # Ownership FIFO RAM is transport state, not CSR memory.
                    self.hbm_transport.autocsr_exclude = {"window"}
                    if hbm_endpoint_traffic:
                        from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint_traffic import HBMEndpointTrafficCSR
                        self.hbm_endpoint = (HBMEndpointTrafficCSR(self.hbm_transport,
                            name_prefix="hbm_endpoint_pc31") if hbm_endpoint_two_port
                            else HBMEndpointTrafficCSR(self.hbm_transport))
                        self.add_constant("HBM_ENDPOINT_TRAFFIC", 1)
                        self.add_constant("HBM_ENDPOINT_RANDOM", 1)
                    else:
                        self.hbm_endpoint = HBMEndpointCSR(self.hbm_transport)
                    endpoint_idle = self.hbm_endpoint.drained & (~host_quiesce | self.hbm_endpoint.quiesced)
                    if hbm_endpoint_two_port:
                        self.hbm_transport30 = HBMWideEndpoint(app_domain="sys", native_domain="axi",
                            gray_bus_prefix="hbm_endpoint_pc30", control_prefix="pc30_", max_outstanding=4,
                            native_pipe_ready=hbm_experimental_500_1000,
                            registered_quiesce_ack=hbm_experimental_500_1000)
                        self.hbm_transport30.autocsr_exclude = {"window"}
                        self.hbm_endpoint30 = HBMEndpointTrafficCSR(self.hbm_transport30,
                            port=30, name_prefix="hbm_endpoint_pc30")
                        self.comb += [
                            self.hbm_endpoint30.admission_enable.eq(self.hbm.memory_ready),
                            self.hbm_endpoint30.stop.eq(host_quiesce),
                            self.hbm_transport30.quiesce.eq(self.hbm_endpoint30.endpoint_quiesce),
                        ]
                        endpoint_idle = endpoint_idle & self.hbm_endpoint30.drained & (
                            ~host_quiesce | self.hbm_endpoint30.quiesced)
                        self.add_constant("HBM_ENDPOINT_TWO_PORT", 1)
                        self.add_constant("HBM_ENDPOINT_SECOND_PORT", 30)
                    self.comb += [
                        self.hbm_endpoint.admission_enable.eq(self.hbm.memory_ready),
                        self.hbm_endpoint.stop.eq(host_quiesce),
                        self.hbm_transport.quiesce.eq(self.hbm_endpoint.endpoint_quiesce),
                        all_host_idle.eq((all_windows_idle & endpoint_idle) if hbm_endpoint_two_port
                            else all_windows_idle & self.hbm_endpoint.drained &
                            (~host_quiesce | self.hbm_endpoint.quiesced)),
                    ]
                    self.add_constant("HBM_ENDPOINT_PORT", 31)
                    self.add_constant("HBM_ENDPOINT_MAX_OUTSTANDING", 4)
                if hbm_bist:
                    host_quiesce = host_quiesce | self.hbm_bist.owner_sys
                    if hbm_axi_domain:
                        host_quiesce = host_quiesce | self.hbm_bist.admission_block_sys
                if hbm_axi_domain:
                    # Roundtrip barrier: stale native idle is insufficient.
                    # This level only rises after SYS admission has closed and
                    # every accepted request AND returned response has retired.
                    self.hbm_host_barrier = HBMHostDrainBarrier(host_quiesce,
                        all_host_idle, self.hbm_bist.command_token_sys if hbm_bist else 0,
                        self.hbm_runtime.request_generation_sys)
                    self.comb += [
                        host_idle_axi.eq(self.hbm_host_barrier.idle_native),
                        observed_idle_axi.eq(self.hbm_host_barrier.observed_idle_native),
                        self.hbm_runtime.host_barrier_generation_native.eq(
                            self.hbm_host_barrier.generation_native),
                    ]
                    if hbm_bist:
                        self.comb += self.hbm_bist.host_barrier_token_native.eq(
                            self.hbm_host_barrier.token_native)
                admission_gates = []
                window_idle_signals = []
                cdc_idle_signals = []
                cdc_fault_signals = []
                for channel in range(4):
                    host   = wishbone.Interface(data_width=32, adr_width=30)
                    gated  = wishbone.Interface(data_width=32, adr_width=30)
                    narrow = axi.AXILiteInterface(data_width=32, address_width=32)
                    native = axi.AXILiteInterface(data_width=32, address_width=33)
                    admission = WishboneAdmissionGate(host, gated,
                        self.hbm.memory_ready, host_quiesce)
                    admission_gates.append(admission)
                    self.submodules += admission
                    if hbm_axi_domain:
                        held = wishbone.Interface(data_width=32, adr_width=30)
                        hold = HBMCPUTransactionLatch(gated, held)
                        self.submodules += hold
                        self.submodules += axi.Wishbone2AXILite(held, narrow)
                    else:
                        self.submodules += axi.Wishbone2AXILite(gated, narrow)
                    window_pipeline = HBMCPUWindowPipeline(narrow, native, bist=hbm_bist,
                        name=f"hbm_window_{channel}",
                        pipe_ready_capture_when_empty=hbm_experimental_500_1000)
                    self.submodules += window_pipeline
                    window_idle_signals.append(window_pipeline.idle & hold.idle
                        if hbm_axi_domain else window_pipeline.idle)
                    ports = []
                    for bank in range(8):
                        # Route only 32-bit data across the bank switch. Width
                        # conversion and registered native channels stay local
                        # to each physical HBM port instead of broadcasting a
                        # 256-bit payload to eight distant ports.
                        port = axi.AXILiteInterface(data_width=32, address_width=33)
                        local = axi.AXILiteInterface(data_width=32, address_width=33)
                        wide = axi.AXILiteInterface(data_width=256, address_width=33)
                        routed = axi.AXILiteInterface(data_width=256, address_width=33)
                        ports.append((lambda address, bank=bank: self.hbm_window.bank_effective == bank, port))
                        if hbm_axi_domain:
                            crossed = axi.AXILiteInterface(data_width=32, address_width=33)
                            crossing = HBMCPUWindowCDC(port, crossed,
                                gray_bus_prefix=f"hbm_cpu_pc{4*bank + channel:02d}",
                                forward_read_pipeline=hbm_experimental_500_1000,
                                reverse_read_pipeline=hbm_experimental_500_1000)
                            self.submodules += crossing
                            cdc_idle_signals.append(crossing.idle)
                            cdc_fault_signals.append(crossing.fault)
                            port = crossed
                        native_channel = 4*bank + channel
                        if hbm_endpoint and (native_channel == 31 or (hbm_endpoint_two_port and native_channel == 30)):
                            # Keep the narrow CPU CDC and its terminal-response
                            # accounting, but never attach CPU traffic to reserved ports.
                            self.submodules += ClockDomainsRenamer(axi_domain)(HBMReservedPortError(port))
                            # The existing HBM wrapper uses an AXI4-shaped
                            # record for the ordered AXI3 subset: no physical
                            # WID pin and only four significant LEN bits.
                            # Preserve endpoint AXI3 and all native data widths.
                            endpoint_native = (self.hbm_transport.native if native_channel == 31
                                else self.hbm_transport30.native)
                            runtime_native = axi.AXIInterface(data_width=256,
                                address_width=33, id_width=6)
                            if native_channel == 31:
                                self.hbm_endpoint_runtime_native = runtime_native
                            else:
                                self.hbm_endpoint30_runtime_native = runtime_native
                            self.submodules += HBMEndpointNativeWiring(endpoint_native, runtime_native)
                            self.hbm_runtime.add_port(native_channel, runtime_native,
                                runtime_ports[native_channel], host_idle=endpoint_host_idle_axi,
                                downstream_idle=downstream_idle[native_channel])
                            continue
                        for name in ("aw", "w", "ar"):
                            self.comb += getattr(port, name).connect(getattr(local, name))
                        self.comb += local.b.connect(port.b)
                        # Register the local lane mux before the narrow bank mux.
                        local_read = stream.Buffer(local.r.description,
                            pipe_valid=True, pipe_ready=True,
                            pipe_ready_capture_when_empty=hbm_experimental_500_1000,
                            pipe_ready_valid_max_fanout=64 if hbm_experimental_500_1000 else None)
                        self.submodules += ClockDomainsRenamer(axi_domain)(local_read)
                        self.comb += local.r.connect(local_read.sink)
                        self.comb += local_read.source.connect(port.r)
                        self.submodules += ClockDomainsRenamer(axi_domain)(axi.AXILiteConverter(local, wide))
                        for name in ("aw", "w", "ar"):
                            request = getattr(wide, name)
                            buffer = stream.Buffer(request.description,
                                pipe_valid=True, pipe_ready=True,
                                pipe_ready_capture_when_empty=hbm_experimental_500_1000,
                                pipe_ready_valid_max_fanout=64 if hbm_experimental_500_1000 else None)
                            self.submodules += ClockDomainsRenamer(axi_domain)(buffer)
                            self.comb += request.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(routed, name))
                        for name in ("b", "r"):
                            response = getattr(routed, name)
                            buffer = stream.Buffer(response.description,
                                pipe_valid=True, pipe_ready=True,
                                pipe_ready_capture_when_empty=hbm_experimental_500_1000,
                                pipe_ready_valid_max_fanout=64 if hbm_experimental_500_1000 else None)
                            self.submodules += ClockDomainsRenamer(axi_domain)(buffer)
                            self.comb += response.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(wide, name))
                        native_channel = 4*bank + channel
                        native_upstream = axi.AXIInterface(data_width=256, address_width=33, id_width=6)
                        tracked_upstream = native_upstream
                        port_idle = endpoint_host_idle_axi if hbm_axi_domain else admission.idle & window_pipeline.idle
                        if hbm_bist:
                            tracked_upstream = axi.AXIInterface(data_width=256, address_width=33, id_width=6)
                            engine_idle = self.hbm_bist.add_port(native_channel,
                                native_upstream, tracked_upstream)
                            port_idle = port_idle & engine_idle
                        self.hbm_runtime.add_port(native_channel, tracked_upstream,
                            runtime_ports[native_channel], host_idle=port_idle,
                            downstream_idle=downstream_idle[native_channel])
                        self.submodules += ClockDomainsRenamer(axi_domain)(axi.AXILite2AXI(routed, native_upstream))
                    self.submodules += axi.AXILiteDecoder(native, ports)
                    self.bus.add_slave(f"hbm{channel}", host,
                        SoCRegion(origin=hbm_origins[channel], size=0x1000_0000, cached=True))
                self.bus.add_region("main_ram", SoCRegion(
                    origin=hbm_origins[0], size=0x1000_0000, linker=True))
                self.add_constant("HBM_CHANNELS", 32)
                self.add_constant("HBM_WINDOW_CHANNELS", 4)
                self.add_constant("HBM_WINDOW_BANKS", 8)
                self.add_constant("HBM_MAIN_CHANNEL", 0)
                self.add_constant("HBM_HIGH_BASE", hbm_high_base)
                if hbm_axi_domain:
                    self.add_constant("HBM_AXI_CLOCK_DOMAIN", 1)
                    self.add_constant("HBM_AXI_CLK_FREQ", int(hbm_axi_clk_freq))
                self.comb += all_windows_idle.eq(Reduce("AND",
                    [admission_gates[index].idle & window_idle_signals[index]
                        for index in range(4)] + cdc_idle_signals))
                bank_change = (self.hbm_window.bank.wr_stb &
                    (self.hbm_window.bank.storage != self.hbm_window.bank_effective))
                bank_change_safe = (all_windows_idle & self.hbm_runtime.all_drained &
                    (self.hbm_runtime.error.status == 0) & ~self.hbm_window.bank_fault)
                if hbm_axi_domain:
                    # A requested STOP must be acknowledged without native faults.
                    bank_change_safe = bank_change_safe & (
                        ~self.hbm_runtime.quiesce_request | self.hbm_runtime.quiesced)
                if hbm_bist:
                    bank_change_safe = bank_change_safe & ~self.hbm_bist.owner_sys
                self.sync += If(bank_change,
                    If(bank_change_safe,
                        self.hbm_window.bank_effective.eq(self.hbm_window.bank.storage),
                    ).Else(
                        self.hbm_window.bank_fault.eq(1),
                    ),
                )
                external_fault = self.hbm_window.bank_fault
                if hbm_axi_domain:
                    external_fault = external_fault | Reduce("OR", cdc_fault_signals)
                if hbm_bist:
                    external_fault = external_fault | (self.hbm_bist.error_mask_sys != 0)
                if hbm_endpoint:
                    external_fault = external_fault | self.hbm_transport.protocol_error
                    if hbm_endpoint_two_port:
                        external_fault = external_fault | self.hbm_transport30.protocol_error
                self.comb += self.hbm_runtime.external_fault.eq(external_fault)

        # PCIe -------------------------------------------------------------------------------------
        if with_pcie:
            assert self.csr_data_width == 32
            self.pcie_phy = USPHBMPCIEPHY(platform, platform.request(f"pcie_x{pcie_lanes}"),
                data_width = {4: 128, 8: 256, 16: 512}[pcie_lanes],
                bar0_size  = 0x20000)
            self.add_pcie(
                phy              = self.pcie_phy,
                ndmas            = pcie_ndmas,
                address_width    = pcie_address_width,
                with_dma_status  = with_pcie_dma_status,
                with_dma_monitor = with_pcie_dma_monitor)

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq)

        if self.hbm_primitive:
            for name, location in (("ctrl", 0), ("hbm", 1), ("identifier_mem", 2),
                    ("leds", 3), ("timer0", 4), ("uart", 5), ("hbm_control", 6),
                    ("hbm_window", 7), ("hbm_runtime", 8)):
                if name in self.csr.locs and self.csr.locs[name] != location:
                    raise ValueError(f"Primitive HBM requires CSR bank {name} at {location}")
                self.csr.add(name, location, use_loc_if_exists=True)
            if hbm_bist:
                self.csr.add("hbm_bist", 9, use_loc_if_exists=True)
            if hbm_endpoint:
                self.csr.add("hbm_endpoint", 9, use_loc_if_exists=True)
                if hbm_endpoint_two_port:
                    self.csr.add("hbm_endpoint30", 10, use_loc_if_exists=True)

    def _finalize_bus(self):
        if not (self.hbm_primitive and (hasattr(self, "hbm_bist") or hasattr(self, "hbm_endpoint"))):
            return super()._finalize_bus()
        # The primitive-HBM BIST CSR bank is reached through the JTAG Wishbone
        # master. Register this bridge to split the address/decode path before
        # it reaches the CSR read mux. The CSR map remains unchanged.
        csr_origin = self.mem_map.get("csr", None)
        if csr_origin is None:
            self.logger.error("CSR main-bus origin is not defined.")
            raise SoCError()
        self.add_csr_bridge(name="csr", origin=csr_origin, with_register=True, read_latency=2)
        self.bus.finalize()
        if hasattr(self, "ctrl") and self.bus.timeout is not None:
            if hasattr(self.ctrl, "bus_error") and hasattr(self.bus._interconnect, "timeout"):
                self.comb += self.ctrl.bus_error.eq(self.bus._interconnect.timeout.error)
        self.add_config("BUS_STANDARD",      self.bus.standard)
        self.add_config("BUS_DATA_WIDTH",    self.bus.data_width)
        self.add_config("BUS_ADDRESS_WIDTH", self.bus.address_width)
        self.add_config("BUS_BURSTING",      int(self.bus.bursting))

    def _finalize_csr(self):
        if not self.hbm_primitive:
            return super()._finalize_csr()
        from litex.soc.cores.ram.xilinx_usp_hbm2 import MANAGEMENT_REGISTERS
        # The control RTL already decodes bank 6. Attach its raw CSR bus to
        # the normal interconnect, exporting metadata without another decoder.
        if any(isinstance(x, (CSR, CSRStatus, CSRStorage)) for x in self.__dict__.values()):
            raise ValueError("Primitive HBM raw CSR hook does not support top-level CSRs")
        if (self.csr.data_width, self.csr.address_width, self.csr.alignment,
                self.csr.paging) != (32, 14, 32, 0x800):
            raise ValueError("Primitive HBM requires CSR32/14, 32-bit alignment and 0x800 paging")
        origin = self.bus.regions["csr"].origin
        self.csr_bankarray = csr_bus.CSRBankArray(self,
            address_map=self.csr.address_map, data_width=self.csr.data_width,
            address_width=self.csr.address_width, alignment=self.csr.alignment,
            paging=self.csr.paging, ordering=self.csr.ordering,
            read_pipeline=hasattr(self, "hbm_bist") or hasattr(self, "hbm_endpoint"))
        self.csr_interconnect = csr_bus.InterconnectShared(
            list(self.csr.masters.values()), self.csr_bankarray.get_buses() + [self.hbm.control_bus])
        for name, registers, address, bank in self.csr_bankarray.banks:
            self.csr.add_region(name, SoCCSRRegion(origin + address*0x800, 32, registers))
        for name, memory, address, bank in self.csr_bankarray.srams:
            self.csr.add_region(name + "_" + memory.name_override, SoCCSRRegion(origin + address*0x800, 32, memory))
        metadata = [MockCSR(name, 1, "ro" if access == "ro" else "rw", 32)
                    for name, access in MANAGEMENT_REGISTERS]
        self.csr.add_region("hbm_control", SoCCSRRegion(origin + 0x3000, 32, metadata))
        self.csr.regions = dict(sorted(self.csr.regions.items(), key=lambda item: item[1].origin))
        for name, constant in self.csr_bankarray.constants:
            self.add_constant(name + "_" + constant.name, constant.value.value)

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=sqrl_fk33.Platform, description="LiteX SoC on FK33.")
    parser.add_target_argument("--sys-clk-freq", default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--with-pcie",    action="store_true",       help="Enable PCIe support.")
    parser.add_target_argument("--pcie-lanes",   default=4, type=int,       choices=[4, 8, 16], help="PCIe lane count.")
    parser.add_target_argument("--pcie-ndmas",   default=1, type=int,       help="Number of PCIe DMA channels.")
    parser.add_target_argument("--pcie-address-width", default=32, type=int, choices=[32, 64], help="PCIe address width.")
    parser.add_target_argument("--pcie-with-dma-status",  action="store_true", help="Enable PCIe DMA status CSRs.")
    parser.add_target_argument("--pcie-with-dma-monitor", action="store_true", help="Enable PCIe DMA monitor CSRs.")
    parser.add_target_argument("--with-hbm",              action="store_true", help="Use HBM2.")
    parser.add_target_argument("--hbm-primitive", action="store_true", help="Use bundled primitive HBM and processor calibration firmware.")
    parser.add_target_argument("--hbm-primitive-rtl", help="Override the bundled primitive/control RTL directory.")
    parser.add_target_argument("--hbm-bist", action="store_true", help="Add exclusive per-channel burst test masters and counters.")
    parser.add_target_argument("--hbm-endpoint-two-port", action="store_true", help="Add independent PC30 traffic alongside PC31 (requires endpoint traffic and AXI domain).")
    parser.add_target_argument("--hbm-endpoint-traffic", action="store_true", help="Use processor-configured RTL traffic generation on the dedicated PC31 endpoint.")
    parser.add_target_argument("--hbm-endpoint", action="store_true", help="Dedicate PC31 to the processor-driven four-command wide endpoint.")
    parser.add_target_argument("--hbm-axi-domain", action="store_true", help="Independent HBM AXI clock domain (requires primitive BIST or endpoint).")
    parser.add_target_argument("--hbm-experimental-500-1000", action="store_true", help="Unqualified AXI500/HBM1000 overclock; native model remains 1800 MT/s.")
    parser.add_target_argument("--hbm-axi-clk-freq", default=250e6, type=float, choices=[250e6, 400e6, 500e6], help="HBM AXI clock frequency; 500 MHz requires --hbm-experimental-500-1000.")
    parser.add_target_argument("--hbm-channels", default="0,1,2,3",         help="HBM channels to map (comma/range list or all).")
    parser.add_target_argument("--hbm-main-channel", default=0, type=int,   help="Mapped HBM channel used as main RAM.")
    parser.add_target_argument("--hbm-base",       default=USPHBM2_DEFAULT_BASE,
        type=lambda x: int(x, 0), help="HBM bus base address.")
    parser.add_target_argument("--hbm-high-base",  default=USPHBM2_HIGH_BASE,
        type=lambda x: int(x, 0), help="HBM bus base for channels above the low 32-bit cached window.")
    parser.add_target_argument("--hbm-strip-origin", action="store_true",   help="Expose each mapped HBM channel with local AXI addresses.")
    parser.add_target_argument("--driver",       action="store_true",       help="Generate PCIe driver.")
    args = parser.parse_args()
    if args.pcie_ndmas < 0:
        parser.error("--pcie-ndmas must be >= 0")
    try:
        hbm_channels = parse_usphbm2_channels(args.hbm_channels)
    except ValueError as e:
        parser.error(str(e))

    soc = BaseSoC(
        sys_clk_freq          = args.sys_clk_freq,
        with_pcie             = args.with_pcie,
        pcie_lanes            = args.pcie_lanes,
        pcie_ndmas            = args.pcie_ndmas,
        pcie_address_width    = args.pcie_address_width,
        with_pcie_dma_status  = args.pcie_with_dma_status,
        with_pcie_dma_monitor = args.pcie_with_dma_monitor,
        with_hbm              = args.with_hbm,
        hbm_channels          = hbm_channels,
        hbm_main_channel      = args.hbm_main_channel,
        hbm_base              = args.hbm_base,
        hbm_high_base         = args.hbm_high_base,
        hbm_strip_origin      = args.hbm_strip_origin,
        hbm_primitive         = args.hbm_primitive,
        hbm_primitive_rtl     = args.hbm_primitive_rtl,
        hbm_bist              = args.hbm_bist,
        hbm_endpoint          = args.hbm_endpoint,
        hbm_endpoint_traffic  = args.hbm_endpoint_traffic,
        hbm_endpoint_two_port = args.hbm_endpoint_two_port,
        hbm_axi_domain        = args.hbm_axi_domain,
        hbm_axi_clk_freq      = args.hbm_axi_clk_freq,
        hbm_experimental_500_1000 = args.hbm_experimental_500_1000,
        **parser.soc_argdict
    )
    builder = Builder(soc, **parser.builder_argdict)
    if args.build or args.driver:
        if soc.hbm_primitive and soc.cpu_type is not None:
            from litex.soc.integration.export import get_csr_json
            from litex_boards.software.fk33_hbm.build import build
            soc.finalize()
            import json
            layout = json.loads(get_csr_json(soc=soc, csr_regions=soc.csr.regions,
                constants=soc.constants, mem_regions=soc.bus.regions))
            firmware_options = {}
            if soc.hbm_experimental_500_1000:
                firmware_options["profile_path"] = soc.hbm.rtl_dir / "fk33_hbm_profile_1000_experimental.json"
                firmware_options["experimental_500_1000"] = True
            firmware = build(os.path.join(builder.output_dir, "software", "fk33_hbm"), csr_json=layout,
                **firmware_options)
            soc.init_rom("rom", firmware["rom_words"], auto_size=False)
            soc.integrated_rom_initialized = True
            builder.compile_software = False
        if not args.build:
            builder.compile_software = False
            builder.compile_gateware = False
        builder.build(**parser.toolchain_argdict)

    if args.driver:
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))

if __name__ == "__main__":
    main()
