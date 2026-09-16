# XEM8320 DDR4 profiles

The default target uses component-mode `USPDDRPHY` (ISERDESE3/OSERDESE3).
`--ddr-rate 1000` selects its 125 MHz system clock. `--ddr-rate 2000
--overclock` selects 250 MHz with a phase-related 1 GHz serializer clock and
an independent 500 MHz IDELAYCTRL reference. DDR4-2000 is buildable for
laboratory testing, but its known 1.600 ns ISERDESE3 minimum-period requirement
against a 1.000 ns clock produces a -0.600 ns pulse-width violation; no timing
or DRC check is waived. The 1000 MT/s component profile is the baseline
timing-qualified configuration.

```sh
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --ddr-rate 1000 --with-dma --dma-data-width 128 --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --ddr-rate 1000 --sdram-debug --with-dma --dma-data-width 256 --with-dma-bank-group-interleaving --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --ddr-rate 2000 --overclock --sdram-debug --with-dma --dma-data-width 256 --with-dma-bank-group-interleaving --build
```

Component DMA admission starts disabled. BIOS enables it only after calibration
and the mandatory final memory test pass; failed or repeated initialization
revokes admission. `--sdram-debug` enables component command-delay and
write-latency calibration detail. It does not report HSSIO eye windows.
The component target selects IDELAYCTRL's simulation-device spelling from the
`vivado` executable on `PATH`, which is also the implementation executable:
Vivado 2026.1 and later use `ULTRASCALE_PLUS`; older or unavailable tools use
the portable `ULTRASCALE` spelling.

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
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2666.667 --usnative-debug --with-dma --dma-data-width 256 --with-dma-bank-group-interleaving --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2666.667 --with-dma --dma-data-width 256 --with-dma-bank-group-interleaving --usnative-dma-calibration --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 2933.333 --overclock --with-dma --build
python -m litex_boards.targets.opalkelly_xem8320 --toolchain vivado --with-usnative --ddr-rate 3200 --overclock --usnative-debug --with-dma --dma-data-width 256 --build
```

| Option | Behavior |
|---|---|
| `--with-usnative` | Native PHY and normal BIOS calibration; defaults to 2400 MT/s. |
| `--usnative-debug` | Verbose calibration windows and optional trace hardware. |
| `--usnative-dma-calibration` | Opt-in DMA calibration. Requires USNative, DMA, a 256-bit port, and paired bank-group interleaving; independent of `--usnative-debug`. |
| `--sdram-debug` | Component-PHY calibration diagnostics; invalid with `--with-usnative`. |
| `--with-dma` | DMA integrity/bandwidth engine and `native_dma` BIOS command. Does not run DMA automatically. |
| `--dma-data-width 128\|256` | Fabric DMA port width; default 128. Requires DMA when selecting 256. |
| `--with-dma-bank-group-interleaving` | Experimental paired 256-bit DMA path. Requires DMA and `--dma-data-width 256`; works with either PHY. |
| `--ddr-rate` | Component: 1000 or 2000 MT/s. Native: 2400, 2666.667, 2933.333, or 3200 MT/s. |
| `--overclock` | Required for component 2000 and native 2933.333/3200; does not waive timing checks. |

### Clean experimental build dependencies

The USNative target requires coordinated changes in all three projects:
LiteX-Boards supplies the target and flags, LiteDRAM supplies the native PHY and
paired controller path, and LiteX supplies the BIOS calibration routine. The
new DMA-calibration flag is currently a local, unpublished change; use matching
review checkouts until the coordinated branches are published.

With those checkouts in sibling `litex`, `litedram`, and `litex-boards`
directories, create and activate a virtual environment (`python -m venv .venv`,
then `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate`
in Bash). From their parent directory, install into that active environment:

```sh
python -m pip install -e ./litex -e ./litedram -e ./litex-boards
```

Use the normal LiteX build prerequisites, including Migen, the VexRiscv CPU data
package, a RISC-V compiler, Make, and Vivado. Select Vivado on PATH and run a
build command above with a fresh `--output-dir`. Native device queries run for
each build; no saved connection map or workstation firmware override is needed.
`--usnative-dma-calibration` does not enable DMA, debug, or a wider port implicitly.
Fresh hardware qualification of this entry point and the updated paired-write
rejection logic is still required; software/RTL generation alone is insufficient.

The physical channel remains x16 and the PHY ratio remains 1:4. In the
USNative profiles, the CPU clock is half the controller clock (150 to 200 MHz).
In the component profiles, the CPU runs at the controller/system clock (125 or
250 MHz). The initial profile uses the standard VexRiscv CPU and JTAG UART;
video is not supported with the USNative clock tree.
The 256-bit mode uses the standard width converter, not the previous local
paired-port bank-group controller. Its bandwidth must be measured separately.
`--with-dma-bank-group-interleaving` is the distinct opt-in paired-port path:
it enables controller bank-group scheduling, maps paired commands to opposite
groups, and uses two 128-bit native ports to present one ordered 256-bit DMA
endpoint. It supplies drain and sticky-error gating to the benchmark. The CPU
and all other masters use the same opt-in controller mapping, so it must be
tested as a whole-system configuration rather than treated as a width-converter
optimization.

The earlier local paired-controller experiment reported 4.725 GB/s writes and
4.760 GB/s reads at 2666.667 MT/s. It is retained only as a historical
comparison: the portable paired path has its own measured 2666.667 MT/s result
below. That result does not apply to other rates or configurations without their
own integrity, timing, and hardware evidence.

BIOS runs calibration and its normal memory checks at startup. Optional DMA
runs only when explicitly commanded. `native_dma` defaults to a destructive
64 MiB scratch test starting at 0x41000000. Flushes precede DMA, and CPU execution
must remain outside that range. A fatal DMA timeout requires reconfiguration.

Report calibration and memory-test results separately. Record CPU memspeed in
MiB/s and DMA write/read in decimal GB/s, with efficiency against the x16 raw
peak. Debug windows are sampled calibration pass ranges, with potentially
search-limited endpoints; they are not complete eye scans or thermal qualification.
No new configuration is hardware-qualified merely because generation succeeds.

The paired 2666.667 MT/s configuration was tested on one XEM8320 with Vivado
2026.1 on 2026-09-16. It passed three calibration/memory checks, five DMA tests
(including full 1 GiB PRBS write/read/reread), and CPU-induced first/last-word
corruption detection followed by repair/reread. Sustained full-range DMA was
4.734 GB/s write and 4.760 GB/s read (88.76% and 89.25% of physical peak).
Final setup/hold slack was +0.002/+0.007 ns, with zero failing endpoints,
zero routing errors and no clock-period/pulse-width violations. This result
does not qualify other rates or temperature/power-cycle behavior.

This paired result is historical evidence for that earlier configuration. A
later width-converted 2666.667 MT/s debug run showed an intermittent DMA
counter failure and remains unqualified. Do not combine that run with the
historical throughput or calibration evidence above.

## Component PHY validation

The standard `USPDDRPHY` (ISERDESE3/OSERDESE3) x16, four-phase component path
was hardware-tested with the DMA admission gate enabled only after normal BIOS
initialization. Each entry below passed three initialization/controller-memory
checks and five DMA checks, including a full 1 GiB PRBS write and reread.

| Profile | DDR rate | DMA port | DMA write / read | Write / read efficiency | Setup / hold / pulse slack |
|---|---:|---:|---:|---:|---:|
| Standard global `tCCD_L=8` | 1000 MT/s | 128-bit | 0.905 / 0.918 GB/s | 45.24% / 45.90% | +0.146 / +0.017 / +0.139 ns |
| Paired bank groups | 1000 MT/s | 256-bit | 1.797 / 1.815 GB/s | 89.85% / 90.77% | +0.093 / +0.010 / +0.127 ns |
| Paired bank groups | 2000 MT/s | 256-bit | 3.569 / 3.584 GB/s | 89.23% / 89.60% | +0.083 / +0.014 / -0.600 ns |

The 1000 and 2000 MT/s paired configurations also passed CPU/DMA
interoperability: three expected corruption detections and zero unexpected
failures in each case. The 2000 MT/s profile remains an explicit experimental
profile. Its fabric setup and hold checks are positive, but its -0.600 ns
ISERDESE3 pulse-width violation means it is not fully STA-qualified.

The original component-PHY RTL is unchanged. The BIOS restores DQS increment
accounting before PHY reset to make repeated initialization coherent. After full
placement and routing, a verified ROM-only update added the hardware-timer DMA
wait; placement, routing, clocks, and constraints were retained. This result
does not qualify other rates, temperature, or power-cycle behavior.
