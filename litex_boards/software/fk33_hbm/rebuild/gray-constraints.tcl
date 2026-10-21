# Separate SYS250/AXI250 FIFO pointers. Keep functional clocks related.
# Bound each individual Gray vector, never an aggregate of unrelated FIFOs.
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
