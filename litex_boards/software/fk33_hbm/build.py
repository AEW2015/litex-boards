"""Build the freestanding FK33 destructive-clock processor demonstration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(output_dir, csr_json=None, profile_path=None):
    """Return ELF/BIN paths and little-endian ``rom_words`` for init_rom()."""
    source = Path(__file__).resolve().parent
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if profile_path is None:
        from litex.soc.cores.ram import xilinx_usp_hbm2
        profile_path = Path(xilinx_usp_hbm2.__file__).with_suffix("") / "fk33_hbm_profile.json"
    profile_path = Path(profile_path).resolve()
    p = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    if tuple(p[k] for k in ("reference_mhz", "hbm_mhz", "stacks", "regions_per_stack", "region_stride")) != (200, 900, 2, 8, 0x20000):
        raise ValueError("Firmware requires the reviewed 200/900 MHz two-stack boot profile")
    profiles = p["destructive_clock_profiles"]
    approved = {"800": ([0xa402, 0x6300, 0x1212], 3120), "900": ([0xa482, 0x9600, 0x1414], 3510)}
    if set(profiles) != set(approved):
        raise ValueError("Only reviewed 800/900 MHz destructive profiles are supported")
    for mhz, (words, refresh) in approved.items():
        if profiles[mhz]["pll_words"] != words or profiles[mhz]["refresh_period_cycles"] != refresh:
            raise ValueError("Unreviewed runtime PLL/refresh values")
    if [row["value"] for row in p["pll"][:3]] != profiles["900"]["pll_words"]:
        raise ValueError("Boot PLL table differs from the 900 MHz runtime profile")
    for name, count in (("ms_pre", 11), ("pll", 16), ("ms_post", 3), ("phy", 9), ("mc", 42)):
        if len(p[name]) != count:
            raise ValueError(f"Incomplete {name} table")
        for row in p[name]:
            if not (0 <= row["address"] <= 0x3ffffc and row["address"] % 4 == 0 and 0 <= row["value"] <= 0xffffffff):
                raise ValueError("Invalid APB table entry")
    if any(row["address"] == 0x18 for row in p["phy"]):
        raise ValueError("PHY06 writes are forbidden")
    if len(p["selectors"]) != 4 or any(not 0 <= x <= 0xffffffff for x in p["selectors"]):
        raise ValueError("Four calibration selectors required")
    layout = csr_json
    if isinstance(layout, (str, Path)):
        layout = json.loads(Path(layout).read_text(encoding="utf-8-sig"))
    addresses = {"HBM_CONTROL_BASE": 0xf0003000, "TIMER_BASE": 0xf0002000, "CTRL_BUS_ERRORS": 0xf0000008, "HBM_WINDOW_BASE": 0xf0003800, "HBM_RUNTIME_BASE": 0xf0004000}
    if layout is not None:
        addresses = {"HBM_CONTROL_BASE": layout["csr_bases"]["hbm_control"],
                     "TIMER_BASE": layout["csr_bases"]["timer0"],
                     "CTRL_BUS_ERRORS": layout["csr_registers"]["ctrl_bus_errors"]["addr"],
                     "HBM_WINDOW_BASE": layout["csr_bases"]["hbm_window"],
                     "HBM_RUNTIME_BASE": layout["csr_bases"]["hbm_runtime"]}
        if addresses != {"HBM_CONTROL_BASE": 0xf0003000, "TIMER_BASE": 0xf0002000, "CTRL_BUS_ERRORS": 0xf0000008, "HBM_WINDOW_BASE": 0xf0003800, "HBM_RUNTIME_BASE": 0xf0004000}:
            raise ValueError("Unqualified CPU CSR map")
    header = ["/* Generated from the source-bound, reviewed cold-init profile. */"]
    header.extend(f"#define {key} 0x{value:08x}u" for key, value in addresses.items())
    header.extend([f"#define BOOT_HBM_MHZ {p['hbm_mhz']}u", f"#define CLOCK_PROFILE_COUNT {len(profiles)}u", "static const clock_profile clock_profiles[] = {"])
    for mhz, profile in sorted(profiles.items(), key=lambda row: int(row[0])):
        words = ", ".join(f"0x{word:08x}u" for word in profile["pll_words"])
        header.append(f"    {{{int(mhz)}u, {{{words}}}, {profile['refresh_period_cycles']}u}},")
    header.append("};")
    for key in ("calibration_status_mask", "calibration_status_expected", "calibration_result_mask", "calibration_result_expected"):
        header.append(f"#define {key.upper()} 0x{p[key]:08x}u")
    for name in ("ms_pre", "pll", "ms_post", "phy", "mc"):
        header.append(f"#define {name.upper()}_COUNT {len(p[name])}u")
        header.append(f"static const reg_write {name}[] = {{")
        header.extend(f"    {{0x{x['address']:08x}u, 0x{x['value']:08x}u}}," for x in p[name])
        header.append("};")
    header.append("static const uint32_t selectors[] = {" + ", ".join(f"0x{x:08x}u" for x in p["selectors"]) + "};")
    (output / "profile.h").write_text("\n".join(header) + "\n", encoding="utf-8")
    selected = os.environ.get("FK33_RISCV_GCC")
    if not selected:
        selected = next((found for name in ("riscv64-unknown-elf-gcc", "riscv32-unknown-elf-gcc", "riscv32-xilinx-elf-gcc") if (found := shutil.which(name))), None)
    if not selected:
        fallback = Path("C:/Xilinx/2025.2/gnu/riscv/nt/bin/riscv64-unknown-elf-gcc.exe")
        if fallback.is_file():
            selected = str(fallback)
    if not selected:
        raise FileNotFoundError("Install a RISC-V bare-metal GCC toolchain or set FK33_RISCV_GCC")
    compiler = Path(shutil.which(selected) or selected).resolve()
    if not compiler.is_file():
        raise FileNotFoundError(f"RISC-V compiler not found: {compiler}")
    def tool(name):
        suffix = ".exe" if compiler.name.lower().endswith(".exe") else ""
        stem = compiler.name[:-len(suffix)] if suffix else compiler.name
        if not stem.endswith("gcc"):
            raise ValueError("FK33_RISCV_GCC must name a GCC executable ending in gcc")
        return str(compiler.with_name(stem[:-3] + name + suffix))
    elf, binary = output / "fk33_hbm.elf", output / "fk33_hbm.bin"
    command = [str(compiler), "-march=rv32i_zicsr", "-mabi=ilp32", "-Os", "-g", "-ffreestanding", "-fno-builtin", "-fno-pic", "-mno-relax", "-ffunction-sections", "-fdata-sections", "-Wall", "-Wextra", "-Werror", "-nostdlib", "-nostartfiles", "-I", str(output), "-T", str(source / "linker.ld"), "-Wl,--gc-sections", f"-Wl,-Map,{output / 'fk33_hbm.map'}", str(source / "start.S"), str(source / "main.c"), "-lgcc", "-o", str(elf)]
    result = subprocess.run(command, text=True, capture_output=True)
    (output / "compile.log").write_text(f"Compiler exit status: {result.returncode}\n" + result.stdout + result.stderr, encoding="utf-8")
    result.check_returncode()
    subprocess.run([tool("objcopy"), "-O", "binary", str(elf), str(binary)], check=True)
    disasm = subprocess.run([tool("objdump"), "-d", str(elf)], check=True, text=True, capture_output=True)
    (output / "fk33_hbm.dis").write_text(disasm.stdout, encoding="utf-8")
    data = binary.read_bytes()
    if not data or len(data) > 65536:
        raise ValueError("Firmware exceeds integrated ROM")
    data += b"\0" * (-len(data) % 4)
    metadata = {"elf": str(elf), "bin": str(binary), "rom_bytes": len(data),
                "rom_words": list(struct.unpack(f"<{len(data) // 4}I", data)),
                "profile": str(profile_path), "profile_sha256": _sha(profile_path),
                "sources": [{"path": str(source / name), "sha256": _sha(source / name)} for name in ("build.py", "main.c", "start.S", "linker.ld")],
                "elf_sha256": _sha(elf), "bin_sha256": _sha(binary), "command": command,
                "compiler_version": subprocess.check_output([str(compiler), "--version"], text=True).splitlines()[0]}
    (output / "firmware-build.json").write_text(json.dumps({k: v for k, v in metadata.items() if k != "rom_words"}, indent=2) + "\n", encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--csr-json")
    args = parser.parse_args()
    result = build(args.output_dir, csr_json=args.csr_json, profile_path=args.profile)
    print(json.dumps({k: v for k, v in result.items() if k != "rom_words"}, indent=2))
