"""Run mutmut over the core's control path and gate on the mutation score.

mutmut forks one worker per mutant, so this runs on POSIX only: Linux CI, WSL, or
the ``mutation`` service of ``docker/docker-compose.test.yml`` on Windows. The
mutated modules and the test selection live in ``[tool.mutmut]`` in
``pyproject.toml``; results persist in the git-ignored ``mutants/`` directory, so
a re-run only re-tests what changed.

Score = (killed + caught by the type checker + timeout) / all mutants. A mutant
``mypy --strict`` rejects cannot reach master (mypy is a CI gate), and a mutant
that hangs the suite fails CI just as a failing test does. Survivors, mutants
without a covering test and "suspicious" ones count against the score.

Every mutant re-runs the tests that reach it, so the property-based tier runs
with fewer examples here (``TORTOISE_HYPOTHESIS_EXAMPLES``, default 10 under
this script, 60 in a plain pytest run).

Usage::

    python scripts/mutation.py [--fail-under 96] [--max-children N]
                               [--survivors-dir mutation-survivors]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DETECTED = frozenset({"killed", "caught by type check", "timeout"})
NOT_DETECTED = frozenset({"survived", "no tests", "suspicious"})
_RESULT_LINE = re.compile(r"^\s*(?P<name>\S+__mutmut_\d+): (?P<status>.+?)\s*$")


def _mutmut(*args: str, check: bool = True) -> str:
    """Run a mutmut sub-command and return its stdout."""
    proc = subprocess.run(
        ["mutmut", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        msg = f"mutmut {' '.join(args)} exited with {proc.returncode}"
        raise SystemExit(msg)
    return proc.stdout


def _module_of(mutant: str) -> str:
    """``custom_components.tortoise_ufh.core.pid.x_f__mutmut_3`` -> ``pid``."""
    return mutant.split(".")[3]


def _results() -> dict[str, str]:
    """Map every mutant name to its mutmut status."""
    out = _mutmut("results", "--all", "true")
    results: dict[str, str] = {}
    for line in out.splitlines():
        match = _RESULT_LINE.match(line)
        if match:
            results[match["name"]] = match["status"]
    return results


def _report(results: dict[str, str]) -> float:
    """Print the per-module table and return the overall score [%]."""
    by_module: dict[str, Counter[str]] = defaultdict(Counter)
    for name, status in results.items():
        by_module[_module_of(name)][status] += 1
    header = f"{'module':<16}{'total':>7}{'detected':>10}{'survived':>10}"
    header += f"{'no tests':>10}{'score':>8}"
    print(header)
    total = detected = 0
    for module in sorted(by_module):
        counts = by_module[module]
        n = sum(counts.values())
        d = sum(counts[s] for s in DETECTED)
        total += n
        detected += d
        print(
            f"{module:<16}{n:>7}{d:>10}{counts['survived']:>10}"
            f"{counts['no tests']:>10}{100.0 * d / n:>7.1f}%"
        )
    score = 100.0 * detected / total if total else 0.0
    print(f"{'TOTAL':<16}{total:>7}{detected:>10}{'':>20}{score:>7.1f}%")
    return score


def _dump_survivors(results: dict[str, str], out_dir: Path) -> None:
    """Write one markdown file per module with the diff of every undetected mutant."""
    undetected = sorted(n for n, s in results.items() if s in NOT_DETECTED)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        diffs = list(pool.map(lambda n: _mutmut("show", n, check=False), undetected))
    by_module: dict[str, list[str]] = defaultdict(list)
    for name, diff in zip(undetected, diffs, strict=True):
        body = "\n".join(
            line
            for line in diff.splitlines()
            if line[:1] in "+-@" and not line.startswith(("+++", "---"))
        )
        by_module[_module_of(name)].append(
            f"## {name} ({results[name]})\n\n```diff\n{body}\n```\n"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.md"):
        stale.unlink()
    for module, sections in by_module.items():
        (out_dir / f"{module}.md").write_text(
            f"# Undetected mutants: {module} ({len(sections)})\n\n"
            + "\n".join(sections),
            encoding="utf-8",
        )
    print(f"survivor diffs: {out_dir} ({len(undetected)} mutants)")


def main() -> int:
    """Run the mutation suite, print the report, return the gate verdict."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fail-under", type=float, default=0.0)
    parser.add_argument("--max-children", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--survivors-dir", type=Path)
    args = parser.parse_args()

    os.environ.setdefault("TORTOISE_HYPOTHESIS_EXAMPLES", "10")
    run = subprocess.run(["mutmut", "run", "--max-children", str(args.max_children)])
    if run.returncode != 0:
        print(f"FAIL: mutmut run exited with {run.returncode}")
        return run.returncode
    _mutmut("export-cicd-stats")
    stats = json.loads(Path("mutants/mutmut-cicd-stats.json").read_text("utf-8"))
    print(json.dumps(stats, indent=2))

    results = _results()
    score = _report(results)
    if args.survivors_dir is not None:
        _dump_survivors(results, args.survivors_dir)
    if score < args.fail_under:
        print(f"FAIL: mutation score {score:.1f}% < {args.fail_under:.1f}%")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
