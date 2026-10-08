U250 channel-0 Native DDR4-2400 / full-range DMA example
======================================================

Source branches: AEW2015/litedram, AEW2015/litex and AEW2015/litex-boards,
each on u250/native-ddr4-2400. These remain branches of the existing forks.
Migen is used unchanged at e19524c963a8342952840983047557707fbe0b6a.
No additional hardware-core repository is needed for this CPU-free design.
repositories.lock.json pins the exact LiteDRAM, LiteX and Migen commits used
for this repository port. After cloning, check out those commits for a fixed
dependency set; keep the litex-boards commit containing this example recorded.

One Micron MTA18ASF2G72PZ-2G3B1IG RDIMM, channel 0, x64 data with 16 x4 DQS
groups, ECC excluded, CL17/CWL12, physical DDR data rate 2400 MT/s. FPGA:
xcu250-figd2104-2L-e. Other channels have RESET_n low, CKE low and CS high.
Full 16-GiB addressing is through two 512-bit native DMA ports; there is no
CPU DDR aperture. Partial writes use exclusive read/merge/full-beat write.
Controller/DMA is 150 MHz, PHY DFI is 300 MHz, DDR CK is 1200 MHz. The native
interface ceiling is 9.6 GB/s, 50% of the physical 19.2 GB/s theoretical rate.

Checkout sibling repositories (from an empty project workspace):

  git clone --branch u250/native-ddr4-2400 https://github.com/AEW2015/litedram.git
  git clone --branch u250/native-ddr4-2400 https://github.com/AEW2015/litex.git
  git clone --branch u250/native-ddr4-2400 https://github.com/AEW2015/litex-boards.git
  git clone https://github.com/m-labs/migen.git
  git -C migen checkout e19524c963a8342952840983047557707fbe0b6a
  cd litex-boards/examples/u250_native_2400
  python -m pip install -r requirements.txt

The bootstrap gives those sibling checkouts import priority. If needed, set
U250_REPO_ROOT to their parent, or U250_LITEDRAM_REPO/U250_LITEX_REPO/
U250_LITEX_BOARDS_REPO/U250_MIGEN_REPO individually. Put licensed Vivado 2025.2
on PATH, or set VIVADO_BIN to its bin directory.

Generate RTL and query the actual Vivado device database, then test:

  python scripts/build.py --generate-only
  python scripts/run_tests.py

This uses executed topology queries; it does not substitute invented U250
BITSLICE locations. A fresh topology query needs Vivado even for generation.
The DMA simulations take several minutes. They cover high addresses, the
last 64-byte word, all byte masks, independent engines, random stalls and
walking/pseudorandom/native-counter patterns. They do not simulate all 16 GiB
of physical DRAM or qualify the vendor PHY primitives.

Build with the delivered default-synthesis / Explore-implementation flow:

  vivado -mode batch -source scripts/synthesize.tcl -log build/synthesis.log
  vivado -mode batch -source scripts/resume_implementation.tcl -log build/implementation.log

Output: build/gateware/u250_native_2400.bit and routed checkpoint/reports.
Alternatively, python scripts/build.py uses the standard LiteX toolchain
directives. Generated .bit/.dcp, local queries, downloaded vendor references
and temporary files are ignored by Git. There is no ILA/VIO or .ltx file.

The original delivered bitstream's hashes, routed reports and tests are in
offline_build_evidence/. These describe the previous isolated source snapshot,
not a newly routed build of every future branch commit. The repository port
also fixes legacy PHY-settings objects without with_dm, keeping their previous
DM default while preserving explicit DM=false for this U250 design.

Fresh upstream rebase and rebuild (2026-10-07)
--------------------------------------------
The three U250 branches were rebased onto upstream master at:
  LiteDRAM:     74522f715d7b10163ef6f6caa09887a90874be83
  LiteX:        3843e71b0a6932acad2fb3e7a36ca8cb060b7261
  LiteX-Boards: 8215d8d9b4b23ed138b7e634bbf87ff2920c4221
The DFI rebase combines upstream configurable serializers/deserializers with
the Native PHY throughput, reset and read-slot options. Exact rebuilt core
commits are in repositories.lock.json and upstream_rebuild_evidence/.

A fresh Vivado 2025.2 synthesis, placement, routing and bitstream generation
completed successfully. Routed setup +0.201 ns, hold +0.011 ns, pulse width
+0.039 ns; worst Gray-pointer bus skew 0.493 ns against a 6.667 ns bound.
Routing and DRC errors: zero. All 141 selected tests passed, including DMA
address/RMW tests and upstream PHY, DFI and stream regressions. A current
LiteEth checkout was needed only for upstream test imports; its exact revision
is recorded in source_catalog.json. Migen remains at the pinned revision.

upstream_rebuild_evidence/ contains fresh reports, executed logs, source and
artifact hashes, the pre-rebase/upstream pins and warning review. Its manifest
binds the locally generated .bit and routed .dcp; binaries are ignored by Git.
The recorded built LiteX-Boards revision precedes this documentation/evidence
commit; compiled RTL/constraints and build scripts are independently hashed
and were unchanged throughout the build. The earlier evidence is preserved.

Hardware is explicitly out of scope. This is an experimental candidate, with
no SPD verification, physical calibration, retention or measured bandwidth.
Calibration/initialization is not autonomous in the CPU-free image: external
CSR management must complete training before DMA admission. Five critical
CDC diagnostics and inactive-channel DCI warnings remain documented in
upstream_rebuild_evidence/warnings_rebuild_review.txt (with earlier results
in offline_build_evidence/warnings_review.txt). RDIMM latency, VTC/reset behavior,
Native/MIG reference-frequency attribute differences and ODT require further
qualification. No hardware success is implied by bitstream generation.

scripts/run_dma.py prints a plan covering all 16 GiB. --execute is intended
for future already-initialized/trained hardware behind LiteX RemoteClient;
it does not program or calibrate a board. JTAG accesses management CSRs only.

Pin audit / MIG comparison references
------------------------------------
Download the official AMD U250 XDC archive from the board support page and
place alveo-u250-xdc.xdc at references/u250_xdc/alveo-u250-xdc.xdc. Then run
python scripts/audit_pins.py. Vendor PDFs/ZIPs are not rehosted here.
scripts/mig_reference.tcl generates a matching offline reference; MIG is
never part of the final LiteDRAM implementation.

https://www.amd.com/en/support/downloads/alveo-downloads.html/accelerators/alveo/u250.html
https://docs.amd.com/r/en-US/ug1289-u200-u250-reconfig-accel/DDR4-DIMM-Memory
https://www.mouser.com/datasheet/2/671/asf18c2gx72pz-3079314.pdf
https://docs.amd.com/r/en-US/ug949-vivado-design-methodology
https://github.com/enjoy-digital/litedram/pull/406
