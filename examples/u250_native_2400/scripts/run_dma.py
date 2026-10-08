"""Plan or run full-capacity DMA using already initialized/trained gateware."""
from bootstrap import ROOT
import argparse
import json
import time
from dma_demo import CAPACITY

FREQUENCY = 150_000_000
THEORETICAL = 19_200_000_000


def plan(chunk_bytes=256*1024*1024):
    assert chunk_bytes > 0 and chunk_bytes % 64 == 0
    return [dict(base=base, length=min(chunk_bytes,CAPACITY-base))
        for base in range(0,CAPACITY,chunk_bytes)]


def run(wb, mode, write_base, read_base, length, pattern):
    regs = wb.regs
    for name, value in dict(write_base=write_base,read_base=read_base,length=length,
        mode=mode,pattern=pattern,rmw=0,admit=1,timeout=FREQUENCY*60).items():
        getattr(regs,'dma_'+name).write(value)
    regs.dma_start.write(1)
    deadline = time.monotonic()+70
    while not regs.dma_done.read():
        if time.monotonic() > deadline:
            raise TimeoutError('DMA did not finish; preserve outstanding state and reconfigure to recover')
        time.sleep(0.02)
    result = {name:getattr(regs,'dma_'+name).read() for name in
        ('fault','errors','write_cycles','read_cycles','total_cycles','write_beats','read_beats','first_error_address')}
    result.update(mode=mode,write_base=write_base,read_base=read_base,length=length,
        pattern=pattern,clock_hz=FREQUENCY)
    for kind in ('read','write'):
        result[kind+'_bytes'] = result[kind+'_beats']*64
        cycles = result[kind+'_cycles']
        result[kind+'_bytes_per_second'] = result[kind+'_bytes']*FREQUENCY/cycles if cycles else 0
        result[kind+'_efficiency'] = result[kind+'_bytes_per_second']/THEORETICAL
    cycles = result['total_cycles']
    result['combined_bytes_per_second'] = (result['read_bytes']+result['write_bytes'])*FREQUENCY/cycles if cycles else 0
    result['combined_efficiency'] = result['combined_bytes_per_second']/THEORETICAL
    if result['fault'] or result['errors']:
        raise RuntimeError(json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--port',type=int,default=1234)
    parser.add_argument('--output',type=str,default=str(ROOT/'evidence/hardware_dma_results.json'))
    args = parser.parse_args()
    ranges = plan()
    if not args.execute:
        print(json.dumps(dict(capacity=CAPACITY,pattern=0,operations=ranges),indent=2))
        return
    from litex import RemoteClient
    wb = RemoteClient(port=args.port,csr_csv=str(ROOT/'build/csr.csv'))
    results = []
    wb.open()
    try:
        # Admission in RTL requires DFII ownership, all-ready, successful
        # training state, VTC and software admission. This script cannot train.
        for pattern in range(4):
            for item in ranges:
                results.append(run(wb,2,item['base'],item['base'],item['length'],pattern))
        # Disjoint halves: verify the existing counter data while writing it.
        half = CAPACITY//2
        for item in plan(half)[:1]:
            results.append(run(wb,2,half,half,half,0))
        results.append(run(wb,3,0,half,half,0))
    finally:
        wb.close()
        with open(args.output,'w') as output:
            json.dump(results,output,indent=2)


if __name__ == '__main__':
    main()
