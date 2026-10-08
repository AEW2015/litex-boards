# Synthesize the three generated compile inputs, then resume with Explore.
set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build gateware]
set_param general.maxThreads 4
create_project -in_memory -name u250_native_2400 -part xcu250-figd2104-2L-e
set_msg_config -id {Common 17-55} -new_severity {Warning}
read_verilog [file join $root build native usnative_core.v]
set workspace [file normalize [file join $root .. .. ..]]
if {[info exists ::env(U250_REPO_ROOT)]} {set workspace $::env(U250_REPO_ROOT)}
set repository [file join $workspace litedram]
if {[info exists ::env(U250_LITEDRAM_REPO)]} {set repository $::env(U250_LITEDRAM_REPO)}
if {![file exists $repository]} {set repository [file join $workspace u250-litedram]}
read_verilog [file join $repository litedram phy usnative riu_falling_launch.v]
read_verilog [file join $out u250_native_2400.v]
read_xdc [file join $out u250_native_2400.xdc]
set_property PROCESSING_ORDER EARLY [get_files *u250_native_2400.xdc]
synth_design -directive default -top u250_native_2400 -part xcu250-figd2104-2L-e
report_timing_summary -file [file join $out u250_native_2400_timing_synth.rpt]
report_utilization -file [file join $out u250_native_2400_utilization_synth.rpt]
write_checkpoint -force [file join $out u250_native_2400_synth.dcp]
exit
