#!/usr/bin/env python3
"""Fabric-only wide-endpoint characterization; no HBM primitive or programming.

The internal peer is a registered single-address transaction model, not HBM.
All legal one-to-sixteen native-beat lengths are selected by registered input.
"""
import json
from pathlib import Path

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer
from litex.soc.cores.clock import USPMMCM
from litex_boards.platforms.sqrl_fk33 import Platform
from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint import HBMWideEndpoint


class ProtectedResetRelease(Module):
    """Target-local standard two-FDPE release with preserved BUFG boundary."""
    def __init__(self, cd, async_reset):
        assert getattr(cd,'rst_buf',None) == 'bufg'
        name = 'endpoint_reset_' + cd.name
        protected = {('DONT_TOUCH','TRUE'),('KEEP','TRUE')}
        meta = Signal(name=name+'_meta',attr=protected.copy())
        release = Signal(name=name+'_prebuf',attr=protected.copy())
        cd.rst.attr.update(protected)
        self.specials += [
            Instance('FDPE',name=name+'_stage1',attr={'async_reg','ars_ff1',('DONT_TOUCH','TRUE')},
                p_INIT=1,i_PRE=async_reset,i_CE=1,i_C=cd.clk,i_D=0,o_Q=meta),
            Instance('FDPE',name=name+'_stage2',attr={'async_reg','ars_ff2',('DONT_TOUCH','TRUE')},
                p_INIT=1,i_PRE=async_reset,i_CE=1,i_C=cd.clk,i_D=meta,o_Q=release),
            Instance('BUFG',name=name+'_buffer',attr={('DONT_TOUCH','TRUE')},i_I=release,o_O=cd.rst)]


class ProtectedResetLowerer:
    @staticmethod
    def lower(dr):
        return ProtectedResetRelease(dr.cd,dr.async_reset)


class EndpointCRG(Module):
    def __init__(self, platform):
        self.clock_domains.cd_sys = ClockDomain('sys')
        self.clock_domains.cd_hbm_ref = ClockDomain('hbm_ref')
        self.clock_domains.cd_apb = ClockDomain('apb')
        self.clock_domains.cd_axi = ClockDomain('axi')
        self.submodules.pll = pll = USPMMCM(speedgrade=-2, name='endpoint_mmcm')
        self.clk200 = platform.request('clk200')
        pll.register_clkin(self.clk200, 200e6)
        self.comb += pll.reset.eq(0)
        for domain, rate in ((self.cd_sys, 250e6), (self.cd_hbm_ref, 200e6),
                (self.cd_apb, 100e6), (self.cd_axi, 500e6)):
            pll.create_clkout(domain, rate, margin=0, reset_buf='bufg')


class RegisteredNativePeer(Module):
    """AXI3 registered source/sink; stores the most recent sixteen native beats.

    AW and W accept independently. Responses remain stable while stalled.
    This deliberately bounded model is not a memory-capacity/bandwidth model.
    """
    def __init__(self, bus):
        self.protocol_error = Signal()
        storage = Array(Signal(256, name='endpoint_native_write_sink_%02d' % n, attr={('DONT_TOUCH', 'TRUE')}) for n in range(16))
        aw_seen, w_done = Signal(), Signal()
        aw_addr = Signal(33, name='endpoint_native_address_sink', attr={('DONT_TOUCH', 'TRUE')})
        aw_id, w_id = Signal(6), Signal(6)
        aw_len, w_count = Signal(4), Signal(5)
        aw_ready, w_ready, ar_ready = Signal(reset=1), Signal(reset=1), Signal(reset=1)
        b_valid, b_id = Signal(), Signal(6)
        read_pending, read_index = Signal(), Signal(4)
        read_len, read_id = Signal(4), Signal(6)
        r_valid, r_last = Signal(), Signal()
        r_data = Signal(256, name='endpoint_native_read_source', attr={('DONT_TOUCH', 'TRUE')})
        self.comb += [bus.aw.ready.eq(aw_ready), bus.w.ready.eq(w_ready),
            bus.b.valid.eq(b_valid), bus.b.id.eq(b_id), bus.b.resp.eq(0),
            bus.ar.ready.eq(ar_ready), bus.r.valid.eq(r_valid), bus.r.data.eq(r_data),
            bus.r.id.eq(read_id), bus.r.resp.eq(0), bus.r.last.eq(r_last)]
        aw_fire, w_fire = bus.aw.valid & bus.aw.ready, bus.w.valid & bus.w.ready
        b_fire = bus.b.valid & bus.b.ready
        ar_fire, r_fire = bus.ar.valid & bus.ar.ready, bus.r.valid & bus.r.ready
        writes = [If(bus.w.strb[byte], storage[w_count[:4]][8*byte:8*(byte+1)].eq(bus.w.data[8*byte:8*(byte+1)])) for byte in range(32)]
        self.sync.axi += [
            If(aw_fire,
                aw_ready.eq(0), aw_seen.eq(1), aw_addr.eq(bus.aw.addr),
                aw_id.eq(bus.aw.id), aw_len.eq(bus.aw.len),
                If((bus.aw.size != 5) | (bus.aw.burst != 1), self.protocol_error.eq(1))),
            If(w_fire,
                If(w_count == 0, w_id.eq(bus.w.id))
                .Elif(bus.w.id != w_id, self.protocol_error.eq(1)),
                *writes, w_count.eq(w_count + 1),
                If(bus.w.last, w_done.eq(1), w_ready.eq(0))),
            If(aw_seen & w_done & ~b_valid,
                b_valid.eq(1), b_id.eq(aw_id),
                If((w_id != aw_id) | (w_count != aw_len + 1), self.protocol_error.eq(1))),
            If(b_fire, b_valid.eq(0), aw_seen.eq(0), w_done.eq(0), w_count.eq(0), aw_ready.eq(1), w_ready.eq(1)),
            If(ar_fire,
                ar_ready.eq(0), read_pending.eq(1), read_index.eq(0),
                read_len.eq(bus.ar.len), read_id.eq(bus.ar.id),
                If((bus.ar.addr != aw_addr) | (bus.ar.size != 5) | (bus.ar.burst != 1), self.protocol_error.eq(1))),
            If(r_fire, r_valid.eq(0), If(r_last, ar_ready.eq(1))),
            If(read_pending & (~r_valid | bus.r.ready),
                r_valid.eq(1), r_data.eq(storage[read_index]), r_last.eq(read_index == read_len),
                If(read_index == read_len, read_pending.eq(0)).Else(read_index.eq(read_index + 1))),
        ]


class ApplicationExerciser(Module):
    """Variable-length transaction generator with byte-exact retained-data oracle."""
    def __init__(self, endpoint, enable, seed, strobe, native_len):
        self.errors = Signal()
        self.completed = Signal(32)
        self.checksum = Signal(32)
        self.busy = Signal()
        transaction = Signal(32)
        captured_seed, captured_strobe = Signal(32), Signal(64)
        length, write_index, read_index = Signal(4), Signal(3), Signal(3)
        expected = Array(Signal(512, name='application_expected_%d' % n) for n in range(8))
        address, identity = Signal(33), Signal(6)
        payload = Cat(*[(transaction ^ captured_seed ^ write_index ^ Constant((0x94660000 + lane*0x10203) & 0xffffffff, 32)) for lane in range(16)])
        final_word = length[1:4]
        odd_final = ~length[0] & (write_index == final_word)
        valid_mask = Mux(odd_final, Constant(0xffffffff,64), Constant(2**64-1,64))
        effective_strobe = captured_strobe & valid_mask
        mask = Cat(*[Replicate(effective_strobe[byte], 8) for byte in range(64)])
        read_odd = ~length[0] & (read_index == final_word)
        read_mask = Mux(read_odd, Constant(2**256-1,512), Constant(2**512-1,512))
        self.comb += [endpoint.aw.addr.eq(address), endpoint.aw.len.eq(length), endpoint.aw.id.eq(identity),
            endpoint.w.data.eq(payload), endpoint.w.strb.eq(captured_strobe), endpoint.w.last.eq(write_index == final_word),
            endpoint.ar.addr.eq(address), endpoint.ar.len.eq(length), endpoint.ar.id.eq(identity)]
        self.submodules.fsm = fsm = FSM(reset_state='IDLE')
        fsm.act('IDLE', If(enable, NextValue(captured_seed,seed), NextValue(captured_strobe,strobe),
            NextValue(length,native_len), NextValue(write_index,0), NextValue(read_index,0),
            NextValue(address,transaction << 9), NextValue(identity,transaction[:6]), NextState('AW')))
        fsm.act('AW', self.busy.eq(1), endpoint.aw.valid.eq(1), If(endpoint.aw.ready, NextState('W')))
        fsm.act('W', self.busy.eq(1), endpoint.w.valid.eq(1), If(endpoint.w.ready,
            NextValue(expected[write_index], (payload & mask) | (expected[write_index] & ~mask)),
            If(write_index == final_word, NextState('B')).Else(NextValue(write_index,write_index+1))))
        fsm.act('B', self.busy.eq(1), endpoint.b.ready.eq(1), If(endpoint.b.valid,
            If((endpoint.b.id != identity) | (endpoint.b.resp != 0), NextValue(self.errors,1)), NextState('AR')))
        fsm.act('AR', self.busy.eq(1), endpoint.ar.valid.eq(1), If(endpoint.ar.ready, NextState('R')))
        checksum_fold = Constant(0,32)
        for lane in range(16):
            lane_data = (endpoint.r.data & read_mask)[32*lane:32*(lane+1)]
            rotated = lane_data if lane == 0 else Cat(lane_data[32-lane:32],lane_data[:32-lane])
            checksum_fold = checksum_fold ^ rotated
        checksum_next = (Cat(self.checksum[25:32],self.checksum[:25]) + checksum_fold)[:32]
        fsm.act('R', self.busy.eq(1), endpoint.r.ready.eq(1), If(endpoint.r.valid,
            If(((endpoint.r.data & read_mask) != (expected[read_index] & read_mask)) |
                (endpoint.r.keep != Mux(read_odd,Constant(0xffffffff,64),Constant(2**64-1,64))) |
                (endpoint.r.id[:6] != identity) | (~read_odd & (endpoint.r.id[6:] != identity)) |
                (endpoint.r.resp != 0) | (endpoint.r.last != (read_index == final_word)), NextValue(self.errors,1)),
            NextValue(self.checksum,checksum_next),
            If(read_index == final_word, NextValue(self.completed,self.completed+1),
                NextValue(transaction,transaction+1), NextState('IDLE')).Else(NextValue(read_index,read_index+1))))


class RegisteredApplicationShell(Module):
    """Real SYS launch/config FFs; closes after each complete LEN1..16 sweep."""
    def __init__(self, application, endpoint):
        self.enable = Signal()
        self.seed = Signal(32, reset=0x94660001)
        self.strobe = Signal(64, reset=2**64-1)
        self.native_len = Signal(4)
        self.quiesce = Signal()
        self.pauses = Signal(8)
        self.running = Signal(reset=1)
        sequence = Signal(6)
        next_sequence = Signal(6)
        seen_completed = Signal(32)
        close_age = Signal(3)
        self.comb += [next_sequence.eq(sequence+1),
            self.enable.eq(self.running & (application.completed == seen_completed)),
            self.quiesce.eq(~self.running & ~application.busy)]
        masks = Array(Constant(mask,64) for mask in
            (2**64-1,0,0x5555aaaa00ff0ff0,0xffffffff00000000))
        self.sync.sys += [
            If(application.completed != seen_completed,
                seen_completed.eq(application.completed), sequence.eq(next_sequence),
                self.native_len.eq(next_sequence[:4]), self.strobe.eq(masks[next_sequence[4:6]]),
                self.seed.eq(Cat(self.seed[31] ^ self.seed[21] ^ self.seed[1] ^ self.seed[0],self.seed[:31])),
                If(self.native_len == 15, self.running.eq(0),close_age.eq(0))),
            If(~self.running,
                If(close_age != 7,close_age.eq(close_age+1)),
                If((close_age >= 3) & endpoint.quiesced,
                    self.running.eq(1),self.pauses.eq(self.pauses+1)))
        ]


class WideEndpointPrototype(Module):
    def __init__(self, platform):
        self.status = Signal(8, name='observed_status')
        self.checksum = Signal(32, name='observed_checksum')
        self.completed = Signal(32, name='observed_completed')
        self.submodules.crg = EndpointCRG(platform)
        self.submodules.endpoint = endpoint = HBMWideEndpoint(
            app_domain='sys', native_domain='axi', gray_bus_prefix='hbm_endpoint_pc00')
        self.submodules.peer = peer = RegisteredNativePeer(endpoint.native)
        enable,seed,strobe,native_len = Signal(),Signal(32),Signal(64),Signal(4)
        self.submodules.application = application = ApplicationExerciser(endpoint,enable,seed,strobe,native_len)
        self.submodules.shell = shell = RegisteredApplicationShell(application,endpoint)
        self.comb += [enable.eq(shell.enable),seed.eq(shell.seed),strobe.eq(shell.strobe),
            native_len.eq(shell.native_len),endpoint.quiesce.eq(shell.quiesce)]
        # Peer diagnostics cross through real synchronizer flops; no false-path
        # timing exception is emitted by this target/flow.
        from migen.genlib.cdc import MultiReg
        peer_error = Signal()
        self.specials += MultiReg(peer.protocol_error, peer_error, 'sys')
        self.sync.sys += [self.status.eq(Cat(application.busy, application.errors,
            endpoint.protocol_error, peer_error, endpoint.drained, endpoint.quiesced, shell.running, shell.pauses[0])),
            self.checksum.eq(application.checksum), self.completed.eq(application.completed)]


def generate(output_dir):
    """Generate source only; caller records imports, snapshots and CAD inputs."""
    output = Path(output_dir).resolve(); output.mkdir(parents=True, exist_ok=False)
    platform = Platform(); prototype = WideEndpointPrototype(platform)
    config = prototype.crg.pll.compute_config()
    ios={prototype.crg.clk200.p, prototype.crg.clk200.n, prototype.status, prototype.checksum, prototype.completed}
    converted=platform.get_verilog(prototype.get_fragment(), ios=ios, name='fk33_wide_endpoint',
        special_overrides={AsyncResetSynchronizer:ProtectedResetLowerer})
    (output/'fk33_wide_endpoint.v').write_text(converted.main_source, encoding='utf-8')
    for name,data in converted.data_files.items(): (output/name).write_text(data, encoding='utf-8')
    names={key:converted.ns.get_name(signal) for key,signal in {
        'clk200_p':prototype.crg.clk200.p,'clk200_n':prototype.crg.clk200.n,
        'status':prototype.status,'checksum':prototype.checksum,'completed':prototype.completed}.items()}
    clock_io=[]
    from litex.build.generic_platform import IOStandard
    for port,pins,others,resource in platform.resolve_signals(converted.ns)[0]:
        if resource[0] == 'clk200':
            standards=[item.name for item in others if isinstance(item,IOStandard)]
            assert len(pins)==1 and len(standards)==1
            clock_io.append({'port':port,'package_pin':pins[0],'iostandard':standards[0]})
    assert len(clock_io)==2
    result={'clock_io':clock_io,'scope':'Fabric-only single-address registered peer; NOT HBM or a programming image',
        'part':platform.device,'top':'fk33_wide_endpoint','ports':names,
        'mmcm':config,'platform_sources':[str(row[0]) for row in platform.sources],
        'gray_groups':10,'gray_bits_per_group':3,'external_application_inputs':0,'registered_diagnostic_outputs':72,
        'launch_contract':'Internal SYS registered config, all LEN1..16, four strobes, varying seed; quiesced pause each sweep',
        'output_delay_ns':1.0,
        'clock_domain_contract':{'sys_MHz':250,'axi_MHz':500,'hbm_ref_MHz':200,'apb_MHz':100,
            'common_MMCM_lock_reset':True,'independent_hot_reset_supported':False,'reset_buf':'bufg'}}
    (output/'target-description.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result
