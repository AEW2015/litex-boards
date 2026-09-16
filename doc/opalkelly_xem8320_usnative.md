# Experimental XEM8320 native DDR4

Requires coordinated experimental LiteDRAM and LiteX BIOS branches. The default
XEM8320 target continues to use USPDDRPHY. Select `--with-usnative` explicitly;
this path requires `--toolchain vivado` and queries that installation's device
connections for every build. Set the Vivado executable in PATH, or pass
`--vivado /path/to/vivado` for the query runner (implementation still uses PATH).
Generated Tcl, maps, logs and RTL stay under the build output directory.
The XEM8320 path has been exercised with Vivado 2026.1. A successful query or
implementation does not qualify a different Vivado release or board revision.
The 2933.333 and 3200 MT/s options are deliberately buildable for laboratory
hardware experiments, even if timing does not close. They are not signoff
profiles: retain and review the timing reports separately from a calibration or
memory-test pass. These rates retain the actual constraints rather than waiving
them, including the 0.375 ns `PLL_CLK0` requirement and the 2.667 ns TX-control
requirement at 3200 MT/s. At 3200 only, `--overclock` also downgrades Vivado's
known `PDRC-182` 1600 MHz PLLE4-VCO versus 1500 MHz limit to a warning so an
experimental bitstream can be generated. It does not waive any other DRC or
timing check; 2933.333 does not receive this downgrade.

```sh
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2400 --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2666.667 --usnative-debug --with-dma --dma-data-width 256 --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2933.333 --overclock --with-dma --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 3200 --overclock --usnative-debug --with-dma --dma-data-width 256 --build
```

| Option | Behavior |
|---|---|
| `--with-usnative` | Native PHY and normal BIOS calibration; defaults to 2400 MT/s. |
| `--usnative-debug` | Verbose calibration windows and optional trace hardware. |
| `--with-dma` | DMA integrity/bandwidth engine and `native_dma` BIOS command. Does not run DMA automatically. |
| `--dma-data-width 128\|256` | Fabric DMA port width; default 128. Requires DMA when selecting 256. |
| `--ddr-rate` | 2400, 2666.667, 2933.333, or 3200 MT/s. |
| `--overclock` | Required for 2933.333/3200; does not waive timing checks. |

The physical channel remains x16 and the PHY ratio remains 1:4. CPU clock is
half the controller clock (150 to 200 MHz). The initial profile uses the
standard VexRiscv CPU and JTAG UART; video is not supported with this clock tree.
The 256-bit mode uses the standard width converter, not the previous local
paired-port bank-group controller. Its bandwidth must be measured separately.

BIOS runs calibration and its normal memory checks at startup. Optional DMA
runs only when explicitly commanded. `native_dma` defaults to a destructive
64 MiB scratch test starting at 0x41000000. Flushes precede DMA, and CPU execution
must remain outside that range. A fatal DMA timeout requires reconfiguration.

Report calibration and memory-test results separately. Record CPU memspeed in
MiB/s and DMA write/read in decimal GB/s, with efficiency against the x16 raw
peak. Debug windows are sampled calibration pass ranges, with potentially
search-limited endpoints; they are not complete eye scans or thermal qualification.
No new configuration is hardware-qualified merely because generation succeeds.
