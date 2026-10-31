# FK33 primitive HBM build for Wolf

This recipe builds the bundled direct-primitive HBM replacement, processor-driven
APB initialization/calibration firmware, and 32-port burst test engines. The
portable rebuild wrapper includes Python RapidWright analysis after routing.
For the AXI500 direct-target recipe below, run RapidWright separately using its
listed commands. It analyzes the routed checkpoint without changing placement,
routing or timing exceptions. Vivado remains the timing authority.

## AXI500 / HBM1000 PC18 RAM-WE clone review branch

Use `work/hbm-native500-pc18-we-clone` in the AEW2015 litex-boards fork,
with the unchanged LiteX post-routing hook from the paired core below. The
source pair for this iteration is:

- LiteX core: `aa0df3251b2dc129ac0b84cd6628d9111ab6a71e`, available on
  `work/hbm-native500-pc31-control-lut` in the LiteX fork.
- litex-boards code: `3352b06bd06773414b3bd6036b2cb6912e822b4f` on
  `work/hbm-native500-pc18-we-clone`; a later README-only commit may describe
  the same code.

For a fresh checkout, substitute these clones for the reference clone step in
the setup instructions below:

```text
git clone --branch work/hbm-native500-pc18-we-clone https://github.com/AEW2015/litex-boards.git
git clone --branch work/hbm-native500-pc31-control-lut https://github.com/AEW2015/litex.git
git -C litex checkout aa0df3251b2dc129ac0b84cd6628d9111ab6a71e
cd litex-boards
```

Use the Windows or Linux environment setup below. Its requirements file installs
an older reference LiteX checkout. After installing those dependencies, install
both paired worktrees with the active build environment's Python:

```text
python -m pip install --no-deps -e ../litex -e .
```

Set `FK33_RISCV_GCC` to your local RISC-V compiler and put Vivado on PATH. Use a
fresh output directory for each complete build:

```text
python -m litex_boards.targets.sqrl_fk33 --with-hbm --hbm-primitive --hbm-endpoint --hbm-endpoint-traffic --hbm-reference-placement --no-ident-version --hbm-axi-domain --hbm-axi-clk-freq 500e6 --hbm-experimental-500-1000 --sys-clk-freq 250e6 --cpu-type vexriscv --cpu-variant minimal --build --output-dir build/fk33-native500-pc18-we-clone --vivado-max-threads 8 --vivado-place-directive Explore --vivado-post-place-phys-opt-directive AggressiveExplore --vivado-route-directive Explore --vivado-post-route-phys-opt-directive AggressiveExplore --vivado-report-level signoff
```

This direct target command selects SYS250 / AXI500 / HBM1000 / REF200 / APB100.
The portable rebuild wrapper's rated profiles do not select AXI500. Run the
separate read-only RapidWright commands below after implementation. The direct
command does not run the recorded private final audits or hardware tests.

The additional PC18 change adds one helper and one call in the existing FK33
target, within the experimental reference-placement profile. The LiteX core and
normal profiles remain unchanged. After the existing PC31 LUT6/LUT5 control
reductions, the source recipe applies these bounded stages:

1. Select the physical PC22 AW FIFO by `hbm_cpu_pc22_aw_consume` and move only
   its original bit0 RAM-driving binary-pointer FF to `SLICE_X151Y28/HFF2`.
2. Move the existing PC17 AW data LUT6 to `SLICE_X124Y8/A6LUT`, preserving its
   six-input mapping, INIT and both data captures.
3. Add one equivalent quiesce LUT3 at `SLICE_X83Y28/G6LUT` for the critical
   capture, retaining the original LUT's other eleven capture loads. Only
   originally true input-net `DONT_TOUCH` flags are temporarily cleared for
   connection and restored before placement/routing. Source FFs and their CDC
   protection remain unchanged.
4. Add one equivalent PC18 RAM-WE LUT4 at `SLICE_X126Y38/A6LUT`, redirecting
   only the whole eight-leaf RAM32M macro through its public WE pin. The
   original LUT retains the other two RAM32M16 macros and their 32 WE leaves.
   Preserve existing routing while completing the new connections.

The helpers run after normal route/post-route optimization and before the
canonical routed checkpoint, reports and bitstream. Stage and cumulative checks
retain 100,164 FFs, preserve the other 100,163 FF placements/INIT/fixed flags,
and declare exactly one FF relocation, one existing LUT relocation and two LUT
additions. Final fixed FF count is 21, including the original twenty anchors;
final LUT count is 58,060, including the earlier PC31 one-LUT reduction. The
native 256-bit AXI3 ports, clocks, reset architecture, cycle latency, CSR map,
firmware and operating profile remain unchanged. Generated RTL/CSR/firmware
comparisons verify the unchanged behavioral source for this attempt. This branch
does not include the separate proposed Migen `max_fanout=32` experiment.

**New source-build status: COMPLETED.** Full fresh synthesis/implementation
returns native and wrapper exit 0. Actual final fabric timing is **+0.012 ns
setup / +0.008 ns hold**, with **zero setup and zero hold failures**. Bounded
source/image-bound offline checks pass; overall qualification remains
**INCOMPLETE / UNQUALIFIED**. The source-built image has its own closed timing,
topology, protection and functional reviews. These verify all 330 Gray groups / 990 finite paths, 330
bus-skew checks and 128 reset paths; 32 tracker bindings and 400 read-only
RapidWright native paths also pass their bounded checks.

The actual pre-PC18-to-final source checkpoints preserve all 100,164 FF
logical graphs, including 500,820 logical ports, with zero mismatches. Sixteen
Boolean cases and the exact 40 = 32 + 8 WE partition pass. Selected setup
minima improve **+12 to +485 ps** on the cloned eight leaves. The retained
32 improve **+57 to +204 ps**; final hold minima are +225 and +212 ps, respectively.

**This source-built image passes its own 7/7 hardware smoke jobs on the FK33.**
All sixteen MC/PHY calibrations pass (`0xffff`), with processor word/byte accesses
on 31 ports (`0x7fffffff`) and deterministic/random/masked native PC31 traffic.
Actual counts are 3,751 word writes, 3,844 reads, 124 byte writes, 124 byte
reads, 1,240 guards and 3,751 host markers, with zero errors. STOP retains calibration,
drains `0xffffffff` and clears READY flags `0x300`. Owned monitor/transports
close, JTAG returns to 15 MHz and flash is untouched. Observed PL maximum is
28.2 C; HBM stack maxima are 29/29 C. The independent review binds these results
to this exact BIT/DCP. This is one fresh SRAM load and a bounded campaign.

The previous margin-locality source at boards `27fed6d9` also passed its own
seven jobs; its results remain evidence for that earlier image.

Closed source-build evidence (local paths are relative to the LiteHBM workspace):

- Independent offline review: `builds/fk33-native500-20261026/pc18-we-clone-build-20261030-01/final-offline-review.json`, SHA256
  `32383947b8dbbf43703a47c4139dd733e7620e798682664988e2ddd5f6541fb5`.
- Captured source inputs / initial generated outputs: 433 / 24.
- Source-input manifest SHA256: `187e3352012e0a69bf8be028d76b67d72dd51015bea01ba78f1a8c90b0b35172`.
- Generated-output manifest SHA256: `261d95a55c56468a7bcb07a116a73969a42f07daf805b3582c726db98f6142a1`.
- Routed DCP SHA256: `7abf35193c5be6ee9c89acbad49841f7e063ce90073185a96d5f9b8b11a5e8c0`.
- BIT SHA256: `41fc62f99d1a78d70be10b2926c317ff08d075e547d928fd13c9fd8f18bb7459`.
- Independent hardware review: `builds/fk33-native500-20261026/pc18-we-clone-build-20261030-01/smoke-independent-review.json`, SHA256
  `c09b82d2e378c3e57f69bff84085f996437463a9a8256e44b778e5ac61f2ca8f`.
- Final campaign report: `builds/fk33-native500-20261026/pc18-we-clone-build-20261030-01/candidate-report.md`.
- Separately sealed evidence manifest: `builds/fk33-native500-20261026/pc18-we-clone-build-20261030-01/evidence-manifest.json`.
  Report and manifest hashes are recorded separately; the tested source
  checkout remains frozen at `3352b06b`.

This remains an **INCOMPLETE / UNQUALIFIED** experimental clock profile. The
32 primitive-model minimum-period failures remain 2.220 ns modeled versus
2.000 ns operating AXI period. The 98-bit status-bundle hold waiver is
protocol-based and is not an STA pass. The 36 inherited critical CDC findings
remain within the reviewed bounded scope. Fabric margins are tiny; the available
evidence does not establish cross-OS reproduction, PVT or implementation-seed
robustness, whole-HBM capacity coverage or full-native qualification. Actual
HBM frequency was not independently measured. Review your own resulting image
and reports before a separate hardware campaign.

### Separate RapidWright analysis

For a direct-target build, run the read-only RapidWright assistance separately from the
boards repository root, using the prepared RapidWright Python environment. First export
a readable EDIF from the routed checkpoint:

```text
python -c "import importlib.util; from pathlib import Path; s=importlib.util.spec_from_file_location('fk33_rw','litex_boards/software/fk33_hbm/rebuild/rapidwright_assist.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); b=Path('build/fk33-native500-pc18-we-clone').resolve(); o=b/'rapidwright'; o.mkdir(exist_ok=True); (o/'export.tcl').write_text(m.export_tcl(b/'gateware/sqrl_fk33_route.dcp',o/'readable.edf',o/'native-paths.rpt',o/'native-summary.rpt'),encoding='utf-8')"
vivado -mode batch -source build/fk33-native500-pc18-we-clone/rapidwright/export.tcl
```

Then run the existing worker with a 4 GiB Java heap (set `JAVA_HOME` if needed):

```text
python -c "import os,runpy,sys; os.environ['JAVA_TOOL_OPTIONS']='-Xmx4g'; sys.argv=['rapidwright_assist.py']+sys.argv[1:]; runpy.run_path('litex_boards/software/fk33_hbm/rebuild/rapidwright_assist.py',run_name='__main__')" --worker --dcp build/fk33-native500-pc18-we-clone/gateware/sqrl_fk33_route.dcp --readable-edif build/fk33-native500-pc18-we-clone/rapidwright/readable.edf --timing-report build/fk33-native500-pc18-we-clone/rapidwright/native-paths.rpt --hbm-endpoint --checkpoint-stage routed --output build/fk33-native500-pc18-we-clone/rapidwright
```

Review `rapidwright/physical-analysis.json` alongside Vivado timing, route and DRC
reports. RapidWright reads the routed checkpoint; it does not edit the checkpoint or
program hardware.

## Previous read-address fanout review branch

Use `work/hbm-native500-cpu-read-address-fanout` in both AEW2015 forks. The
tested source pair is LiteX `7d3bcb59` and litex-boards `78d00a53`; subsequent
README-only commits describe that source. Activate the LiteX Python environment,
put Vivado and the RISC-V toolchain on PATH (or set `FK33_RISCV_GCC`), then run:

```sh
python -m litex_boards.targets.sqrl_fk33 \
    --with-hbm --hbm-primitive --hbm-endpoint --hbm-endpoint-traffic \
    --hbm-reference-placement --no-ident-version --hbm-axi-domain \
    --hbm-axi-clk-freq 500e6 --hbm-experimental-500-1000 \
    --sys-clk-freq 250e6 --cpu-type vexriscv --cpu-variant minimal \
    --build --output-dir build/fk33-native500-read-address \
    --vivado-max-threads 8 --vivado-place-directive Explore \
    --vivado-post-place-phys-opt-directive AggressiveExplore \
    --vivado-route-directive Explore \
    --vivado-post-route-phys-opt-directive AggressiveExplore \
    --vivado-report-level signoff
```

This direct target command builds the image. Run the separate experimental
RapidWright commands below with `build/fk33-native500-read-address` substituted
for their output path. The portable rebuild wrapper's rated profiles do not
select this AXI500 recipe, and this command alone does not run the recorded
private signoff audits or hardware tests.

The operating profile is SYS250 / AXI500 / HBM1000 / REF200 / APB100. This
revision lowers only the native CPU FIFO read-address fanout hint from 16 to 8;
the reverse SYS250 hint remains 16. No clock, AXI3 interface or cycle latency
changes. In the measured routed image, all 93 RAM-backed CPU banks have lower
peak address-driver loads; the overall peak falls from 22 to 14. The hint is
not a strict eight-load cap. The three reserved-PC31 CPU pointer banks retain
registers but have no RAM address loads; count them separately from RAM banks.

One fresh Vivado 2025.2 Windows build passes the fabric/reset/CDC/control and
read-only RapidWright checks. Fabric margins are +0.006 ns setup / +0.008 ns
hold: global setup margin has not improved. Its own SRAM load passes seven
hardware smoke jobs, all 16 MC/PHY calibrations, CPU accesses on 31 ports and
native PC31 traffic, including guards, markers and STOP/drain cleanup.
The measured BIT SHA256 is
`3a680d29b3b177ae6a41beaa99d0cbf231a2d86b4fefb33898650845fd9fe7f8`.
This remains experimental: the 32 declared primitive-model clock failures and
existing status-bundle hold waiver remain. This result does not establish
cross-OS, PVT, full-capacity or all-application qualification. The normal rated
profiles below are unchanged. The next timing target is native PC31 WREADY
control placement, not another clock-rate change.

## Normal-build clock policy

Normal FK33 builds follow AMD DS923 controller limits. For this -2L device,
the AXI ceiling is 400 MHz at the low-voltage operating point or 450 MHz at
0.85 V. Until the board's operating voltage is verified, use 400 MHz as the
conservative performance target. HBM is limited to 900 MHz / 1800 MT/s;
retain the 200 MHz HBM reference and 100 MHz APB clock.
See [AMD's HBM controller limits](https://docs.amd.com/r/en-US/ds923-virtex-ultrascale-plus/High-Bandwidth-Memory-Controller).

The reference recipe below uses 250 MHz SYS/AXI. The new rated AXI400
candidate selects SYS200 and is described below; it needs fresh timing closure
and hardware qualification. There is no implemented AXI450 profile.
The normal primitive target keeps the rated clock policy and defaults unchanged.
An explicit `--hbm-experimental-500-1000` opt-in is available in the experimental
source branch documented below; it is not an AMD-rated profile or a normal-build
recommendation.

## Reference configuration

### Experimental processor endpoint

The opt-in `--hbm-endpoint --hbm-axi-domain` recipe dedicates PC31 to the
four-command, 512-bit application endpoint with unchanged 256-bit native AXI3
ports. It uses SYS250/AXI250, HBM900 and a 200 MHz reference. It replaces the
BIST engines for this build; the other 31 ports retain processor memory access.
Accidental CPU-window access to PC31 returns an error. Use matching LiteX and
litex-boards endpoint branches containing `HBMEndpointCSR`.

Add these options to the rebuild command below:

```sh
python litex_boards/software/fk33_hbm/rebuild/build.py \
    --hbm-endpoint --hbm-axi-domain --generate-only \
    --output-dir build/fk33-endpoint
```

Generation compiles the matching ROM firmware and checks the source constraints.
To implement that unchanged attempt, replace `--generate-only` with `--resume`
and supply the Vivado, RapidWright Python and optional Java paths as described
below. The endpoint profile additionally audits ten Gray buses and 36 transport
control bits; it retains all 320 CPU Gray-bus checks.

After cold initialization with mailbox command 1, command 6 runs processor-driven
endpoint transactions. It establishes scratch contents with full-strobe writes,
then checks zero/mixed strobes, native lengths 1 through 16, held B/R responses,
four-credit backpressure and a queued fifth command. STOP tests finish owed W
data and held responses before acknowledging drain. Scratch starts at local
offset `0x00100000` on PC31. The normal CPU tests omit this reserved port.

This new image requires its own routed timing, calibration and hardware results.
It does not qualify native AXI500/HBM1000 or inherit the fabric-only timing result.

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

## Portability verification

Run the independent host tests from the repository root:

```sh
python -m unittest discover -s test -p test_fk33_portability.py -v
```

These standard-library tests do not install or launch Vivado, GCC, Java or
RapidWright. They exercise the actual launcher code with real subprocesses,
arbitrary compiler-wrapper names, PATH and explicit tool paths, changed working
directories, spaces/Unicode, UTF-8/BOM and LF/CRLF data, Tcl quoting, source-name
case collisions, binary I/O, and rename/replace behavior. Compiler orchestration
uses explicitly fake compiler outputs; it does not validate firmware compilation.
Tests are separate from the build command and introduce no version/environment
preflight gates. Missing optional Tcl or symlink privileges produce named skips.

The `FK33 portability checks` GitHub Actions workflow runs the suite on x64
Ubuntu 22.04/24.04 and Windows Server 2022/2025, with Python 3.10, 3.11 and 3.12.
It uploads each test log, including failures and capability skips. Windows Server
runners exercise the Windows scripts; they are not a claim of AMD-supported
Vivado installations. No licensed tools or hardware run in these CI jobs.

To exercise a particular writable filesystem or mounted share, point Python's
temporary directory at an existing directory there and run a fresh process:

```sh
TMPDIR=/mnt/your_volume/fk33_test python -m unittest discover -s test -p test_fk33_portability.py -v
```

```powershell
$env:TMPDIR = 'D:\fk33_test'
python -m unittest discover -s test -p test_fk33_portability.py -v
```

The suite prints the actual OS, Python architecture/encoding, temporary path and
observed case-sensitivity behavior. Wrapper tests also execute files from this
directory, so a no-execute mount cannot pass those tests. Case-sensitive and
case-insensitive behavior is tested without requiring either mode. Symlink
availability is reported rather than presumed. A different filesystem needs
its own run; passing on NTFS does not establish SMB, NFS, exFAT or ReFS coverage.

The compatibility target is x86-64 Windows/Linux within the selected Vivado
release's [AMD-supported OS list](https://docs.amd.com/r/2025.2-English/ug973-vivado-release-notes-install-license/Supported-Operating-Systems).
Fast host tests cannot certify every supported OS release or a complete HBM
bitstream build. Full qualification still requires the normal build on that
OS/tool installation and review of its native timing and other outputs.
AMD also documents [filename, path-length and cross-platform naming limits](https://docs.amd.com/r/2025.2-English/ug973-vivado-release-notes-install-license/Vivado-Naming-Conventions).
Passing the Python Unicode/space tests establishes launcher handling, not
support for those names throughout Vivado or the complete dependency chain.

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

## Experimental separate AXI domain

This branch adds `--hbm-axi-domain` to the rebuild launcher and board target.
Use a LiteX checkout with the separate-domain core interface. The local
reference used commit `906eb27990c68e8b30f661201eea22b18ef7c1ee`. Optional
`--core-commit` records reference metadata; it does not enforce a checkout or
clean worktree. Keep `--hbm-axi-domain` when resuming that profile.

This first migration keeps SYS and native AXI at 250 MHz on separate MMCM
outputs, with HBM at 900 MHz and its reference at 200 MHz. CPU, CSRs and
management remain in SYS. Native BIST/AXI/drain run in AXI; CPU requests,
commands and coherent status cross explicitly. CPU FIFO Gray pointers receive
individual 4 ns datapath and bus-skew bounds, checked against the routed design.
The launcher also checks the fourth clock/reset pair and firmware waits for
the cross-domain release acknowledgment. The flag is optional; the existing
shared-SYS build remains the default.

This option keeps the normal AMD-rated clock policy above. Native
timing closure, calibration and processor transactions must qualify each new
image. Simulation alone does not establish those hardware results.

## Rated SYS200 / AXI400 candidate

With a LiteX checkout containing SYS200/AXI400 support, add these arguments
to the rebuild launcher command:

```text
--hbm-axi-domain --hbm-axi-clk-freq 400e6
```

The launcher selects SYS200 / AXI400 / HBM900 / REF200 / APB100. The direct
board-target CLI also requires `--sys-clk-freq=200e6`. The standard MMCM
planner selects VCO1600 with output divisors 8/8/16/4, preserving output and
reset numbering and a 2:1 SYS/AXI ratio. All functional clocks remain related.
Per-vector Gray max-delay and skew bounds are 2.5 ns, and native inspection
checks SYS5 ns / AXI2.5 ns and the requested/actual clock roots. No normal
clock-limit, setup, hold, pulse-width, DRC or RapidWright gate is waived.

Firmware delays/deadlines and the mailbox SYS frequency come from the CSR
clock metadata. The core preserves the control watchdog duration and 15-second
temperature age at SYS200. HBM PLL/PHY/MC tables and destructive 800/900 MHz
profiles are unchanged. This is a separate candidate, not qualification of a
new programming image; require its source-bound native and hardware results.
Resume with the same AXI-rate arguments. AXI250 remains the launcher's default.

## Experimental CPU write-enable fanout build

The boards branch `work/hbm-native500-cpu-enable-fanout` uses LiteX core
`93c19c9c6d2e9ad720648f760c0e2858dd5f9d6f` from
`work/hbm-native500-status-publication`. The measured boards code is
`15ee73efc4a1cf0bbe46b9bad91686d5d19accdc`; later documentation-only commits
on this branch do not change that code.

The source change is two lines in the FK33 target: pass the existing
`pipe_valid_enable_max_fanout=32` option for experimental CPU W buffers only.
Normal configurations pass `None`. No state, pipeline stages, native AXI3
ports, clocks or timing bounds change. No source or test files were added.
Use the direct target command below with these paired revisions importable
and a fresh output directory, such as `build/fk33-native500-cpu-enable-fanout`.
The existing portable `rebuild/build.py` interface still exposes 250/400 MHz.

A fresh Vivado 2025.2 Windows build completed synthesis and implementation
without checkpoint reuse. Final fabric setup/hold were +0.006/+0.010 ns,
with zero failing endpoints, versus +0.002/+0.001 ns for the prior fresh
published-source build. The finite Gray/reset/control audits and read-only
400-path RapidWright analysis passed.

In the first build, measured write-enable driver fanout fell in all 31 source buffer groups;
peak logical loads fell from 300 to 88. `MAX_FANOUT=32` is a synthesis hint,
not a strict final fanout cap. CE setup slack improved in 21 groups; this is
not a claim that every path improved. The candidate passed seven hardware
smoke jobs on one fresh SRAM load, all 16 MC/PHY calibrations, and processor
word/byte accesses on 31 ports. Isolation guards, markers and final STOP/drain
passed; owned transports closed, revision checking and 15 MHz JTAG were
restored. Flash was untouched.

This remains experimental and unqualified: the hard primitive model retains
32 clock failures at 2.000 ns against its declared 2.220 ns minimum. The
98 status paths retain their finite 4 ns bound and protocol-based hold
waiver, not a hold-STA pass. Margins remain small. These Windows builds and
bounded smoke do not establish cross-OS, implementation-variation, PVT,
whole-HBM capacity or aggregate-bandwidth qualification. HBM1000 settings
were programmed and read back, without independent frequency measurement.
A second clean build of the same code and settings also passed the complete
fabric/CDC/control audit and read-only RapidWright, again at +0.006/+0.010 ns
setup/hold. Its own fresh SRAM load passed all seven smoke jobs, 16 MC/PHY
calibrations and processor accesses on 31 ports. These two local builds and
14 total jobs are separate image-bound records. Targeted fanout was measured
on the first image; the repeat does not add a new fanout-cap claim. Both runs
remain experimental, with no cross-OS, seed-variation or PVT qualification.
The older source-publication experiment below is separate historical evidence.

## Experimental SYS250 / AXI500 / HBM1000 candidate

This separate opt-in candidate uses source branch
`work/hbm-native500-status-publication` in both repositories:

- LiteX core: `93c19c9c6d2e9ad720648f760c0e2858dd5f9d6f`.
- litex-boards: `7d6fc10e8ecfb5cbc1f176b1b5b142da4e62828f`.

Use the direct target CLI below with both revisions importable. The portable
`rebuild/build.py` interface remains limited to its 250/400 MHz profiles and does
not expose this 500 MHz target configuration. Use a fresh output directory for
each run. The command keeps synthesis at one thread and implementation at eight,
with the measured Explore/AggressiveExplore directives:

```text
python -m litex_boards.targets.sqrl_fk33 --with-hbm --hbm-primitive --hbm-endpoint --hbm-endpoint-traffic --hbm-reference-placement --no-ident-version --hbm-axi-domain --hbm-axi-clk-freq 500e6 --hbm-experimental-500-1000 --sys-clk-freq 250e6 --cpu-type vexriscv --cpu-variant minimal --build --output-dir build/fk33-native500-status-publication --vivado-max-threads 8 --vivado-place-directive Explore --vivado-post-place-phys-opt-directive AggressiveExplore --vivado-route-directive Explore --vivado-post-route-phys-opt-directive AggressiveExplore --vivado-report-level signoff
```

Provide the RISC-V compiler through the existing `FK33_RISCV_GCC` environment
setting. The measured dependency was `pythondata_cpu_vexriscv` 1.0.1.post407,
data revision `5f0c7a7faf6b65c907a93be6e3723e297d37ee71`; its consumed
`VexRiscv_Min.v` was 198,950 bytes with SHA256
`758874a989724fe2b92a5413b43265c9a5129638fee9d7358fcd6724992b598f`. The package
version alone does not identify those source bytes. `--no-ident-version` omits the
changing build timestamp from the identifier ROM; it does not establish bitwise
or cross-OS build repeatability.

The candidate uses SYS250 / native AXI500 / software-programmed HBM1000 /
REF200 / APB100 MHz with native 256-bit AXI3 ports and the PC31 endpoint. It adds
status-publication stages in the existing runtime path; the normal defaults and
rated profiles are unchanged. Its image reused an equivalent synthesis result,
not a fresh synthesis. Two fresh SRAM loads passed 125 bounded hardware jobs in
total (7 smoke and 118 qualification), including all 16 calibration checks and
31 CPU-port checks. The source-bound fabric report had +0.002 ns setup WNS,
+0.010 ns hold WHS and zero setup/hold endpoint failures.

This remains an experimental, unqualified candidate. The primitive timing model
still reports a 2.220 ns minimum period against the 2 ns AXI clock. The reviewed
98-bit status hold exception depends on the documented protocol and is not an STA
pass. These results do not establish full-native qualification, whole-HBM address
coverage, PVT qualification or cross-OS routed repeatability.

Read-only RapidWright analysis also passed on the tested routed checkpoint.
RapidWright remains an integrated stage of the existing portable build flow.
For the direct-target command above, run the following analysis separately from
the boards repository root, using the RapidWright Python environment prepared
earlier. First generate its readable-EDIF export script:

```text
python -c "import importlib.util; from pathlib import Path; s=importlib.util.spec_from_file_location('fk33_rw','litex_boards/software/fk33_hbm/rebuild/rapidwright_assist.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); b=Path('build/fk33-native500-status-publication').resolve(); o=b/'rapidwright'; o.mkdir(exist_ok=True); (o/'export.tcl').write_text(m.export_tcl(b/'gateware/sqrl_fk33_route.dcp',o/'readable.edf',o/'native-paths.rpt',o/'native-summary.rpt'))"
vivado -mode batch -source build/fk33-native500-status-publication/rapidwright/export.tcl
```

Then run the existing worker with a 4 GiB Java heap. `JAVA_HOME` can select the
installed JVM as described in the prerequisites:

```text
python -c "import os,runpy,sys; os.environ['JAVA_TOOL_OPTIONS']='-Xmx4g'; sys.argv=['rapidwright_assist.py']+sys.argv[1:]; runpy.run_path('litex_boards/software/fk33_hbm/rebuild/rapidwright_assist.py',run_name='__main__')" --worker --dcp build/fk33-native500-status-publication/gateware/sqrl_fk33_route.dcp --readable-edif build/fk33-native500-status-publication/rapidwright/readable.edf --timing-report build/fk33-native500-status-publication/rapidwright/native-paths.rpt --hbm-endpoint --checkpoint-stage routed --output build/fk33-native500-status-publication/rapidwright
```

Inspect `rapidwright/physical-analysis.json` alongside Vivado's final timing,
route and DRC reports. This worker reads the checkpoint and never repairs
timing or programs hardware. The normal `--attempt` entry point consumes the
portable launcher's completed attempt and receipts; direct-target output uses
the explicit DCP/EDIF commands above.

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


## Processor-configured RTL traffic experiment

The opt-in traffic build keeps the PC31 four-command endpoint, native AXI3
interface and SYS250/AXI250/HBM900/reference200 MHz profile. The processor
configures runs; RTL supplies and checks every data beat. The default build and
`--hbm-endpoint` CSR-stream example remain available.

```sh
python litex_boards/software/fk33_hbm/rebuild/build.py \
    --hbm-endpoint --hbm-endpoint-traffic --hbm-axi-domain \
    --output-dir build/fk33-endpoint-traffic
```

Use the same Vivado, RapidWright Python and optional Java arguments described
above. `--generate-only` performs generation and source auditing without CAD.
This experiment requires fresh native timing, netlist and hardware qualification;
adding this option alone does not establish a hardware bandwidth result.

After calibration, mailbox command 7 captures `traffic_mode`,
`traffic_cycles_limit` and `traffic_seed`. Mode 0 seeds the full reserved PC31
2 MiB region at byte address `0x1f0200000`; modes 1/2/3 run write/read/mixed traffic.
Timed modes require 1 through 2^28 SYS cycles. Read and mixed modes require a
successful seed with the same pattern seed. STOP or lost admission invalidates
that seed, so seed again after resume or reinitialization. Command 6 belongs to
the CSR-stream build and is unavailable in the traffic build. Existing commands
1, 3, 4 and 5 retain their initialization, stop, CPU stress and retuning roles.

The append-only mailbox fields occupy words 40 through 61; the first 40 words
retain their existing ABI. Results contain frozen elapsed SYS cycles, accepted
native 256-bit write/read beat counts, completed write/read burst counts,
accepted AW/AR counts, packed 512-bit W/R counts, channel stall counts and
minimum/maximum application descriptor-to-response latency in SYS cycles.
Elapsed cycles include final drain. Compute payload bandwidth on the host as
`native_beats * 32 * sys_hz / traffic_cycles`, independently for reads and
writes; mixed aggregate bandwidth is their sum. Latency includes endpoint
queuing and response handling and is not an isolated DRAM timing measurement.

Firmware polls completion with thermal and timeout checks, then copies the
stable counters. Engine errors request STOP and fail closed without resetting
outstanding AXI work. A host may request runtime STOP during command 7; the CPU
can process mailbox command 3 after the active run returns and drains.


## Random address and masked-write qualification

The traffic build also reports `HBM_ENDPOINT_RANDOM=1` and accepts
`traffic_random=1` in processor command 7. The existing build flags, native
AXI3 ports, four-command window and clock profile remain the same.
`traffic_random=0` retains sequential modes 0 through 3.

Timed random modes select among the 4,096 reserved 512-byte slots using
independent captured PRNG streams for writes and reads, variable AXI3 lengths
of 1 through 16 native beats, address-dependent data and byte strobes. Seed
and random setting identify the known memory image. Start
with finite mode 0 (`cycles=0`) to seed the whole 2 MiB region, then finite
mode 5 to verify it. Mode 4 is a one-time finite masked epoch: selected bytes
change while disabled bytes and each slot's unwritten tail retain their seed
values. This masked phase visits every slot once using a seeded permutation.
Mode 5 reads every byte using full-length bursts to verify that complete
composite image. Modes 4 and 5 require `traffic_random=1` and `cycles=0`.

Use mode 5 before and after timed random write, read or mixed jobs. Mixed
writes reproduce the current expected composite image, so read/write overlap
does not create a data race in the checker. STOP, loss of admission or a fatal
error invalidates seed, masked-image and full-verification state. Restart with
a full seed rather than reusing a partially completed masked epoch.

Mailbox words 0 through 61 retain their offsets. Words 62 through 70 append
`traffic_random`, enabled-write-byte low/high words, AW/AR accepted native-beat
sums, AW/AR accepted packed-word sums and AW/AR length-seen masks. Status bits
9 and 10 report a valid masked image and a completed full-region verification.
All statistics freeze after DONE and drain. Reconstruct enabled write bytes as
`lo + (hi << 32)` on the host; this counts asserted WSTRB bytes separately from
native bus bytes (`write_beats * 32`). For variable or odd burst lengths, use
the accepted descriptor sums to reconcile retired native beats and packed
512-bit words. Length-seen bit `n` denotes native length `n + 1`.

The new phase checks require their own simulation, native build and hardware
evidence. The earlier sequential traffic qualification does not establish
random or masked-write performance.


## Concurrent PC31 and PC30 traffic

Add `--hbm-endpoint-two-port` alongside `--hbm-endpoint`,
`--hbm-endpoint-traffic` and `--hbm-axi-domain` to instantiate two independent
four-command engines. PC31 keeps CSR bank 9 and the existing mailbox ABI;
PC30 uses CSR bank 10. Both reserve local 0x200000 through 0x3fffff on their
own native pseudochannel. Ordinary processor transactions and stress skip
both ports. This option uses the existing SYS250/AXI250/HBM900/ref200 profile.

Processor command 7 retains the PC31-only operation. Command 8 captures the
primary request and the secondary request independently, validates both
before launching either, then starts both engines with consecutive MMIO
writes. It polls both errors, DONE and response retirement before publishing
statistics. The host must measure overlap from actual BUSY/accepted-command
observations; the starts are not promised to occur in the same cycle.

Mailbox words 0 through 70 retain their offsets. Words 71 through 74 contain
`traffic2_mode`, `traffic2_cycles_limit`, `traffic2_seed`, and `traffic2_random`.
Words 75 through 101 contain the secondary's 27 statistics in the same order
as the primary snapshot: status, errors, cycles, native write/read beats,
completed write/read bursts, AW/AR counts, packed W/R words, AW/W/AR stalls,
read wait, write/read minimum/maximum latency, enabled-byte low/high words,
AW/AR accepted native beats, AW/AR accepted packed words, and AW/AR LEN masks.
Word 102 adds `traffic_pair_elapsed_cycles`: a processor-observed common SYS
interval starting immediately before the first START and ending after both
engines report DONE and drain. It includes start skew, polling and retirement;
use it with the sum of retired bytes for aggregate bandwidth. Each engine also
retains its own elapsed-cycle counter. Both per-port counters must fit inside
this common interval. Each engine has its own seed, image qualification and
counters. Seed/profile validation assumes firmware owns the configuration CSRs; configure requests
through the mailbox instead of editing engine CSRs during a command.

A global STOP closes both engines and the existing runtime barrier waits for
both to drain and quiesce. Errors and timeouts request STOP without resetting
busy AXI transactions. Report each port's bandwidth from its own retired
counters and elapsed cycles; separately establish simultaneous activity before
claiming aggregate throughput. Single-port qualification does not qualify
this optional two-port design; it requires its own build and hardware evidence.
