# Offline comparison only. This IP is never part of the LiteDRAM build.
set root [file normalize [file join [file dirname [info script]] ..]]
set_param general.maxThreads 4
create_project mig_reference [file join $root mig_reference] -part xcu250-figd2104-2L-e -force
create_ip -name ddr4 -vendor xilinx.com -library ip -module_name u250_rdimm_reference
set ip [get_ips u250_rdimm_reference]
# MIG requires the complete physical RDIMM width. ECC is disabled; the
# reference retains the unused ECC devices for primitive/init comparison.
set_property -dict [list CONFIG.C0.DDR4_MemoryType {RDIMMs} \
    CONFIG.C0.DDR4_MemoryPart {MTA18ASF2G72PZ-2G3} \
    CONFIG.C0.DDR4_DataWidth {72} CONFIG.C0.DDR4_TimePeriod {833} \
    CONFIG.C0.DDR4_DataMask {NONE} CONFIG.C0.DDR4_Ecc {false} \
    CONFIG.C0.DDR4_CasLatency {17} CONFIG.C0.DDR4_CasWriteLatency {12} \
    CONFIG.C0.DDR4_InputClockPeriod {3332}] $ip
report_property -all $ip -file [file join $root evidence mig_reference_properties.rpt]
generate_target all $ip
puts "MIG_REFERENCE_GENERATED [get_property IP_FILE $ip]"
close_project
exit
