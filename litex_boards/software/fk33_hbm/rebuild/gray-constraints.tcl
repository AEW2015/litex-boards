# Separate SYS250/AXI250 FIFO pointers. Keep functional clocks related.
# Bound each individual Gray vector, never an aggregate of unrelated FIFOs.
# Keep the two related 250 MHz global clocks rooted in the same region.
# This opt-in physical constraint preserves all functional timing checks.
proc hbm_clock_root_one {objects label} {
    if {[llength $objects] != 1} {error "$label expected one object: $objects"}
    return [lindex $objects 0]
}
set hbm_clock_root_region [hbm_clock_root_one [get_clock_regions -quiet X4Y1] region]
set hbm_clock_root_nets {}
foreach {buffer expected_clock} {BUFG main_clkout0 BUFG_3 main_clkout3} {
    set pin [hbm_clock_root_one [get_pins -quiet ${buffer}/O] output_pin]
    set cell [hbm_clock_root_one [get_cells -of_objects $pin] buffer_cell]
    if {[get_property REF_NAME $cell] ni {BUFG BUFGCE}} {error "Unexpected global clock buffer $cell"}
    set net [hbm_clock_root_one [get_nets -of_objects $pin] direct_output_net]
    set clock [hbm_clock_root_one [get_clocks -of_objects $pin] native_clock]
    if {$clock ne $expected_clock || abs([get_property PERIOD $clock] - 4.0) > 0.0001} {
        error "Unexpected clock or period on $pin: $clock"
    }
    lappend hbm_clock_root_nets $net
    set_property USER_CLOCK_ROOT $hbm_clock_root_region $net
    if {[get_property USER_CLOCK_ROOT $net] ne "X4Y1"} {error "Clock-root assignment did not stick on $net"}
}
if {[llength [lsort -unique $hbm_clock_root_nets]] != 2} {error "SYS and AXI clock nets must remain distinct"}
puts "HBM_CLOCK_ROOTS_REQUESTED region=X4Y1 clocks=main_clkout0,main_clkout3"

# A hierarchical property scan takes seconds on this device. Cache its result
# once per attribute instead of repeating the scan for every three-bit bus.
set hbm_gray_sources [dict create]
set hbm_gray_destinations [dict create]
foreach cell [get_cells -hierarchical -filter {hbm_gray_src =~ hbm_cpu_pc*}] {
    dict lappend hbm_gray_sources [get_property hbm_gray_src $cell] $cell
}
foreach cell [get_cells -hierarchical -filter {hbm_gray_dst =~ hbm_cpu_pc*}] {
    dict lappend hbm_gray_destinations [get_property hbm_gray_dst $cell] $cell
}
if {[dict size $hbm_gray_sources] != 320 || [dict size $hbm_gray_destinations] != 320} {
    error "Expected exactly 320 tagged Gray source and destination buses"
}
set hbm_gray_groups 0
for {set pc 0} {$pc < 32} {incr pc} {
    foreach channel {aw w b ar r} {
        foreach direction {produce consume} {
            set id [format "hbm_cpu_pc%02d_%s_%s" $pc $channel $direction]
            if {![dict exists $hbm_gray_sources $id] || ![dict exists $hbm_gray_destinations $id]} {
                error "Missing tagged Gray bus $id"
            }
            set src [dict get $hbm_gray_sources $id]
            set dst [dict get $hbm_gray_destinations $id]
            if {[llength $src] != 3 || [llength $dst] != 3} {
                error "Gray vector $id must retain exactly three source and destination registers"
            }
            foreach cell $dst {
                if {[string equal -nocase [get_property -quiet mr_ff $cell] true]} {
                    error "Gray vector $id first stage retains the generic false-path marker"
                }
            }
            set src_c [get_pins -of_objects $src -filter {REF_PIN_NAME == C}]
            set dst_d [get_pins -of_objects $dst -filter {REF_PIN_NAME == D}]
            if {[llength $src_c] != 3 || [llength $dst_d] != 3} {
                error "Gray vector $id does not map to flip-flop C/D timing pins"
            }
            set_max_delay -datapath_only 4.0 -from $src_c -to $dst_d
            set_bus_skew 4.0 -from $src_c -to $dst_d
            incr hbm_gray_groups
        }
    }
}
if {$hbm_gray_groups != 320} {error "Incomplete Gray-pointer constraint inventory"}
puts "HBM_GRAY_CONSTRAINTS_APPLIED groups=$hbm_gray_groups bound_ns=4.0"
