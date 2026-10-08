# Derive the channel's SLR from its fixed native sites, rather than assuming
# another board's SLR numbering. Keep controller, DMA and PHY fabric local.
set native [get_cells -hierarchical -filter {REF_NAME == RXTX_BITSLICE}]
set slrs {}
foreach cell $native {
    set site [get_sites [get_property LOC $cell]]
    foreach slr [get_slrs -of_objects $site] {lappend slrs $slr}
}
set slrs [lsort -unique $slrs]
if {[llength $slrs] != 1} {error "Channel-0 native sites must occupy exactly one SLR: $slrs"}
create_pblock channel0_local
resize_pblock [get_pblocks channel0_local] -add $slrs
set fabric [get_cells -hierarchical -filter {IS_PRIMITIVE == TRUE &&
    (REF_NAME =~ FD* || REF_NAME =~ LUT* || REF_NAME =~ RAM* ||
     REF_NAME =~ MMCME4* || REF_NAME =~ PLLE4* ||
     REF_NAME == RXTX_BITSLICE || REF_NAME == BITSLICE_CONTROL || REF_NAME == TX_BITSLICE_TRI)}]
add_cells_to_pblock [get_pblocks channel0_local] $fabric
set_property IS_SOFT FALSE [get_pblocks channel0_local]
puts "CHANNEL0_FLOORPLAN slr=$slrs cells=[llength $fabric] native=[llength $native]"
