# FK33 primitive HBM build for Wolf

This recipe builds the bundled direct-primitive HBM replacement, processor-driven
APB initialization/calibration firmware, and 32-port burst test engines. Python
RapidWright analysis is a **required post-route stage** of the build command.
It analyzes the routed checkpoint; it does not change placement, routing or
timing exceptions. Vivado remains the timing authority.

## Reference configuration

| Setting | Value |
| --- | --- |
| Device | `xcvu33p-fsvh2104-2L-e` |
| Board input / HBM reference | 200 MHz / 200 MHz |
| SYS and AXI / APB | 250 MHz / 100 MHz |
| JTAG timing constraint | 15 MHz |
| CPU | VexRiscv Minimal, firmware in integrated ROM, stack/mailbox in SRAM |
| HBM primitive timing model | 1800 MT/s |
| Firmware profiles | nominal 900 MHz boot and destructive 800/900 MHz retune |
| Placement / routing | `Explore` / `Explore` |
| Physical optimization | `AggressiveExplore` before and after routing |
| Thread limits | synthesis 1; implementation up to 8 |

SYS, APB and the reference remain related MMCM clocks. Only the reviewed JTAG
asynchronous boundary and synchronizer exceptions are allowed. LED outputs and
the external BSCAN/TDI boundary are explicitly inventoried as untimed.

The RTL reference was hardware-tested at LiteX commit
`0308e0e709a9c703684e5e9f18c01f256207f9a2` and litex-boards commit
`f1f904b8039d49ff8afe725230345d00e3a3a2bc`. Its routed setup/hold slack was
**+0.003 / +0.007 ns**. Three fresh SRAM configurations passed calibration,
processor transactions, 2/4/32-port burst trials, STOP/drain and destructive
retunes. Four inherited JTAG reset CDC critical findings remain; this is not a
CDC-clean claim. This packaging adds build tools and documentation without
changing that RTL or firmware.

The portable tools were checked in fresh Python environments: generation
reproduced the reference XDC, ROM initialization and firmware binary exactly;
HDL/source ordering and Tcl matched after accounting for paths, line endings
and generated comment headers. The packaged RapidWright stage and native
inspection passed against the preserved reference routed checkpoint. A complete
new synthesis/place/route using this packaged launcher has not been rerun, and
a build on a second machine has not yet been demonstrated.

Reference firmware BIN SHA-256:
`77fc893958544f8c98c3628cfda8d6e7fc0284335827b879dd8c33b89e0732a6`.

## Prerequisites

- Python with the build dependencies; the reference used 64-bit Python 3.11.9.
  Git is used by the setup commands; the build launcher does not require it.
- Vivado with Virtex UltraScale+ device support and an available VU33P
  synthesis/implementation license. The reference used Vivado 2025.2, SW build 6299465.
  Connect any required license-server VPN before building.
- A RISC-V bare-metal GCC toolchain supporting `-march=rv32i_zicsr -mabi=ilp32`.
  The reference used the Vivado/Vitis 2025.2 toolchain, GCC 13.4.0. Different
  compiler versions can change the ROM contents.
- Java usable by RapidWright; the reference used 64-bit Java 21.0.5+11.
  `JAVA_HOME` or `--java-home` optionally overrides normal JVM discovery.
- Internet access for Python/Git dependencies and RapidWright's initial
  JAR/device-data download. Keep sufficient RAM/disk space for implementation
  and the checkpoint, EDIF, source snapshots and reports.

No XCI download is needed for this primitive configuration. The build does not
program hardware, bypass revision checks, write flash or publish branches.

## Windows / PowerShell setup

Clone this branch into a reasonably short path, then run from the repository
root. Preserve `git rev-parse HEAD` when comparing builds between machines.

```powershell
git clone --branch hbm-primitive-blackbox https://github.com/AEW2015/litex-boards.git
cd litex-boards
git rev-parse HEAD

py -3.11 -m venv .venv-fk33
.\.venv-fk33\Scripts\python.exe -m pip install -r litex_boards/software/fk33_hbm/rebuild/requirements-build.txt
.\.venv-fk33\Scripts\python.exe -m pip install --no-deps -e .

py -3.11 -m venv .venv-rapidwright
.\.venv-rapidwright\Scripts\python.exe -m pip install -r litex_boards/software/fk33_hbm/rebuild/requirements-rapidwright.txt

$env:FK33_RISCV_GCC = 'C:\Xilinx\2025.2\gnu\riscv\nt\bin\riscv64-unknown-elf-gcc.exe'
```

Adjust tool paths to your installation. The requirements install a reference
LiteX checkout; other compatible or edited checkouts are accepted. Git commit
metadata is recorded when available, alongside snapshots of the sources used.
The CPU data archives include the generated Verilog, so Scala/SBT and recursive
CPU regression-test submodules are unnecessary.

## Build, including RapidWright

Choose a **new output directory** for every complete build:

```powershell
.\.venv-fk33\Scripts\python.exe -B litex_boards/software/fk33_hbm/rebuild/build.py `
  --output-dir build/fk33-hbm-wolf-01 `
  --vivado C:/Xilinx/2025.2/Vivado/bin/vivado.bat `
  --rapidwright-python .venv-rapidwright/Scripts/python.exe
```

The launcher invokes the supplied tool commands or their PATH defaults without
environment or version preflights. RapidWright uses the current Python unless
`--rapidwright-python` is supplied; `--java-home` is optional. The launcher generates
the RTL and calibration firmware, snapshots inputs, audits source constraints,
implements once, runs Python RapidWright, checks final native timing/coverage and
routing/DRC, and inspects the native HBM model and three reset synchronizer pairs.
It preserves the exact reviewed implementation prefix, including the diagnostic
checkpoint before physical optimization. No automatic retry or clock relaxation
is performed. Run one implementation at a time.

For a quick generation/firmware/source-constraint check without Vivado:

```powershell
.\.venv-fk33\Scripts\python.exe -B litex_boards/software/fk33_hbm/rebuild/build.py --output-dir build/fk33-generate-01 --generate-only
```

That returns `GENERATED`, **not** `BUILD_PASS`. To implement that unchanged
directory, use the complete build command with `--resume` and the same
`--output-dir`. Resume accepts only `GENERATED`; a failed implementation must be
kept and any new attempt must use another output directory.

## Linux paths

Use the same Python 3.11 requirements and repository commit, with
`python3.11 -m venv`, `.venv-fk33/bin/python` and
`.venv-rapidwright/bin/python`. Set `FK33_RISCV_GCC` to the local toolchain and
pass `vivado` or its path to `--vivado`. Set `JAVA_HOME` only to override JVM discovery.
The scripts accept Linux paths; the original full hardware-qualified build was
on Windows. Cross-OS/cross-machine reproduction has not yet been demonstrated.

## Outputs and failure diagnosis

Under the selected output directory:

- `result.json`: final `BUILD_PASS`, `GENERATED` or `FAIL`, plus timing metrics.
- `generation.log`, `generation-command.json`, `input-hashes.json`, `sources/`:
  generation command, package versions and source snapshots.
- `output/gateware/sqrl_fk33.bit` and `sqrl_fk33_route.dcp`: programming image
  and routed checkpoint; use the image only after reviewing the final result.
- `output/software/fk33_hbm/fk33_hbm.elf`, `.bin`, `firmware-build.json`:
  processor firmware, profile/source hashes and compiler version.
- `timing-audit-source.json`, `timing-audit-final.json`,
  `output/gateware/*timing*.rpt`, `*drc.rpt`, `*route_status.rpt`, `*cdc.rpt`:
  constraints, setup/hold/pulse/recovery/removal, coverage and findings.
- `rapidwright/result.json`, `rapidwright/physical-analysis.json`,
  `rapidwright/readable.edf`, `rapidwright/native-paths.rpt`: required Python
  RapidWright results, physical fanout/site-pin inventory and correlation to
  400 native timing paths.
- `inspection/result.json`: hard HBM data rate and implemented reset-chain proof.

Any failed required stage exits nonzero. Read `result.json` and that stage's log;
an existing `.bit` alone is not a passing result. A failed timing run can still
produce useful RapidWright analysis. The prior local 17-gate simulation/hardware
campaign is separate from the portable build checks; its historical receipts
are not reused to certify a new checkout.

## Processor calibration after loading

This firmware intentionally waits in SRAM for host mailbox commands; loading
the image alone does not start calibration. Mailbox base is `0x10000000`;
the layout and command handling are in [main.c](main.c). This configuration uses
JTAGBone on USER chain 2; PCIe DMA is not enabled. Keep JTAG at or below 15 MHz;
the reference host performed memory transactions at 1 MHz. With a working,
serialized LiteX memory transport:

1. Check cooling and PL temperature, then wait until `state` at base + `0x0c`
   is 1 (`WAIT`).
2. Write 900 to `target_mhz` at base + `0x84`, then 1 to `command` at + `0x08`.
3. Poll for state 3 (`READY`), checking `failure` (+ `0x10`) and `errors`
   (+ `0x2c`). Calibration/processor counters and per-stack temperatures are in
   the same structure. State `0x80000000` indicates failure; retain the status
   fields. State 4 means the processor is testing, not failure.
   A successful regular test reports calibration mask `0xffff`, written/checked
   channel masks `0xffffffff`, 3,872 writes, 3,968 reads and zero errors.
4. Command 3 requests STOP/drain. State 5 is `STOPPED`; the runtime drained mask
   and status should be checked before any reconfiguration.

Command 5 performs a **destructive** retune to `target_mhz` (800 or 900), then
calibration and processor data checks. Memory contents are discarded.
Do not run host APB transactions concurrently with processor initialization or
temperature polling. This README does not install or configure a board-specific
JTAG transport, automatically program an engineering-sample card, or grant a
revision-check override. Review the firmware/CSR map before using your own host.

## Demonstrating reproduction

For a controlled comparison, use matching sources and dependencies on both machines.
Record OS, Python, exact Vivado build/patches, GCC and Java versions, commands,
source/generated-input hashes, and native timing reports. Require all build
checks to pass, then test each newly built image on an FK33 for calibration and
processor transactions. Compare configuration payloads separately from `.bit`
header metadata if testing bit identity. RapidWright analysis does not enforce
an identical implementation. The reference margin is narrow, so a passing build
on this machine is not a guarantee that another build will close timing.
