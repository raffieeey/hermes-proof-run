"""Run the registered-command example using an installed Hermes runtime."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-source", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("workflow-check.json"))
    parser.add_argument("--hermes", default="hermes", help="Hermes executable for the supported host build")
    args = parser.parse_args()
    executable = shutil.which(args.hermes)
    if executable is None:
        parser.error("Hermes is not installed or is not on PATH")
    script = Path(__file__).resolve().parent / "scripts" / "run_actual_host_journey.py"
    argv = [str(script), "--host-source", str(args.host_source.resolve()), "--out", str(args.out.resolve())]
    # Resolve the provisioned dependency paths through a stdlib-only module.
    # A fresh process avoids mixing cached modules from another Hermes checkout.
    marker = "PROOF_RUN_RUNTIME="
    probe_code = (
        "import json, sys; print("
        + repr(marker)
        + "+json.dumps({'python':sys.executable,'path':sys.path}))"
    )
    probe = subprocess.run(
        [executable, "--run-module", "timeit", "-n", "1", "-r", "1", "-s", probe_code, "pass"],
        capture_output=True, text=True, check=True,
    )
    record = next((line[len(marker):] for line in probe.stdout.splitlines() if line.startswith(marker)), None)
    if record is None:
        parser.error("Could not resolve Hermes' Python runtime")
    runtime = json.loads(record)
    setup = (
        "import os, runpy, sys; "
        f"sys.path[:] = {runtime['path']!r}; "
        f"sys.path.insert(0, {str(args.host_source.resolve())!r}); "
        "os.environ['HERMES_PLUGIN_HOST_PROCESS']='1'; "
        f"sys.argv = {argv!r}; "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )
    return subprocess.run(
        [runtime["python"], "-I", "-c", setup],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
