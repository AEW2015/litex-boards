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
import math
from pathlib import Path

from migen import *
from migen.genlib.cdc import MultiReg

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
from litex.soc.cores.ram.xilinx_usp_hbm2_runtime import (
    HBMClockMailbox,
    HBMRuntimeQuiesce,
    WishboneAdmissionGate,
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

class _HBMResetRequest(LiteXModule):
    """Bound one SYS request before the MMCM's independent-input reset delay."""
    def __init__(self, request, sys_clk_freq):
        if not math.isfinite(sys_clk_freq) or sys_clk_freq <= 0:
            raise ValueError("Reset request requires a finite positive SYS frequency")
        self.history_cycles = math.ceil(sys_clk_freq / 100e6) + 1
        self.maximum_apb_pulse_cycles = math.ceil(self.history_cycles * 100e6 / sys_clk_freq)
        if self.maximum_apb_pulse_cycles > 3:
            raise ValueError("SYS reset pulse cannot clear before the eight CLK200 delay stages")
        self.seen = Signal()
        self.history = Signal(self.history_cycles)
        self.output = Signal()
        # A held/repeated request cannot reload the history before SYS reset.
        # More than one APB period covers capture at every phase. Bound the
        # APB pulse to30ns, below the eight CLK200 stages' earliest35ns stop.
        self.sync += [
            self.history.eq(Cat(request & ~self.seen, self.history[:self.history_cycles - 1])),
            If(request, self.seen.eq(1)),
        ]
        self.sync.apb += self.output.eq(self.history != 0)


class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_hbm, hbm_ref_clk_freq=100e6,
            strict_timing=False, hbm_axi_domain=False):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()
        if with_hbm:
            self.cd_hbm_ref = ClockDomain()
            self.cd_apb     = ClockDomain()
            if hbm_axi_domain:
                self.cd_axi = ClockDomain()

        # # #

        self.pll = pll = USPMMCM(speedgrade=-2)
        if strict_timing:
            self.reset_request = _HBMResetRequest(self.rst, sys_clk_freq)
            self.comb += pll.reset.eq(self.reset_request.output)
        else:
            self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(platform.request("clk200"), 200e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        if not strict_timing:
            platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin) # Ignore sys_clk to pll.clkin path created by SoC's rst.

        if with_hbm:
            pll.create_clkout(self.cd_hbm_ref, hbm_ref_clk_freq)
            pll.create_clkout(self.cd_apb,     100e6)
            if hbm_axi_domain:
                # Preserve SYS/ref/APB CLKOUT0/1/2 assignments. This output is
                # independently reset-synchronized by USPMMCM.create_clkout.
                pll.create_clkout(self.cd_axi, 250e6)

# BaseSoC ------------------------------------------------------------------------------------------

class HBMHostDrainBarrier(LiteXModule):
    """Acknowledge the current close generation after all SYS work retires.

    A synchronized idle level can remain high from the previous operation.
    The command token and runtime generation prevent a new owner or STOP
    request from consuming that old acknowledgment.
    """
    def __init__(self, admission_closed, host_idle, command_token, generation):
        self.idle_native = Signal()
        self.observed_idle_native = Signal()
        self.token_native = Signal()
        self.generation_native = Signal(32)
        level = Signal()
        self.sync += level.eq(admission_closed & host_idle)
        self.specials += MultiReg(level, self.idle_native, "axi")
        # Open-admission idle observation remains distinct from the tokenized
        # close acknowledgment used for ownership and STOP authorization.
        observed_idle = Signal()
        self.sync += observed_idle.eq(host_idle)
        self.specials += MultiReg(observed_idle, self.observed_idle_native, "axi")
        self.mailbox = mailbox = HBMClockMailbox(33, "sys", "axi")
        sent = Signal(33)
        valid = Signal()
        value = Cat(command_token, generation)
        self.comb += [
            mailbox.data.eq(value),
            mailbox.send.eq(admission_closed & host_idle &
                (~valid | (sent != value))),
            self.token_native.eq(mailbox.output[0]),
            self.generation_native.eq(mailbox.output[1:]),
        ]
        self.sync += If(mailbox.send & ~mailbox.busy,
            sent.eq(value), valid.eq(1))

class HBMCPUTransactionLatch(LiteXModule):
    """Finish an admitted CPU operation even if its Wishbone master times out.

    The AXI-Lite bridge reads Wishbone payload throughout independent AW/W
    acceptance. Hold that payload and CYC until its terminal response; an
    abandoned operation must never acknowledge a subsequent Wishbone cycle.
    """
    def __init__(self, master, slave):
        self.active = Signal()
        self.idle = Signal()
        abandoned = Signal()
        blocked = Signal()
        address = Signal.like(master.adr)
        data = Signal.like(master.dat_w)
        select = Signal.like(master.sel)
        write = Signal()
        self.comb += [
            slave.adr.eq(address), slave.dat_w.eq(data), slave.sel.eq(select),
            slave.we.eq(write), slave.cyc.eq(self.active), slave.stb.eq(self.active),
            master.dat_r.eq(slave.dat_r),
            master.ack.eq(slave.ack & self.active & ~abandoned & master.cyc),
            master.err.eq(slave.err & self.active & ~abandoned & master.cyc),
            self.idle.eq(~self.active),
        ]
        self.sync += [
            If(~master.cyc, blocked.eq(0)),
            If(self.active,
                If(~master.cyc, abandoned.eq(1)),
                If(slave.ack | slave.err,
                    self.active.eq(0),
                    # A newer cycle may already have arrived while the old
                    # response was outstanding. Require its withdrawal too.
                    If(abandoned & master.cyc, blocked.eq(1)),
                ),
            ).Elif(master.cyc & master.stb & ~blocked,
                self.active.eq(1), abandoned.eq(0),
                address.eq(master.adr), data.eq(master.dat_w),
                select.eq(master.sel), write.eq(master.we),
            ),
        ]


class HBMCPUWindowCDC(LiteXModule):
    """Narrow per-port CDC with SYS-side terminal-response accounting.

    Counts extend through the return FIFOs, not just native AXI completion.
    Separate AW/W counts retain half-writes across STOP and arbitrary channel
    skew. One CPU window permits at most one read and one write outstanding.
    """
    def __init__(self, master, slave, gray_bus_prefix=None):
        self.idle = Signal()
        self.fault = Signal()
        self.crossing = axi.AXILiteClockDomainCrossing(master, slave,
            cd_from="sys", cd_to="axi", gray_bus_prefix=gray_bus_prefix)
        pending = [Signal(4, name=f"hbm_cdc_{name}_pending")
            for name in ("aw", "w", "ar")]
        self.pending = pending
        self.sync += [
            If((getattr(master, name).valid & getattr(master, name).ready) !=
                    (getattr(master, response).valid & getattr(master, response).ready),
                If(getattr(master, name).valid & getattr(master, name).ready,
                    If(count == 15, self.fault.eq(1)).Else(count.eq(count + 1)),
                ).Else(
                    If(count == 0, self.fault.eq(1)).Else(count.eq(count - 1)),
                ),
            ) for name, response, count in zip(("aw", "w", "ar"), ("b", "b", "r"), pending)
        ]
        self.comb += self.idle.eq(~self.fault &
            (Cat(*pending) == 0) & ~master.aw.valid & ~master.w.valid &
            ~master.ar.valid & ~master.b.valid & ~master.r.valid)

class HBMCPUWindowPipeline(LiteXModule):
    """BIST-aware narrow AXI-Lite window boundary with idle accounting."""
    def __init__(self, cpu, native, bist=False, name="hbm_cpu_window"):
        self.idle = Signal(name=f"{name}_idle")
        converted = axi.AXILiteInterface(data_width=32, address_width=32)
        self.converted = converted
        self.native = native
        self.comb += [
            cpu.aw.connect(converted.aw), cpu.w.connect(converted.w),
            cpu.ar.connect(converted.ar),
        ]
        request_buffered = axi.AXILiteInterface(data_width=32, address_width=32)
        self.request_buffered = request_buffered
        offset_input = request_buffered if bist else converted
        request_buffers = {}
        self.request_buffers = request_buffers
        if bist:
            for channel in ("aw", "w", "ar"):
                request = getattr(converted, channel)
                buffer = stream.Buffer(request.description, pipe_valid=True, pipe_ready=True)
                request_buffers[channel] = buffer
                self.submodules += buffer
                self.comb += [request.connect(buffer.sink),
                    buffer.source.connect(getattr(request_buffered, channel))]
            write_buffer = stream.Buffer(offset_input.b.description,
                pipe_valid=True, pipe_ready=True)
            self.submodules += write_buffer
            self.write_buffer = write_buffer
            self.comb += [offset_input.b.connect(write_buffer.sink),
                write_buffer.source.connect(cpu.b)]
        else:
            write_buffer = None
            self.comb += converted.b.connect(cpu.b)

        read_buffer = stream.Buffer(offset_input.r.description,
            pipe_valid=True, pipe_ready=True)
        self.read_buffer = read_buffer
        self.submodules += read_buffer
        self.comb += [offset_input.r.connect(read_buffer.sink),
            read_buffer.source.connect(cpu.r)]
        self.submodules += axi.AXILiteOffset(offset_input, native, offset=0)

        if bist:
            write_pending = Signal(name=f"{name}_write_pending")
            read_pending = Signal(name=f"{name}_read_pending")
            self.write_pending = write_pending
            self.read_pending = read_pending
            write_request = ((converted.aw.valid & converted.aw.ready) |
                (converted.w.valid & converted.w.ready))
            read_request = converted.ar.valid & converted.ar.ready
            write_retire = write_buffer.source.valid & write_buffer.source.ready
            read_retire = read_buffer.source.valid & read_buffer.source.ready
            self.sync += [
                If(write_retire, write_pending.eq(0)
                ).Elif(write_request, write_pending.eq(1)),
                If(read_retire, read_pending.eq(0)
                ).Elif(read_request, read_pending.eq(1)),
            ]
            visible_request = (converted.aw.valid | converted.w.valid | converted.ar.valid |
                request_buffers["aw"].source.valid | request_buffers["w"].source.valid |
                request_buffers["ar"].source.valid)
            visible_response = (write_buffer.sink.valid | write_buffer.source.valid |
                read_buffer.sink.valid | read_buffer.source.valid)
            self.comb += self.idle.eq(~(write_pending | read_pending |
                visible_request | visible_response))
        else:
            self.comb += self.idle.eq(1)

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
        hbm_bist              = False,
        hbm_axi_domain        = False,
        **kwargs):
        self.hbm_primitive = hbm_primitive or hbm_primitive_rtl is not None
        self.hbm_axi_domain = bool(hbm_axi_domain)
        if hbm_axi_domain and not (self.hbm_primitive and hbm_bist):
            raise ValueError("Separate HBM AXI domain requires primitive HBM and BIST")
        if hbm_bist and not self.hbm_primitive:
            raise ValueError("HBM burst testing requires the primitive HBM profile")
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
            hbm_ref_clk_freq=200e6 if self.hbm_primitive else 100e6,
            strict_timing=self.hbm_primitive, hbm_axi_domain=hbm_axi_domain)
        if self.hbm_primitive:
            platform.toolchain.pre_placement_commands += [
                "create_clock -name jtag_tck -period 66.666667 [get_pins BSCANE2/INTERNAL_TCK]",
                "set_clock_groups -asynchronous -group [get_clocks jtag_tck] -group [get_clocks -filter {{NAME != jtag_tck}}]",
            ]
            platform.toolchain.additional_commands += [
                "check_timing -verbose -file {build_name}_check_timing.rpt",
                "report_cdc -details -file {build_name}_cdc.rpt",
                "report_clock_interaction -file {build_name}_clock_interaction.rpt",
            ]
            if hbm_axi_domain:
                gray_constraints = (Path(__file__).resolve().parents[1] / "software" /
                    "fk33_hbm" / "rebuild" / "gray-constraints.tcl").read_text()
                # Vivado command templates expand braces with str.format.
                platform.toolchain.pre_placement_commands.append(
                    gray_constraints.replace("{", "{{").replace("}", "}}"))
                platform.toolchain.additional_commands.append(
                    "report_bus_skew -file {build_name}_bus_skew.rpt")

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
                hbm_core = PrimitiveHBM2(platform, rtl_dir=hbm_primitive_rtl)
                self.hbm = hbm_core if hbm_axi_domain else ClockDomainsRenamer({"axi": "sys"})(hbm_core)
                axi_domain = "axi" if hbm_axi_domain else "sys"
                # Four RV32 windows select eight groups of four native ports.
                # AXILiteDecoder holds each route through its response, including
                # when a selector write arrives while a request is outstanding.
                # The runtime interposer gates each Wishbone root and drains all
                # 32 native ports before software may change bank or retune HBM.
                self.hbm_window = LiteXModule()
                self.hbm_window.bank = CSRStorage(3, name="bank")
                self.hbm_window.bank_effective = Signal(3, name="hbm_window_bank_effective")
                self.hbm_window.bank_fault = Signal(name="hbm_window_bank_fault")
                all_windows_idle = Signal(name="hbm_all_windows_idle")
                self.hbm_runtime = HBMRuntimeQuiesce(platform,
                    registered_request=hbm_bist, registered_completions=hbm_bist,
                    clock_domain=axi_domain,
                    sys_host_idle=all_windows_idle if hbm_axi_domain else None)
                host_idle_axi = Signal(name="hbm_host_admission_closed_and_idle_axi")
                observed_idle_axi = Signal(name="hbm_host_observed_idle_axi")
                if hbm_bist:
                    from litex.soc.cores.ram.xilinx_usp_hbm2_bist import HBMBurstBIST
                    self.hbm_bist = HBMBurstBIST(platform,
                        ready=self.hbm.memory_ready_axi if hbm_axi_domain else self.hbm.memory_ready,
                        quiesce_request=self.hbm_runtime.native_quiesce_request,
                        all_drained=self.hbm_runtime.native_all_drained,
                        host_idle=host_idle_axi if hbm_axi_domain else all_windows_idle,
                        clock_domain=axi_domain)
                    self.add_constant("HBM_BIST_CHANNELS", 32)
                host_quiesce = self.hbm_runtime.quiesce_request
                if hbm_bist:
                    host_quiesce = host_quiesce | self.hbm_bist.owner_sys
                    if hbm_axi_domain:
                        host_quiesce = host_quiesce | self.hbm_bist.admission_block_sys
                if hbm_axi_domain:
                    # Roundtrip barrier: stale native idle is insufficient.
                    # This level only rises after SYS admission has closed and
                    # every accepted request AND returned response has retired.
                    self.hbm_host_barrier = HBMHostDrainBarrier(host_quiesce,
                        all_windows_idle, self.hbm_bist.command_token_sys,
                        self.hbm_runtime.request_generation_sys)
                    self.comb += [
                        host_idle_axi.eq(self.hbm_host_barrier.idle_native),
                        observed_idle_axi.eq(self.hbm_host_barrier.observed_idle_native),
                        self.hbm_bist.host_barrier_token_native.eq(
                            self.hbm_host_barrier.token_native),
                        self.hbm_runtime.host_barrier_generation_native.eq(
                            self.hbm_host_barrier.generation_native),
                    ]
                admission_gates = []
                window_idle_signals = []
                cdc_idle_signals = []
                cdc_fault_signals = []
                for channel in range(4):
                    host   = wishbone.Interface(data_width=32, adr_width=30)
                    gated  = wishbone.Interface(data_width=32, adr_width=30)
                    narrow = axi.AXILiteInterface(data_width=32, address_width=32)
                    native = axi.AXILiteInterface(data_width=32, address_width=33)
                    admission = WishboneAdmissionGate(host, gated,
                        self.hbm.memory_ready, host_quiesce)
                    admission_gates.append(admission)
                    self.submodules += admission
                    if hbm_axi_domain:
                        held = wishbone.Interface(data_width=32, adr_width=30)
                        hold = HBMCPUTransactionLatch(gated, held)
                        self.submodules += hold
                        self.submodules += axi.Wishbone2AXILite(held, narrow)
                    else:
                        self.submodules += axi.Wishbone2AXILite(gated, narrow)
                    window_pipeline = HBMCPUWindowPipeline(narrow, native, bist=hbm_bist,
                        name=f"hbm_window_{channel}")
                    self.submodules += window_pipeline
                    window_idle_signals.append(window_pipeline.idle & hold.idle
                        if hbm_axi_domain else window_pipeline.idle)
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
                        ports.append((lambda address, bank=bank: self.hbm_window.bank_effective == bank, port))
                        if hbm_axi_domain:
                            crossed = axi.AXILiteInterface(data_width=32, address_width=33)
                            crossing = HBMCPUWindowCDC(port, crossed,
                                gray_bus_prefix=f"hbm_cpu_pc{4*bank + channel:02d}")
                            self.submodules += crossing
                            cdc_idle_signals.append(crossing.idle)
                            cdc_fault_signals.append(crossing.fault)
                            port = crossed
                        for name in ("aw", "w", "ar"):
                            self.comb += getattr(port, name).connect(getattr(local, name))
                        self.comb += local.b.connect(port.b)
                        # Register the local lane mux before the narrow bank mux.
                        local_read = stream.Buffer(local.r.description,
                            pipe_valid=True, pipe_ready=True)
                        self.submodules += ClockDomainsRenamer(axi_domain)(local_read)
                        self.comb += local.r.connect(local_read.sink)
                        self.comb += local_read.source.connect(port.r)
                        self.submodules += ClockDomainsRenamer(axi_domain)(axi.AXILiteConverter(local, wide))
                        for name in ("aw", "w", "ar"):
                            request = getattr(wide, name)
                            buffer = stream.Buffer(request.description,
                                pipe_valid=True, pipe_ready=True)
                            self.submodules += ClockDomainsRenamer(axi_domain)(buffer)
                            self.comb += request.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(routed, name))
                        for name in ("b", "r"):
                            response = getattr(routed, name)
                            buffer = stream.Buffer(response.description,
                                pipe_valid=True, pipe_ready=True)
                            self.submodules += ClockDomainsRenamer(axi_domain)(buffer)
                            self.comb += response.connect(buffer.sink)
                            self.comb += buffer.source.connect(getattr(wide, name))
                        native_channel = 4*bank + channel
                        native_upstream = axi.AXIInterface(data_width=256, address_width=33, id_width=6)
                        tracked_upstream = native_upstream
                        port_idle = observed_idle_axi if hbm_axi_domain else admission.idle & window_pipeline.idle
                        if hbm_bist:
                            tracked_upstream = axi.AXIInterface(data_width=256, address_width=33, id_width=6)
                            engine_idle = self.hbm_bist.add_port(native_channel,
                                native_upstream, tracked_upstream)
                            port_idle = port_idle & engine_idle
                        self.hbm_runtime.add_port(native_channel, tracked_upstream,
                            self.hbm.axi[native_channel], host_idle=port_idle)
                        self.submodules += ClockDomainsRenamer(axi_domain)(axi.AXILite2AXI(routed, native_upstream))
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
                if hbm_axi_domain:
                    self.add_constant("HBM_AXI_CLOCK_DOMAIN", 1)
                    self.add_constant("HBM_AXI_CLK_FREQ", 250000000)
                self.comb += all_windows_idle.eq(Reduce("AND",
                    [admission_gates[index].idle & window_idle_signals[index]
                        for index in range(4)] + cdc_idle_signals))
                bank_change = (self.hbm_window.bank.wr_stb &
                    (self.hbm_window.bank.storage != self.hbm_window.bank_effective))
                bank_change_safe = (all_windows_idle & self.hbm_runtime.all_drained &
                    (self.hbm_runtime.error.status == 0) & ~self.hbm_window.bank_fault)
                if hbm_bist:
                    bank_change_safe = bank_change_safe & ~self.hbm_bist.owner_sys
                self.sync += If(bank_change,
                    If(bank_change_safe,
                        self.hbm_window.bank_effective.eq(self.hbm_window.bank.storage),
                    ).Else(
                        self.hbm_window.bank_fault.eq(1),
                    ),
                )
                external_fault = self.hbm_window.bank_fault
                if hbm_axi_domain:
                    external_fault = external_fault | Reduce("OR", cdc_fault_signals)
                if hbm_bist:
                    external_fault = external_fault | (self.hbm_bist.error_mask_sys != 0)
                self.comb += self.hbm_runtime.external_fault.eq(external_fault)

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
                    ("leds", 3), ("timer0", 4), ("uart", 5), ("hbm_control", 6),
                    ("hbm_window", 7), ("hbm_runtime", 8)):
                if name in self.csr.locs and self.csr.locs[name] != location:
                    raise ValueError(f"Primitive HBM requires CSR bank {name} at {location}")
                self.csr.add(name, location, use_loc_if_exists=True)
            if hbm_bist:
                self.csr.add("hbm_bist", 9, use_loc_if_exists=True)

    def _finalize_bus(self):
        if not (self.hbm_primitive and hasattr(self, "hbm_bist")):
            return super()._finalize_bus()
        # The primitive-HBM BIST CSR bank is reached through the JTAG Wishbone
        # master. Register this bridge to split the address/decode path before
        # it reaches the CSR read mux. The CSR map remains unchanged.
        csr_origin = self.mem_map.get("csr", None)
        if csr_origin is None:
            self.logger.error("CSR main-bus origin is not defined.")
            raise SoCError()
        self.add_csr_bridge(name="csr", origin=csr_origin, with_register=True, read_latency=2)
        self.bus.finalize()
        if hasattr(self, "ctrl") and self.bus.timeout is not None:
            if hasattr(self.ctrl, "bus_error") and hasattr(self.bus._interconnect, "timeout"):
                self.comb += self.ctrl.bus_error.eq(self.bus._interconnect.timeout.error)
        self.add_config("BUS_STANDARD",      self.bus.standard)
        self.add_config("BUS_DATA_WIDTH",    self.bus.data_width)
        self.add_config("BUS_ADDRESS_WIDTH", self.bus.address_width)
        self.add_config("BUS_BURSTING",      int(self.bus.bursting))

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
            paging=self.csr.paging, ordering=self.csr.ordering,
            read_pipeline=hasattr(self, "hbm_bist"))
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
    parser.add_target_argument("--hbm-bist", action="store_true", help="Add exclusive per-channel burst test masters and counters.")
    parser.add_target_argument("--hbm-axi-domain", action="store_true", help="Experimental independent 250 MHz HBM AXI clock domain (requires primitive BIST).")
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
        hbm_bist              = args.hbm_bist,
        hbm_axi_domain        = args.hbm_axi_domain,
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
