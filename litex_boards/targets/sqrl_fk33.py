#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2020-2022 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# Build/Use:
# python3 -m litex_boards.targets.sqrl_fk33 --with-hbm --sys-clk-freq=250e6 --csr-csv=csr.csv --build --load
# litex_server --jtag --jtag-config=openocd_xc7_ft2232.cfg --jtag-chain=2
# litex_term crossover

import os

from migen import *

from litex.gen import *

from litex_boards.platforms import sqrl_fk33
from litex.soc.cores.clock import *
from litex.soc.cores.ram.xilinx_usp_hbm2 import (
    USPHBM2,
    USPHBM2_DEFAULT_BASE,
    USPHBM2_HIGH_BASE,
    add_usphbm2_pseudochannels,
    parse_usphbm2_channels,
    usphbm2_channel_origins,
    usphbm2_window_end,
)
from litex.soc.integration.soc import *
from litex.soc.integration.builder import *
from litex.soc.integration.export import MockCSR
from litex.soc.interconnect import axi, csr_bus, stream, wishbone
from litex.soc.interconnect.csr import CSR, CSRStatus, CSRStorage
from litex.soc.cores.led import LedChaser

from litepcie.phy.usppciephy import USPHBMPCIEPHY
from litepcie.software import generate_litepcie_software

# HBM XCI ------------------------------------------------------------------------------------------

def ensure_hbm_xci(url, hbm_xci=os.path.join("ip", "hbm", "hbm_0.xci")):
    if not os.path.exists(hbm_xci):
        os.makedirs(os.path.dirname(hbm_xci), exist_ok=True)
        os.system(f"wget -O {hbm_xci} {url}")

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_hbm, hbm_ref_clk_freq=100e6):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()
        if with_hbm:
            self.cd_hbm_ref = ClockDomain()
            self.cd_apb     = ClockDomain()

        # # #

        self.pll = pll = USPMMCM(speedgrade=-2)
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(platform.request("clk200"), 200e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin) # Ignore sys_clk to pll.clkin path created by SoC's rst.

        if with_hbm:
            pll.create_clkout(self.cd_hbm_ref, hbm_ref_clk_freq)
            pll.create_clkout(self.cd_apb,     100e6)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=125e6,
        with_led_chaser       = True,
        with_pcie             = False,
        pcie_lanes            = 4,
        pcie_ndmas            = 1,
        pcie_address_width    = 32,
        with_pcie_dma_status  = False,
        with_pcie_dma_monitor = False,
        with_hbm              = False,
        hbm_channels          = (0, 1, 2, 3),
        hbm_main_channel      = 0,
        hbm_base              = USPHBM2_DEFAULT_BASE,
        hbm_high_base         = USPHBM2_HIGH_BASE,
        hbm_strip_origin      = False,
        hbm_primitive         = False,
        hbm_primitive_rtl     = None,
        **kwargs):
        self.hbm_primitive = hbm_primitive or hbm_primitive_rtl is not None
        if self.hbm_primitive:
            if not with_hbm:
                raise ValueError("Primitive HBM requires --with-hbm")
            if (sys_clk_freq != 250e6 or with_pcie or not with_led_chaser or
                    tuple(parse_usphbm2_channels(hbm_channels)) != (0, 1, 2, 3) or
                    hbm_main_channel != 0 or hbm_base != USPHBM2_DEFAULT_BASE or
                    hbm_high_base != USPHBM2_HIGH_BASE or hbm_strip_origin):
                raise ValueError("Primitive HBM requires the fixed 250 MHz, PC0-3 FK33 profile without PCIe")
            if kwargs.get("cpu_type", "vexriscv") not in (None, "None", "vexriscv"):
                raise ValueError("Primitive HBM supports CPU-less or VexRiscv minimal operation")
            if kwargs.get("cpu_type", "vexriscv") not in (None, "None"):
                if kwargs.get("cpu_variant") not in (None, "minimal"):
                    raise ValueError("HBM firmware requires the uncached VexRiscv minimal variant")
                kwargs["cpu_variant"] = "minimal"
                if kwargs.get("cpu_reset_address") not in (None, 0x10000):
                    raise ValueError("HBM firmware ROM starts at 0x10000")
                kwargs["cpu_reset_address"] = 0x10000
                kwargs["integrated_rom_size"] = 0x10000
                kwargs["integrated_sram_size"] = 0x8000
                if kwargs.get("integrated_main_ram_size", 0):
                    raise ValueError("HBM firmware uses SRAM until external memory is calibrated")
            required = {"bus_standard": "wishbone", "bus_data_width": 32,
                "bus_address_width": 32, "bus_bursting": False, "bus_interconnect": "shared",
                "bus_timeout": int(1e6),
                "csr_data_width": 32, "csr_address_width": 14, "csr_paging": 0x800,
                "with_ctrl": True, "with_timer": True, "with_uart": True}
            for name, value in required.items():
                if kwargs.get(name, value) != value:
                    raise ValueError(f"Primitive HBM requires {name}={value!r}")
            if kwargs.get("bus_addressing") not in (None, "word"):
                raise ValueError("Primitive HBM requires word-addressed Wishbone")
            if kwargs.get("uart_name", "serial") not in ("serial", "crossover"):
                raise ValueError("Primitive HBM requires the crossover UART")
        platform = sqrl_fk33.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6
            hbm_channels = parse_usphbm2_channels(hbm_channels)
            if hbm_main_channel not in hbm_channels:
                raise ValueError("HBM main channel must be one of the mapped HBM channels")
            hbm_origins = usphbm2_channel_origins(hbm_channels, hbm_base, hbm_high_base)
            hbm_end = usphbm2_window_end(hbm_origins)
            if hbm_end > 2**kwargs.get("bus_address_width", 32):
                kwargs["bus_address_width"] = 64
            if hbm_end > 2**pcie_address_width:
                pcie_address_width = 64
            if hbm_end > 2**33:
                hbm_strip_origin = True

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq, with_hbm,
            hbm_ref_clk_freq=200e6 if self.hbm_primitive else 100e6)
        if self.hbm_primitive:
            platform.toolchain.pre_placement_commands += [
                "create_clock -name jtag_tck -period 100.0 [get_pins BSCANE2/INTERNAL_TCK]",
                "set_clock_groups -asynchronous -group [get_clocks jtag_tck] -group [get_clocks -filter {{NAME != jtag_tck}}]",
            ]
            platform.toolchain.additional_commands += [
                "check_timing -verbose -file {build_name}_check_timing.rpt",
                "report_cdc -details -file {build_name}_cdc.rpt",
                "report_clock_interaction -file {build_name}_clock_interaction.rpt",
            ]

        # SoCCore ----------------------------------------------------------------------------------
        if kwargs.get("uart_name", "serial") == "serial":
            if kwargs.get("uart_name", "serial") == "serial": kwargs["uart_name"] = "crossover" # Defaults to Crossover-UART.
        kwargs["with_jtagbone"]  = True
        kwargs["jtagbone_chain"] = 2 # Chain 1 already used by HBM2 debug probes.
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on FK33", **kwargs)

        # HBM --------------------------------------------------------------------------------------
        if with_hbm:
            # Add HBM Core.
            if not self.hbm_primitive:
                self.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))
                ensure_hbm_xci("https://github.com/litex-hub/litex-boards/files/8178874/hbm_0.xci.txt")
                add_usphbm2_pseudochannels(
                    soc              = self,
                    hbm              = hbm,
                    channels         = hbm_channels,
                    main_channel     = hbm_main_channel,
                    origins          = hbm_origins,
                    hbm_high_base    = hbm_high_base,
                    hbm_strip_origin = hbm_strip_origin)
            else:
                from litex.soc.cores.ram.xilinx_usp_hbm2 import PrimitiveHBM2
                self.hbm = ClockDomainsRenamer({"axi": "sys"})(
                    PrimitiveHBM2(platform, rtl_dir=hbm_primitive_rtl))
                # Four RV32 windows select eight groups of four native ports.
                # AXILiteDecoder holds each route through its response, including
                # when a selector write arrives while a request is outstanding.
                # Change banks only after all four windows have completed
                # their transactions; request buffers do not capture the bank.
                self.hbm_window = LiteXModule()
                self.hbm_window.bank = CSRStorage(3, name="bank")
                for channel in range(4):
                    host   = wishbone.Interface(data_width=32, adr_width=30)
                    gated  = wishbone.Interface(data_width=32, adr_width=30)
                    narrow = axi.AXILiteInterface(data_width=32, address_width=32)
                    native = axi.AXILiteInterface(data_width=32, address_width=33)
                    # The existing watchdog supplies bounded errors while
                    # READY is clear, without queuing a request in the bridge.
                    self.comb += host.connect(gated, omit={"cyc"})
                    self.comb += gated.cyc.eq(host.cyc & self.hbm.memory_ready)
                    self.submodules += axi.Wishbone2AXILite(gated, narrow)
                    converted = axi.AXILiteInterface(data_width=32, address_width=32)
                    for name in ("aw", "w", "ar"):
                        self.comb += getattr(narrow, name).connect(getattr(converted, name))
                    self.comb += converted.b.connect(narrow.b)
                    read_buffer = stream.Buffer(converted.r.description,
                        pipe_valid=True, pipe_ready=True)
                    self.submodules += read_buffer
                    self.comb += converted.r.connect(read_buffer.sink)
                    self.comb += read_buffer.source.connect(narrow.r)
                    self.submodules += axi.AXILiteOffset(converted, native, offset=0)
                    ports = []
                    for bank in range(8):
                        # Route only 32-bit data across the bank switch. Width
                        # conversion and registered native channels stay local
                        # to each physical HBM port instead of broadcasting a
                        # 256-bit payload to eight distant ports.
                        port = axi.AXILiteInterface(data_width=32, address_width=33)
                        local = axi.AXILiteInterface(data_width=32, address_width=33)
                        wide = axi.AXILiteInterface(data_width=256, address_width=33)
                        routed = axi.AXILiteInterface(data_width=256, address_width=33)
                        ports.append((lambda address, bank=bank: self.hbm_window.bank.storage == bank, port))
                        for name in ("aw", "w", "ar"):
                            self.comb += getattr(port, name).connect(getattr(local, name))
                        self.comb += local.b.connect(port.b)
                        # Register the local lane mux before the narrow bank mux.
                        local_read = stream.Buffer(local.r.description,
                            pipe_valid=True, pipe_ready=True)
                        self.submodules += local_read
                        self.comb += local.r.connect(local_read.sink)
                        self.comb += local_read.source.connect(port.r)
                        self.submodules += axi.AXILiteConverter(local, wide)
                        for name in ("aw", "w", "ar"):
                            request = getattr(wide, name)
                            buffer = stream.Buffer(request.description,
                                pipe_valid=True, pipe_ready=True)
                            self.submodules += buffer
                            self.comb += request.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(routed, name))
                        for name in ("b", "r"):
                            response = getattr(routed, name)
                            buffer = stream.Buffer(response.description,
                                pipe_valid=True, pipe_ready=True)
                            self.submodules += buffer
                            self.comb += response.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(wide, name))
                        self.submodules += axi.AXILite2AXI(routed, self.hbm.axi[4*bank + channel])
                    self.submodules += axi.AXILiteDecoder(native, ports)
                    self.bus.add_slave(f"hbm{channel}", host,
                        SoCRegion(origin=hbm_origins[channel], size=0x1000_0000, cached=True))
                self.bus.add_region("main_ram", SoCRegion(
                    origin=hbm_origins[0], size=0x1000_0000, linker=True))
                self.add_constant("HBM_CHANNELS", 32)
                self.add_constant("HBM_WINDOW_CHANNELS", 4)
                self.add_constant("HBM_WINDOW_BANKS", 8)
                self.add_constant("HBM_MAIN_CHANNEL", 0)
                self.add_constant("HBM_HIGH_BASE", hbm_high_base)

        # PCIe -------------------------------------------------------------------------------------
        if with_pcie:
            assert self.csr_data_width == 32
            self.pcie_phy = USPHBMPCIEPHY(platform, platform.request(f"pcie_x{pcie_lanes}"),
                data_width = {4: 128, 8: 256, 16: 512}[pcie_lanes],
                bar0_size  = 0x20000)
            self.add_pcie(
                phy              = self.pcie_phy,
                ndmas            = pcie_ndmas,
                address_width    = pcie_address_width,
                with_dma_status  = with_pcie_dma_status,
                with_dma_monitor = with_pcie_dma_monitor)

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq)

        if self.hbm_primitive:
            for name, location in (("ctrl", 0), ("hbm", 1), ("identifier_mem", 2),
                    ("leds", 3), ("timer0", 4), ("uart", 5), ("hbm_control", 6), ("hbm_window", 7)):
                if name in self.csr.locs and self.csr.locs[name] != location:
                    raise ValueError(f"Primitive HBM requires CSR bank {name} at {location}")
                self.csr.add(name, location, use_loc_if_exists=True)

    def _finalize_csr(self):
        if not self.hbm_primitive:
            return super()._finalize_csr()
        from litex.soc.cores.ram.xilinx_usp_hbm2 import MANAGEMENT_REGISTERS
        # The control RTL already decodes bank 6. Attach its raw CSR bus to
        # the normal interconnect, exporting metadata without another decoder.
        if any(isinstance(x, (CSR, CSRStatus, CSRStorage)) for x in self.__dict__.values()):
            raise ValueError("Primitive HBM raw CSR hook does not support top-level CSRs")
        if (self.csr.data_width, self.csr.address_width, self.csr.alignment,
                self.csr.paging) != (32, 14, 32, 0x800):
            raise ValueError("Primitive HBM requires CSR32/14, 32-bit alignment and 0x800 paging")
        origin = self.bus.regions["csr"].origin
        self.csr_bankarray = csr_bus.CSRBankArray(self,
            address_map=self.csr.address_map, data_width=self.csr.data_width,
            address_width=self.csr.address_width, alignment=self.csr.alignment,
            paging=self.csr.paging, ordering=self.csr.ordering)
        self.csr_interconnect = csr_bus.InterconnectShared(
            list(self.csr.masters.values()), self.csr_bankarray.get_buses() + [self.hbm.control_bus])
        for name, registers, address, bank in self.csr_bankarray.banks:
            self.csr.add_region(name, SoCCSRRegion(origin + address*0x800, 32, registers))
        for name, memory, address, bank in self.csr_bankarray.srams:
            self.csr.add_region(name + "_" + memory.name_override, SoCCSRRegion(origin + address*0x800, 32, memory))
        metadata = [MockCSR(name, 1, "ro" if access == "ro" else "rw", 32)
                    for name, access in MANAGEMENT_REGISTERS]
        self.csr.add_region("hbm_control", SoCCSRRegion(origin + 0x3000, 32, metadata))
        self.csr.regions = dict(sorted(self.csr.regions.items(), key=lambda item: item[1].origin))
        for name, constant in self.csr_bankarray.constants:
            self.add_constant(name + "_" + constant.name, constant.value.value)

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=sqrl_fk33.Platform, description="LiteX SoC on FK33.")
    parser.add_target_argument("--sys-clk-freq", default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--with-pcie",    action="store_true",       help="Enable PCIe support.")
    parser.add_target_argument("--pcie-lanes",   default=4, type=int,       choices=[4, 8, 16], help="PCIe lane count.")
    parser.add_target_argument("--pcie-ndmas",   default=1, type=int,       help="Number of PCIe DMA channels.")
    parser.add_target_argument("--pcie-address-width", default=32, type=int, choices=[32, 64], help="PCIe address width.")
    parser.add_target_argument("--pcie-with-dma-status",  action="store_true", help="Enable PCIe DMA status CSRs.")
    parser.add_target_argument("--pcie-with-dma-monitor", action="store_true", help="Enable PCIe DMA monitor CSRs.")
    parser.add_target_argument("--with-hbm",              action="store_true", help="Use HBM2.")
    parser.add_target_argument("--hbm-primitive", action="store_true", help="Use bundled primitive HBM and processor calibration firmware.")
    parser.add_target_argument("--hbm-primitive-rtl", help="Override the bundled primitive/control RTL directory.")
    parser.add_target_argument("--hbm-channels", default="0,1,2,3",         help="HBM channels to map (comma/range list or all).")
    parser.add_target_argument("--hbm-main-channel", default=0, type=int,   help="Mapped HBM channel used as main RAM.")
    parser.add_target_argument("--hbm-base",       default=USPHBM2_DEFAULT_BASE,
        type=lambda x: int(x, 0), help="HBM bus base address.")
    parser.add_target_argument("--hbm-high-base",  default=USPHBM2_HIGH_BASE,
        type=lambda x: int(x, 0), help="HBM bus base for channels above the low 32-bit cached window.")
    parser.add_target_argument("--hbm-strip-origin", action="store_true",   help="Expose each mapped HBM channel with local AXI addresses.")
    parser.add_target_argument("--driver",       action="store_true",       help="Generate PCIe driver.")
    args = parser.parse_args()
    if args.pcie_ndmas < 0:
        parser.error("--pcie-ndmas must be >= 0")
    try:
        hbm_channels = parse_usphbm2_channels(args.hbm_channels)
    except ValueError as e:
        parser.error(str(e))

    soc = BaseSoC(
        sys_clk_freq          = args.sys_clk_freq,
        with_pcie             = args.with_pcie,
        pcie_lanes            = args.pcie_lanes,
        pcie_ndmas            = args.pcie_ndmas,
        pcie_address_width    = args.pcie_address_width,
        with_pcie_dma_status  = args.pcie_with_dma_status,
        with_pcie_dma_monitor = args.pcie_with_dma_monitor,
        with_hbm              = args.with_hbm,
        hbm_channels          = hbm_channels,
        hbm_main_channel      = args.hbm_main_channel,
        hbm_base              = args.hbm_base,
        hbm_high_base         = args.hbm_high_base,
        hbm_strip_origin      = args.hbm_strip_origin,
        hbm_primitive         = args.hbm_primitive,
        hbm_primitive_rtl     = args.hbm_primitive_rtl,
        **parser.soc_argdict
    )
    builder = Builder(soc, **parser.builder_argdict)
    if args.build or args.driver:
        if soc.hbm_primitive and soc.cpu_type is not None:
            from litex.soc.integration.export import get_csr_json
            from litex_boards.software.fk33_hbm.build import build
            soc.finalize()
            import json
            layout = json.loads(get_csr_json(soc=soc, csr_regions=soc.csr.regions,
                constants=soc.constants, mem_regions=soc.bus.regions))
            firmware = build(os.path.join(builder.output_dir, "software", "fk33_hbm"), csr_json=layout)
            soc.init_rom("rom", firmware["rom_words"], auto_size=False)
            soc.integrated_rom_initialized = True
            builder.compile_software = False
        if not args.build:
            builder.compile_software = False
            builder.compile_gateware = False
        builder.build(**parser.toolchain_argdict)

    if args.driver:
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))

if __name__ == "__main__":
    main()
