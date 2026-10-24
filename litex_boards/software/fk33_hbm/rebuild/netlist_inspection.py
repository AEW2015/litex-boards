"""Source-bound, read-only routed DCP inspection; no hardware access.

--prepare-only writes the query without launching Vivado. Native properties,
reset-synchronizer topology and bounded interstage paths are mandatory outputs.
"""
import argparse
import hashlib
import math
import json
from pathlib import Path
import re
import subprocess

RESET_CHAINS = {'sys_reset_sync': 'main_clkout0', 'apb_reset_sync': 'main_clkout2',
                'apb_monitor_reset_sync': 'main_clkout2'}

def expected_reset_chains(axi_domain=False):
    return dict(RESET_CHAINS, **({'axi_domain_reset': 'main_clkout3'} if axi_domain else {}))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output_paths(evidence, axi_domain=False, endpoint=False):
    return [evidence / name for name in (
        'implemented-hbm-properties.txt', 'implemented-clocks.txt',
        'implemented-reset-chains.txt', 'netlist-inspection-console.log',
        *(['implemented-endpoint-controls.txt'] if endpoint else []),
        *(f'implemented-reset-{name}-timing.rpt' for name in expected_reset_chains(axi_domain)))]


def query_tcl(checkpoint, evidence, axi_domain=False, endpoint=False):
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
    if axi_domain:
        # Identify the extra MMCM output reset by its physical clock and tags,
        # rather than unstable Migen instance numbering. This remains distinct
        # from the three management-control reset chains checked above.
        axi_query = r'''
set axi_stage0s {}
foreach cell [get_cells -hier -filter {ars_ff1 == TRUE}] {
    set pin [require_one [get_pins -of_objects $cell -filter {REF_PIN_NAME == C}] "AXI reset candidate clock pin"]
    set clocks [get_clocks -of_objects $pin]
    if {[llength $clocks] == 1 && [lindex $clocks 0] eq "main_clkout3"} {lappend axi_stage0s $cell}
}
set stage0 [require_one $axi_stage0s "AXI domain reset stage0"]
set q0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == Q}] "AXI reset Q0"]
set sinks [get_pins -leaf -of_objects [get_nets -segments -of_objects $q0] -filter {REF_PIN_NAME == D}]
set stage1 [require_one [get_cells -of_objects $sinks -filter {ars_ff2 == TRUE}] "AXI domain reset stage1"]
foreach cell [list $stage0 $stage1] {
    if {[get_property REF_NAME $cell] ne "FDPE" || ![true_attribute $cell ASYNC_REG]} {error "AXI reset lost FDPE/ASYNC_REG: $cell"}
}
if {![true_attribute $stage0 ars_ff1] || [true_attribute $stage0 ars_ff2] ||
    ![true_attribute $stage1 ars_ff2] || [true_attribute $stage1 ars_ff1]} {error "AXI reset stage tags missing or swapped"}
set clock0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == C}] "AXI reset C0"]
set clock1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == C}] "AXI reset C1"]
foreach pin [list $clock0 $clock1] {
    if {[require_one [get_clocks -of_objects $pin] "AXI reset clock"] ne "main_clkout3"} {error "AXI reset clock mapping changed"}
}
set d1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == D}] "AXI reset D1"]
set pre0 [require_one [get_pins -of_objects $stage0 -filter {REF_PIN_NAME == PRE}] "AXI reset PRE0"]
set pre1 [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == PRE}] "AXI reset PRE1"]
set path [require_one [get_timing_paths -from $clock0 -to $d1 -delay_type max -max_paths 1] "AXI reset max path"]
set requirement [get_property REQUIREMENT $path]
set slack [get_property SLACK $path]
if {![string is double -strict $requirement] || abs($requirement - 2.0) > 0.001} {error "AXI reset lost 2ns bound: $requirement"}
if {![string is double -strict $slack] || $slack < 0 || $slack > 100} {error "AXI reset timing invalid/failing: $slack"}
report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file {@@EVIDENCE@@/implemented-reset-axi_domain_reset-timing.rpt}
puts $reset_out "RESET_CHAIN axi_domain_reset STAGE0=$stage0 STAGE1=$stage1 CLOCK=main_clkout3 REF0=FDPE REF1=FDPE ASYNC0=TRUE ASYNC1=TRUE ARS0=ars_ff1 ARS1=ars_ff2 PRE0=$pre0 PRE1=$pre1 DIRECT=1 REQUIREMENT_NS=$requirement SLACK_NS=$slack"
'''
        template = template.replace('close $reset_out', axi_query + '\nclose $reset_out')
    template = template.replace('report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file {@@EVIDENCE@@/implemented-reset-PLACEHOLDER-timing.rpt}',
        'report_timing -from $clock0 -to $d1 -delay_type max -max_paths 1 -input_pins -file [file join {@@EVIDENCE@@} "implemented-reset-${chain}-timing.rpt"]')
    if endpoint:
        if not axi_domain:
            raise ValueError("Endpoint inspection requires the separate AXI domain")
        policy = json.loads(Path(__file__).with_name("timing-contract-endpoint.json").read_text())["direct_control_policy"]
        tags = [f"hbm_control_{role}_bit{bit}"
            for role, width in policy["roles"].items() for bit in range(width)]
        controls = r'''
set expected_controls {@@TAGS@@}
set controls [lsort -unique [concat [get_cells -hier -quiet -filter {hbm_direct_reset =~ hbm_control_*}] [get_cells -hier -quiet -filter {NAME =~ *hbm_control_* && REF_NAME =~ FD*}]]]
set reset_q [require_one [get_pins -of_objects $stage1 -filter {REF_PIN_NAME == Q}] "AXI reset output"]
set control_out [open {@@EVIDENCE@@/implemented-endpoint-controls.txt} w]
set seen [dict create]
foreach cell $controls {
    set tag [get_property -quiet hbm_direct_reset $cell]
    if {$tag eq "" && ![regexp {hbm_control_[a-z_]+_bit[0-9]+} $cell tag]} {error "Untagged endpoint control replica $cell"}
    if {[lsearch -exact $expected_controls $tag] < 0} {error "Unexpected endpoint control $tag"}
    set ref [get_property REF_NAME $cell]
    set init [get_property INIT $cell]
    if {$ref ne "FDRE" || $init ni {0 1'b0 1'h0}} {error "Invalid endpoint FDRE/INIT $cell"}
    foreach pin {C R} {
        set inverted [get_property IS_INVERTED [require_one [get_pins $cell/$pin] "Control pin"]]
        if {$inverted ni {0 FALSE false}} {error "Inverted endpoint control $cell/$pin"}
    }
    set reset [require_one [get_pins -leaf -of_objects [get_nets -segments -of_objects [get_pins $cell/R]] -filter {DIRECTION == OUT}] "Control reset driver"]
    if {$reset ne $reset_q} {error "Endpoint reset is combined or not direct: $cell $reset"}
    set clock [require_one [get_clocks -of_objects [get_pins $cell/C]] "Control clock"]
    if {$clock ne "main_clkout3" || abs([get_property PERIOD $clock] - 4.0) > 0.0001} {error "Invalid endpoint control clock"}
    puts $control_out "ENDPOINT_CONTROL $tag CELL=$cell REF=FDRE INIT=0 RESET_DRIVER=$reset CLOCK=$clock PERIOD_NS=4.0 INVERTED=0"
    dict set seen $tag 1
}
if {[lsort [dict keys $seen]] ne [lsort $expected_controls]} {error "Missing endpoint direct control bits"}
close $control_out
'''.replace("@@TAGS@@", " ".join(tags))
        template = template.replace("puts HBM_NETLIST_INSPECTION_COMPLETE", controls + "\nputs HBM_NETLIST_INSPECTION_COMPLETE")
    return template.replace('@@CHECKPOINT@@', checkpoint.as_posix()).replace('@@EVIDENCE@@', evidence.as_posix())


def parse_reset_chains(path, axi_domain=False):
    expected = expected_reset_chains(axi_domain)
    rows = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 16 or fields[0] != 'RESET_CHAIN':
            raise ValueError('Malformed native reset-chain row: ' + line)
        row = dict(item.split('=', 1) for item in fields[2:])
        name = fields[1]
        if name not in expected or row['CLOCK'] != expected[name]:
            raise ValueError('Unexpected control reset chain/clock: ' + name)
        if (row['REF0'], row['REF1'], row['ASYNC0'], row['ASYNC1'], row['ARS0'], row['ARS1'], row['DIRECT']) != (
                'FDPE', 'FDPE', 'TRUE', 'TRUE', 'ars_ff1', 'ars_ff2', '1'):
            raise ValueError('Control reset chain topology/tags failed: ' + name)
        if not (abs(float(row['REQUIREMENT_NS']) - 2) <= .001 and 0 <= float(row['SLACK_NS']) <= 100):
            raise ValueError('Control reset interstage timing failed: ' + name)
        row['name'] = name
        rows.append(row)
    if len(rows) != len(expected) or {row['name'] for row in rows} != set(expected):
        raise ValueError('Unexpected reset-chain inventory: expected ' + str(sorted(expected)))
    return rows


def parse_endpoint_controls(path):
    """Validate every transport control tag, primitive, reset and native clock."""
    policy = json.loads(Path(__file__).with_name("timing-contract-endpoint.json").read_text())["direct_control_policy"]
    expected = {f"hbm_control_{role}_bit{bit}"
        for role, width in policy["roles"].items() for bit in range(width)}
    rows, seen, cells, resets = [], set(), set(), set()
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 9 or fields[0] != "ENDPOINT_CONTROL":
            raise ValueError("Malformed endpoint control row: " + line)
        tag = fields[1]
        row = dict(item.split("=", 1) for item in fields[2:])
        if tag not in expected or row["CELL"] in cells:
            raise ValueError("Unexpected/duplicate endpoint control: " + tag)
        if (row["REF"], row["INIT"], row["CLOCK"], row["INVERTED"]) != ("FDRE", "0", "main_clkout3", "0"):
            raise ValueError("Invalid endpoint control primitive/reset/clock: " + tag)
        period = float(row["PERIOD_NS"])
        if not math.isfinite(period) or abs(period - 4) > .0001 or not row["RESET_DRIVER"]:
            raise ValueError("Invalid endpoint control period/reset: " + tag)
        seen.add(tag)
        cells.add(row["CELL"])
        resets.add(row["RESET_DRIVER"])
        rows.append(dict(tag=tag, **row))
    if seen != expected or len(resets) != 1 or len(expected) != policy["logical_bits"]:
        raise ValueError("Incomplete endpoint logical control/reset inventory")
    return dict(status="PASS", logical_bits=len(expected), physical_ffs=len(rows), rows=rows)
