from bootstrap import ROOT
import random
import unittest
from collections import deque
from migen import *
from migen.sim import run_simulation
from litedram.common import LiteDRAMNativePort
from dma_demo import FullRangeDMA, CAPACITY


def expected_word(address, pattern):
    data = 0
    for lane in range(16):
        counter = address*16 + lane
        if pattern == 0:
            value = counter
        elif pattern == 1:
            value = (counter ^ (counter << 13)) & 0xffffffff
            value = (value ^ (value >> 17)) & 0xffffffff
            value = (value ^ (value << 5)) & 0xffffffff
        else:
            walk = 1 << (address % 64)
            value = (walk >> (32*(lane % 2))) & 0xffffffff
            if pattern == 3:
                value ^= 0xffffffff
        data |= value << (32*lane)
    return data


class DMATest(unittest.TestCase):
    def exercise(self, jobs, initial=None):
        wp = LiteDRAMNativePort('write', 28, 512)
        rp = LiteDRAMNativePort('read', 28, 512)
        dut = FullRangeDMA(wp, rp, fifo_depth=8)
        memory = dict(initial or {})
        write_commands, read_returns = deque(), deque()
        results = []
        rng = random.Random(0x250)

        @passive
        def model():
            pending = None
            while True:
                yield wp.cmd.ready.eq(rng.randrange(4) != 0)
                yield wp.wdata.ready.eq(bool(write_commands) and rng.randrange(4) != 0)
                yield rp.cmd.ready.eq(rng.randrange(4) != 0)
                if pending is None and read_returns and rng.randrange(3) != 0:
                    pending = read_returns.popleft()
                yield rp.rdata.valid.eq(pending is not None)
                yield rp.rdata.data.eq(pending or 0)
                yield
                if (yield wp.cmd.valid) and (yield wp.cmd.ready):
                    write_commands.append((yield wp.cmd.addr))
                if (yield wp.wdata.valid) and (yield wp.wdata.ready):
                    address = write_commands.popleft()
                    self.assertEqual((yield wp.wdata.we), (1 << 64)-1)
                    memory[address] = (yield wp.wdata.data)
                if (yield rp.cmd.valid) and (yield rp.cmd.ready):
                    read_returns.append(memory.get((yield rp.cmd.addr), 0))
                if pending is not None and (yield rp.rdata.ready):
                    pending = None

        def driver():
            yield dut.allowed.eq(1)
            yield dut._admit.storage.eq(1)
            for job in jobs:
                for key, value in job.items():
                    yield getattr(dut, '_'+key).storage.eq(value)
                yield dut._start.re.eq(1)
                yield
                yield dut._start.re.eq(0)
                yield
                for cycle in range(6000):
                    if (yield dut._done.status):
                        break
                    yield
                else:
                    self.fail('DMA simulation timeout')
                results.append(dict(fault=(yield dut._fault.status), errors=(yield dut._errors.status),
                    write_beats=(yield dut._write_beats.status), read_beats=(yield dut._read_beats.status),
                    total_cycles=(yield dut._total_cycles.status), rmw_result=(yield dut._rmw_result.status)))
                yield
                yield
        fragment = dut.get_fragment()
        # Compatibility with the pinned Migen simulator's write-only FIFO ports.
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, [driver(), model()])
        return memory, results

    def test_all_patterns_high_addresses_and_last_word(self):
        jobs = []
        for pattern in range(4):
            base = (1 << 32) + pattern*4096
            jobs.append(dict(write_base=base, read_base=base, length=1024, pattern=pattern, mode=2))
        jobs.append(dict(write_base=CAPACITY-64, read_base=CAPACITY-64, length=64, pattern=0, mode=2))
        memory, results = self.exercise(jobs)
        for result in results:
            self.assertEqual((result['fault'], result['errors']), (0, 0))
        self.assertEqual(memory[(CAPACITY-64)//64], expected_word((CAPACITY-64)//64, 0))
        for job in jobs:
            for address in range(job['write_base']//64, (job['write_base']+job['length'])//64):
                self.assertEqual(memory[address], expected_word(address, job['pattern']))

    def test_simultaneous_independent_reader_writer(self):
        read_base, write_base = 5 << 30, 12 << 30
        initial = {address:expected_word(address, 1) for address in range(read_base//64, read_base//64+64)}
        _, results = self.exercise([dict(write_base=write_base, read_base=read_base, length=4096, pattern=1, mode=3)], initial)
        self.assertEqual((results[0]['fault'], results[0]['errors']), (0, 0))
        self.assertEqual((results[0]['write_beats'], results[0]['read_beats']), (64, 64))

    def test_rmw_all_byte_masks(self):
        base = (9 << 30) + 64
        initial = {base//64:expected_word(base//64, 0)}
        patches = [1 << byte for byte in range(64)] + [0, (1 << 64)-1, 0xaaaaaaaaaaaaaaaa]
        value = int.from_bytes(bytes(range(64)), 'little')
        jobs = [dict(write_base=base, read_base=base, length=64, mode=2, pattern=0,
            rmw=1, patch_mask=mask, patch_data=value) for mask in patches]
        memory, results = self.exercise(jobs, initial)
        for result in results:
            self.assertEqual((result['fault'], result['errors']), (0, 0))
        self.assertEqual(memory[base//64], value)

    def test_invalid_ranges_and_alias_protection(self):
        jobs = [dict(write_base=CAPACITY-64, read_base=CAPACITY-64, length=128, mode=2),
                dict(write_base=0, read_base=0, length=128, mode=3),
                dict(write_base=1, read_base=1, length=64, mode=2),
                dict(write_base=0, read_base=0, length=0, mode=2)]
        _, results = self.exercise(jobs)
        self.assertTrue(all(result['fault'] == 1 for result in results))


if __name__ == '__main__':
    unittest.main(verbosity=2)
