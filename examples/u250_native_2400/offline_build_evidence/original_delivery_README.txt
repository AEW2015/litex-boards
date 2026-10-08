U250 channel-0 LiteDRAM Native DDR4-2400 example
=============================================

Scope: offline bitstream generation. The user explicitly excluded hardware
validation. No U250 was programmed. SPD, physical calibration, retention and
actual DMA bandwidth have not been tested. This is an experimental RDIMM PHY
candidate, not a hardware-qualified memory subsystem.

Configuration
-------------
FPGA: xcu250-figd2104-2L-e, Vivado 2025.2.
Module: Micron MTA18ASF2G72PZ-2G3B1IG, single rank, x4 devices.
Geometry: 17 row bits, 10 column bits, 4 bank bits (4 groups x 4 banks).
Data: 64 bits, 16 independent DQS groups. ECC DQ/DQS are excluded.
Capacity: 17,179,869,184 bytes (16 GiB), native beat addresses 0..2^28-1.
Reference: channel-0 300 MHz differential clock AY37/AY38.
Controller/DMA: 150 MHz. Internal PHY DFI: 300 MHz.
DDR CK: 1200 MHz, data rate 2400 MT/s, native BITSLICE PLL_CLK: 2400 MHz.
CL17, CWL12; 2400 timing profile; normal-temperature refresh below 7.8 us.
Other channels have reset asserted, CKE low and CS high. Their clocks and
data interfaces are not instantiated. DDR4_RESET_GATE is low.

There is no CPU, DDR Wishbone aperture or memory banking. JTAGBone accesses
only management CSRs. One controller and one local test engine use independent
512-bit DMA reader/writer ports and 128-entry buffering. The default 1:8 DFI
conversion presents one 64-byte beat per controller clock: its theoretical
interface ceiling is 9.6 GB/s (50% of the 19.2 GB/s DDR4-2400 x64 bandwidth).
This ceiling is not a measured result and does not change the physical rate.

Sources and isolation
---------------------
All modified LiteDRAM/LiteX/LiteX-Boards sources are under sources/.
Migen is also copied there; bootstrap.py gives these copies import priority.
Existing BCU1525 sources, bitstreams, logs and evidence were not modified.
evidence/original_sources.json identifies the original repositories and files.
evidence/build_inputs_*.json and manifest.json bind implementation to hashes.
The external Python environment supplies packaging, PyYAML, pyserial, requests
and other generic dependencies. No RISC-V toolchain or firmware build is used.

Build and verification
----------------------
Run from this directory in PowerShell:

  $python = '..\hssio_experimental\integration_env\Scripts\python.exe'
  & $python scripts\build.py --generate-only
  & $python scripts\test_x4_phy.py
  & $python scripts\test_dma_demo.py
  & $python scripts\build.py

Vivado must be available; bootstrap.py defaults to
C:/Xilinx/2025.2/Vivado/bin. Use that same installation to reproduce this build.
Build.py generates the three RTL compile inputs and XDC, then invokes Vivado
synthesis, optimization, placement, routing, reports and write_bitstream.
The saved build/gateware/u250_native_2400.tcl is the direct Vivado entrypoint.
For the delivered checkpoint-based implementation, the additional entrypoint
is scripts/resume_implementation.tcl, using the same synthesized RTL and the
explicit management-clock and channel-floorplan constraints.

To reproduce the delivered default-synthesis / Explore-implementation flow:

  & $python scripts\build.py --generate-only
  & C:\Xilinx\2025.2\Vivado\bin\vivado.bat -mode batch -source scripts\synthesize.tcl -log evidence\rebuild_synth.log
  & C:\Xilinx\2025.2\Vivado\bin\vivado.bat -mode batch -source scripts\resume_implementation.tcl -log evidence\rebuild_impl.log

The delivered implementation used the original generated synthesis Tcl,
followed by resume_implementation.tcl. synthesize.tcl provides its equivalent
synthesis-only entrypoint for a fresh rebuild. A final saved-checkpoint pass,
scripts/correct_jtag_signoff.tcl, corrected the JTAG clock source without
changing placement/routing. The normal rebuild already includes that fix.
All source timestamps/hashes and original/final report versions are retained.
No ILA or VIO is instantiated, so no .ltx probe file is required or supplied.
See evidence/warnings_review.txt for the retained CDC and methodology warnings.

Outputs are under build/gateware/. csr.csv and csr.json are under build/.
No ILA/VIO is instantiated, so an LTX probe file is not required or generated.
Full logs, failed attempts and reports are retained rather than replacing them.

DMA demonstration interface
---------------------------
DMA byte bases are 34-bit CSRs and length is 35 bits, including a single
16-GiB operation. They require 64-byte alignment and reject overflow.
Modes: 0=write, 1=read/verify, 2=write then verify, 3=simultaneous write and
read/verify in disjoint ranges. Mode 2 requires equal read/write bases.
Patterns: 0=32-bit native counter, 1=address-seeded xorshift pseudorandom,
2=64-bit walking one, 3=64-bit walking zero. Patterns cover all 64 data bits.
Each 32-bit counter includes the absolute word address, including high bits.
Read-only verification expects memory to contain the selected pattern.

There are 64-bit cycle, beat and error counters plus first-error address/XOR.
Write timing ends when all native write-data beats retire. A 256-cycle settling
interval is used before verification; native writes have no completion response.
Simultaneous results use total elapsed cycles for combined bandwidth.
Read and write bytes/cycles are reported separately and never confused with
JTAG transport throughput. Efficiency denominator is always 19.2 GB/s.

x4 devices have no DM. Ordinary DMA always writes full beats. For partial
writes, the exclusive engine reads one beat, merges the selected bytes and
writes a complete beat, then reads it back. Set rmw=1, length=64, equal bases,
patch_mask (64 byte enables) and patch_data (512 bits). No other master can
access DDR through this example, so RMW cannot race another memory writer.
Simulations check all 64 individual byte masks, zero/all/alternating masks,
random stalls, the final 64-byte word, addresses above 4 GiB and both engines.

scripts/run_dma.py prints a full 16-GiB sweep plan by default. --execute is for
a future already-trained board behind a LiteX RemoteClient server; it does not
program, initialize or calibrate hardware. No hardware results are supplied.
DMA admission requires DFII controller ownership, PHY ready, training stage 5,
zero training error, BISC-only disabled, VTC enabled and the software admit bit.
The gateware does not assert that calibration has passed by itself. Initialization
commands are recorded in evidence/design_config.json; 16 per-nibble calibration
selectors, bitslips, gates, delay counts and RIU control remain accessible by CSR.

RDIMM and Native PHY changes
---------------------------
The layout/mapping and integrated PHY now distinguish 4-bit strobe groups from
8-bit mask bytes. Every nibble owns its DQS, DQ FIFO accounting, bitslip, gate,
delay selection and one physical BITSLICE_CONTROL. No dummy DM is created.
The DFI converter preserves strobes=16, with_dm=false and is_rdimm=true; the
legacy converter otherwise lost these settings. DDR4 MR5 disables DM for x4.
RDIMM RCW initialization remains enabled independently of ECC/DM. RCD drive
words RC03/04/05 use 0x5, matching the generated MIG reference. The model adds
one registered-command CK to the serializer's existing five-CK command delay,
and adjusts DFI command phases accordingly. These latency changes have source
and geometry/init checks, but no vendor DRAM-model or hardware qualification.

The matching offline MIG reference uses the same module, x4, CL17/CWL12 and
no DM, with ECC disabled. MIG requires its complete physical 72-bit width,
whereas this final Native build intentionally has 64 bits. MIG is not compiled
into the final implementation. Local x4 strobe clocks and SHIFT_90 capture,
VCO_2X clocking, RIU/reset/VTC controls and RCD drive words were compared.
The MIG uses RX/TX_REFCLK_FREQUENCY=300 while the experimental Native PHY
uses 2400 with REFCLK_SRC=PLLCLK; this modeling/configuration difference is
retained and requires qualification. Native ODT uses RTT_WR=120 ohms whereas
the reference MR2 disables write termination. Neither profile is SPD-verified.
Native reset/BISC sequencing and electrical margins still require physical
qualification. Board-level parity/alert handling is not implemented; parity
checking remains disabled in the initialization profile.

Authoritative references retained in references/
------------------------------------------------
AMD U250 XDC (2021-09-09) and board presets (2020-06-16), and the exact Micron
module datasheet. The XDC comments use a different bank numbering convention;
the queried XCU250 device database resolves channel 0 to banks 61/62/63.
Pin identity is checked directly against the official XDC; standards for the
reference clock and DQS are corrected to its DIFF_POD12_DCI values.

https://www.amd.com/en/support/downloads/alveo-downloads.html/accelerators/alveo/u250.html
https://docs.amd.com/r/en-US/ug1289-u200-u250-reconfig-accel/DDR4-DIMM-Memory
https://www.mouser.com/datasheet/2/671/asf18c2gx72pz-3079314.pdf
https://docs.amd.com/r/en-US/ug949-vivado-design-methodology
https://github.com/enjoy-digital/litedram/pull/406
