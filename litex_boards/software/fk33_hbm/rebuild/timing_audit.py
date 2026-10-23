"""Read-only FK33 generated constraints/native report audit; no Vivado invocation.

Exit 0 PASS, 1 FAIL, 2 INCOMPLETE. Source stage runs before CAD; final stage
also requires completed routed STA. Audit output is a new evidence artifact.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

SCHEMA = "fk33-timing-constraint-audit-v1"
TIMING_COMMAND = re.compile(r"^(?:create_(?:generated_)?clock|set_(?:false_path|max_delay|min_delay|bus_skew|multicycle_path|clock_groups|input_delay|output_delay|disable_timing|clock_uncertainty|clock_latency))\b")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def norm(line):
    return " ".join(line.split())


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def audit(attempt, contract_path=None, stage="final"):
    a = Path(attempt).resolve()
    contract_path = Path(contract_path or Path(__file__).with_name("timing-contract.json")).resolve()
    c = load(contract_path)
    need(c["schema"] == "fk33-timing-contract-v1", "Unknown timing contract")
    g = a / "output/gateware"
    checks, artifacts = {}, {}

    def read(path):
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        artifacts[str(path.resolve())] = dict(path=str(path.resolve()), sha256=sha(path), size=path.stat().st_size)
        return text

    def check(name, function):
        try:
            checks[name] = dict(status="PASS", detail=function())
        except FileNotFoundError as error:
            checks[name] = dict(status="INCOMPLETE", error=str(error))
        except Exception as error:
            checks[name] = dict(status="FAIL", error=str(error))

    def constraints():
        xdc, tcl = read(g / "sqrl_fk33.xdc"), read(g / "sqrl_fk33.tcl")
        commands = [norm(line) for line in xdc.splitlines() if TIMING_COMMAND.match(line.strip())]
        need(Counter(commands) == Counter(c["accepted_xdc_timing_commands"]),
             "Generated XDC timing commands differ from contract: " + str(commands))
        tc = [norm(line) for line in tcl.splitlines() if TIMING_COMMAND.match(line.strip())]
        jtag = [line for line in tc if line.startswith("create_clock -name jtag_tck ")]
        need(len(jtag) == 1, "Expected exactly one JTAG clock declaration")
        period = re.search(r"-period\s+([\d.]+)", jtag[0])
        need(period and 0 < float(period[1]) <= c["clocks"]["jtag_tck"]["maximum_period_ns"],
             "JTAG clock constraint does not cover15MHz cable envelope")
        need(jtag[0].endswith("[get_pins BSCANE2/INTERNAL_TCK]"), "JTAG source must be BSCAN TCK, not DRCK")
        accepted = [jtag[0], c["accepted_tcl_async_command"]]
        outside_gray = tcl.replace("\r\n", "\n")
        if c.get("separate_axi_domain"):
            axi_period = c["clocks"]["main_clkout3"]["period_ns"]
            need(axi_period in (2, 4), "Unsupported separate AXI clock period")
            gray_name = "gray-constraints-axi500.tcl" if axi_period == 2 else "gray-constraints.tcl"
            need(c.get("gray_constraints_file", "gray-constraints.tcl") == gray_name,
                 "Gray constraint profile differs from AXI clock")
            gray = read(Path(__file__).with_name(gray_name)).replace("\r\n", "\n").strip()
            normalized_tcl = tcl.replace("\r\n", "\n")
            need(normalized_tcl.count(gray) == 1, "Generated Gray constraints differ from source or are duplicated")
            root_policy = {"region": "X4Y1",
                 "buffers": {"SYS": {"pin": "BUFG/O", "clock": "main_clkout0"},
                             "AXI": {"pin": "BUFG_3/O", "clock": "main_clkout3"}},
                 "native_actual_root_required": True}
            root_policy.update({"periods_ns": {"SYS": 4, "AXI": 2}} if axi_period == 2 else {"period_ns": 4})
            need(c.get("clock_root_policy") == root_policy, "Clock-root policy differs from contract")
            need(c["gray_pointer_policy"]["maximum_datapath_ns"] == min(4, axi_period) and
                 c["gray_pointer_policy"]["maximum_bus_skew_ns"] == min(4, axi_period),
                 "Gray bounds must cover the faster functional clock")
            outside_gray = normalized_tcl.replace(gray, "", 1)
            need(normalized_tcl.index(jtag[0]) < normalized_tcl.index(gray) <
                 normalized_tcl.index("place_design -directive"), "Gray constraints must follow clock creation and precede placement")
            accepted += [norm(line) for line in gray.splitlines() if TIMING_COMMAND.match(line.strip())]
            need("report_bus_skew -file sqrl_fk33_bus_skew.rpt" in tcl, "Missing routed Gray bus-skew report")
        need(not re.search(r"\bUSER_CLOCK_ROOT\b", outside_gray + "\n" + xdc, re.I),
             "USER_CLOCK_ROOT occurs outside the canonical opt-in constraints")
        need(Counter(tc) == Counter(accepted), "Unexpected Tcl timing exception/clock command")
        return dict(xdc_timing_commands=commands, tcl_timing_commands=tc,
                    legacy_sys_parent_group_present=False, broadening=False)

    def generated_binding():
        rows = json.loads(read(a / "generated-hashes.json"))
        result = []
        for name in ("sqrl_fk33.xdc", "sqrl_fk33.v", "sqrl_fk33.tcl"):
            p = g / name
            text = read(p)
            row = next(r for r in rows if Path(r["path"]).name == name)
            digest = sha(p)
            if digest != row["sha256"]:
                # Existing launchers prepend a recorded resource/diagnostic hook.
                need(name.endswith(".tcl"), "Captured generated input changed: " + name)
                override = a / "tool-resource-override.json"
                if override.exists():
                    record = json.loads(read(override))
                else:
                    record = json.loads(read(a / "implementation-result.json"))["resource_override"]
                need(record.get("original_generated_tcl_sha256") == row["sha256"] and
                     record.get("executed_tcl_sha256") == digest, "Unrecorded generated Tcl modification")
            result.append(dict(path=str(p), sha256=digest))
        return result

    def topology():
        v = read(g / "sqrl_fk33.v")
        csr = json.loads(read(a / "output/csr.json"))
        need(csr["constants"]["config_clock_frequency"] == 250000000, "SYS frequency changed")
        expected = c.get("mmcm_parameters", {
            "CLKFBOUT_MULT_F":5, "DIVCLK_DIVIDE":1, "CLKOUT0_DIVIDE_F":4,
            "CLKOUT1_DIVIDE":5, "CLKOUT2_DIVIDE":10})
        values = {}
        for key, wanted in expected.items():
            hits = re.findall(r"\." + key + r"\s*\(\s*\d+'d(\d+)\s*\)", v)
            need(hits == [str(wanted)], "MMCM parameter mismatch: " + key + "=" + str(hits))
            values[key] = wanted
        for index in range(c.get("mmcm_output_count", 3)):
            need(re.search(r"\.CLKOUT"+str(index)+r"\s*\(main_clkout"+str(index)+r"\)",v), "Clock output mapping changed")
        axi_mhz = 1000 / c["clocks"]["main_clkout3"]["period_ns"] if c.get("separate_axi_domain") else 250
        if c.get("separate_axi_domain"):
            need(csr["constants"].get("hbm_axi_clk_freq") == int(axi_mhz * 1e6),
                 "AXI frequency metadata differs from timing contract")
            need(csr["constants"].get("hbm_experimental_overclock") == int(axi_mhz == 500),
                 "Experimental AXI500 overclock must be identified in metadata")
        return dict(mmcm=values, sys_mhz=250, axi_mhz=axi_mhz,
                    separate_axi_domain=c.get("separate_axi_domain", False),
                    hbm_ref_mhz=200, apb_mhz=100,
                    hbm_internal_pll_profiles_are_not_fabric_clocks=True)

    def completed():
        result = json.loads(read(a / "implementation-result.json"))
        need(result["returncode"] == 0 and result["sources_unchanged"] and
             result["status"] == "IMPLEMENTED", "Native implementation incomplete/failed/changed")
        return result

    def timing_coverage():
        text = read(g / "sqrl_fk33_check_timing.rpt")
        need("Physopt postRoute" in text, "check_timing is not final post-route")
        counts = {name:int(value) for name,value in re.findall(r"checking\s+(\w+)\s+\((\d+)\)", text)}
        for name in c["check_timing_zero"]:
            need(counts.get(name) == 0, "Timing coverage gap: " + name + "=" + str(counts.get(name)))
        need(counts.get("no_output_delay") == 7, "Unexpected output timing boundary count")
        missing = re.search(r"There are 7 ports with no output delay specified.*?\n(.*?)\n\s*There are",text,re.S)
        need(missing, "Missing output-delay detail inventory")
        ports = re.findall(r"^\s*(user_led\d+)\s*$",missing[1],re.M)
        need(sorted(ports) == sorted(c["allowed_missing_output_delays"]), "Missing output delay on non-LED port")
        return dict(counts=counts, untimed_output_ports=ports)

    def native_clocks():
        text = read(g / "sqrl_fk33_timing.rpt")
        section = text.split("| Clock Summary",1)[1].split("| Intra Clock Table",1)[0]
        rows = re.findall(r"^\s*(\w+)\s+\{[^}]+\}\s+([\d.]+)\s+([\d.]+)",section,re.M)
        periods = {name:float(period) for name,period,_ in rows}
        need(set(periods) == set(c["clocks"]), "Unexpected/missing native clock; do not invent HBM900 fabric clock")
        for name, item in c["clocks"].items():
            if "period_ns" in item:
                need(abs(periods[name]-item["period_ns"]) < .0001, "Native period mismatch: " + name)
            else:
                need(0 < periods[name] <= item["maximum_period_ns"]+.001, "Native JTAG period exceeds cable envelope")
        return periods

    def interaction():
        text = read(g / "sqrl_fk33_clock_interaction.rpt")
        rows = {}
        for line in text.splitlines():
            m = re.match(r"^(\w+)\s+(\w+)\s+",line)
            if m and m[1] in c["clocks"] and m[2] in c["clocks"]:
                rows[(m[1],m[2])] = line
        related = set(c["clock_relationships"]["related"])
        for (source,dest),line in rows.items():
            if source in related and dest in related:
                need("Asynchronous" not in line and "Ignored" not in line, "Related clock pair cut: "+line)
        timed_pairs = c.get("required_timed_clock_pairs", [
            ["main_clkout0", "main_clkout2"], ["main_clkout2", "main_clkout0"]])
        for source, destination in timed_pairs:
            pair = (source, destination)
            need(pair in rows and ("Timed" in rows[pair] or "Partial False Path" in rows[pair]),
                 "Related functional timed paths missing: " + str(pair))
        for pair in (("jtag_tck","main_clkout0"),("main_clkout0","jtag_tck")):
            need(pair in rows and "Asynchronous Groups" in rows[pair], "JTAG crossing not inventoried")
        return list(rows.values())

    def exceptions():
        text = read(g / "sqrl_fk33_timing_exceptions.rpt")
        positions = re.findall(r"^\s*(\d+)\s+",text,re.M)
        # After removing legacy group, XDC exceptions are positions2/3/4 and
        # the pre-placement JTAG clock+group are5/6, giving two group rows6.
        if c.get("separate_axi_domain"):
            # Native positions are assigned as each loop iteration creates an
            # exception; do not identify their scope by a guessed position.
            rows = [line for line in text.splitlines() if re.match(r"^\s*\d+\s+", line)]
            bound = c["gray_pointer_policy"]["maximum_datapath_ns"]
            gray_rows = [line for line in rows if re.search(r"\bmax_dpo=" +
                format(bound, "g") + r"(?:\.0+)?(?:\s|$)", line)]
            need(len(gray_rows) == 320, "Expected 320 native Gray max-delay exceptions")
            need(len(rows) == 325, "Unexpected native exception count: " + str(len(rows)))
            # datapath_only intentionally removes hold analysis on these
            # first-stage CDC paths; the max bound itself must remain active.
            need(all("overridden" not in line.lower() for line in gray_rows),
                 "Gray max-delay exception overridden")
        else:
            need(positions == ["2","3","4","6","6"], "Unexpected native exception positions/inventory: "+str(positions))
        for marker in ("mr_ff == TRUE", "ars_ff1 == TRUE", "ars_ff2 == TRUE", "max=2", "jtag_tck"):
            need(marker in text, "Native exception missing expected object: "+marker)
        need(not re.search(r"\[get_clocks \{ main_clkout0 \}\].*\[get_clocks \{ clk200_p \}\]",text), "Legacy related-clock cut remains")
        return dict(positions=positions, reviewed_scopes=["mr_ff","ars_ff_PRE","ars_interstage2ns","jtag_async"])

    def sta():
        text = read(g / "sqrl_fk33_timing.rpt")
        need("Physopt postRoute" in text, "Timing summary is not final post-route")
        section = text.split("| Design Timing Summary",1)[1]
        row = re.search(r"^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s*$",section,re.M)
        need(row, "No parseable complete timing summary")
        values = list(map(float,row.groups()))
        metrics = dict(zip(("wns","tns","setup_fail","setup_total","whs","ths","hold_fail","hold_total","wpws","tpws","pulse_fail","pulse_total"),values))
        need(all(metrics[k] >= 0 for k in ("wns","whs","wpws")) and
             all(metrics[k] == 0 for k in ("tns","setup_fail","ths","hold_fail","tpws","pulse_fail")), "Final STA failed: "+str(metrics))
        return metrics

    def reset_and_unconstrained():
        text = read(g / "sqrl_fk33_timing_unconstrained_path.rpt")
        need("Physopt postRoute" in text, "Detailed timing is not final post-route")
        need("Path Type:              Recovery" in text and "Path Type:              Removal" in text, "Recovery/removal native coverage absent")
        async_rows = re.findall(r"^\*\*async_default\*\*\s+.*$",text,re.M)
        need(async_rows, "Async reset timing group absent")
        for line in async_rows:
            fields=line.split()[3:]
            need(len(fields)>=8 and float(fields[0])>=0 and float(fields[1])==0 and int(fields[2])==0 and float(fields[4])>=0 and float(fields[5])==0 and int(fields[6])==0, "Reset recovery/removal summary fails: "+line)
        table = text.split("| Unconstrained Path Table",1)[1].split("| Timing Details",1)[0]
        pairs=[norm(line) for line in table.splitlines() if line.strip().startswith("(none)")]
        need(pairs == ["(none) main_clkout0", "(none) jtag_tck"], "New unconstrained path group: "+str(pairs))
        # The detailed report also includes intentionally ignored/false-path
        # groups. Isolate only the two genuinely unconstrained boundary pairs.
        blocks = re.split(r"(?m)^Path Group:  \(none\)", text)[1:]
        boundaries = []
        for block in blocks:
            pair = re.search(r"From Clock:([^\n]*)\n  To Clock:([^\n]*)", block)
            if pair and tuple(x.strip() for x in pair.groups()) in (("main_clkout0", ""), ("", "jtag_tck")):
                boundaries.append(block)
        need(len(boundaries) == 2, "Unconstrained boundary detail blocks missing")
        tail = "\n".join(boundaries)
        sources=re.findall(r"^\s*Source:\s+(\S+)",tail,re.M)
        dests=re.findall(r"^\s*Destination:\s+(\S+)",tail,re.M)
        need(sources and all(x=="BSCANE2/INTERNAL_TDI" or "led" in x for x in sources), "Unexpected untimed source: "+str(sources))
        need(dests and all(re.fullmatch(r"user_led[0-6]",x) or "jtag" in x or x.startswith("storage_") for x in dests), "Unexpected untimed endpoint: "+str(dests))
        return dict(async_rows=async_rows, unconstrained_groups=pairs, sampled_sources=sources,
                    sampled_destinations=dests, limitation="BSCAN/TDI external timing boundary remains unqualified; not all-path constrained claim")

    check("generated_bindings", generated_binding)
    check("constraint_commands", constraints)
    check("mmcm_topology", topology)
    if stage == "final":
        for name, function in (("native_completed",completed),("timing_coverage",timing_coverage),
            ("native_clocks",native_clocks),("clock_interaction",interaction),
            ("native_exceptions",exceptions),("routed_sta",sta),("reset_unconstrained_inventory",reset_and_unconstrained)):
            check(name,function)
    status = "FAIL" if any(r["status"]=="FAIL" for r in checks.values()) else "INCOMPLETE" if any(r["status"]=="INCOMPLETE" for r in checks.values()) else "PASS"
    return dict(schema=SCHEMA,status=status,stage=stage,attempt=str(a),created_utc=datetime.now(timezone.utc).isoformat(),
                contract_path=str(contract_path),contract_sha256=sha(contract_path),auditor_sha256=sha(Path(__file__)),
                checks=checks,artifacts=list(artifacts.values()),limitations=[
                "No CAD/DCP/hardware operation. Source stage does not claim native timing closure.",
                "Known LED and BSCAN/TDI external timing boundaries remain explicitly untimed; report is not universal timing/CDC signoff.",
                "Common-MMCM functional crossings retain real timing; no blanket asynchronous groups for SYS/APB/AXI.",
                "RapidWright physical analysis cannot guarantee closure; native route/STA and independent hardware gates remain mandatory."])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt",required=True,type=Path)
    parser.add_argument("--contract",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--stage",choices=("source","final"),default="final")
    args=parser.parse_args()
    result=audit(args.attempt,args.contract,args.stage)
    output=args.output or args.attempt / ("timing-constraint-audit"+("-source" if args.stage=="source" else "")+".json")
    output.parent.mkdir(exist_ok=True,parents=True)
    output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(dict(status=result["status"],stage=args.stage,attempt=result["attempt"],output=str(output),checks={k:v["status"] for k,v in result["checks"].items()})))
    return {"PASS":0,"FAIL":1,"INCOMPLETE":2}[result["status"]]


if __name__=="__main__":
    raise SystemExit(main())
