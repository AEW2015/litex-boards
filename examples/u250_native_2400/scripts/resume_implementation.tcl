# Implement the unchanged generated RTL with explicit management-clock coverage.
set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build gateware]
set_param general.maxThreads 4
open_checkpoint [file join $out u250_native_2400_synth.dcp]
source [file join $root scripts management_constraints.tcl]
source [file join $root scripts channel_floorplan.tcl]
opt_design -directive Explore
place_design -directive Explore
phys_opt_design -directive Explore
route_design -directive Explore
phys_opt_design -directive Explore
write_checkpoint -force [file join $out u250_native_2400_final.dcp]
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose -file [file join $out u250_native_2400_final_timing.rpt]
report_pulse_width -all_violators -file [file join $out u250_native_2400_pulse_width.rpt]
report_bus_skew -file [file join $out u250_native_2400_bus_skew.rpt]
report_cdc -details -file [file join $out u250_native_2400_cdc.rpt]
report_methodology -file [file join $out u250_native_2400_methodology.rpt]
report_drc -file [file join $out u250_native_2400_final_drc.rpt]
report_route_status -file [file join $out u250_native_2400_route_status.rpt]
report_utilization -file [file join $out u250_native_2400_final_utilization.rpt]
if {[llength [get_timing_paths -quiet -delay_type max -slack_lesser_than 0]]} {error "Setup timing failed"}
if {[llength [get_timing_paths -quiet -delay_type min -slack_lesser_than 0]]} {error "Hold timing failed"}
write_bitstream -force [file join $out u250_native_2400.bit]
exit
