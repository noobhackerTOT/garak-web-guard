"""Isolated native garak CLI run with a hard per-probe request cap."""
import bootstrap
bootstrap.configure_runtime()
import json
import sys
from pathlib import Path


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from garak import cli, _config
    from garak.probes.base import Probe
    original = Probe._execute_all
    cap = config["max_per_probe"]

    def bounded_execute(probe, attempts):
        attempts = list(attempts)
        selected = attempts[:cap]
        print("WG_EVENT " + json.dumps({"type": "probe_start", "probe": probe.probename.replace("garak.probes.", ""),
                                       "planned": len(selected), "available": len(attempts)}), flush=True)
        return original(probe, selected)

    Probe._execute_all = bounded_execute
    prefix = str(Path(config["report_prefix"]).resolve())
    cli.main([
        "--target_type", "function.Single", "--target_name", "bridge#generate",
        "--spec", ",".join("probes." + p for p in config["probes"]),
        "--generations", "1", "--parallel_attempts", "1", "--parallel_requests", "1",
        "--seed", "42", "--report_prefix", prefix,
        "--config", config["garak_config"],
    ])
    # garak CLI catches some errors. Exit zero only with a completed native report.
    report = Path(prefix + ".report.jsonl")
    complete = False
    if report.exists():
        for line in report.read_text(encoding="utf-8").splitlines():
            try:
                complete = complete or json.loads(line).get("entry_type") == "completion"
            except json.JSONDecodeError:
                pass
    if not complete:
        raise RuntimeError("garak 未完成检测；查看运行日志和连接错误")
    # Native completion can still contain skipped probes. The console checks coverage.
    print("WG_EVENT " + json.dumps({"type": "completed", "version": _config.version}), flush=True)


if __name__ == "__main__":
    main()
