"""Read-only native timing proof for the opt-in FK33 CPU FIFO Gray buses.

Append query_tcl() after opening the routed checkpoint. parse() fails closed
on missing inventory, inactive max-delay paths, or missing/failing skew rows.
"""
import math
from pathlib import Path
import re


def expected_groups(endpoint=False):
    groups = {f"hbm_cpu_pc{port:02d}_{channel}_{pointer}":
        (("main_clkout0", "main_clkout3")
            if (channel in ("aw", "w", "ar")) == (pointer == "produce")
            else ("main_clkout3", "main_clkout0"))
        for port in range(32) for channel in ("aw", "w", "b", "ar", "r")
        for pointer in ("produce", "consume")}
    if endpoint:
        groups.update({name.replace("hbm_cpu_pc31_", "hbm_endpoint_pc31_"): clocks
            for name, clocks in list(groups.items()) if name.startswith("hbm_cpu_pc31_")})
    return groups


def output_paths(evidence):
    evidence = Path(evidence)
    return [evidence / name for name in ("implemented-gray-buses.txt",
        "implemented-gray-paths.rpt", "implemented-gray-bus-skew.rpt",
        "implemented-clock-roots.txt", "implemented-clock-roots.rpt")]


def _clock_periods_ns(axi_clk_freq):
    if axi_clk_freq == 250e6:
        return 4, 4
    if axi_clk_freq == 400e6:
        return 5, 2.5
    raise ValueError("Native Gray proof requires SYS250/AXI250 or SYS200/AXI400")


def query_tcl(evidence, axi_clk_freq=250e6, endpoint=False):
    """Return standalone query body; caller owns opening/closing checkpoint."""
    sys_period, axi_period = _clock_periods_ns(axi_clk_freq)
    template = r'''
proc hbm_gray_one {objects label} {
    if {[llength $objects] != 1} {error "$label expected one object: $objects"}
    return [lindex $objects 0]
}
proc hbm_gray_attr {cell name} {
    set props [list_property $cell]
    set pos [lsearch -nocase -exact $props $name]
    if {$pos < 0} {return ""}
    return [get_property [lindex $props $pos] $cell]
}
set root_out [open {@@EVIDENCE@@/implemented-clock-roots.txt} w]
set root_nets {}
foreach {domain buffer expected_clock} {SYS BUFG main_clkout0 AXI BUFG_3 main_clkout3} {
    set pin [hbm_gray_one [get_pins -quiet ${buffer}/O] "$domain clock output"]
    set cell [hbm_gray_one [get_cells -of_objects $pin] "$domain clock buffer"]
    set ref [get_property REF_NAME $cell]
    if {$ref ni {BUFG BUFGCE}} {error "Unexpected $domain global buffer $ref"}
    set net [hbm_gray_one [get_nets -of_objects $pin] "$domain direct clock net"]
    set clock [hbm_gray_one [get_clocks -of_objects $pin] "$domain native clock"]
    set period [get_property PERIOD $clock]
    if {$clock ne $expected_clock || abs($period - 4.0) > 0.0001} {error "Unexpected $domain clock/period"}
    set requested [get_property USER_CLOCK_ROOT $net]
    set actual [get_property CLOCK_ROOT $net]
    if {$requested ne "X4Y1" || $actual ne "X4Y1"} {error "$domain clock roots differ: requested=$requested actual=$actual"}
    lappend root_nets $net
    puts $root_out "CLOCK_ROOT $domain PIN=$pin CELL=$cell REF=$ref NET=$net CLOCK=$clock PERIOD_NS=$period USER_CLOCK_ROOT=$requested CLOCK_ROOT=$actual"
}
if {[llength [lsort -unique $root_nets]] != 2} {error "Native SYS/AXI clock nets are not distinct"}
close $root_out
report_clock_utilization -clock_roots_only -file {@@EVIDENCE@@/implemented-clock-roots.rpt}
set gray_sources [get_cells -hier -filter {HBM_GRAY_SRC =~ hbm_cpu_pc*}]
set gray_destinations [get_cells -hier -filter {HBM_GRAY_DST =~ hbm_cpu_pc*}]
if {[llength $gray_sources] != 960 || [llength $gray_destinations] != 960} {
    error "Expected 960 source and 960 destination Gray FFs, got [llength $gray_sources]/[llength $gray_destinations]"
}
set gray_src_groups [dict create]
set gray_dst_groups [dict create]
foreach cell $gray_sources {
    dict lappend gray_src_groups [hbm_gray_attr $cell HBM_GRAY_SRC] $cell
}
foreach cell $gray_destinations {
    dict lappend gray_dst_groups [hbm_gray_attr $cell HBM_GRAY_DST] $cell
}
if {[dict size $gray_src_groups] != 320 || [dict size $gray_dst_groups] != 320} {error "Gray bus group count changed"}
set gray_out [open {@@EVIDENCE@@/implemented-gray-buses.txt} w]
set gray_detail [open {@@EVIDENCE@@/implemented-gray-paths.rpt} w]
foreach group [lsort [dict keys $gray_src_groups]] {
    if {![regexp {^hbm_cpu_pc(0[0-9]|[12][0-9]|3[01])_(aw|w|b|ar|r)_(produce|consume)$} $group -> port channel pointer]} {error "Unexpected Gray group $group"}
    if {![dict exists $gray_dst_groups $group]} {error "Missing Gray destination $group"}
    set sources [dict get $gray_src_groups $group]
    set destinations [dict get $gray_dst_groups $group]
    if {[llength $sources] != 3 || [llength $destinations] != 3} {error "$group must have three source/destination FFs"}
    set forward [expr {$channel in {aw w ar}}]
    if {$pointer eq "consume"} {set forward [expr {!$forward}]}
    set expected_source [expr {$forward ? "main_clkout0" : "main_clkout3"}]
    set expected_destination [expr {$forward ? "main_clkout3" : "main_clkout0"}]
    set used_sources {}
    set index 0
    foreach destination [lsort $destinations] {
        if {![string match FD* [get_property REF_NAME $destination]]} {error "Gray destination is not FF: $destination"}
        if {[string toupper [hbm_gray_attr $destination ASYNC_REG]] ni {TRUE 1}} {error "$destination lost ASYNC_REG"}
        if {[string toupper [hbm_gray_attr $destination mr_ff]] in {TRUE 1}} {error "$destination still has generic mr_ff false path"}
        set d [hbm_gray_one [get_pins -of_objects $destination -filter {REF_PIN_NAME == D}] "$group destinationD"]
        set clock_d [hbm_gray_one [get_pins -of_objects $destination -filter {REF_PIN_NAME == C}] "$group destinationC"]
        set destination_clock [hbm_gray_one [get_clocks -of_objects $clock_d] "$group destination clock"]
        set net [get_nets -segments -of_objects $d]
        set q [hbm_gray_one [get_pins -leaf -of_objects $net -filter {DIRECTION == OUT}] "$group direct sourceQ"]
        set source [hbm_gray_one [get_cells -of_objects $q] "$group source FF"]
        if {[lsearch -exact $sources $source] < 0 || [lsearch -exact $used_sources $source] >= 0} {error "$group direct connectivity is not three distinct source FFs"}
        if {![string match FD* [get_property REF_NAME $source]] || [get_property REF_PIN_NAME $q] ne "Q"} {error "$group source is not FF/Q"}
        lappend used_sources $source
        set clock_s [hbm_gray_one [get_pins -of_objects $source -filter {REF_PIN_NAME == C}] "$group sourceC"]
        set source_clock [hbm_gray_one [get_clocks -of_objects $clock_s] "$group source clock"]
        if {$source_clock ne $expected_source || $destination_clock ne $expected_destination} {error "$group unexpected clocks $source_clock/$destination_clock"}
        set path [hbm_gray_one [get_timing_paths -from $clock_s -to $d -delay_type max -max_paths 1] "$group active max-delay path"]
        set requirement [get_property REQUIREMENT $path]
        set slack [get_property SLACK $path]
        if {![string is double -strict $requirement] || abs($requirement - 4.0) > 0.001} {error "$group lost active4ns max-delay requirement: $requirement"}
        if {![string is double -strict $slack] || $slack < 0 || $slack > 100} {error "$group invalid/failing max-delay slack: $slack"}
        set timing [report_timing -from $clock_s -to $d -delay_type max -max_paths 1 -input_pins -return_string]
        puts $gray_detail "GRAY_PATH $group BIT=$index SRC=$q DST=$d"
        puts $gray_detail $timing
        if {![regexp -nocase {max[ _-]*delay} $timing]} {error "$group native report does not identify active Max Delay exception"}
        puts $gray_out "GRAY_BIT $group BIT=$index SRC=$q DST=$d SRC_CLOCK=$source_clock DST_CLOCK=$destination_clock DIRECT=1 ASYNC=TRUE MR_FF=FALSE MAX_DELAY=TRUE REQUIREMENT_NS=$requirement SLACK_NS=$slack"
        incr index
    }
}
close $gray_out
close $gray_detail
report_bus_skew -max_paths 1 -nworst 1 -significant_digits 3 -warn_on_violation -file {@@EVIDENCE@@/implemented-gray-bus-skew.rpt}
puts HBM_GRAY_NATIVE_QUERY_COMPLETE
'''
    if axi_period == 2.5:
        # Keep the established default query byte-identical.
        template = template.replace("abs($period - 4.0)",
            'abs($period - [expr {$domain eq "AXI" ? 2.5 : 5.0}])')
        template = template.replace("abs($requirement - 4.0)", "abs($requirement - 2.5)")
        template = template.replace("active4ns", "active2.5ns")
    if endpoint:
        if axi_clk_freq != 250e6:
            raise ValueError("Endpoint native proof requires SYS250/AXI250")
        template = template.replace("HBM_GRAY_SRC =~ hbm_cpu_pc*",
            "HBM_GRAY_SRC =~ hbm_cpu_pc* || HBM_GRAY_SRC =~ hbm_endpoint_pc31_*")
        template = template.replace("HBM_GRAY_DST =~ hbm_cpu_pc*",
            "HBM_GRAY_DST =~ hbm_cpu_pc* || HBM_GRAY_DST =~ hbm_endpoint_pc31_*")
        template = template.replace("960", "990").replace("320", "330")
        template = template.replace(
            "{^hbm_cpu_pc(0[0-9]|[12][0-9]|3[01])_(aw|w|b|ar|r)_(produce|consume)$} $group -> port channel pointer",
            "{^hbm_(cpu_pc(?:0[0-9]|[12][0-9]|3[01])|endpoint_pc31)_(aw|w|b|ar|r)_(produce|consume)$} $group -> port channel pointer")
    return template.replace("@@EVIDENCE@@", Path(evidence).as_posix())


def _skew_summary_rows(text):
    """Read the native wrapped summary; skip its duplicate contents headings."""
    heading = re.search(r"(?m)^[ \t]*\d+\.[ \t]+Bus Skew Report Summary[ \t]*\r?\n[ \t]*-+[ \t]*\r?\n", text)
    if heading is None:
        raise ValueError("Native bus-skew summary heading/underline missing")
    remaining = text[heading.end():]
    end = re.search(r"(?m)^[ \t]*\d+\.[ \t]+Bus Skew Report Per Constraint[ \t]*\r?\n[ \t]*-+[ \t]*\r?\n", remaining)
    if end is None:
        raise ValueError("Native bus-skew per-constraint heading/underline missing")
    entries = []
    current = None
    for line in remaining[:end.start()].splitlines():
        start = re.match(r"^[ \t]*(\d+)[ \t]+(\d+)[ \t]+(.+)$", line)
        if start:
            if current is not None:
                entries.append(current)
            current = (start[1], [start[3]])
        elif current is not None and line.strip():
            current[1].append(line.strip())
    if current is not None:
        entries.append(current)
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
    tail = re.compile(r"^(.*?)\s+(Slow|Fast)\s+(" + number + r")\s+(" + number + r")\s+(" + number + r")\s*$")
    rows = []
    for identifier, lines in entries:
        match = tail.fullmatch(" ".join(lines))
        if match is None:
            raise ValueError("Malformed wrapped native bus-skew row: " + identifier)
        rows.append((identifier, *match.groups()))
    return rows


def _parse_clock_roots(inventory, report, axi_clk_freq=250e6):
    """Validate the final native physical root inventory, not source intent."""
    sys_period, axi_period = _clock_periods_ns(axi_clk_freq)
    expected = {"SYS": ("BUFG", "main_clkout0"), "AXI": ("BUFG_3", "main_clkout3")}
    required = {"PIN", "CELL", "REF", "NET", "CLOCK", "PERIOD_NS", "USER_CLOCK_ROOT", "CLOCK_ROOT"}
    rows, domains, nets = [], set(), set()
    for line in inventory.read_text().splitlines():
        fields = line.split()
        if len(fields) != len(required) + 2 or fields[0] != "CLOCK_ROOT" or fields[1] not in expected:
            raise ValueError("Malformed native clock-root row: " + line)
        domain = fields[1]
        row = dict(item.split("=", 1) for item in fields[2:])
        if set(row) != required or domain in domains:
            raise ValueError("Duplicate/unexpected native clock-root fields: " + domain)
        buffer, clock = expected[domain]
        period = float(row["PERIOD_NS"])
        if (row["PIN"], row["CELL"], row["CLOCK"]) != (buffer + "/O", buffer, clock):
            raise ValueError("Native clock-root topology mismatch: " + domain)
        if row["REF"] not in ("BUFG", "BUFGCE") or not math.isfinite(period) or abs(period - (axi_period if domain == "AXI" else sys_period)) > .0001:
            raise ValueError("Native clock buffer/period mismatch: " + domain)
        if row["USER_CLOCK_ROOT"] != "X4Y1" or row["CLOCK_ROOT"] != "X4Y1":
            raise ValueError("Native clock root differs from X4Y1: " + domain)
        if not row["NET"] or row["NET"] in nets:
            raise ValueError("Native clock nets must be distinct")
        domains.add(domain)
        nets.add(row["NET"])
        rows.append(dict(domain=domain, **row))
    if domains != set(expected) or not report.read_text().strip():
        raise ValueError("Native clock-root inventory/report incomplete")
    return {"status": "PASS", "required_region": "X4Y1", "rows": rows}


def parse(evidence, axi_clk_freq=250e6, endpoint=False):
    """Validate native output; missing/unrecognized report formats are errors."""
    bound = min(_clock_periods_ns(axi_clk_freq))
    buses, paths, skew, root_inventory, root_report = output_paths(evidence)
    clock_roots = _parse_clock_roots(root_inventory, root_report, axi_clk_freq)
    if endpoint and axi_clk_freq != 250e6:
        raise ValueError("Endpoint native proof requires SYS250/AXI250")
    expected = expected_groups(endpoint)
    group_count = len(expected)
    bit_count = 3 * group_count
    rows = []
    source_pins, destination_pins, keys = set(), set(), set()
    required = {"BIT", "SRC", "DST", "SRC_CLOCK", "DST_CLOCK", "DIRECT", "ASYNC",
        "MR_FF", "MAX_DELAY", "REQUIREMENT_NS", "SLACK_NS"}
    for line in buses.read_text().splitlines():
        fields = line.split()
        if len(fields) != len(required) + 2 or fields[0] != "GRAY_BIT":
            raise ValueError("Malformed native Gray row: " + line)
        name = fields[1]
        row = dict(item.split("=", 1) for item in fields[2:])
        if set(row) != required or name not in expected:
            raise ValueError("Unexpected native Gray fields/group: " + name)
        key = name, int(row["BIT"])
        if key in keys or key[1] not in (0, 1, 2):
            raise ValueError("Duplicate/unexpected native Gray bit: " + str(key))
        keys.add(key)
        if (row["SRC_CLOCK"], row["DST_CLOCK"]) != expected[name]:
            raise ValueError("Native Gray clock direction mismatch: " + name)
        if (row["DIRECT"], row["ASYNC"], row["MR_FF"], row["MAX_DELAY"]) != ("1", "TRUE", "FALSE", "TRUE"):
            raise ValueError("Native Gray topology/exception failed: " + name)
        requirement, slack = float(row["REQUIREMENT_NS"]), float(row["SLACK_NS"])
        if not (math.isfinite(requirement) and abs(requirement - bound) <= .001 and math.isfinite(slack) and 0 <= slack <= 100):
            raise ValueError("Native Gray max-delay timing failed: " + name)
        if row["SRC"] in source_pins or row["DST"] in destination_pins:
            raise ValueError("A native Gray pin was reused across buses")
        source_pins.add(row["SRC"])
        destination_pins.add(row["DST"])
        rows.append(dict(name=name, **row))
    if keys != {(name, bit) for name in expected for bit in range(3)}:
        raise ValueError(f"Native Gray inventory must contain all {group_count} buses / {bit_count} bits")
    detail = paths.read_text()
    if len(re.findall(r"^GRAY_PATH ", detail, re.M)) != bit_count:
        raise ValueError("Native Gray detailed max-delay reports missing")
    skew_text = skew.read_text()
    skew_rows = _skew_summary_rows(skew_text)
    if len(skew_rows) != group_count or len({row[0] for row in skew_rows}) != group_count:
        raise ValueError(f"Native bus-skew summary must contain {group_count} distinct constraints; found " + str(len(skew_rows)))
    violations = 0
    for identifier, _, corner, requirement, actual, slack in skew_rows:
        requirement, actual, slack = map(float, (requirement, actual, slack))
        if not all(map(math.isfinite, (requirement, actual, slack))) or abs(requirement - bound) > .001 or actual < 0 or abs(requirement - actual - slack) > .003:
            raise ValueError("Invalid native bus-skew row: " + identifier)
        violations += slack < 0
    if violations or re.search(r"(?i)\bVIOLATED\b", skew_text):
        raise ValueError("Native bus-skew violations: " + str(violations))
    return {"status": "PASS", "groups": group_count, "bits": bit_count,
        "clock_roots": clock_roots,
        "max_delay_ns": bound, "worst_max_delay_slack_ns": min(float(row["SLACK_NS"]) for row in rows),
        "bus_skew_constraints": group_count, "bus_skew_violations": violations,
        "worst_bus_skew_slack_ns": min(float(row[-1]) for row in skew_rows), "rows": rows}
