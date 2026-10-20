#!/usr/bin/env python3
"""Parse the repo's YAML/JSON config *and* every shell block inside the GitHub
workflows.

Actions pastes ``run:`` bodies into a bash heredoc, so a stray quote or a broken
``${{ }}`` substitution only shows up as a red job minutes later. This catches it
in one second, locally or in CI:

    python3 .scripts/check_workflows.py            # bash -n on each block
    python3 .scripts/check_workflows.py --shellcheck
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTEXT = re.compile(r"\$\{\{[^}]*\}\}")


def load_yaml(text: str) -> object:
    try:
        import yaml  # type: ignore
    except ImportError:
        print("  (pyyaml missing - skipping YAML checks)", file=sys.stderr)
        return None
    return yaml.safe_load(text)


def run_blocks(doc: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            script = step.get("run")
            if script:
                label = str(step.get("name") or step.get("uses") or "?")
                out.append((str(job_name), label, script))
    return out


def check_data_files() -> int:
    bad = 0
    for path in sorted(ROOT.rglob("*.json")):
        if any(part in {".git", "node_modules", "instance", ".venv"} for part in path.parts):
            continue
        try:
            json.loads(path.read_text())
            print(f"  ok   json  {path.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"  FAIL json  {path.relative_to(ROOT)}: {exc}", file=sys.stderr)

    yaml_paths = [ROOT / "rig.yaml", ROOT / "docker-compose.yml"]
    yaml_paths += sorted((ROOT / ".github").glob("*.yml"))
    yaml_paths += sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    for path in yaml_paths:
        if not path.is_file():
            print(f"  miss yaml  {path.relative_to(ROOT)}", file=sys.stderr)
            bad += 1
            continue
        text = path.read_text()
        doc = load_yaml(text)
        if doc is None:
            return 0
        if path.parent.name == "workflows":
            if not isinstance(doc, dict) or not doc.get("jobs"):
                bad += 1
                print(f"  FAIL yaml  {path.relative_to(ROOT)}: no jobs", file=sys.stderr)
                continue
            for job_name, label, script in run_blocks(doc):
                # GitHub substitutes expressions before bash sees them; emulate
                # an empty-ish and a quoted-ish value so both breakage modes show.
                for value in ("ci-value", "1"):
                    probe = CONTEXT.sub(value, script)
                    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                        handle.write(probe)
                        tmp = handle.name
                    result = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True)
                    Path(tmp).unlink(missing_ok=True)
                    if result.returncode != 0:
                        bad += 1
                        print(
                            f"  FAIL run   {path.relative_to(ROOT)} · {job_name} · {label}\n"
                            f"{result.stderr.strip()}",
                            file=sys.stderr,
                        )
            print(f"  ok   yaml  {path.relative_to(ROOT)} (+ its run blocks)")
        else:
            print(f"  ok   yaml  {path.relative_to(ROOT)}")
    return bad


def check_shellcheck() -> int:
    exe = shutil.which("shellcheck")
    if not exe:
        print("  (shellcheck not installed - skipped)")
        return 0
    scripts = [ROOT / "autosetup.sh", ROOT / "docker" / "entrypoint.sh"]
    scripts += sorted((ROOT / ".scripts").glob("*.sh"))
    bad = 0
    for script in scripts:
        if not script.is_file():
            continue
        result = subprocess.run(
            [exe, "-S", "warning", "-e", "SC1091,SC2206", str(script)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            bad += 1
            print(f"  FAIL shellcheck {script.relative_to(ROOT)}\n{result.stdout}", file=sys.stderr)
        else:
            print(f"  ok   shellcheck {script.relative_to(ROOT)}")
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shellcheck", action="store_true", help="also lint the shell scripts")
    args = parser.parse_args(argv)
    bad = check_data_files()
    if args.shellcheck:
        bad += check_shellcheck()
    if bad:
        print(f"\n{bad} problem(s) found", file=sys.stderr)
        return 1
    print("\nconfig and workflow shell blocks all parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
