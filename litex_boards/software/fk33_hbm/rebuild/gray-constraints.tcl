# Separate SYS250/AXI250 FIFO pointers. Keep functional clocks related.
# Bound each individual Gray vector, never an aggregate of unrelated FIFOs.
set hbm_gray_groups 0
for {set pc 0} {$pc < 32} {incr pc} {
    foreach channel {aw w b ar r} {
        foreach direction {produce consume} {
            set id [format "hbm_cpu_pc%02d_%s_%s" $pc $channel $direction]
            set src [get_cells -hierarchical -filter "hbm_gray_src == $id"]
            set dst [get_cells -hierarchical -filter "hbm_gray_dst == $id"]
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
