"""Generate and implement the CPU-free U250 channel-0 DDR4-2400 example."""
from bootstrap import ROOT
import argparse
import copy
import hashlib
import json
from pathlib import Path
from migen import *
from migen.genlib.cdc import MultiReg
from migen.genlib.resetsync import AsyncResetSynchronizer
from litex.gen import LiteXModule
from litex.build.generic_platform import Pins, Subsignal, IOStandard
from litex.build.xilinx import XilinxUSPPlatform
from litex_boards.platforms import xilinx_alveo_u250
from litex.soc.cores.clock import USPMMCM
from litex.soc.integration.soc_core import SoCMini
from litex.soc.integration.builder import Builder
from litedram.core import LiteDRAMCore
from litedram.core.controller import ControllerSettings
from litedram.modules import MTA18ASF2G72PZ
from litedram.phy.usnative import USNativeDDRPHY
from litedram.phy.usnative.pins import extract_ddr_pins
from litedram.phy.usnative.query import query_device
from litedram.phy.usnative.layout import native_layout
from litedram.phy.dfi import DFIRateConverter
from litedram.init import get_ddr4_phy_init_sequence
from dma_demo import FullRangeDMA, CAPACITY


class Platform(XilinxUSPPlatform):
    default_clk_name = 'clk300'
    default_clk_period = 1e9/300e6

    def __init__(self):
        ios = copy.deepcopy(xilinx_alveo_u250._io)
        for resource in ios:
            if resource[0] == 'clk300' and resource[1] == 0:
                for sub in resource[2:]:
                    for constraint in sub.constraints:
                        if isinstance(constraint, IOStandard):
                            constraint.name = 'DIFF_POD12_DCI'
            if resource[0] == 'ddram' and resource[1] == 0:
                for sub in resource[2:]:
                    if isinstance(sub, Subsignal) and sub.name in ('dqs_p', 'dqs_n'):
                        for constraint in sub.constraints:
                            if isinstance(constraint, IOStandard):
                                constraint.name = 'DIFF_POD12_DCI'
        super().__init__('xcu250-figd2104-2L-e', ios, toolchain='vivado')
        self.ddr_banks = ()
        # Request only the inactive-channel reset/CKE/CS pins. DQ and clocks remain undriven.
        for resource in ios:
            if resource[0] == 'ddram' and resource[1] != 0:
                self.add_extension([('inactive_ddr', resource[1], *[
                    sub for sub in resource[2:] if isinstance(sub, Subsignal) and sub.name in ('reset_n', 'cke', 'cs_n')])])

    def do_finalize(self, fragment):
        super().do_finalize(fragment)
        self.add_period_constraint(self.lookup_request('clk300', 0), 1e9/300e6)
        self.add_platform_command('set_property BITSTREAM.CONFIG.OVERTEMPSHUTDOWN ENABLE [current_design]')
        self.add_platform_command('set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]')
        for bank in self.ddr_banks:
            self.add_platform_command(f'set_property INTERNAL_VREF 0.84 [get_iobanks {bank}]')


class CRG(LiteXModule):
    def __init__(self, platform, banks):
        self.cd_sys = ClockDomain()
        self.cd_sys2x = ClockDomain()
        self.cd_riu = ClockDomain()
        self.cd_ref = ClockDomain()
        self.pll = pll = USPMMCM(speedgrade=-2)
        pll.vco_margin = 0.01
        pll.register_clkin(platform.request('clk300', 0), 300e6)
        self.comb += self.cd_ref.clk.eq(pll.clkin)
        self.specials += AsyncResetSynchronizer(self.cd_ref, 0)
        pll.create_clkout(self.cd_sys, 150e6)
        pll.create_clkout(self.cd_sys2x, 300e6)
        pll.create_clkout(self.cd_riu, 150e6)
        self.native_clock = Signal(len(banks))
        self.native_locked = Signal(len(banks))
        self.native_enable = Signal(len(banks))
        for index, bank in enumerate(banks):
            feedback = Signal(name=f'native_feedback_bank{bank}')
            self.specials += Instance('PLLE4_ADV', name=f'native_pll_bank{bank}',
                p_COMPENSATION='INTERNAL', p_CLKFBOUT_MULT=4, p_CLKFBOUT_PHASE=90.0,
                p_CLKIN_PERIOD=1e9/300e6, p_CLKOUT0_DIVIDE=1,
                p_CLKOUTPHY_MODE='VCO_2X', p_DIVCLK_DIVIDE=1,
                i_CLKIN=self.cd_sys2x.clk, i_CLKFBIN=feedback, o_CLKFBOUT=feedback,
                i_RST=~pll.locked, i_PWRDWN=0,
                i_CLKOUTPHYEN=self.native_enable[index],
                o_CLKOUTPHY=self.native_clock[index], o_LOCKED=self.native_locked[index],
                i_DADDR=0, i_DCLK=0, i_DEN=0, i_DI=0, i_DWE=0)


class Module2400(MTA18ASF2G72PZ):
    technology_timings = copy.copy(MTA18ASF2G72PZ.technology_timings)
    # Controller uses conservative long bank-group limits for every command pair.
    technology_timings.tCCD = (8, None)
    technology_timings.tWTR = (4, 7.5)
    technology_timings.tRRD = (4, 4.9)
    # Stay below the 7.8 us normal-temperature refresh limit after cycle rounding.
    technology_timings.tREFI = {'1x':7750, '2x':3875, '4x':1937.5}


class Demo(SoCMini):
    def __init__(self):
        platform = Platform()
        pads = platform.request('ddram', 0)
        pin_map = extract_ddr_pins(platform, pads)
        physical, auxiliary, directory = query_device(pin_map, ROOT/'build/native', cache_dir=ROOT/'query_cache')
        layout = native_layout(physical, auxiliary, family=pin_map.family)
        platform.ddr_banks = layout.banks
        assert len(layout.lanes) == 16 and all(len(lane.dq) == 4 for lane in layout.lanes)
        self.crg = CRG(platform, layout.banks)
        super().__init__(platform, 150e6, ident='U250 x64 x4 RDIMM Native DDR4-2400 DMA', csr_data_width=32)
        self.add_jtagbone()
        self.comb += platform.request('ddram_reset_gate').eq(0)
        for channel in (1, 2, 3):
            inactive = platform.request('inactive_ddr', channel)
            self.comb += [inactive.reset_n.eq(0), inactive.cke.eq(0), inactive.cs_n.eq(1)]
        phy = DFIRateConverter.phy_wrapper(USNativeDDRPHY, ratio=2,
            serdes_reset=ResetSignal('sys'),
            phy_attrs=['software_control', '_ready', '_training_stage', '_training_error', '_bisc_only', '_en_vtc'])
        self.ddrphy = phy(pads, platform, self.crg.native_clock, self.crg.native_locked,
            self.crg.native_enable, sys_clk_freq=300e6, output_dir=ROOT/'build/native',
            queried_topology=(pin_map, physical, auxiliary, directory),
            is_rdimm=True, rcd_latency=1, trained_gate_delays=True,
            with_read_monitor=True, with_rx_trace=False)
        self.ddrphy.settings.mr0_wr = 28
        module = Module2400(150e6, '1:8', speedgrade='2400')
        self.sdram = LiteDRAMCore(self.ddrphy, module.geom_settings, module.timing_settings,
            150e6, controller_settings=ControllerSettings(
                with_registered_row_hit=True, with_registered_refresh_timers=True))
        self.specials += MultiReg(~self.sdram.dfii._control.fields.sel, self.ddrphy.software_control, 'sys2x')
        write_port = self.sdram.crossbar.get_port(mode='write', data_width=512)
        read_port = self.sdram.crossbar.get_port(mode='read', data_width=512)
        self.dma = FullRangeDMA(write_port, read_port)
        self.comb += self.dma.allowed.eq(self.sdram.dfii._control.fields.sel &
            self.ddrphy._ready.status & (self.ddrphy._training_stage.storage == 5) &
            (self.ddrphy._training_error.storage == 0) & ~self.ddrphy._bisc_only.storage &
            self.ddrphy._en_vtc.storage)
        self.add_config('SDRAM_USNATIVE')
        self.add_config('SDRAM_CAPACITY_BYTES', CAPACITY)
        sequence, registers = get_ddr4_phy_init_sequence(self.ddrphy.settings, module.timing_settings)
        config = dict(part=platform.device, channel=0, data_rate=2400e6,
            controller_hz=150e6, phy_hz=300e6, ck_hz=1200e6, bitslice_pll_hz=2400e6,
            capacity_bytes=CAPACITY, native_address_width=write_port.address_width,
            native_beat_bytes=64, strobes=self.ddrphy.settings.strobes,
            with_dm=self.ddrphy.settings.with_dm, is_rdimm=self.ddrphy.settings.is_rdimm,
            cl=self.ddrphy.settings.cl, cwl=self.ddrphy.settings.cwl,
            cmd_latency=self.ddrphy.settings.cmd_latency,
            init_sequence=sequence, init_registers=registers,
            banks=layout.banks, hardware_validated=False)
        (ROOT/'evidence/design_config.json').write_text(json.dumps(config, indent=2))
        platform.toolchain.bitstream_commands += [
            'report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose -file {build_name}_final_timing.rpt',
            'report_pulse_width -all_violators -file {build_name}_pulse_width.rpt',
            'report_bus_skew -file {build_name}_bus_skew.rpt',
            'report_cdc -details -file {build_name}_cdc.rpt',
            'report_methodology -file {build_name}_methodology.rpt',
            'report_drc -file {build_name}_final_drc.rpt',
            'write_checkpoint -force {build_name}_final.dcp',
            'if {{[llength [get_timing_paths -quiet -delay_type max -slack_lesser_than 0]]}} {{error "Setup timing failed"}}',
            'if {{[llength [get_timing_paths -quiet -delay_type min -slack_lesser_than 0]]}} {{error "Hold timing failed"}}',
        ]
        platform.toolchain.pre_placement_commands += [
            f'source "{(ROOT/"scripts/management_constraints.tcl").as_posix()}"',
            f'source "{(ROOT/"scripts/channel_floorplan.tcl").as_posix()}"',
        ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-only', action='store_true')
    args = parser.parse_args()
    soc = Demo()
    builder = Builder(soc, output_dir=str(ROOT/'build'), compile_software=False,
        compile_gateware=not args.generate_only, csr_csv=str(ROOT/'build/csr.csv'),
        csr_json=str(ROOT/'build/csr.json'))
    builder.build(build_name='u250_native_2400', vivado_report_level='signoff', vivado_max_threads=4)


if __name__ == '__main__':
    main()
