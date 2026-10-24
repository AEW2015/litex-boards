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
from litex.soc.cores.ram.xilinx_usp_hbm2_endpoint import HBMWideEndpoint, HBMSynchronousControlRegister


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
        # Preserve the original statement priority: transaction retirement
        # overrides AW/W acceptance and B creation on a coincident edge.
        set_b = aw_seen & w_done & ~b_valid
        last_w = w_fire & bus.w.last
        for name,q,d,ce in (
            ('peer_aw_seen',aw_seen,Mux(b_fire,0,1),b_fire | aw_fire),
            ('peer_aw_ready',aw_ready,Mux(b_fire,1,0),b_fire | aw_fire),
            ('peer_w_ready',w_ready,Mux(b_fire,1,0),b_fire | last_w),
            ('peer_w_done',w_done,Mux(b_fire,0,1),b_fire | last_w),
            ('peer_b_valid',b_valid,Mux(b_fire,0,1),b_fire | set_b),
            ('peer_w_count',w_count,Mux(b_fire,0,w_count+1),b_fire | w_fire)):
            setattr(self.submodules,name,HBMSynchronousControlRegister(q,d,ce,domain='axi',name=name))
        writes = [If(bus.w.strb[byte], storage[w_count[:4]][8*byte:8*(byte+1)].eq(bus.w.data[8*byte:8*(byte+1)])) for byte in range(32)]
        self.sync.axi += [
            If(aw_fire,
                aw_addr.eq(bus.aw.addr),
                aw_id.eq(bus.aw.id), aw_len.eq(bus.aw.len),
                If((bus.aw.size != 5) | (bus.aw.burst != 1), self.protocol_error.eq(1))),
            If(w_fire,
                If(w_count == 0, w_id.eq(bus.w.id))
                .Elif(bus.w.id != w_id, self.protocol_error.eq(1)),
                *writes),
            If(aw_seen & w_done & ~b_valid,
                b_id.eq(aw_id),
                If((w_id != aw_id) | (w_count != aw_len + 1), self.protocol_error.eq(1))),
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


class QueuedNativePeer(Module):
    """Ordered four-command AXI3 peer; 2KiB RAM with reset byte-valid fencing.

    AW and whole-burst W ingress are independent. The commit engine pairs them
    in order and checks WID/LAST. Latency/stalls are identical for window1/4 tests.
    """
    def __init__(self,bus,*,response_latency=12,read_latency=12,stall_period=0):
        from litex.soc.interconnect import stream
        if not (0<=response_latency<=255 and 0<=read_latency<=255):raise ValueError('Latency must fit8bits')
        if stall_period not in (0,4,8,16):raise ValueError('Stall period must be0/4/8/16')
        self.protocol_error=Signal();self.write_outstanding=Signal(3);self.read_outstanding=Signal(3)
        self.w_bursts_pending=Signal(3)
        self.submodules.awq=awq=ClockDomainsRenamer('axi')(stream.SyncFIFO([('addr',33),('len',4),('id',6)],4,buffered=True))
        self.submodules.wq=wq=ClockDomainsRenamer('axi')(stream.SyncFIFO([('data',256),('strb',32),('id',6)],64,buffered=True))
        self.submodules.arq=arq=ClockDomainsRenamer('axi')(stream.SyncFIFO([('addr',33),('len',4),('id',6)],4,buffered=True))
        accept=Signal(reset=1)
        if stall_period:
            phase=Signal(max=stall_period);self.sync.axi+=phase.eq(phase+1)
            self.comb+=accept.eq(phase!=0)
        self.comb += [
            bus.aw.ready.eq(awq.sink.ready & (self.write_outstanding<4) & accept),
            awq.sink.valid.eq(bus.aw.valid & (self.write_outstanding<4) & accept),
            awq.sink.addr.eq(bus.aw.addr),awq.sink.len.eq(bus.aw.len),awq.sink.id.eq(bus.aw.id),
            bus.w.ready.eq(wq.sink.ready & (self.w_bursts_pending<4) & accept),
            wq.sink.valid.eq(bus.w.valid & (self.w_bursts_pending<4) & accept),
            wq.sink.data.eq(bus.w.data),wq.sink.strb.eq(bus.w.strb),wq.sink.id.eq(bus.w.id),wq.sink.last.eq(bus.w.last),
            bus.ar.ready.eq(arq.sink.ready & (self.read_outstanding<4) & accept),
            arq.sink.valid.eq(bus.ar.valid & (self.read_outstanding<4) & accept),
            arq.sink.addr.eq(bus.ar.addr),arq.sink.len.eq(bus.ar.len),arq.sink.id.eq(bus.ar.id)]
        aw_fire=bus.aw.valid & bus.aw.ready;w_fire=bus.w.valid & bus.w.ready;ar_fire=bus.ar.valid & bus.ar.ready
        b_fire=bus.b.valid & bus.b.ready;r_fire=bus.r.valid & bus.r.ready
        memory=Memory(256,64);self.specials+=memory
        self.write_port=wp=memory.get_port(write_capable=True,we_granularity=8,clock_domain='axi')
        self.read_port=rp=memory.get_port(async_read=True,clock_domain='axi');self.specials += [wp,rp]
        self.byte_valid=valid=Array(Signal(32,name='peer4_byte_valid_%02d'%i) for i in range(64))
        active=Signal();left=Signal(5);address=Signal(6);write_id=Signal(6)
        bhead,btail=Signal(2),Signal(2);bcount=Signal(3)
        bvalid=Array(Signal(name='peer4_bslot_valid_%d'%i) for i in range(4))
        bdelay=Array(Signal(8,name='peer4_bslot_delay_%d'%i) for i in range(4))
        bid=Array(Signal(6,name='peer4_bslot_id_%d'%i) for i in range(4))
        self.comb += [bus.b.valid.eq((bcount!=0) & bvalid[bhead] & (bdelay[bhead]==0)),bus.b.id.eq(bid[bhead]),bus.b.resp.eq(0)]
        # The memory stage always drains. AW/W ownership advances on enqueue;
        # visible memory/valid bits and B allocation advance only on commit.
        stage_valid=Signal();stage_data=Signal(256);stage_strb=Signal(32)
        stage_address=Signal(6);stage_id=Signal(6);stage_last=Signal()
        self.commit_stage_valid=stage_valid
        self.commit_stage_terminal=stage_last
        self.commit_stage_address=stage_address
        self.commit_words=commit_words=Signal(64)

        self.b_count=bcount;self.aw_queue_level=awq.level;self.write_active=active
        self.write_accounted=Signal(4)
        self.comb += self.write_accounted.eq(bcount+active+awq.level+(stage_valid & stage_last))
        start_w=awq.source.valid & ~active
        enqueue=active & wq.source.valid
        finish_enqueue=enqueue & (left==1)
        self.submodules.peer_window_commit_words=HBMSynchronousControlRegister(
            commit_words,Cat(*[enqueue & (address==word) for word in range(64)]),1,
            domain='axi',name='peer_window_commit_words')
        commit=stage_valid
        finish_w=commit & stage_last
        self.comb += [awq.source.ready.eq(~active),wq.source.ready.eq(active),
            wp.adr.eq(stage_address),wp.dat_w.eq(stage_data),wp.we.eq(Mux(commit,stage_strb,0))]
        self.sync.axi += [
            If(start_w,address.eq(awq.source.addr[5:11]),write_id.eq(awq.source.id)),
            If(enqueue,address.eq(address+1),stage_address.eq(address),
                stage_data.eq(wq.source.data),stage_strb.eq(wq.source.strb),
                stage_id.eq(write_id),stage_last.eq(left==1),
                If((wq.source.id!=write_id) | (wq.source.last!=(left==1)),self.protocol_error.eq(1))),
            *[If(commit_words[word],valid[word].eq(valid[word] | stage_strb)) for word in range(64)],
            If(aw_fire,If((bus.aw.size!=5)|(bus.aw.burst!=1)|(bus.aw.addr[:5]!=0)|(bus.aw.addr[11:]!=0)|
                ((bus.aw.addr[5:11]+bus.aw.len+1)>64),self.protocol_error.eq(1))),
            If(ar_fire,If((bus.ar.size!=5)|(bus.ar.burst!=1)|(bus.ar.addr[:5]!=0)|(bus.ar.addr[11:]!=0)|
                ((bus.ar.addr[5:11]+bus.ar.len+1)>64),self.protocol_error.eq(1)))]
        for slot in range(4):
            allocate=finish_w & (btail==slot)
            decrement=bvalid[slot] & (bdelay[slot]!=0)
            self.sync.axi += [
                If(b_fire & (bhead==slot),bvalid[slot].eq(0)),
                If(allocate,bvalid[slot].eq(1),bid[slot].eq(stage_id))]
            setattr(self.submodules,'peer_window_bdelay_%d'%slot,HBMSynchronousControlRegister(
                bdelay[slot],Mux(allocate,response_latency,bdelay[slot]-1),allocate | decrement,
                domain='axi',name='peer_window_bdelay_'+('zero','one','two','three')[slot]))
        read_active=Signal();read_left=Signal(5);read_address=Signal(6);read_id=Signal(6);delay=Signal(8)
        issued_last=Signal();rvalid=Signal();rlast=Signal();rdata=Signal(256,name='peer4_registered_read_source',attr={('DONT_TOUCH','TRUE')})
        # Three elastic stages: capture RAM and byte-valid metadata, retain
        # an unmasked raw stage for memory output registering, then mask R.
        prefetch_valid=Signal();prefetch_data=Signal(256,reset_less=True);prefetch_mask=Signal(32)
        prefetch_id=Signal(6);prefetch_last=Signal();response_id=Signal(6)
        self.prefetch_valid=prefetch_valid
        self.prefetch_data=prefetch_data;self.prefetch_mask=prefetch_mask
        self.prefetch_id=prefetch_id;self.prefetch_last=prefetch_last
        raw_valid=Signal();raw_data=Signal(256,reset_less=True);raw_mask=Signal(32)
        raw_id=Signal(6);raw_last=Signal()
        self.raw_valid=raw_valid;self.raw_data=raw_data;self.raw_mask=raw_mask
        self.raw_id=raw_id;self.raw_last=raw_last
        start_r=arq.source.valid & ~read_active
        response_space=~rvalid | bus.r.ready
        raw_space=~raw_valid | response_space
        prefetch_space=~prefetch_valid | raw_space
        prefetch_enqueue=read_active & ~issued_last & (delay==0) & prefetch_space & accept
        load_raw=prefetch_valid & raw_space
        load_r=raw_valid & response_space
        finish_r=r_fire & rlast
        self.comb += [arq.source.ready.eq(~read_active),rp.adr.eq(read_address),bus.r.valid.eq(rvalid),bus.r.last.eq(rlast),
            bus.r.data.eq(rdata),bus.r.id.eq(response_id),bus.r.resp.eq(0)]
        mask=Cat(*[Replicate(raw_mask[byte],8) for byte in range(32)])
        self.sync.axi += [If(start_r,read_address.eq(arq.source.addr[5:11]),read_id.eq(arq.source.id)),
            If(prefetch_enqueue,prefetch_data.eq(rp.dat_r),prefetch_mask.eq(valid[read_address]),
                prefetch_id.eq(read_id),prefetch_last.eq(read_left==1),read_address.eq(read_address+1)),
            If(load_raw,raw_data.eq(prefetch_data),raw_mask.eq(prefetch_mask),raw_id.eq(prefetch_id),raw_last.eq(prefetch_last)),
            If(load_r,rdata.eq(raw_data & mask),rlast.eq(raw_last),response_id.eq(raw_id))]
        decrement_read=read_active & (delay!=0)
        self.submodules.peer_window_read_delay=HBMSynchronousControlRegister(
            delay,Mux(start_r,read_latency,delay-1),start_r | decrement_read,
            domain='axi',name='peer_window_read_delay')
        controls=(
            ('peer_window_aw_count',self.write_outstanding,self.write_outstanding+Mux(aw_fire,1,-1),aw_fire ^ b_fire),
            ('peer_window_w_count',self.w_bursts_pending,self.w_bursts_pending+Mux(w_fire & bus.w.last,1,-1),(w_fire & bus.w.last)^b_fire),
            ('peer_window_ar_count',self.read_outstanding,self.read_outstanding+Mux(ar_fire,1,-1),ar_fire ^ finish_r),
            ('peer_window_write_active',active,Mux(start_w,1,0),start_w | finish_enqueue),
            ('peer_window_write_left',left,Mux(start_w,awq.source.len+1,left-1),start_w | enqueue),
            ('peer_window_bhead',bhead,bhead+1,b_fire),('peer_window_btail',btail,btail+1,finish_w),
            ('peer_window_bcount',bcount,bcount+Mux(finish_w,1,-1),finish_w ^ b_fire),
            ('peer_window_read_active',read_active,Mux(start_r,1,0),start_r | finish_r),
            ('peer_window_read_left',read_left,Mux(start_r,arq.source.len+1,read_left-1),start_r | prefetch_enqueue),
            ('peer_window_read_issued_last',issued_last,Mux(start_r,0,1),start_r | (prefetch_enqueue & (read_left==1))),
            ('peer_window_rvalid',rvalid,Mux(load_r,1,0),response_space),
            ('peer_window_commit_valid',stage_valid,enqueue,1),
            ('peer_window_prefetch_valid',prefetch_valid,prefetch_enqueue,prefetch_space),
            ('peer_window_raw_valid',raw_valid,prefetch_valid,raw_space))
        for name,q,d,ce in controls:setattr(self.submodules,name,HBMSynchronousControlRegister(q,d,ce,domain='axi',name=name))


class WindowApplicationExerciser(Module):
    """Four ordered commands per frame; overlapping AW/W/B and AR/R streams."""
    def __init__(self,endpoint,enable,seed,strobe,native_len):
        self.errors=Signal();self.completed=Signal(32);self.checksum=Signal(32);self.busy=Signal()
        self.max_write_credits=Signal(3);self.max_read_credits=Signal(3)
        frame=Signal(32);captured_seed=Signal(32);captured_strobe=Signal(64);length=Signal(4);identity=Signal(6)
        aw_count=Signal(3);w_command=Signal(3);w_word=Signal(3);b_count=Signal(3);ar_count=Signal(3);r_command=Signal(2);r_word=Signal(3)
        expected=Array(Signal(512,name='window_expected_%02d'%i) for i in range(32))
        final_word=length[1:4];w_slot=Cat(w_word,w_command[:2]);r_slot=Cat(r_word,r_command)
        payload=Cat(*[(frame ^ captured_seed ^ (w_command<<16) ^ w_word ^ Constant((0x94660000+lane*0x10203)&0xffffffff,32)) for lane in range(16)])
        odd_write=~length[0] & (w_word==final_word);odd_read=~length[0] & (r_word==final_word)
        byte_mask=captured_strobe & Mux(odd_write,Constant(0xffffffff,64),Constant(2**64-1,64))
        write_mask=Cat(*[Replicate(byte_mask[byte],8) for byte in range(64)])
        read_mask=Mux(odd_read,Constant(2**256-1,512),Constant(2**512-1,512))
        self.comb += [endpoint.aw.addr.eq(aw_count[:2]<<9),endpoint.aw.len.eq(length),endpoint.aw.id.eq(identity),
            endpoint.w.data.eq(payload),endpoint.w.strb.eq(captured_strobe),endpoint.w.last.eq(w_word==final_word),
            endpoint.ar.addr.eq(ar_count[:2]<<9),endpoint.ar.len.eq(length),endpoint.ar.id.eq(identity)]
        self.submodules.fsm=fsm=FSM(reset_state='IDLE')
        fsm.act('IDLE',If(enable,NextValue(captured_seed,seed),NextValue(captured_strobe,strobe),NextValue(length,native_len),
            NextValue(identity,frame[:6]),NextValue(aw_count,0),NextValue(w_command,0),NextValue(w_word,0),NextValue(b_count,0),
            NextValue(ar_count,0),NextValue(r_command,0),NextValue(r_word,0),NextState('WRITE')))
        fsm.act('WRITE',self.busy.eq(1),endpoint.aw.valid.eq(aw_count<4),endpoint.w.valid.eq((w_command<aw_count)&(w_command<4)),endpoint.b.ready.eq(1),
            If(endpoint.aw.valid & endpoint.aw.ready,NextValue(aw_count,aw_count+1)),
            If(endpoint.w.valid & endpoint.w.ready,
                NextValue(expected[w_slot],(payload & write_mask)|(expected[w_slot] & ~write_mask)),
                If(w_word==final_word,NextValue(w_word,0),NextValue(w_command,w_command+1)).Else(NextValue(w_word,w_word+1))),
            If(endpoint.b.valid,If((endpoint.b.id!=identity)|(endpoint.b.resp!=0),NextValue(self.errors,1)),
                NextValue(b_count,b_count+1),If(b_count==3,NextState('READ'))))
        fold=Constant(0,32)
        for lane in range(16):
            word=(endpoint.r.data & read_mask)[32*lane:32*(lane+1)]
            fold=fold ^ (word if lane==0 else Cat(word[32-lane:32],word[:32-lane]))
        fsm.act('READ',self.busy.eq(1),endpoint.ar.valid.eq(ar_count<4),endpoint.r.ready.eq(1),
            If(endpoint.ar.valid & endpoint.ar.ready,NextValue(ar_count,ar_count+1)),
            If(endpoint.r.valid,
                If(((endpoint.r.data & read_mask)!=(expected[r_slot] & read_mask))|
                    (endpoint.r.keep!=Mux(odd_read,Constant(0xffffffff,64),Constant(2**64-1,64)))|
                    (endpoint.r.id[:6]!=identity)|(~odd_read & (endpoint.r.id[6:]!=identity))|
                    (endpoint.r.resp!=0)|(endpoint.r.first!=(r_word==0))|(endpoint.r.last!=(r_word==final_word)),NextValue(self.errors,1)),
                NextValue(self.checksum,(Cat(self.checksum[25:32],self.checksum[:25])+fold)[:32]),
                If(r_word==final_word,NextValue(r_word,0),If(r_command==3,NextValue(self.completed,self.completed+1),NextValue(frame,frame+1),NextState('IDLE'))
                    .Else(NextValue(r_command,r_command+1))).Else(NextValue(r_word,r_word+1))))
        write_credits=getattr(endpoint,"write_credits",endpoint.write_pending)
        read_credits=getattr(endpoint,"read_credits",endpoint.read_pending)
        self.sync.sys += [If(write_credits>self.max_write_credits,self.max_write_credits.eq(write_credits)),
            If(read_credits>self.max_read_credits,self.max_read_credits.eq(read_credits))]


class WideEndpointPrototype(Module):
    def __init__(self, platform, *, max_outstanding=1):
        if max_outstanding not in (1,4):raise ValueError("Window must be1or4")
        self.status = Signal(8, name='observed_status')
        self.checksum = Signal(32, name='observed_checksum')
        self.completed = Signal(32, name='observed_completed')
        self.submodules.crg = EndpointCRG(platform)
        options={} if max_outstanding==1 else {'max_outstanding':4}
        self.submodules.endpoint = endpoint = HBMWideEndpoint(
            app_domain='sys', native_domain='axi', gray_bus_prefix='hbm_endpoint_pc00',**options)
        self.submodules.peer = peer = (RegisteredNativePeer(endpoint.native) if max_outstanding==1 else QueuedNativePeer(endpoint.native))
        enable,seed,strobe,native_len = Signal(),Signal(32),Signal(64),Signal(4)
        App=ApplicationExerciser if max_outstanding==1 else WindowApplicationExerciser
        self.submodules.application = application = App(endpoint,enable,seed,strobe,native_len)
        self.submodules.shell = shell = RegisteredApplicationShell(application,endpoint)
        self.comb += [enable.eq(shell.enable),seed.eq(shell.seed),strobe.eq(shell.strobe),
            native_len.eq(shell.native_len),endpoint.quiesce.eq(shell.quiesce)]
        # Peer diagnostics cross through real synchronizer flops; no false-path
        # timing exception is emitted by this target/flow.
        from migen.genlib.cdc import MultiReg
        peer_error = Signal()
        self.specials += MultiReg(peer.protocol_error, peer_error, 'sys')
        observed_window=([shell.running,shell.pauses[0]] if max_outstanding==1 else
            [application.max_write_credits==4,application.max_read_credits==4])
        self.sync.sys += [self.status.eq(Cat(application.busy, application.errors,
            endpoint.protocol_error, peer_error, endpoint.drained, endpoint.quiesced, *observed_window)),
            self.checksum.eq(application.checksum), self.completed.eq(application.completed)]


def generate(output_dir, *, max_outstanding=1):
    """Generate source only; caller records imports, snapshots and CAD inputs."""
    output = Path(output_dir).resolve(); output.mkdir(parents=True, exist_ok=False)
    platform = Platform(); prototype = WideEndpointPrototype(platform,max_outstanding=max_outstanding)
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
    result={'max_outstanding':max_outstanding,'peer_capacity_commands':1 if max_outstanding==1 else 4,
        'peer_memory_words':16 if max_outstanding==1 else 64,'peer_memory_data_width':256,
        'peer_byte_valid_bits':0 if max_outstanding==1 else 2048,'peer_W_ingress_beats':0 if max_outstanding==1 else 64,
        'peer_write_commit_stages':0 if max_outstanding==1 else 1,'peer_read_prefetch_stages':0 if max_outstanding==1 else 2,
        'peer_response_latency_cycles':0 if max_outstanding==1 else 12,'peer_read_latency_cycles':0 if max_outstanding==1 else 12,
        'clock_io':clock_io,'scope':('Fabric-only single-address registered peer' if max_outstanding==1 else 'Fabric-only four-address queued registered peer')+'; NOT HBM or a programming image',
        'part':platform.device,'top':'fk33_wide_endpoint','ports':names,
        'mmcm':config,'platform_sources':[str(row[0]) for row in platform.sources],
        'gray_groups':10,'gray_bits_per_group':3,'external_application_inputs':0,'registered_diagnostic_outputs':72,
        'launch_contract':'Internal SYS registered config, all LEN1..16, four strobes, varying seed; quiesced pause each sweep',
        'output_delay_ns':1.0,
        'clock_domain_contract':{'sys_MHz':250,'axi_MHz':500,'hbm_ref_MHz':200,'apb_MHz':100,
            'common_MMCM_lock_reset':True,'independent_hot_reset_supported':False,'reset_buf':'bufg'}}
    (output/'target-description.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result
