# UltraScale external DDR target survey

This is a pad inventory from the local LiteX-Boards platforms, not a native-PHY
build or hardware qualification result. `--with-usnative` is currently provided
only by XEM8320; other targets retain their component PHY until the integrated
native datapath and BIOS support their topology.

| Target | FPGA | Memory | DQ | DQS | DM | BG | Current native status |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| adi_adrv2crr_fmc | xczu11eg | DDR4 | 32 | 4 | 4 | 1 | Pin extraction only |
| alibaba_vu13p | xcvu13p | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |
| aliexpress_rk_xcku5p | xcku5p | DDR4 | 32 | 4 | 4 | 1 | Pin extraction only |
| alinx_axau15 | xcau15p | DDR4 | 16 | 2 | 2 | 1 | Pin extraction only |
| avnet_aesku40 | xcku040 | DDR4 | 32 | 4 | 4 | 1 | ELABORATES with queried topology; no target clock/BIOS build |
| decklink_quad_hdmi_recorder | xcku040 | DDR3 | 64 | 8 | 8 | 0 | Native DDR3 protocol absent |
| enclustra_mercury_xu5 | xczu2eg | DDR4 | 16 | 2 | 2 | 1 | Pin extraction only |
| enclustra_mercury_xu8_pe3 | xczu7ev | DDR4 | 32 | 4 | 4 | 2 | Pin extraction only |
| mlk_cu07_ku15p | xcku15p | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |
| opalkelly_xem8320 | xcau25p | DDR4 | 16 | 2 | 2 | 1 | Existing experimental hardware result |
| sqrl_xcu1525 | xcvu9p | DDR4 | 64 | 8 | 8 | 2 | ELABORATES with queried topology; no target clock/BIOS build |
| xilinx_alveo_u200 | xcu200 | DDR4 | 64 | 16 | 0 | 2 | x4 strobes/no DM unsupported |
| xilinx_alveo_u250 | xcu250 | DDR4 | 64 | 16 | 0 | 2 | x4 strobes/no DM unsupported |
| xilinx_alveo_u280 | xcu280 | DDR4 | 64 | 16 | 0 | 2 | x4 strobes/no DM unsupported |
| xilinx_kcu105 | xcku040 | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |
| xilinx_kcu116 | xcku5p | DDR4 | 32 | 4 | 4 | 1 | Pin extraction only |
| xilinx_vcu118 | xcvu9p | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |
| xilinx_vcu128 | xcvu37p | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |
| xilinx_zcu102 | xczu9eg | DDR4 | 16 | 2 | 2 | 1 | Pin extraction only |
| xilinx_zcu104 | xczu7ev | DDR4 | 64 | 8 | 8 | 2 | Pin extraction only |
| xilinx_zcu106 | xczu7ev | DDR4 | 64 | 8 | 8 | 1 | Pin extraction only |

`sqrl_xcu1525` has four DDR channels in its target; the table describes channel
0. Several others also select a channel at build time. All channels need their
own Vivado topology query and implementation before a board is marked usable.
VCU128 declares two `cs_n` pins and needs explicit rank handling; all other
listed platform resources declare one `cs_n` pin.
The DeckLink target currently reduces its declared x64 DDR3 pads to four byte
lanes for the component PHY. The table shows the platform's physical resource,
not that reduced controller interface. The Alveo targets use x4-strobe RDIMMs;
their 16 DQS pairs and absent DM pins cannot be treated as eight x8 lanes.

All rows have a single differential CK pair in the selected platform resource.
That does not mean the data, command and clock pins occupy one FPGA bank. A
single CK pair can serve a data interface spread over multiple banks, while the
integrated native PHY currently accepts only one bank's PLL clock. The Vivado
query must establish actual bank and byte topology before implementation.

Fresh AES-KU40 channel-0 Vivado 2019.1 discovery found data lanes 0–1 in bank
44 (bytes 2–3) and lanes 2–3 in bank 45 (bytes 0–1). Thus its x32 interface
already crosses two banks. The integrated RTL elaborated with four lanes,
two bank-clock inputs and a 64-bit DFI data bus per phase. This is a resource
and elaboration result, not an implemented bitstream.
The query output is local build data under `C:/ai_tmp`, not committed.

Fresh XCU1525 channel-0 Vivado 2025.2 discovery found eight data lanes across
banks 40, 41 and 42. Their logical-to-physical byte sequence is 41/1, 40/2,
40/1, 40/0, 41/3, 42/0, 40/3, 41/2. A fixed bank or sequential byte-lane
assumption would therefore be incorrect. The queried map passed the native
layout validation. The integrated RTL elaborated with eight lanes, three
bank-clock inputs and a 128-bit DFI data bus per phase. The target has not
provided local PLLs or BIOS training for all eight lanes. The raw Vivado files remain under
`C:/ai_tmp`.

No target in this survey is marked `BITSTREAM GENERATED` from this work.
