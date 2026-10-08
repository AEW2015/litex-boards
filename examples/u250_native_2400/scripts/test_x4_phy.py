from bootstrap import ROOT
import json
import unittest
from migen import *
from migen.sim import run_simulation
from build import Platform, Module2400
from litedram.phy.usnative.pins import extract_ddr_pins
from litedram.phy.usnative.topology import parse_vivado_map
from litedram.phy.usnative.auxiliary import parse_auxiliary_map
from litedram.phy.usnative.layout import native_layout
from litedram.phy.usnative.mapping import NativeMapping
from litedram.phy.usnative.rx_trace import NativeRXLanePopScoreboard


class X4Test(unittest.TestCase):
    def topology(self):
        platform = Platform()
        pads = platform.request('ddram', 0)
        pins = extract_ddr_pins(platform, pads)
        for directory in (ROOT/'build/native').glob('usnative-query-*'):
            path = directory/'provenance.json'
            if not path.exists():
                continue
            provenance = json.loads(path.read_text())
            if provenance['pin_fingerprint'] == pins.fingerprint:
                version = provenance['vivado_version']
                physical = parse_vivado_map(pins, (directory/'physical.tsv').read_text(), vivado_version=version)
                aux = parse_auxiliary_map(pins, physical, (directory/'auxiliary.tsv').read_text(), vivado_version=version)
                return physical, native_layout(physical, aux, family=pins.family)
        self.fail('No executed, matching U250 topology query')

    def test_all_nibbles_own_four_correct_dq_and_one_control(self):
        physical, layout = self.topology()
        self.assertEqual(layout.databits, 64)
        self.assertEqual(len(layout.lanes), 16)
        mapping = NativeMapping(layout)
        self.assertEqual(mapping.lane_count, 16)
        self.assertEqual(mapping.dm_taps, ())
        owned = []
        for lane in layout.lanes:
            self.assertEqual(len(lane.controls), 1)
            self.assertIsNone(lane.mask)
            dqs = physical['dqs_p', lane.index]
            for offset, tap in enumerate(lane.dq):
                key = ('dq', lane.index*4+offset)
                self.assertEqual(layout.slices[tap], key)
                dq = physical[key]
                self.assertEqual((dq.bank,dq.byte,dq.nibble), (dqs.bank,dqs.byte,dqs.nibble))
                owned.append(tap)
        self.assertEqual(len(set(owned)), 64)

    def test_independent_fifo_accounting_for_each_nibble(self):
        _, layout = self.topology()
        dut = NativeRXLanePopScoreboard(len(layout.slices), [lane.dq for lane in layout.lanes])
        all_dq = sum(1 << tap for lane in layout.lanes for tap in lane.dq)
        def test():
            for lane in layout.lanes:
                yield dut.clear.eq(1)
                yield
                yield dut.clear.eq(0)
                yield dut.accepted.eq(all_dq ^ (1 << lane.dq[0]))
                yield dut.read_valid.eq(1)
                yield
                yield
                self.assertEqual((yield dut.missing_lanes), 1 << lane.index)
                yield dut.read_valid.eq(0)
                yield dut.accepted.eq(0)
                yield
        run_simulation(dut, test())

    def test_geometry_profile_and_rdimm_mode_registers(self):
        module = Module2400(150e6, '1:8', speedgrade='2400')
        geom = module.geom_settings
        self.assertEqual((geom.rowbits,geom.colbits,geom.bankbits), (17,10,4))
        self.assertEqual((1 << (geom.rowbits+geom.colbits+geom.bankbits))*8, 1 << 34)
        config = json.loads((ROOT/'evidence/design_config.json').read_text())
        self.assertEqual((config['cl'],config['cwl'],config['strobes']), (17,12,16))
        self.assertTrue(config['is_rdimm'])
        self.assertFalse(config['with_dm'])
        self.assertEqual(config['cmd_latency'], 6)
        sequence = config['init_sequence']
        self.assertEqual(len([row for row in sequence if 'RCD' in row[0]]), 8)
        mr5 = next(row for row in sequence if row[0] == 'Load Mode Register 5')
        self.assertEqual(mr5[1] & (1 << 10), 0)
        coarse = next(row for row in sequence if row[0] == 'Load RCD F0RC0A')
        self.assertEqual(coarse[1] & 7, 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
