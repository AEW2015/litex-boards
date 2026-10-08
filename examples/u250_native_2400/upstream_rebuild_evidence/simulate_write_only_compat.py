"""Run tests with the existing simulation-only write-only RAM compatibility fix.

Pinned Migen's MemoryToArray assumes every memory port has dat_r, whereas current
LiteX FIFO memories have write-only ports. Add an unused output to the simulated
fragment only, as the U250 DMA test already does. Synthesis sources are untouched.
"""
import unittest
import os
import sys
sys.path.insert(0, os.getcwd())
import migen
import migen.sim
import migen.sim.core
from migen import Memory, Signal

original = migen.sim.core.run_simulation


def compatible_simulation(dut, *args, **kwargs):
    fragment = dut.get_fragment() if hasattr(dut, 'get_fragment') else dut
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    return original(fragment, *args, **kwargs)


migen.run_simulation = compatible_simulation
migen.sim.run_simulation = compatible_simulation
migen.sim.core.run_simulation = compatible_simulation
unittest.main(module=None)
