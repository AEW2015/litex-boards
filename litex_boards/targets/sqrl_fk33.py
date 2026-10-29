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
        if {$ref in {RAM32M RAM32M16 RAM32X1D}} {dict set typed_macros $parent $parent}
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


def _add_hbm_status_mailbox_constraints(platform):
    # The runtime status payload is held until its synchronized acknowledgement.
    # Only this 98-bit bundle receives a finite datapath bound. Vivado also
    # excludes its hold checks; this is a protocol exception, not a hold pass.
    commands = r"""
# Exact runtime status bundle only. This procedure does not alter any reset,
# Gray, native-HBM or other-mailbox constraint. DPO automatically waives HOLD.
proc fk33_status_one {objects label} {
    if {[llength $objects] != 1} {error "$label requires one object: $objects"}
    return [lindex $objects 0]
}
proc fk33_status_pin {cell name} {
    return [fk33_status_one [get_pins -of_objects $cell -filter "REF_PIN_NAME == $name"] "$cell/$name"]
}
proc fk33_status_driver {pin} {
    return [fk33_status_one [get_pins -leaf -of_objects [get_nets -segments -of_objects $pin] -filter {DIRECTION == OUT}] "Driver $pin"]
}
proc fk33_status_constant {pin type} {
    set driver [fk33_status_driver $pin]
    if {[get_property REF_NAME [get_cells -of_objects $driver]] ne $type} {error "$pin requires $type"}
}
proc fk33_status_ff {cell period {initial 0} {reference FDRE}} {
    if {[get_property REF_NAME $cell] ne $reference} {error "Unexpected FF $cell"}
    set init [string tolower [get_property INIT $cell]]
    if {$initial == 0 && $init ni {0 1'b0 1'h0 1'd0 0x0}} {error "INIT must be zero $cell/$init"}
    if {$initial == 1 && $init ni {1 1'b1 1'h1 1'd1 0x1}} {error "INIT must be one $cell/$init"}
    set c [fk33_status_pin $cell C]
    set clock [fk33_status_one [get_clocks -of_objects $c] "Clock $cell"]
    if {[get_property PERIOD $clock] != $period || [get_property IS_INVERTED $c]} {error "Unexpected clock $cell/$clock"}
    set expected_clock [dict get {2.0 main_clkout3 4.0 main_clkout0} $period]
    if {[get_property NAME $clock] ne $expected_clock} {error "Wrong mailbox clock identity $cell/$clock"}
    set reset_pin [fk33_status_pin $cell [expr {$reference eq "FDPE" ? "PRE" : "R"}]]
    if {[get_property IS_INVERTED $reset_pin]} {error "Inverted mailbox reset $cell/$reset_pin"}
    return $clock
}
proc fk33_status_sync {cell period} {
    fk33_status_ff $cell $period
    if {[string toupper [get_property ASYNC_REG $cell]] ni {TRUE 1}} {error "Missing ASYNC_REG $cell"}
    fk33_status_constant [fk33_status_pin $cell CE] VCC
    fk33_status_constant [fk33_status_pin $cell R] GND
}
proc fk33_status_q_driver {pin} {
    set seen {}
    for {set depth 0} {$depth < 8} {incr depth} {
        set driver [fk33_status_driver $pin]
        set cell [fk33_status_one [get_cells -of_objects $driver] "Data driver cell"]
        if {[get_property REF_NAME $cell] eq "FDRE"} {
            if {[get_property REF_PIN_NAME $driver] ne "Q"} {error "Not an electrical Q driver: $driver"}
            return [list $cell $driver $seen]
        }
        set init [string tolower [get_property -quiet INIT $cell]]
        if {[get_property REF_NAME $cell] ne "LUT1" || ![regexp {^([0-9]+'h0*2|0x0*2|2|2'b10)$} $init]} {error "Unexpected data transform $cell/$init"}
        lappend seen $cell
        set pin [fk33_status_pin $cell I0]
    }
    error "Data driver depth exceeded"
}

# Bound the actual combinational function, preserving repeated-pin correlation.
# A LUT's INIT is not an identity whitelist; its electrical input aliases matter.
proc fk33_status_init {text} {
    if {![regexp -nocase {^[0-9]+'([hbd])([0-9a-f]+)$} $text ignored radix digits]} {error "Unsupported LUT INIT $text"}
    set radix [string tolower $radix]
    if {$radix eq "h"} {scan $digits %x result} elseif {$radix eq "b"} {
        if {![regexp {^[01]+$} $digits]} {error "Invalid binary INIT"}
        set result [expr 0b$digits]
    } else {if {![regexp {^[0-9]+$} $digits]} {error "Invalid decimal INIT"}; scan $digits %d result}
    return $result
}
proc fk33_status_expr {pin {depth 0}} {
    if {$depth > 8} {error "Mailbox combinational depth exceeds eight"}
    set driver [fk33_status_driver $pin]
    set cell [fk33_status_one [get_cells -of_objects $driver] "Boolean driver"]
    set reference [get_property REF_NAME $cell]
    set output [get_property REF_PIN_NAME $driver]
    if {$reference eq "GND" || $reference eq "VCC"} {
        set expression [list constant [expr {$reference eq "VCC"}]]
    } elseif {([string match FD* $reference] && $output eq "Q") || ($reference eq "BUFGCE" && $output eq "O")} {
        set expression [list leaf $driver]
    } else {
        if {![regexp {^LUT([1-6])$} $reference ignored width] || $output ne "O"} {error "Unsupported Boolean driver $cell/$reference/$output"}
        set inputs {}
        for {set bit 0} {$bit < $width} {incr bit} {
            lappend inputs [fk33_status_expr [fk33_status_pin $cell I$bit] [expr {$depth+1}]]
        }
        set expression [list lut [fk33_status_init [get_property INIT $cell]] $inputs]
    }
    set inversion [get_property IS_INVERTED $pin]
    if {$inversion ni {0 1}} {error "Unknown pin polarity $pin/$inversion"}
    if {$inversion} {set expression [list not $expression]}
    return $expression
}
proc fk33_status_leaves {expression} {
    lassign $expression kind value inputs
    if {$kind eq "leaf"} {return [list $value]}
    if {$kind eq "constant"} {return {}}
    if {$kind eq "not"} {return [fk33_status_leaves $value]}
    set result {}
    foreach input $inputs {lappend result {*}[fk33_status_leaves $input]}
    return [lsort -unique $result]
}
proc fk33_status_eval {expression state} {
    lassign $expression kind value inputs
    if {$kind eq "leaf"} {return [dict get $state $value]}
    if {$kind eq "constant"} {return $value}
    if {$kind eq "not"} {return [expr {![fk33_status_eval $value $state]}]}
    set index 0
    set bit 0
    foreach input $inputs {set index [expr {$index | ([fk33_status_eval $input $state] << $bit)}]; incr bit}
    return [expr {($value >> $index) & 1}]
}
proc fk33_status_assign {leaves number} {
    set state [dict create]
    set bit 0
    foreach leaf $leaves {dict set state $leaf [expr {($number >> $bit) & 1}]; incr bit}
    return $state
}
proc fk33_status_control_source {cell period} {
    set candidates {}
    foreach role {D CE R} {
        foreach leaf [fk33_status_leaves [fk33_status_expr [fk33_status_pin $cell $role]]] {
            set owner [fk33_status_one [get_cells -of_objects $leaf] "Control source"]
            if {[get_property REF_NAME $owner] ne "FDRE" || [string toupper [get_property ASYNC_REG $owner]] ni {1 TRUE}} {continue}
            set clocks [get_clocks -of_objects [fk33_status_pin $owner C]]
            if {[llength $clocks]==1 && [get_property PERIOD $clocks]==$period} {lappend candidates $owner}
        }
    }
    return [fk33_status_one [lsort -unique $candidates] "Actual control synchronizer source"]
}
proc fk33_status_control_transition {cell expected invert} {
    # Existing typed/INIT/clock/reset-root checks run before this proof.
    set q [fk33_status_pin $cell Q]
    set reset [fk33_status_driver [fk33_status_pin $cell R]]
    foreach role {D CE R} {set function($role) [fk33_status_expr [fk33_status_pin $cell $role]]}
    set leaves [list $q $expected $reset]
    foreach role {D CE R} {lappend leaves {*}[fk33_status_leaves $function($role)]}
    set leaves [lsort -unique $leaves]
    if {[llength $leaves] > 8} {error "Mailbox Boolean leaf budget exceeded $cell/$leaves"}
    for {set row 0} {$row < (1 << [llength $leaves])} {incr row} {
        set state [fk33_status_assign $leaves $row]
        set d [fk33_status_eval $function(D) $state]
        set ce [fk33_status_eval $function(CE) $state]
        set r [fk33_status_eval $function(R) $state]
        set actual [expr {$r ? 0 : ($ce ? $d : [dict get $state $q])}]
        set wanted [expr {[dict get $state $reset] ? 0 : ([dict get $state $expected] ^ $invert)}]
        if {$actual != $wanted} {error "Mailbox transition differs $cell/$state: $actual != $wanted"}
    }
    return [dict create status PASS_COMPLETE_FDRE_TRANSITION leaves $leaves assignments $row]
}

proc fk33_status_sync_next {cell period} {
    set candidates {}
    set q [fk33_status_pin $cell Q]
    foreach pin [get_pins -leaf -of_objects [get_nets -segments -of_objects $q] -filter {DIRECTION == IN && REF_PIN_NAME == D}] {
        set target [fk33_status_one [get_cells -of_objects $pin] "Synchronizer load"]
        if {[get_property REF_NAME $target] eq "FDRE" && [string toupper [get_property ASYNC_REG $target]] in {TRUE 1}} {
            set clock [get_clocks -of_objects [fk33_status_pin $target C]]
            if {[llength $clock] == 1 && [get_property PERIOD $clock] == $period} {lappend candidates $target}
        }
    }
    return [fk33_status_one $candidates "Synchronizer next stage"]
}
proc fk33_status_enable {pin a b value} {
    set driver [fk33_status_driver $pin]
    set lut [fk33_status_one [get_cells -of_objects $driver] "Capture enable LUT"]
    set init [string tolower [get_property -quiet INIT $lut]]
    set pattern [format {^([0-9]+'h0*%s|0x0*%s|%s)$} $value $value $value]
    if {[get_property REF_NAME $lut] ne "LUT2" || ![regexp $pattern $init]} {error "Capture enable is not expected two-input function $lut/$init"}
    set inputs [list [fk33_status_driver [fk33_status_pin $lut I0]] [fk33_status_driver [fk33_status_pin $lut I1]]]
    if {[lsort $inputs] ne [lsort [list $a $b]]} {error "Capture enable inputs differ: $inputs"}
}
proc fk33_status_reset_root {pin period} {
    set q [fk33_status_driver $pin]
    set cell [fk33_status_one [get_cells -of_objects $q] "Reset driver"]
    if {[get_property REF_NAME $cell] eq "BUFGCE"} {
        fk33_status_constant [fk33_status_pin $cell CE] VCC
        set q [fk33_status_driver [fk33_status_pin $cell I]]
        set cell [fk33_status_one [get_cells -of_objects $q] "Buffered reset source"]
    }
    set stage1 $cell
    fk33_status_ff $stage1 $period 1 FDPE
    set q0 [fk33_status_driver [fk33_status_pin $stage1 D]]
    set stage0 [fk33_status_one [get_cells -of_objects $q0] "Reset first stage"]
    fk33_status_ff $stage0 $period 1 FDPE
    foreach cell [list $stage0 $stage1] {
        fk33_status_constant [fk33_status_pin $cell CE] VCC
        if {[string toupper [get_property ASYNC_REG $cell]] ni {TRUE 1}} {error "Missing ASYNC_REG reset stage $cell"}
    }
    fk33_status_constant [fk33_status_pin $stage0 D] GND
    set pre0 [fk33_status_driver [fk33_status_pin $stage0 PRE]]
    set pre1 [fk33_status_driver [fk33_status_pin $stage1 PRE]]
    if {$pre0 ne $pre1} {error "Reset stages do not share assertion source"}
    return [list $pre0 $stage0 $stage1 $q]
}
proc fk33_status_mailbox_constraints {{apply 0}} {
    set prefix main_hbmruntimequiesce_hbm_status_snapshot_hbmclockmailbox
    set index [dict create]
    foreach cell [get_cells -hier -filter "NAME =~ $prefix* && REF_NAME =~ FD*"] {dict set index [get_property NAME $cell] $cell}
    set ack [dict get $index ${prefix}_acknowledge_reg]
    fk33_status_ff $ack 4.0
    set req1 [fk33_status_control_source $ack 4.0]
    lassign [fk33_status_q_driver [fk33_status_pin $req1 D]] req0 ignored ignored
    lassign [fk33_status_q_driver [fk33_status_pin $req0 D]] request ignored ignored
    fk33_status_ff $request 2.0
    foreach cell [list $req0 $req1] {fk33_status_sync $cell 4.0}
    set ack0 [fk33_status_sync_next $ack 2.0]
    set ack1 [fk33_status_sync_next $ack0 2.0]
    foreach cell [list $ack0 $ack1] {fk33_status_sync $cell 2.0}
    if {[llength [lsort -unique [list $req0 $req1 $ack0 $ack1 $request $ack]]] != 6} {error "Mailbox control stages alias"}
    set source_reset [fk33_status_driver [fk33_status_pin $request R]]
    set destination_reset [fk33_status_driver [fk33_status_pin $ack R]]
    set source_root [fk33_status_reset_root [fk33_status_pin $request R] 2.0]
    set destination_root [fk33_status_reset_root [fk33_status_pin $ack R] 4.0]
    if {[lindex $source_root 0] ne [lindex $destination_root 0]} {error "Mailbox domains do not share reset assertion source"}
    set request_q [fk33_status_pin $request Q]
    set ack1_q [fk33_status_pin $ack1 Q]
    set req1_q [fk33_status_pin $req1 Q]
    set ack_q [fk33_status_pin $ack Q]
    set request_transition [fk33_status_control_transition $request $ack1_q 1]
    set acknowledge_transition [fk33_status_control_transition $ack $req1_q 0]
    set rows {}
    set commands {}
    for {set bit 0} {$bit < 98} {incr bit} {
        set source [dict get $index [format {%s_held_reg[%d]} $prefix $bit]]
        set destination [dict get $index [format {%s_output_reg[%d]} $prefix $bit]]
        fk33_status_ff $source 2.0
        fk33_status_ff $destination 4.0
        if {[fk33_status_driver [fk33_status_pin $source R]] ne $source_reset || [fk33_status_driver [fk33_status_pin $destination R]] ne $destination_reset} {error "Data/control reset mismatch bit $bit"}
        fk33_status_enable [fk33_status_pin $source CE] $request_q $ack1_q 9
        fk33_status_enable [fk33_status_pin $destination CE] $req1_q $ack_q 6
        set to [fk33_status_pin $destination D]
        lassign [fk33_status_q_driver $to] actual q buffers
        if {$actual ne $source || $q ne [fk33_status_pin $source Q]} {error "Data is not corresponding held bit $bit: $actual/$q"}
        set from [fk33_status_pin $source C]
        lappend commands [list set_max_delay -datapath_only 4.0 -from $from -to $to]
        lappend rows [list bit $bit source $source destination $destination electrical_Q $q identity_delay_cells $buffers max_ns 4.0 hold AUTOMATICALLY_EXCEPTED_NOT_STA_PASS]
    }
    if {$apply} {foreach command $commands {{*}$command}}
    return [dict create status PASS_EXACT_STATUS_MAILBOX_SCOPE_ONLY rows $rows commands $commands applied $apply req_sync [list $req0 $req1] ack_sync [list $ack0 $ack1] request $request acknowledge $ack source_reset $source_root destination_reset $destination_root hold AUTOMATICALLY_EXCEPTED_NOT_STA_PASS other_four_mailboxes UNCHANGED_FULLY_TIMED request_transition $request_transition acknowledge_transition $acknowledge_transition]
}

set fk33_status_mailbox_policy [fk33_status_mailbox_constraints 1]
if {[dict get $fk33_status_mailbox_policy status] ne "PASS_EXACT_STATUS_MAILBOX_SCOPE_ONLY" ||
    ![dict get $fk33_status_mailbox_policy applied] ||
    [llength [dict get $fk33_status_mailbox_policy rows]] != 98} {
    error "Runtime status mailbox constraint scope differs"
}
puts "FK33_STATUS_MAILBOX_BUNDLE pairs=98 max_ns=4.0 hold=EXCEPTED_BUNDLED_PROTOCOL other_mailboxes=UNCHANGED"
"""
    commands = commands.replace("{", "{{").replace("}", "}}")
    platform.toolchain.pre_placement_commands.append(commands)


def _add_hbm_native_floorplan(platform, reserve_pc31=False, tracker_tags=False):
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
# Retained error-net metadata identifies physical PCs even when synthesis
# rebuilds the hierarchy without AXI pins. Never infer PCs from instance order.
set tagged_trackers [dict create]
if {$fk33_tracker_tags} {
    set tagged_ports [dict create]
    # A text match includes PC0; numeric comparison with empty excludes it.
    foreach net [get_nets -hierarchical -filter {HBM_TRACKER_PC =~ ?*}] {
        set pc [get_property HBM_TRACKER_PC $net]
        if {![regexp {^(0|[1-9][0-9]*)$} $pc] || $pc > 31} {error "Invalid tracker PC tag: $net/$pc"}
        set segments [get_nets -segments $net]
        set q [fk33_floorplan_one [get_pins -leaf -of_objects $segments -filter {DIRECTION == OUT}] "PC$pc error source"]
        set source [fk33_floorplan_one [get_cells -of_objects $q] "PC$pc error register"]
        if {![string match FD* [get_property REF_NAME $source]] || [get_property REF_PIN_NAME $q] ne "Q"} {error "PC$pc tracker tag is not register-Q driven"}
        if {![regexp {(^|/)error_reg$} $source]} {error "PC$pc tag does not identify tracker error state: $source"}
        set tracker [get_property PARENT $source]
        set owner [fk33_floorplan_one [get_cells -hierarchical -filter "NAME == $tracker"] "PC$pc tracker"]
        if {![string match fk33_hbm_quiesce* [get_property REF_NAME $owner]]} {error "PC$pc error source has no tracker owner"}
        if {[dict exists $tagged_ports $pc] && [dict get $tagged_ports $pc] ne $tracker} {error "Multiple trackers tagged PC$pc"}
        if {[dict exists $tagged_trackers $tracker] && [dict get $tagged_trackers $tracker] ne $pc} {error "Tracker has conflicting PC tags: $tracker"}
        dict set tagged_ports $pc $tracker
        dict set tagged_trackers $tracker $pc
    }
    if {[dict size $tagged_ports] != 32} {error "Tracker locality requires all 32 physical PC tags"}
}
# The untagged flow retains discovery through the native AW/AR ready inputs.
set tracker_predicates {}
set tracker_ports {}
set trackers [get_cells -hierarchical -filter {REF_NAME =~ fk33_hbm_quiesce* && IS_PRIMITIVE == 0}]
foreach tracker $trackers {
    if {$fk33_tracker_tags} {
        if {![dict exists $tagged_trackers $tracker]} {error "Untagged retained tracker: $tracker"}
        set pc [dict get $tagged_trackers $tracker]
    } else {
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
    }
    if {$pc in $tracker_ports} {error "Duplicate native tracker for PC$pc"}
    lappend tracker_ports $pc
    set region [dict get $fk33_hard_regions $pc]
    set members [get_cells -hierarchical -filter "REF_NAME =~ FD* && NAME =~ $tracker/*"]
    if {$fk33_tracker_tags} {
        set owned {}
        foreach cell $members {
            # Rebuilt hierarchy may contain unrelated CPU buffer registers.
            set leaf [string range $cell [expr {[string length $tracker] + 1}] end]
            if {![regexp {^(reads|addresses|writes_started|writes_completed|ar_held|aw_held|w_held|w_active|r_pending|b_pending|error)_reg(_rep(lica)?(__?[0-9]+)?)?(\[[0-9]+\])?(_rep(lica)?(__?[0-9]+)?)?$} $leaf]} {
                puts "FK33_NATIVE_TRACKER_EXCLUDED pc=$pc cell=$cell reason=foreign_or_unreviewed_state"
                continue
            }
            lappend owned $cell
        }
        set members $owned
    }
    if {![llength $members]} {error "$tracker has no retained state"}
    foreach cell $members {
        set clock_pin [fk33_cpu_pin $cell C]
        if {![dict exists $fk33_cpu_clock_pins $clock_pin]} {error "$tracker state is not native-clocked: $cell"}
        if {[dict exists $cell_regions $cell] && [dict get $cell_regions $cell] ne $region} {error "Tracker storage crosses native regions: $cell"}
        dict set cell_regions $cell $region
        dict lappend region_members $region $cell
        lappend tracker_predicates $cell
    }
    puts "FK33_NATIVE_TRACKER_LOCALITY tracker=$tracker pc=$pc registers=[llength $members]"
}
if {[llength $tracker_ports] != 32} {
    if {$fk33_tracker_tags} {error "Not all physical trackers were bound"}
    puts "FK33_NATIVE_TRACKER_LOCALITY_PENDING retained_hierarchies=[llength $trackers] bound_ports=[llength $tracker_ports]"
}
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
# Vivado assigns RAM32M, RAM32M16 and RAM32X1D atomically. Check the parent and the
# complete indexed sibling cohort together, once per macro, after assignment.
dict for {macro children} $fk33_cpu_macro_children {
    set expected [list fk33_native_[dict get $cell_regions $macro]]
    set objects [get_cells [concat [list $macro] $children]]
    set actual [lsort -unique [get_pblocks -quiet -of_objects $objects]]
    if {$actual ne $expected} {error "RAM macro effective membership differs: $macro/$actual/$expected"}
}
puts "FK33_NATIVE_FLOORPLAN ports=32 boundaries=32 permits=64"
"""
    commands = (_hbm_cpu_locality_commands() +
        f"\nset fk33_reserve_pc31 {int(reserve_pc31)}\nset fk33_tracker_tags {int(tracker_tags)}\n" + commands)
    commands = commands.replace("{", "{{").replace("}", "}}")
    platform.toolchain.pre_placement_commands.append(commands)
    platform.toolchain.pre_routing_commands.append("fk33_check_native_floorplan_anchors")


def _add_hbm_reference_placement(platform):
    # Optional source-shape preset measured on FK33; not a general placement policy.
    # Literal FF copies must match the reviewed configuration. No alias fallback.
    platform.toolchain.pre_synthesis_commands.append("set_param synth.maxThreads 1")
    commands = r"""
# Twenty source-bound old LOC references; no BELs and no timing edits.
set fk33_reference_control_locations {
    {{fk33_hbm_quiesce_17/error_reg} {SLICE_X88Y14}}
    {{fk33_hbm_quiesce_23/aw_held_reg} {SLICE_X191Y18}}
    {{main_hbmnativeelasticboundary1_buffer5_pipe_valid_source_valid_reg} {SLICE_X29Y14}}
    {{main_hbmnativeelasticboundary28_buffer141_pipe_ready_valid_reg_rep} {SLICE_X177Y12}}
    {{main_hbmnativeelasticboundary28_buffer141_pipe_ready_valid_reg_rep__1} {SLICE_X177Y8}}
    {{main_hbmnativeelasticboundary29_buffer146_pipe_ready_valid_reg_rep__1} {SLICE_X183Y25}}
    {{main_hbmnativeelasticboundary2_buffer11_pipe_ready_valid_reg} {SLICE_X33Y18}}
    {{main_hbmnativeelasticboundary2_buffer11_pipe_ready_valid_reg_rep__2} {SLICE_X33Y18}}
    {{main_hbmnativeelasticboundary31_buffer155_pipe_ready_valid_reg} {SLICE_X191Y16}}
    {{main_hbmnativeelasticboundary31_buffer155_pipe_valid_source_valid_reg} {SLICE_X191Y16}}
    {{main_hbmnativeelasticboundary31_buffer156_pipe_ready_valid_reg_rep__0} {SLICE_X204Y14}}
    {{main_hbmnativeelasticboundary31_buffer156_pipe_ready_valid_reg_rep__1} {SLICE_X204Y14}}
    {{main_hbmnativeelasticboundary31_buffer156_pipe_ready_valid_reg_rep__2} {SLICE_X204Y14}}
    {{main_hbmruntimequiesce_hbm_status_snapshot_hbmclockmailbox_held_reg[64]} {SLICE_X116Y29}}
    {{main_hbmruntimequiesce_status_image_reg[20]} {SLICE_X116Y18}}
    {{main_hbmruntimequiesce_status_image_reg[21]} {SLICE_X127Y28}}
    {{main_hbmruntimequiesce_status_image_reg[27]} {SLICE_X130Y16}}
    {{main_hbmruntimequiesce_status_image_reg[31]} {SLICE_X165Y21}}
    {{main_hbmruntimequiesce_status_image_reg[32]} {SLICE_X107Y50}}
    {{main_hbmruntimequiesce_status_image_reg[9]} {SLICE_X89Y28}}
}
proc fk33_refseed_one {objects label} {
    if {[llength $objects]!=1} {error "$label requires one exact object: $objects"}
    return [lindex $objects 0]
}
proc fk33_refseed_pin {cell name} {
    return [fk33_refseed_one [get_pins -of_objects $cell -filter "REF_PIN_NAME == $name"] "$cell/$name"]
}
proc fk33_refseed_driver {pin} {
    return [fk33_refseed_one [get_pins -leaf -of_objects [get_nets -segments -of_objects $pin] -filter {DIRECTION == OUT}] "Driver $pin"]
}
# Run AFTER the unchanged reset/status98/8regional floorplan validation and
# BEFORE place_design. No BEL, pblock, clock, timing or RTL edits are made.
proc fk33_reference_control_seed {{apply 0}} {
    global fk33_reference_control_locations fk33_axi_reset_buf
    if {![info exists fk33_axi_reset_buf] || ![llength [info commands fk33_check_reset_buffer]]} {error "Validated AXI reset preparation is required before reference LOCs"}
    fk33_check_reset_buffer
    if {[get_property REF_NAME $fk33_axi_reset_buf] ne "BUFGCE"} {error "AXI reset buffer type changed"}
    set reset_output [fk33_refseed_pin $fk33_axi_reset_buf O]
    if {[llength $fk33_reference_control_locations]!=20} {error "Reference LOC scope must contain exactly20 cells"}
    set rows {}
    set validated {}
    set seen [dict create]
    foreach reference $fk33_reference_control_locations {
        lassign $reference name location
        if {[dict exists $seen $name]} {error "Duplicate reference cell $name"}
        dict set seen $name 1
        set cell [fk33_refseed_one [get_cells -quiet -hier -filter [format {NAME == "%s"} $name]] "Reference cell $name (no alias inference)"]
        if {[get_property REF_NAME $cell] ne "FDRE" || [string tolower [get_property INIT $cell]] ni {0 1'b0 1'h0 1'd0 0x0}} {error "Reference FF type/INIT differs $name"}
        set c [fk33_refseed_pin $cell C]
        set clock [fk33_refseed_one [get_clocks -of_objects $c] "Reference FF clock"]
        set waveform [get_property WAVEFORM $clock]
        if {$clock ne "main_clkout3" || [get_property PERIOD $clock]!=2.0 || [get_property IS_INVERTED $c] ||
                [llength $waveform]!=2 || [lindex $waveform 0]!=0.0 || [lindex $waveform 1]!=1.0} {error "Reference FF must retain positive-edge AXI500 $name/$clock"}
        set r [fk33_refseed_pin $cell R]
        if {[get_property IS_INVERTED $r]} {error "Reference FF reset pin must stay positive $name"}
        set actual_reset_driver [fk33_refseed_driver $r]
        set reset_realization DIRECT_POSITIVE_R
        if {$actual_reset_driver ne $reset_output} {
            # Exactly these two observed PC2 state copies have synchronous
            # reset folded into D. Every other reference keeps direct R.
            if {$name ni {
                main_hbmnativeelasticboundary2_buffer11_pipe_ready_valid_reg
                main_hbmnativeelasticboundary2_buffer11_pipe_ready_valid_reg_rep__2
            }} {error "Unapproved folded reset realization $name"}
            set reset_constant [fk33_refseed_one [get_cells -of_objects $actual_reset_driver] "Folded reset GND"]
            if {[get_property REF_NAME $reset_constant] ne "GND" || [get_property REF_PIN_NAME $actual_reset_driver] ne "G"} {error "Folded reset requires direct positive GND on R $name"}
            set d [fk33_refseed_pin $cell D]
            set ce [fk33_refseed_pin $cell CE]
            if {[get_property IS_INVERTED $d] || [get_property IS_INVERTED $ce]} {error "Folded reset D/CE pin inversion $name"}
            set enable_driver [fk33_refseed_driver $ce]
            set enable_constant [fk33_refseed_one [get_cells -of_objects $enable_driver] "Folded reset VCC"]
            if {[get_property REF_NAME $enable_constant] ne "VCC" || [get_property REF_PIN_NAME $enable_driver] ne "P"} {error "Folded reset requires constant-one CE $name"}
            set data_driver [fk33_refseed_driver $d]
            set lut [fk33_refseed_one [get_cells -of_objects $data_driver] "Folded reset D LUT"]
            if {[get_property REF_PIN_NAME $data_driver] ne "O" || [get_property REF_NAME $lut] ne "LUT4" ||
                    [string tolower [get_property INIT $lut]] ne "16'h0032"} {error "Folded reset requires exact observed LUT4/0032 $name"}
            set reset_input [fk33_refseed_pin $lut I3]
            if {[get_property IS_INVERTED $reset_input] || [fk33_refseed_driver $reset_input] ne $reset_output} {error "Folded D reset must directly receive positive protected AXI reset on I3 $name"}
            # INIT0032 bits8..15 are all0: I3=reset=1 forcesD=0 for
            # arbitraryI0/I1/I2; CE=1,R=0 then forcesnextQ=0.
            set reset_realization EXACT_TWO_CELL_D_LUT4_0032_I3_CE1_R0
        }
        foreach property {MACRO_NAME PARENT_MACRO} {
            if {[get_property -quiet $property $cell] ne ""} {error "Reference FF is macro-owned; no partial macro LOC $name"}
        }
        if {[get_property IS_BEL_FIXED $cell]} {error "Reference FF already has a BEL constraint $name"}
        set current [get_property LOC $cell]
        if {$current ne "" && $current ne $location} {error "Reference LOC conflicts with existing placement $name/$current"}
        set site [fk33_refseed_one [get_sites -quiet $location] "Reference SLICE site"]
        if {[get_property SITE_TYPE $site] ni {SLICEL SLICEM}} {error "Reference site is not a compatible SLICE $location"}
        set blocks [get_pblocks -quiet -of_objects $cell]
        foreach block $blocks {
            if {![get_property IS_SOFT $block] || [get_property EXCLUDE_PLACEMENT $block] || [get_property CONTAIN_ROUTING $block]} {error "Reference LOC requires unchanged soft/nonexclusive regional policy $name/$block"}
        }
        lappend validated [list $cell $location]
        lappend rows [dict create name $name REF FDRE INIT 0 clock $clock period 2.0 reset $reset_output physical_R_driver $actual_reset_driver reset_realization $reset_realization requested_LOC $location BEL NOT_CONSTRAINED pblocks $blocks]
    }
    # Validate the entire scope first: a missing/copy/role mismatch applies no
    # partial LOCs. Excluding a missing name requires a new reviewed manifest.
    if {$apply} {
        foreach reference $validated {
            lassign $reference cell location
            set_property LOC $location $cell
        }
    }
    return [dict create status VALIDATED20_REFERENCE_LOC_ONLY applied $apply rows $rows exclusions {main_buffer76_pipe_ready_valid_reg_rep__1 {fk33_hbm_quiesce_8/addresses_reg[1]_replica} {main_hbmruntimequiesce_status_image_reg[31]_replica}} no_BEL_constraints 1 no_timing_constraints 1 no_clock_changes 1 no_RTL_changes 1 qualification NOT_CLAIMED]
}

set fk33_reference_seed_result [fk33_reference_control_seed 1]
if {[dict get $fk33_reference_seed_result status] ne "VALIDATED20_REFERENCE_LOC_ONLY" ||
        ![dict get $fk33_reference_seed_result applied] ||
        [llength [dict get $fk33_reference_seed_result rows]]!=20} {
    error "Reference LOC20 scope differs"
}
puts "FK33_REFERENCE_CONTROL_LOC20_APPLIED no_BELs=1 no_timing_edits=1 no_clock_edits=1 qualification=NOT_CLAIMED"

# Source-tagged endpoint R capture; source KEEP preserves only this alias.
# The actual placer property is applied through its supported Tcl interface.
set capture [fk33_refseed_one [get_nets -hier -filter {HBM_ENDPOINT_R_CAPTURE == hbm_endpoint_pc31}] "Tagged PC31 R-capture net"]
set segments [get_nets -segments $capture]
if {[llength [get_clocks -quiet -of_objects $segments]]} {error "Capture tag unexpectedly denotes a clock"}
set output [fk33_refseed_one [get_pins -leaf -of_objects $segments -filter {DIRECTION == OUT}] "Capture driver"]
set lut [fk33_refseed_one [get_cells -of_objects $output] "Capture driver cell"]
set ref [get_property REF_NAME $lut]
if {![regexp {^LUT[1-6]$} $ref] && $ref ne "LUT6_2"} {error "Capture must be ordinary LUT-driven"}
if {[get_property REF_PIN_NAME $output] ni {O O5 O6}} {error "Capture output role changed"}
set driver_net [fk33_refseed_one [get_nets -of_objects $output] "LUT-adjacent capture net"]
if {[lsort -unique [get_nets -segments -of_objects $output]] ne [lsort -unique $segments]} {error "Capture driver/tag electrical component differs"}
set protected_cells [list $lut]
set parent [get_property PARENT $lut]
while {$parent ne "" && [llength [get_cells -quiet $parent]]} {
    set object [fk33_refseed_one [get_cells -quiet $parent] "Capture hierarchy"]
    lappend protected_cells $object
    set parent [get_property PARENT $object]
}
foreach cohort [list $segments $protected_cells] {
    foreach object $cohort {
        set value [string toupper [get_property -quiet DONT_TOUCH $object]]
        if {$value ni {{} FALSE 0 1'B0 1'H0 1'D0 0X0}} {error "Capture has protected/unknown DONT_TOUCH:$object/$value"}
    }
}
set loads [get_pins -leaf -of_objects $segments -filter {DIRECTION == IN}]
set count 0
foreach load $loads {
    set role [get_property REF_PIN_NAME $load]
    set cell [fk33_refseed_one [get_cells -of_objects $load] "Capture load owner"]
    set load_ref [get_property REF_NAME $cell]
    set protection [string toupper [get_property -quiet DONT_TOUCH $cell]]
    if {$protection ni {{} FALSE 0 1'B0 1'H0 1'D0 0X0}} {error "Capture load is protected/unknown:$cell/$protection"}
    if {[string toupper [get_property -quiet ASYNC_REG $cell]] ni {{} FALSE 0} ||
            [get_property -quiet HBM_GRAY_SRC $cell] ne "" || [get_property -quiet HBM_GRAY_DST $cell] ne ""} {error "Capture load is CDC/Gray state:$cell"}
    if {([regexp {^LUT[1-6]$} $load_ref] || $load_ref eq "LUT6_2") && [regexp {^I[0-5]$} $role]} {continue}
    if {$load_ref ne "FDRE" || $role ni {D CE}} {error "Capture load must be ordinary LUT input or FDRE D/CE:$load/$load_ref"}
    set c [fk33_refseed_pin $cell C]
    set clock [fk33_refseed_one [get_clocks -of_objects $c] "Capture load clock"]
    if {$clock ne "main_clkout3" || [get_property PERIOD $clock]!=2.0 || [get_property IS_INVERTED $c]} {error "Capture FDRE load must retain positive native500:$cell"}
    if {$role eq "CE"} {
        if {[get_property IS_INVERTED $load]} {error "Capture CE polarity differs:$load"}
        incr count
    }
}
if {!$count} {error "Capture net has no validated native500 CE consumer"}
set_property FORCE_MAX_FANOUT 8 $driver_net
if {[get_property FORCE_MAX_FANOUT $driver_net]!=8} {error "Capture force-fanout readback differs"}
puts [list HBM_ENDPOINT_R_CAPTURE_FORCE8 $capture $driver_net LUT $lut CE_consumers $count physical_effect NOT_MEASURED]
"""
    commands = commands.replace("{", "{{").replace("}", "}}")
    platform.toolchain.pre_placement_commands.append(commands)
    _add_hbm_pc31_control_luts(platform)


def _add_hbm_pc31_control_luts(platform):
    # Preserve the reviewed native500 placement; collapse only these two cones.
    commands = r"""
proc fk33_pc31_ff_placement {} {
    set cells [get_cells -hier -filter {REF_NAME =~ FD*}]
    set rows {}
    foreach name [get_property NAME $cells] ref [get_property REF_NAME $cells] init [get_property INIT $cells] loc [get_property LOC $cells] bel [get_property BEL $cells] loc_fixed [get_property IS_LOC_FIXED $cells] bel_fixed [get_property IS_BEL_FIXED $cells] {
        lappend rows [list $name $ref $init $loc $bel $loc_fixed $bel_fixed]
    }
    return [lsort -index 0 $rows]
}
proc fk33_pc31_component {cell role} {
    return [lsort -unique [get_nets -segments -of_objects [fk33_refseed_pin $cell $role]]]
}
proc fk33_pc31_inputs {cell} {
    set rows {}
    foreach pin [lsort [get_pins -of_objects $cell -filter {DIRECTION == IN}]] {
        lappend rows [list [get_property REF_PIN_NAME $pin] [lsort -unique [get_nets -segments -of_objects $pin]]]
    }
    return $rows
}
proc fk33_pc31_init {cell} {
    set init [get_property INIT $cell]
    if {![regsub {^[0-9]+'h} $init 0x value]} {error "PC31 LUT INIT must be hexadecimal: $cell/$init"}
    return $value
}
proc fk33_pc31_replace {cell reference init inputs} {
    set destination "[get_property LOC $cell]/[get_property BEL $cell]"
    set output [fk33_refseed_one [get_nets -of_objects [fk33_refseed_pin $cell O]] "PC31 output net"]
    set name "${cell}[expr {$reference eq "LUT6" ? "_eco_lut6" : "_eco_flat"}]"
    remove_cell $cell
    create_cell -reference $reference $name
    set replacement [fk33_refseed_one [get_cells $name] "PC31 replacement"]
    set_property INIT $init $replacement
    set index 0
    foreach input $inputs {
        connect_net -hierarchical -net $input -objects [fk33_refseed_pin $replacement I$index]
        incr index
    }
    connect_net -hierarchical -net $output -objects [fk33_refseed_pin $replacement O]
    place_cell [list $replacement $destination]
    return $replacement
}
proc fk33_pc31_control_luts {} {
    set shared [fk33_refseed_one [get_cells -hier -filter {NAME == hbm_0/main_hbmnativeelasticboundary31_buffer156_pipe_ready_valid_i_2}] "PC31 shared LUT"]
    set valid [fk33_refseed_one [get_cells -hier -filter {NAME == fk33_hbm_quiesce_31/main_hbmnativeelasticboundary31_buffer156_pipe_valid_source_valid_i_1}] "PC31 valid LUT"]
    set ready [fk33_refseed_one [get_cells -hier -filter {NAME == hbm_0/main_hbmnativeelasticboundary31_buffer156_pipe_ready_valid_i_1}] "PC31 ready LUT"]
    set capture [fk33_refseed_one [get_cells -hier -filter {NAME == main_hbmnativeelasticboundary31_buffer156_pipe_valid_source_valid_reg}] "PC31 valid capture"]
    set hard [fk33_refseed_one [get_cells -of_objects [get_sites BLI_HBM_AXI_INTF_X31Y0] -filter {REF_NAME == HBM_SNGLBLI_INTF_AXI}] "PC31 hard interface"]
    foreach {cell reference init} [list $shared LUT3 8'h8F $valid LUT5 32'h8BFF8B00 $ready LUT3 8'h23] {
        if {[get_property REF_NAME $cell] ne $reference || [get_property INIT $cell] ne $init} {error "PC31 original LUT shape differs: $cell"}
    }
    foreach {left role right other} [list $shared I2 $valid I4 $shared O $valid I3 $shared O $ready I1 $valid I1 $ready I0 $valid I2 $ready I2 $valid O $capture D $shared I1 $hard WREADY_PIPE] {
        if {[fk33_pc31_component $left $role] ne [fk33_pc31_component $right $other]} {error "PC31 electrical identity differs: $left/$role and $right/$other"}
    }
    set shared_loads [get_pins -leaf -of_objects [get_nets -segments -of_objects [fk33_refseed_pin $shared O]] -filter {DIRECTION == IN}]
    if {[lsort $shared_loads] ne [lsort [list [fk33_refseed_pin $valid I3] [fk33_refseed_pin $ready I1]]]} {error "PC31 shared LUT has additional consumers"}
    set ready_loads [get_pins -leaf -of_objects [get_nets -segments -of_objects [fk33_refseed_pin $ready O]] -filter {DIRECTION == IN}]
    if {[llength $ready_loads]!=3} {error "PC31 requires three ready-state replicas"}
    set state [list $capture]
    foreach pin $ready_loads {
        set cell [fk33_refseed_one [get_cells -of_objects $pin] "PC31 ready capture"]
        if {[get_property REF_PIN_NAME $pin] ne "D" || ![get_property IS_LOC_FIXED $cell] || [get_property LOC $cell] ne "SLICE_X204Y14"} {error "PC31 ready replica differs"}
        lappend state $cell
    }
    set state_before {}
    foreach cell $state {
        if {[get_property REF_NAME $cell] ne "FDRE" || [string tolower [get_property INIT $cell]] ne "1'b0"} {error "PC31 capture type/INIT differs"}
        set row [list $cell [get_property LOC $cell] [get_property BEL $cell]]
        foreach role {C CE R} {lappend row [list $role [fk33_pc31_component $cell $role]]}
        lappend state_before $row
    }
    set fixed_before {}
    set fixed [get_cells -hier -filter {REF_NAME =~ FD* && IS_LOC_FIXED == 1}]
    if {[llength $fixed]!=20} {error "PC31 requires the existing twenty fixed FFs"}
    foreach cell $fixed {lappend fixed_before [list $cell [get_property LOC $cell] [get_property BEL $cell]]}
    set clocks_before {}
    foreach clock [get_clocks] {lappend clocks_before [list $clock [get_property PERIOD $clock] [get_property WAVEFORM $clock]]}
    set ff_count [llength [get_cells -hier -filter {REF_NAME =~ FD*}]]
    set ff_placement_before [fk33_pc31_ff_placement]
    # Both replacements need the full LUT resource; validate before editing.
    foreach cell [list $valid $ready] {
        set letter [string index [lindex [split [get_property BEL $cell] .] end] 0]
        set paired [fk33_refseed_one [get_bels [get_property LOC $cell]/${letter}5LUT] "PC31 paired BEL"]
        set occupant [get_cells -quiet -of_objects $paired]
        if {[llength $occupant] && $occupant ne $cell} {error "PC31 full LUT resource is shared: $cell/$occupant"}
    }
    set valid_inputs {}
    foreach {cell role} [list $valid I0 $valid I1 $valid I2 $valid I4 $shared I0 $shared I1] {lappend valid_inputs [fk33_refseed_one [get_nets -of_objects [fk33_refseed_pin $cell $role]] "PC31 valid input"]}
    set ready_inputs {}
    foreach {cell role} [list $shared I0 $shared I1 $shared I2 $ready I0 $ready I2] {lappend ready_inputs [fk33_refseed_one [get_nets -of_objects [fk33_refseed_pin $cell $role]] "PC31 ready input"]}
    set shared_init [fk33_pc31_init $shared]
    set valid_init [fk33_pc31_init $valid]
    set ready_init [fk33_pc31_init $ready]
    # Exhaustive original-to-new local proofs include synchronous reset.
    for {set bits 0} {$bits<64} {incr bits} {
        set a [expr {$bits&1}]; set b [expr {($bits>>1)&1}]; set c [expr {($bits>>2)&1}]
        set v [expr {($bits>>3)&1}]; set p [expr {($bits>>4)&1}]; set w [expr {($bits>>5)&1}]
        set shared_value [expr {($shared_init>>($p|($w<<1)|($v<<2)))&1}]
        set old [expr {($valid_init>>($a|($b<<1)|($c<<2)|($shared_value<<3)|($v<<4)))&1}]
        set new [expr {(0x8B8BFF8BFF8BFF8B>>$bits)&1}]
        foreach reset {0 1} {if {[expr {$reset?0:$old}]!=[expr {$reset?0:$new}]} {error "PC31 valid truth table differs"}}
    }
    for {set bits 0} {$bits<32} {incr bits} {
        set p [expr {$bits&1}]; set w [expr {($bits>>1)&1}]; set v [expr {($bits>>2)&1}]
        set b [expr {($bits>>3)&1}]; set c [expr {($bits>>4)&1}]
        set shared_value [expr {($shared_init>>($p|($w<<1)|($v<<2)))&1}]
        set old [expr {($ready_init>>($b|($shared_value<<1)|($c<<2)))&1}]
        set new [expr {(0x70007070>>$bits)&1}]
        foreach reset {0 1} {if {[expr {$reset?0:$old}]!=[expr {$reset?0:$new}]} {error "PC31 ready truth table differs"}}
    }
    puts [list FK33_PC31_CONTROL_CONE VALID ORIGINALS [list $shared [get_property INIT $shared] $valid [get_property INIT $valid]] ORIGINAL_INPUTS [list [fk33_pc31_inputs $shared] [fk33_pc31_inputs $valid]] INPUT_MAP {skid_d_valid ready_valid admission primary_valid permit WREADY} INPUT_NETS $valid_inputs NEW_INIT 64'h8B8BFF8BFF8BFF8B PROOF_INPUT_RESET_CASES 128 PROOF PASS]
    puts [list FK33_PC31_CONTROL_CONE READY ORIGINALS [list $shared [get_property INIT $shared] $ready [get_property INIT $ready]] ORIGINAL_INPUTS [list [fk33_pc31_inputs $shared] [fk33_pc31_inputs $ready]] INPUT_MAP {permit WREADY primary_valid ready_valid admission} INPUT_NETS $ready_inputs NEW_INIT 32'h70007070 PROOF_INPUT_RESET_CASES 64 PROOF PASS]
    set valid [fk33_pc31_replace $valid LUT6 64'h8B8BFF8BFF8BFF8B $valid_inputs]
    set ready [fk33_pc31_replace $ready LUT5 32'h70007070 $ready_inputs]
    if {[llength [get_pins -quiet -leaf -of_objects [get_nets -segments -of_objects [fk33_refseed_pin $shared O]] -filter {DIRECTION == IN}]]} {error "PC31 shared LUT still has consumers"}
    remove_cell $shared
    route_design -preserve -no_psir
    foreach {cell inputs} [list $valid $valid_inputs $ready $ready_inputs] {
        set index 0
        foreach input $inputs {
            if {[lsort -unique [get_nets -segments $input]] ne [fk33_pc31_component $cell I$index]} {error "PC31 replacement connectivity differs"}
            incr index
        }
        puts [list FK33_PC31_CONTROL_REPLACEMENT $cell REF [get_property REF_NAME $cell] INIT [get_property INIT $cell] INPUTS [fk33_pc31_inputs $cell] OUTPUT [fk33_pc31_component $cell O] LOC [get_property LOC $cell] BEL [get_property BEL $cell]]
    }
    set state_after {}
    foreach cell $state {
        set row [list $cell [get_property LOC $cell] [get_property BEL $cell]]
        foreach role {C CE R} {lappend row [list $role [fk33_pc31_component $cell $role]]}
        lappend state_after $row
    }
    set fixed_after {}
    foreach cell $fixed {lappend fixed_after [list $cell [get_property LOC $cell] [get_property BEL $cell]]}
    set clocks_after {}
    foreach clock [get_clocks] {lappend clocks_after [list $clock [get_property PERIOD $clock] [get_property WAVEFORM $clock]]}
    if {$state_before ne $state_after || $fixed_before ne $fixed_after || $clocks_before ne $clocks_after || $ff_count!=[llength [get_cells -hier -filter {REF_NAME =~ FD*}]]} {error "PC31 state/placement/clock inventory changed"}
    if {$ff_placement_before ne [fk33_pc31_ff_placement]} {error "PC31 transformation changed an FF placement or initialization"}
    puts [list FK33_PC31_PRESERVED_FF_PLACEMENTS $ff_count INITIALIZATIONS_FIXEDNESS_CLOCKS_CONTROLS_UNCHANGED]
    puts FK33_PC31_DUAL_CONTROL_LUTS_COMPLETE
}
fk33_pc31_control_luts
"""
    commands = commands.replace("{", "{{").replace("}", "}}")
    platform.toolchain.post_routing_commands.append(commands)


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
                        read_address_max_fanout=8 if forward else 16)
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
        hbm_reference_placement = False,
        **kwargs):
        self.hbm_primitive = hbm_primitive or hbm_primitive_rtl is not None
        self.hbm_axi_domain = bool(hbm_axi_domain)
        self.hbm_experimental_500_1000 = bool(hbm_experimental_500_1000)
        if hbm_reference_placement and not (
                hbm_experimental_500_1000 and with_hbm and hbm_primitive and
                hbm_primitive_rtl is None and hbm_endpoint and hbm_endpoint_traffic and
                hbm_axi_domain and sys_clk_freq == 250e6 and hbm_axi_clk_freq == 500e6 and
                not hbm_endpoint_two_port and not hbm_bist and not with_pcie and
                kwargs.get("cpu_type", "vexriscv") == "vexriscv" and
                kwargs.get("cpu_variant") in (None, "minimal")):
            raise ValueError("Reference placement requires the experimental single-PC31 VexRiscv minimal profile")
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
            _add_hbm_status_mailbox_constraints(platform)
            _add_hbm_native_floorplan(platform, reserve_pc31=hbm_endpoint,
                tracker_tags=hbm_experimental_500_1000)
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
                    registered_status=hbm_experimental_500_1000,
                    local_status_pipeline=hbm_experimental_500_1000,
                    status_publication_pipeline=hbm_experimental_500_1000,
                    native_tracker_tags=hbm_experimental_500_1000)
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
                                pipe_valid_enable_max_fanout=(
                                    32 if hbm_experimental_500_1000 and name == "w" else None),
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

        # Keep the optional LOC preset after all regional and Gray constraints.
        if hbm_reference_placement:
            _add_hbm_reference_placement(platform)

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
        self.add_csr_bridge(name="csr", origin=csr_origin, with_register=True,
            read_latency=3 if self.hbm_experimental_500_1000 else 2)
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
            read_pipeline=hasattr(self, "hbm_bist") or hasattr(self, "hbm_endpoint"),
            read_sample=self.hbm_experimental_500_1000)
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
    parser.add_target_argument("--hbm-reference-placement", action="store_true", help="Use the reviewed experimental single-PC31 placement preset; literal FF copies must match.")
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
        hbm_reference_placement = args.hbm_reference_placement,
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
