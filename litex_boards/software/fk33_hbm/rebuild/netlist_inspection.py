"""Source-bound, read-only routed DCP inspection; no hardware access.

--prepare-only writes the query without launching Vivado. Native properties,
reset-synchronizer topology and bounded interstage paths are mandatory outputs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

RESET_CHAINS = {'sys_reset_sync': 'main_clkout0', 'apb_reset_sync': 'main_clkout2',
                'apb_monitor_reset_sync': 'main_clkout2'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output_paths(evidence):
    return [evidence / name for name in (
        'implemented-hbm-properties.txt', 'implemented-clocks.txt',
        'implemented-reset-chains.txt', 'netlist-inspection-console.log',
        *(f'implemented-reset-{name}-timing.rpt' for name in RESET_CHAINS))]


def query_tcl(checkpoint, evidence):
    template = r'''set checkpoint {@@CHECKPOINT@@}
puts "INSPECT_SELECTED_CHECKPOINT $checkpoint"
open_checkpoint $checkpoint
set out [open {@@EVIDENCE@@/implemented-hbm-properties.txt} w]
foreach cell [get_cells -hier -filter {REF_NAME =~ HBM*}] {
    puts $out "CELL $cell REF_NAME=[get_property REF_NAME $cell]"
    foreach prop [lsort [list_property $cell]] {
        if {[regexp -nocase {DATARATE|FREQ|CLK} $prop]} {puts $out "$prop=[get_property $prop $cell]"}
    }
}
close $out
set clocks [open {@@EVIDENCE@@/implemented-clocks.txt} w]
foreach clock [lsort [get_clocks]] {
    puts $clocks "CLOCK $clock PERIOD=[get_property PERIOD $clock] WAVEFORM=[get_property WAVEFORM $clock]"
}
close $clocks
proc require_one {objects label} {
    if {[llength $objects] != 1} {error "$label expected exactly one object, got [llength $objects]: $objects"}
    return [lindex $objects 0]
}
proc true_attribute {cell attribute} {
    set properties [list_property $cell]
    set position [lsearch -nocase -exact $properties $attribute]
    if {$position < 0} {return 0}
    return [expr {[string toupper [get_property [lindex $properties $position] $cell]] in {TRUE 1}}]
}
set tagged_control_cells {}
foreach cell [get_cells -hier -filter {NAME =~ *fk33_hbm_control/*}] {
    if {[true_attribute $cell ars_ff1] || [true_attribute $cell ars_ff2]} {lappend tagged_control_cells $cell}
}
if {[llength $tagged_control_cells] != 6} {error "Expected exactly six tagged control reset FFs, got $tagged_control_cells"}
set reset_out [open {@@EVIDENCE@@/implemented-reset-chains.txt} w]
set verified_cells {}
foreach {chain expected_clock} {sys_reset_sync main_clkout0 apb_reset_sync main_clkout2 apb_monitor_reset_sync main_clkout2} {
    set stage0 [require_one [get_cells -hier -filter "NAME =~ *fk33_hbm_control/${chain}_stage0_reg"] "$chain stage0"]
    set stage1 [require_one [get_cells -hier -filter "NAME =~ *fk33_hbm_control/${chain}_stage1_reg"] "$chain stage1"]
    foreach cell [list $stage0 $stage1] {
        if {[get_property REF_NAME $cell] ne "FDPE"} {error "$chain requires an asynchronous PRE FF: $cell"}
        if {![true_attribute $cell ASYNC_REG]} {error "$cell lost ASYNC_REG"}
        lappend verified_cells $cell
    }
    if {![true_attribute $stage0 ars_ff1] || [true_attribute $stage0 ars_ff2] ||
        ![true_attribute $stage1 ars_ff2] || [true_attribute $stage1 ars_ff1]} {error "$chain stage tags missing or swapped"}
    set clock0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == C}] "$chain C0"]
    set clock1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == C}] "$chain C1"]
    set q0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == Q}] "$chain Q0"]
    set d1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == D}] "$chain D1"]
    set pre0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == PRE}] "$chain PRE0"]
    set pre1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == PRE}] "$chain PRE1"]
    set name0 [require_one [get_clocks -of_objects $clock0] "$chain clock0"]
    set name1 [require_one [get_clocks -of_objects $clock1] "$chain clock1"]
    if {$name0 ne $expected_clock || $name1 ne $expected_clock} {error "$chain clock mapping changed: $name0 $name1"}
    set sinks [get_pins -leaf -of_objects [get_nets -segments -of_objects $q0] -filter {DIRECTION == IN}]
    if {[lsearch -exact $sinks $d1] < 0} {error "$chain stage0Q does not directly drive stage1D"}
    set path [require_one [get_timing_paths -from $clock0 -to $d1 -delay_type max -max_paths 1] "$chain max path"]
    set requirement [get_property REQUIREMENT $path]
    set slack [get_property SLACK $path]
    if {![string is double -strict $requirement] || abs($requirement - 2.0) > 0.001} {error "$chain lost its2ns max-delay bound: $requirement"}
    if {![string is double -strict $slack] || $slack < 0 || $slack > 100} {error "$chain interstage timing invalid/failing: $slack"}
    report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file {@@EVIDENCE@@/implemented-reset-PLACEHOLDER-timing.rpt}
    puts $reset_out "RESET_CHAIN $chain STAGE0=$stage0 STAGE1=$stage1 CLOCK=$expected_clock REF0=FDPE REF1=FDPE ASYNC0=TRUE ASYNC1=TRUE ARS0=ars_ff1 ARS1=ars_ff2 PRE0=$pre0 PRE1=$pre1 DIRECT=1 REQUIREMENT_NS=$requirement SLACK_NS=$slack"
}
if {[lsort $verified_cells] ne [lsort $tagged_control_cells]} {error "Unexpected tagged control FF outside the three reset chains"}
close $reset_out
puts HBM_RESET_SYNCHRONIZERS_VERIFIED
puts HBM_NETLIST_INSPECTION_COMPLETE
quit
'''
    template = template.replace('report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file {@@EVIDENCE@@/implemented-reset-PLACEHOLDER-timing.rpt}',
        'report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file [file join {@@EVIDENCE@@} "implemented-reset-${chain}-timing.rpt"]')
    return template.replace('@@CHECKPOINT@@', checkpoint.as_posix()).replace('@@EVIDENCE@@', evidence.as_posix())


def parse_reset_chains(path):
    rows = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 16 or fields[0] != 'RESET_CHAIN':
            raise ValueError('Malformed native reset-chain row: ' + line)
        row = dict(item.split('=', 1) for item in fields[2:])
        name = fields[1]
        if name not in RESET_CHAINS or row['CLOCK'] != RESET_CHAINS[name]:
            raise ValueError('Unexpected control reset chain/clock: ' + name)
        if (row['REF0'], row['REF1'], row['ASYNC0'], row['ASYNC1'], row['ARS0'], row['ARS1'], row['DIRECT']) != (
                'FDPE', 'FDPE', 'TRUE', 'TRUE', 'ars_ff1', 'ars_ff2', '1'):
            raise ValueError('Control reset chain topology/tags failed: ' + name)
        if not (abs(float(row['REQUIREMENT_NS']) - 2) <= .001 and 0 <= float(row['SLACK_NS']) <= 100):
            raise ValueError('Control reset interstage timing failed: ' + name)
        row['name'] = name
        rows.append(row)
    if len(rows) != 3 or {row['name'] for row in rows} != set(RESET_CHAINS):
        raise ValueError('Expected exactly three control reset chains')
    return rows
