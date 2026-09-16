#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2022 Andrew Elbert Wilson <Andrew.E.Wilson@ieee.org>
# SPDX-License-Identifier: BSD-2-Clause


from pathlib import Path
from copy import copy

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer
from migen.genlib.resetsync import AsyncResetSynchronizer
from litex.soc.interconnect import wishbone

from litex.gen import *

from litex_boards.platforms import opalkelly_xem8320

from litex.soc.cores.clock import *
from litex.soc.integration.soc import *
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser
from litex.soc.cores.video import VideoDVIPHY

from litedram.modules import MT40A512M16
from litedram.phy import usddrphy

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_video_pll=False):
        self.rst = Signal()
        self.cd_sys    = ClockDomain()
        self.cd_sys4x  = ClockDomain()
        self.cd_idelay = ClockDomain()
        if with_video_pll:
            self.cd_hdmi   = ClockDomain()
            self.cd_hdmi5x = ClockDomain()

        # # #

        # Clk.
        clk100 = platform.request("ddr_clk100")

        # PLL.
        self.pll = pll = USMMCM(speedgrade=-2)
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(clk100, 100e6)
        pll.create_clkout(self.cd_sys,    sys_clk_freq, with_reset=False)
        pll.create_clkout(self.cd_sys4x,  4*sys_clk_freq)
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin) # Ignore sys_clk to pll.clkin path created by SoC's rst.
        self.idelayctrl = USIDELAYCTRL(cd_ref=self.cd_sys4x, cd_sys=self.cd_sys)

        # Video PLL.
        if with_video_pll:
            self.video_pll = video_pll = USMMCM(speedgrade=-2)
            video_pll.reset.eq(self.rst)
            video_pll.register_clkin(self.cd_sys.clk, sys_clk_freq)
            video_pll.create_clkout(self.cd_hdmi,   25e6)
            video_pll.create_clkout(self.cd_hdmi5x, 5*25e6)

# Native clocks ------------------------------------------------------------------------------------

class _NativeCRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.rst = Signal()
        self.cd_sys = ClockDomain()
        self.cd_cpu = ClockDomain()
        self.cd_riu = ClockDomain()
        self.cd_ref = ClockDomain()
        self.pll = pll = USMMCM(speedgrade=-2)
        pll.register_clkin(platform.request("ddr_clk100"), 100e6)
        self.comb += self.cd_ref.clk.eq(pll.clkin)
        self.specials += AsyncResetSynchronizer(self.cd_ref, self.rst)
        self.comb += pll.reset.eq(self.cd_ref.rst)
        pll.create_clkout(self.cd_cpu, sys_clk_freq/2)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        pll.create_clkout(self.cd_riu, sys_clk_freq/2)
        self.native_clock = Signal()
        self.native_locked = Signal()
        self.native_enable = Signal()
        feedback = Signal()
        self.specials += Instance("PLLE4_ADV",
            p_COMPENSATION="INTERNAL", p_CLKFBOUT_MULT=4, p_CLKFBOUT_PHASE=90.0,
            p_CLKIN_PERIOD=1e9/sys_clk_freq, p_CLKOUT0_DIVIDE=1,
            p_CLKOUTPHY_MODE="VCO_2X", p_DIVCLK_DIVIDE=1,
            i_CLKIN=self.cd_sys.clk, i_CLKFBIN=feedback, o_CLKFBOUT=feedback,
            i_RST=~pll.locked, i_PWRDWN=0, i_CLKOUTPHYEN=self.native_enable,
            o_CLKOUTPHY=self.native_clock, o_LOCKED=self.native_locked,
            i_DADDR=0, i_DCLK=0, i_DEN=0, i_DI=0, i_DWE=0)


class _NativeDDR4(MT40A512M16):
    # Match the initial BIOS profile's conservative same-bank-group spacing.
    technology_timings = copy(MT40A512M16.technology_timings)
    technology_timings.tCCD = (8, None)


def _native_post_route_commands(sys_clk_freq):
    """Return narrowly scoped native-PHY post-route checks and settings."""
    commands = [
        "set_property INTERNAL_VREF 0.84 [get_iobanks 64]",
        "report_drc -file opalkelly_xem8320_native_final_drc.rpt",
    ]
    if int(round(sys_clk_freq)) == 400000000:
        # Vivado rejects the 1600 MHz PLLE4 VCO with PDRC-182 (1500 MHz
        # maximum). Keep the real clocks/timing checks and downgrade only this
        # known experimental 3200 MT/s DRC so a laboratory bitstream is made.
        commands.insert(0, "set_property SEVERITY Warning [get_drc_checks PDRC-182]")
    return commands


# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(125e6),
        toolchain              = "vivado",
        with_usnative          = False,
        usnative_debug         = False,
        with_dma               = False,
        dma_data_width         = 128,
        overclock              = False,
        usnative_output_dir    = "build/opalkelly_xem8320/native",
        vivado                 = "vivado",
        with_ethernet          = False,
        with_etherbone         = False,
        eth_ip                 = "192.168.1.50",
        with_led_chaser        = True,
        with_video_framebuffer = False,
        **kwargs):
        # Fail before importing the native PHY or starting a device query.
        if with_usnative:
            kwargs.setdefault("integrated_rom_size", 0x20000)
            kwargs.setdefault("integrated_sram_size", 0x8000)
            if toolchain != "vivado":
                raise ValueError("USNativeDDRPHY requires --toolchain vivado")
            if int(round(sys_clk_freq)) not in (300000000, 333333333, 366666667, 400000000):
                raise ValueError("USNative requires a supported --ddr-rate / --sys-clk-freq combination")
            if sys_clk_freq > 333333334 and not overclock:
                raise ValueError("2933.333 and 3200 MT/s require --overclock")
            if dma_data_width not in (128, 256):
                raise ValueError("Native DMA supports 128-bit or 256-bit ports")
            if dma_data_width != 128 and not with_dma:
                raise ValueError("--dma-data-width requires --with-dma")
            if kwargs.get("uart_name", "jtag_uart") not in ("serial", "jtag_uart"):
                raise ValueError("Initial native target requires JTAG UART")
            if with_video_framebuffer or kwargs.get("with_video_terminal", False):
                raise ValueError("Video is not supported by the initial native clock profile")
            if kwargs.get("integrated_main_ram_size", 0):
                raise ValueError("USNative requires external DDR memory")
            if kwargs.get("cpu_type", "vexriscv") != "vexriscv" or kwargs.get("cpu_variant", "standard") != "standard":
                raise ValueError("Initial native target requires the standard VexRiscv CPU")
        elif usnative_debug or with_dma or overclock or dma_data_width != 128:
            raise ValueError("Native debug, DMA and overclock options require --with-usnative")
        platform = opalkelly_xem8320.Platform(toolchain=toolchain)

        # TODO: add okHost FrontPanel API for UART, Data streaing, and Debug

        # CRG --------------------------------------------------------------------------------------
        self.crg = (_NativeCRG(platform, sys_clk_freq) if with_usnative else
                    _CRG(platform, sys_clk_freq, with_video_pll=with_video_framebuffer))

        # SoCCore ----------------------------------------------------------------------------------
        if kwargs.get("uart_name", "serial") == "serial":
            if kwargs.get("uart_name", "serial") == "serial": kwargs["uart_name"] = "jtag_uart"
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on XEM8320", **kwargs)

        # DDR4 SDRAM -------------------------------------------------------------------------------
        if not self.integrated_main_ram_size:
            if with_usnative:
                from litedram.phy.usnative import USNativeDDRPHY
                self.ddrphy = USNativeDDRPHY(platform.request("ddram"), platform,
                    self.crg.native_clock, self.crg.native_locked, self.crg.native_enable,
                    sys_clk_freq=sys_clk_freq, output_dir=usnative_output_dir,
                    vivado=vivado, with_debug=usnative_debug, overclock=overclock)
                # CL/CWL are selected by the PHY profile; use the module's 2400
                # timing table at both requested clocks, preserving ns minima.
                module = _NativeDDR4(sys_clk_freq, "1:4", speedgrade="2400")
            else:
                self.ddrphy = usddrphy.USPDDRPHY(platform.request("ddram"),
                    memtype="DDR4", sys_clk_freq=sys_clk_freq, iodelay_clk_freq=500e6)
                module = MT40A512M16(sys_clk_freq, "1:4")
            self.add_sdram("sdram", phy=self.ddrphy, module=module,
                size=0x40000000, l2_cache_size=kwargs.get("l2_size", 8192))
            if with_usnative:
                self.comb += self.ddrphy.software_control.eq(~self.sdram.dfii._control.fields.sel)
                self.add_config("SDRAM_USNATIVE_XEM8320")
                if usnative_debug:
                    self.add_config("SDRAM_USNATIVE_DEBUG")
                if self.ddrphy.overclock:
                    self.add_config("SDRAM_USNATIVE_OVERCLOCK")
                    self.logger.warning(
                        "USNativeDDRPHY %.3f MT/s is experimental and exceeds the "
                        "native primitive clock limits; timing reports are retained "
                        "and no timing checks are waived.", sys_clk_freq*8/1e6)
                    if int(round(sys_clk_freq)) == 400000000:
                        self.logger.warning(
                            "3200 MT/s uses a 1600 MHz PLLE4 VCO; PDRC-182's "
                            "1500 MHz limit is downgraded to a warning only for "
                            "this experimental profile.")
                if with_dma:
                    from litedram.frontend.native_benchmark import NativeDMABenchmark
                    write_port = self.sdram.crossbar.get_port(mode="write", data_width=dma_data_width)
                    read_port = self.sdram.crossbar.get_port(mode="read", data_width=dma_data_width)
                    self.dma_bench = NativeDMABenchmark(write_port, read_port, databits=16)
                    self.comb += self.dma_bench.allowed.eq(self.ddrphy._ready.status &
                        (self.ddrphy._training_stage.storage == 5) &
                        (self.ddrphy._training_error.storage == 0) &
                        ~self.ddrphy._bisc_only.storage & self.ddrphy._en_vtc.storage &
                        self.sdram.dfii._control.fields.sel)
                    self.add_config("SDRAM_NATIVE_DMA_TEST")
                # The CPU has its own related half-rate clock. Cross both
                # Wishbone masters, interrupts, and software reset requests.
                self.cpu.cpu_params["i_clk"] = ClockSignal("cpu")
                reset_cpu, interrupt_cpu = Signal(), Signal(32)
                self.specials += MultiReg(self.ctrl.cpu_rst, reset_cpu, "cpu")
                self.specials += MultiReg(self.cpu.interrupt, interrupt_cpu, "cpu")
                self.cpu_reset_pulse = reset_pulse = PulseSynchronizer("sys", "cpu")
                self.comb += reset_pulse.i.eq(self.ctrl.soc_rst)
                self.cpu.cpu_params["i_reset"] = ResetSignal("cpu") | reset_cpu | reset_pulse.o
                self.cpu.cpu_params["i_externalInterruptArray"] = interrupt_cpu
                for n, bus in enumerate(self.cpu.periph_buses):
                    name = "cpu_bus" + str(n)
                    fabric = wishbone.Interface(data_width=32, address_width=32, addressing="word")
                    self.bus.masters[name] = fabric
                    setattr(self.submodules, "cpu_cdc" + str(n),
                        wishbone.ClockDomainCrossing(bus, fabric, cd_from="cpu", cd_to="sys"))
                self.add_constant("CONFIG_CPU_CLK_FREQ", int(sys_clk_freq/2))
                platform.add_platform_command("create_clock -name jtag_tck -period 100.0 [get_pins BSCANE2/INTERNAL_TCK]")
                platform.add_false_path_constraints(self.crg.cd_sys.clk, self.uart.phy.jtag.tck)
                platform.add_platform_command("set_property DRIVE 8 [get_ports ddram_reset_n]")
                platform.add_platform_command("set_property SLEW SLOW [get_ports ddram_reset_n]")
                # Native routing can re-infer the bank's SSTL reference. Restore
                # the platform's POD12 receiver reference before emitting bits.
                platform.toolchain.bitstream_commands += _native_post_route_commands(sys_clk_freq)

        # TODO: add SFP+ cages for ethernet
        # Ethernet / Etherbone ---------------------------------------------------------------------
        # if with_ethernet or with_etherbone:
        #     self.ethphy = KU_1000BASEX(self.crg.cd_eth.clk,
        #         data_pads    = self.platform.request("sfp", 0),
        #         sys_clk_freq = self.clk_freq)
        #     self.comb += self.platform.request("sfp_tx_disable_n", 0).eq(1)
        #     self.platform.add_platform_command("set_property SEVERITY {{Warning}} [get_drc_checks REQP-1753]")
        #     if with_ethernet:
        #         self.add_ethernet(phy=self.ethphy)
        #     if with_etherbone:
        #         self.add_etherbone(phy=self.ethphy, ip_address=eth_ip)

        # Video ------------------------------------------------------------------------------------
        if with_video_framebuffer:
            platform.add_extension(opalkelly_xem8320._dvi_pmod_io)
            self.videophy = VideoDVIPHY(platform.request("dvi"), clock_domain="hdmi")
            self.add_video_framebuffer(phy=self.videophy, timings="640x480@75Hz", clock_domain="hdmi")

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq)

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=opalkelly_xem8320.Platform, description="LiteX SoC on XEM8320.")
    parser.add_target_argument("--sys-clk-freq",        default=None, type=float, help="System clock frequency (125 MHz normally; 300 MHz with USNative).")
    #ethopts = parser.target_group.add_mutually_exclusive_group()
    #ethopts.add_argument("--with-ethernet",        action="store_true",    help="Enable Ethernet support.")
    #ethopts.add_argument("--with-etherbone",       action="store_true",    help="Enable Etherbone support.")
    #parser.add_target_argument("--eth-ip",         default="192.168.1.50", help="Ethernet/Etherbone IP address.")
    #parser.add_target_argument("--eth-dynamic-ip", action="store_true",    help="Enable dynamic Ethernet IP assignment.")
    viopts = parser.target_group.add_mutually_exclusive_group()
    viopts.add_argument("--with-video-terminal",    action="store_true", help="Enable Video Terminal (HDMI).")
    viopts.add_argument("--with-video-framebuffer", action="store_true", help="Enable Video Framebuffer (HDMI).")
    parser.add_target_argument("--with-usnative", action="store_true", help="Use experimental USNativeDDRPHY (Vivado only).")
    parser.add_target_argument("--usnative-debug", action="store_true", help="Include native trace hardware and verbose BIOS calibration.")
    parser.add_target_argument("--vivado", default="vivado", help="Vivado executable for fresh native device queries.")
    parser.add_target_argument("--with-dma", action="store_true", help="Include native DMA test engine and BIOS command.")
    parser.add_target_argument("--dma-data-width", type=int, choices=[128, 256], default=128, help="DMA port width; physical DDR remains x16.")
    parser.add_target_argument("--overclock", action="store_true", help="Allow experimental 2933.333/3200 MT/s profiles.")
    parser.add_target_argument("--ddr-rate", choices=["2400", "2666.667", "2933.333", "3200"], help="Native DDR data rate in MT/s; determines controller clock.")
    args = parser.parse_args()
    if args.ddr_rate:
        if not args.with_usnative:
            parser.error("--ddr-rate requires --with-usnative")
        selected_frequency = {"2400": 300e6, "2666.667": 1e9/3,
                              "2933.333": 1100e6/3, "3200": 400e6}[args.ddr_rate]
        if args.sys_clk_freq is not None and abs(args.sys_clk_freq-selected_frequency) > 1:
            parser.error("--ddr-rate and --sys-clk-freq select different clocks")
        args.sys_clk_freq = selected_frequency
    if args.sys_clk_freq is None:
        args.sys_clk_freq = 300e6 if args.with_usnative else 125e6

    #assert not (args.with_etherbone and args.eth_dynamic_ip)

    soc = BaseSoC(
        sys_clk_freq           = args.sys_clk_freq,
        toolchain              = args.toolchain,
        with_usnative          = args.with_usnative,
        usnative_debug         = args.usnative_debug,
        with_dma               = args.with_dma,
        dma_data_width         = args.dma_data_width,
        overclock              = args.overclock,
        usnative_output_dir    = str(Path(args.output_dir or "build/opalkelly_xem8320") / "native"),
        vivado                 = args.vivado,
        #with_ethernet         = args.with_ethernet,
        #with_etherbone        = args.with_etherbone,
        #eth_ip                = args.eth_ip,
        #eth_dynamic_ip        = args.eth_dynamic_ip,
        with_video_terminal    = args.with_video_terminal,
        with_video_framebuffer = args.with_video_framebuffer,
        **parser.soc_argdict
    )

    soc.platform.add_extension(opalkelly_xem8320._sdcard_pmod_io)
    soc.add_spi_sdcard()

    builder = Builder(soc, **parser.builder_argdict)
    if args.build:
        builder.build(**parser.toolchain_argdict)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))
        # TODO: add option for FrontPanel Programming

if __name__ == "__main__":
    main()
