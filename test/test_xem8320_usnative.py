#
# This file is part of LiteX-Boards.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Reject incompatible native build options before any Vivado device query."""
import unittest
from unittest.mock import patch

from litex_boards.targets.opalkelly_xem8320 import BaseSoC, _native_post_route_commands


class TestXEM8320NativeOptions(unittest.TestCase):
    def test_3200_only_downgrades_the_known_pll_drc(self):
        for frequency in (300e6, 1e9/3, 1100e6/3):
            with self.subTest(frequency=frequency):
                self.assertNotIn("PDRC-182", "\n".join(_native_post_route_commands(frequency)))

        commands = _native_post_route_commands(400e6)
        self.assertEqual(commands[0], "set_property SEVERITY Warning [get_drc_checks PDRC-182]")
        self.assertEqual(commands[-1], "report_drc -file opalkelly_xem8320_native_final_drc.rpt")

    def test_default_target_keeps_125mhz_component_mode(self):
        # Avoid constructing the component PHY here: this checks the target's
        # defaults without requiring Vivado or an external DDR build.
        soc = BaseSoC(integrated_main_ram_size=4096, with_led_chaser=False)
        self.assertEqual(soc.clk_freq, 125e6)
        self.assertNotIn("SDRAM_USNATIVE_XEM8320", soc.constants)

    def test_invalid_options_do_not_query_vivado(self):
        cases = [
            dict(toolchain='yosys+nextpnr'),
            dict(sys_clk_freq=125e6),
            dict(sys_clk_freq=400e6),
            dict(sys_clk_freq=1100e6/3),
            dict(dma_data_width=256),
            dict(with_dma=True, dma_data_width=64),
            dict(with_dma=True, with_dma_bank_group_interleaving=True),
            dict(with_dma=True, dma_data_width=128, with_dma_bank_group_interleaving=True),
            dict(dma_data_width=256, with_dma_bank_group_interleaving=True),
            dict(with_video_framebuffer=True),
            dict(integrated_main_ram_size=4096),
            dict(cpu_type='serv'),
            dict(cpu_variant='minimal'),
            dict(uart_name='crossover'),
        ]
        with patch('litedram.phy.usnative.ddrphy.query_device') as query:
            for options in cases:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    BaseSoC(**dict(dict(with_usnative=True, sys_clk_freq=300e6), **options))
            query.assert_not_called()

    def test_native_only_flags_require_native_phy(self):
        for options in (dict(usnative_debug=True), dict(with_dma=True),
                        dict(overclock=True), dict(dma_data_width=256),
                        dict(with_dma_bank_group_interleaving=True)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                BaseSoC(**options)
