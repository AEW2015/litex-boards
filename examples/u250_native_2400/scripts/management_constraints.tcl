# The USER-chain clock is independent of the board reference. Bound it at 30 MHz.
set bscan [get_cells -hierarchical -filter {REF_NAME == BSCANE2}]
if {[llength $bscan] != 1} {error "Expected one management BSCANE2"}
# UltraScale+ INTERNAL_TCK covers both fabric and the internal TDO register.
# https://docs.amd.com/r/en-US/ug570-ultrascale-configuration/Applications
create_clock -name jtag_tck -period 33.333333333 [get_pins -of_objects $bscan -filter {REF_PIN_NAME == INTERNAL_TCK}]
set_clock_groups -asynchronous -group [get_clocks jtag_tck] \
    -group [get_clocks -include_generated_clocks clk3000_p]
# Asynchronous module reset is governed by initialization hold times, not CK.
set_false_path -to [get_ports ddram0_reset_n]
# Each Gray counter crossing has its own bus-skew bound: one fastest source UI.
# Bus-skew checks remain active even though asynchronous setup/hold is excluded.
foreach direction {rx tx} {
    foreach counter {0 1} {
        set prefix main_jtagbone_phy_${direction}_cdc_cdc_graycounter${counter}_q_reg
        set source [get_cells -hierarchical -filter "NAME =~ ${prefix}*"]
        if {[llength $source] != 3} {error "Expected three Gray-pointer bits for $prefix"}
        set q [get_pins -of_objects $source -filter {REF_PIN_NAME == Q}]
        set nets [get_nets -of_objects $q]
        set d [get_pins -leaf -of_objects $nets -filter {REF_PIN_NAME == D}]
        set destination [get_cells -of_objects $d -filter {mr_ff == TRUE}]
        if {[llength $destination] != 3} {error "Expected three first-stage Gray synchronizers for $prefix"}
        set_bus_skew 6.666666667 -from $source -to $destination
    }
}
