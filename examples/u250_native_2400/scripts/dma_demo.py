"""CPU-free full-capacity native DMA and exclusive x4 read/modify/write test."""
from migen import *
from litex.gen import LiteXModule
from litex.soc.interconnect.csr import CSR, CSRStorage, CSRStatus
from litedram.frontend.dma import LiteDRAMDMAReader, LiteDRAMDMAWriter

CAPACITY = 1 << 34
BEAT_BYTES = 64


def pattern_word(address, pattern):
    """Each 32-bit counter includes all address bits over the 16 GiB module."""
    words = []
    for lane in range(16):
        counter = Cat(Constant(lane, 4), address)  # 32-bit word offset
        a = counter ^ (counter << 13)
        b = a[:32] ^ (a[:32] >> 17)
        c = b[:32] ^ (b[:32] << 5)
        walk = (Constant(1, 64) << address[:6])[:64]
        walking = walk[32*(lane % 2):32*(lane % 2)+32]
        words.append(Mux(pattern == 0, counter,
            Mux(pattern == 1, c[:32], Mux(pattern == 2, walking, ~walking))))
    return Cat(*words)


class FullRangeDMA(LiteXModule):
    def __init__(self, write_port, read_port, fifo_depth=128):
        assert write_port.data_width == read_port.data_width == 512
        assert write_port.address_width == read_port.address_width == 28
        self.allowed = Signal()
        self._start = CSR(name='start')
        self._write_base = CSRStorage(34, name='write_base')
        self._read_base = CSRStorage(34, name='read_base')
        self._length = CSRStorage(35, reset=4096, name='length')
        self._mode = CSRStorage(2, reset=2, name='mode')
        self._pattern = CSRStorage(2, name='pattern')
        self._admit = CSRStorage(name='admit')
        self._timeout = CSRStorage(64, reset=1_500_000_000, name='timeout')
        self._busy = CSRStatus(name='busy')
        self._done = CSRStatus(name='done')
        self._fault = CSRStatus(3, name='fault')
        self._errors = CSRStatus(64, name='errors')
        self._first_error_address = CSRStatus(34, name='first_error_address')
        self._first_error_xor = CSRStatus(512, name='first_error_xor')
        self._write_cycles = CSRStatus(64, name='write_cycles')
        self._read_cycles = CSRStatus(64, name='read_cycles')
        self._total_cycles = CSRStatus(64, name='total_cycles')
        self._write_beats = CSRStatus(64, name='write_beats')
        self._read_beats = CSRStatus(64, name='read_beats')
        self._rmw = CSRStorage(name='rmw')
        self._patch_mask = CSRStorage(64, name='patch_mask')
        self._patch_data = CSRStorage(512, name='patch_data')
        self._rmw_result = CSRStatus(512, name='rmw_result')
        self.writer = writer = LiteDRAMDMAWriter(write_port, fifo_depth, fifo_buffered=True)
        self.reader = reader = LiteDRAMDMAReader(read_port, fifo_depth, fifo_buffered=True)
        self.comb += reader.source.ready.eq(1)

        wb, rb = Signal(28), Signal(28)
        beats = Signal(29)
        wi, ri, received = Signal(29), Signal(29), Signal(29)
        mode, pattern = Signal(2), Signal(2)
        write_on, read_on = Signal(), Signal()
        settle = Signal(9)
        rmw_data, mask, patch = Signal(512), Signal(64), Signal(512)
        rmw_active = Signal()
        patch_phase = Signal()
        read_phase = Signal()
        write_address, read_address, return_address = Signal(28), Signal(28), Signal(28)
        merged = Signal(512)
        for byte in range(64):
            self.comb += merged[8*byte:8*byte+8].eq(Mux(mask[byte],
                patch[8*byte:8*byte+8], rmw_data[8*byte:8*byte+8]))
        self.comb += [
            write_address.eq(wb + wi), read_address.eq(rb + ri), return_address.eq(rb + received),
            writer.sink.address.eq(write_address),
            writer.sink.data.eq(Mux(rmw_active, merged, pattern_word(write_address, pattern))),
            writer.sink.valid.eq(write_on & (wi < beats)), writer.sink.last.eq(wi == beats-1),
            reader.sink.address.eq(read_address), reader.sink.valid.eq(read_on & (ri < beats)),
            reader.sink.last.eq(ri == beats-1),
        ]
        expected = Mux(rmw_active, merged, pattern_word(return_address, pattern))
        write_accept = writer.sink.valid & writer.sink.ready
        read_accept = reader.sink.valid & reader.sink.ready
        write_retire = write_port.wdata.valid & write_port.wdata.ready
        return_accept = reader.source.valid & reader.source.ready
        valid_length = (self._length.storage != 0) & (self._length.storage <= CAPACITY) & (self._length.storage[:6] == 0)
        valid_write = (self._write_base.storage[:6] == 0) & ((self._write_base.storage + self._length.storage) <= CAPACITY)
        valid_read = (self._read_base.storage[:6] == 0) & ((self._read_base.storage + self._length.storage) <= CAPACITY)
        disjoint = ((self._write_base.storage + self._length.storage <= self._read_base.storage) |
            (self._read_base.storage + self._length.storage <= self._write_base.storage))
        valid_config = valid_length & valid_write & valid_read & ((self._mode.storage != 3) | disjoint)
        valid_config = valid_config & ((self._mode.storage != 2) |
            (self._read_base.storage == self._write_base.storage))
        valid_config = valid_config & (~self._rmw.storage | ((self._length.storage == 64) &
            (self._read_base.storage == self._write_base.storage)))

        self.sync += [
            If(self._busy.status,
                self._total_cycles.status.eq(self._total_cycles.status+1),
                If(write_on, self._write_cycles.status.eq(self._write_cycles.status+1)),
                If(read_on, self._read_cycles.status.eq(self._read_cycles.status+1)),
            ),
            If(write_accept, wi.eq(wi+1)),
            If(read_accept, ri.eq(ri+1)),
            If(write_retire, self._write_beats.status.eq(self._write_beats.status+1)),
            If(return_accept,
                received.eq(received+1), self._read_beats.status.eq(self._read_beats.status+1),
                If(rmw_active & ~patch_phase,
                    rmw_data.eq(reader.source.data)
                ).Elif(reader.source.data != expected,
                    self._errors.status.eq(self._errors.status+1),
                    If(self._errors.status == 0,
                        self._first_error_address.status.eq(Cat(Constant(0, 6), return_address)),
                        self._first_error_xor.status.eq(reader.source.data ^ expected)),
                ),
                If(rmw_active & patch_phase, self._rmw_result.status.eq(reader.source.data)),
            ),
        ]
        self.fsm = fsm = FSM(reset_state='IDLE')
        failed = (~self.allowed | ~self._admit.storage | (self._total_cycles.status >= self._timeout.storage))
        fsm.act('IDLE',
            If(self._start.re,
                NextValue(self._done.status, 0), NextValue(self._fault.status, 0),
                NextValue(self._errors.status, 0), NextValue(self._first_error_xor.status, 0),
                NextValue(self._first_error_address.status, 0),
                NextValue(self._write_cycles.status, 0), NextValue(self._read_cycles.status, 0),
                NextValue(self._total_cycles.status, 0), NextValue(self._write_beats.status, 0),
                NextValue(self._read_beats.status, 0), NextValue(wi, 0), NextValue(ri, 0), NextValue(received, 0),
                NextValue(wb, self._write_base.storage[6:]), NextValue(rb, self._read_base.storage[6:]),
                NextValue(beats, self._length.storage[6:]), NextValue(mode, self._mode.storage),
                NextValue(pattern, self._pattern.storage), NextValue(rmw_active, self._rmw.storage),
                NextValue(patch_phase, 0), NextValue(read_phase, 0),
                NextValue(mask, self._patch_mask.storage), NextValue(patch, self._patch_data.storage),
                If(~valid_config, NextValue(self._fault.status, 1), NextValue(self._done.status, 1)
                ).Elif(~self.allowed | ~self._admit.storage,
                    NextValue(self._fault.status, 2), NextValue(self._done.status, 1)
                ).Else(NextValue(self._busy.status, 1), NextState('RUN')),
            ),
        )
        fsm.act('RUN',
            write_on.eq(Mux(rmw_active, patch_phase, (mode != 1) & ~read_phase)),
            read_on.eq(Mux(rmw_active, ~patch_phase, (mode == 1) | (mode == 3) | read_phase)),
            If(failed, NextValue(self._fault.status, 3), NextState('FAULT')
            ).Elif(rmw_active,
                If((~patch_phase & (received == beats)) | (patch_phase & (self._write_beats.status == beats)),
                    NextValue(settle, 255), NextState('SETTLE')),
            ).Elif(mode == 2,
                If(~read_phase & (self._write_beats.status == beats),
                    NextValue(settle, 255), NextState('SETTLE')
                ).Elif(read_phase & (received == beats), NextState('DONE')),
            ).Else(
                If(((mode == 1) | (self._write_beats.status == beats)) &
                   ((mode == 0) | (received == beats)), NextValue(settle, 255), NextState('SETTLE')),
            ),
        )
        fsm.act('SETTLE',
            If(failed, NextValue(self._fault.status, 3), NextState('FAULT')
            ).Elif(settle != 0, NextValue(settle, settle-1)
            ).Elif(rmw_active & ~patch_phase,
                NextValue(patch_phase, 1), NextState('RUN')
            ).Elif(rmw_active,
                NextValue(ri, 0), NextValue(received, 0), NextState('RMW_VERIFY')
            ).Elif((mode == 2) & ~read_phase,
                NextValue(read_phase, 1), NextState('RUN')
            ).Else(NextState('DONE')),
        )
        fsm.act('RMW_VERIFY', read_on.eq(1),
            If(failed, NextValue(self._fault.status, 3), NextState('FAULT')
            ).Elif(received == beats, NextState('DONE')))
        fsm.act('DONE', NextValue(self._busy.status, 0), NextValue(self._done.status, 1), NextState('IDLE'))
        # Stop issuing traffic and continue draining returns; reconfigure to recover.
        fsm.act('FAULT', NextValue(self._busy.status, 0), NextValue(self._done.status, 1))
