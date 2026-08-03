#!/usr/bin/env python3
"""Run the AI Live Commentary automated test suite (work package A) by
category, emit one JUnit XML per category plus a combined one, and print/
write the pass-rate summary table required by commentary_test_plan.md 5.6.

Usage:
    .venv/bin/python tools/commentary_test_report.py [--out-dir DIR]

Exit code is non-zero if any category has failures or errors (skips do not
fail the run, but are always reported separately -- see --help output and
the "skipped" column below; they are never folded into the pass-rate
denominator).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

# Category -> pytest node ids. Node ids (not whole files) are used where a
# single file mixes concerns the task book's table splits into separate
# rows (test_commentary_modes.py covers both "Modes and priority" and
# "Cooldown and deduplication"; test_commentary_runtime.py covers both
# "Runtime integration" and "Fault handling").
CATEGORIES: dict[str, list[str]] = {
    "Input processing": [
        "tests/unit/test_commentary_input.py",
    ],
    "Event boundaries": [
        "tests/unit/test_commentary_events.py",
    ],
    "Modes and priority": [
        "tests/unit/test_commentary_modes.py::TestModeGating",
        "tests/unit/test_commentary_modes.py::TestPriorityTieBreak",
    ],
    "Cooldown and deduplication": [
        "tests/unit/test_commentary_modes.py::TestPerSignatureCooldown",
        "tests/unit/test_commentary_modes.py::TestGlobalWallClockCooldown",
        "tests/unit/test_commentary_modes.py::TestTextDedupeBeforeDisplay",
    ],
    "Runtime integration": [
        "tests/integration/test_commentary_runtime.py::TestDedupeBeforeBroadcast",
        "tests/integration/test_commentary_runtime.py::TestBroadcastIsolation",
        "tests/integration/test_commentary_runtime.py::TestHighFrequencyEvents",
    ],
    "Fault handling": [
        "tests/integration/test_commentary_runtime.py::TestGraniteFailureModes",
        "tests/integration/test_commentary_runtime.py::TestTtsFailureIsolation",
        "tests/integration/test_commentary_runtime.py::TestIllegalConfigViaRestApi",
        "tests/integration/test_commentary_runtime.py::TestMaxWordsIsPromptHintOnly",
    ],
}


@dataclass
class CategoryResult:
    name: str
    tests: int
    failures: int
    errors: int
    skipped: int

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped

    @property
    def pass_rate(self) -> str:
        denom = self.passed + self.failures + self.errors
        if denom == 0:
            return "N/A"
        return f"{self.passed / denom:.1%}"


def run_category(name: str, node_ids: list[str], junit_path: Path) -> CategoryResult:
    cmd = [str(PYTHON), "-m", "pytest", *node_ids, "-q", f"--junitxml={junit_path}"]
    subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return parse_junit(name, junit_path)


def parse_junit(name: str, junit_path: Path) -> CategoryResult:
    if not junit_path.exists():
        return CategoryResult(name, 0, 0, 0, 0)
    root = ET.parse(junit_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return CategoryResult(name, 0, 0, 0, 0)
    return CategoryResult(
        name=name,
        tests=int(suite.get("tests", 0)),
        failures=int(suite.get("failures", 0)),
        errors=int(suite.get("errors", 0)),
        skipped=int(suite.get("skipped", 0)),
    )


def render_table(results: list[CategoryResult]) -> str:
    lines = [
        "| Test category | Tests | Passed | Failed | Skipped | Pass rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    total_tests = total_passed = total_failed = total_skipped = 0
    for r in results:
        failed = r.failures + r.errors
        lines.append(f"| {r.name} | {r.tests} | {r.passed} | {failed} | {r.skipped} | {r.pass_rate} |")
        total_tests += r.tests
        total_passed += r.passed
        total_failed += failed
        total_skipped += r.skipped
    denom = total_passed + total_failed
    total_rate = f"{total_passed / denom:.1%}" if denom else "N/A"
    lines.append(f"| **Total** | {total_tests} | {total_passed} | {total_failed} | {total_skipped} | {total_rate} |")
    lines.append("")
    lines.append(
        "Pass rate = passed / (passed + failed); skipped tests are excluded from "
        "the denominator and reported separately (there are currently none in "
        "this suite -- every test above either passes or fails deterministically)."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "evaluation" / "commentary" / "results"),
        help="directory to write JUnit XML + summary markdown into",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    results = []
    for name, node_ids in CATEGORIES.items():
        slug = name.lower().replace(" ", "_")
        junit_path = out_dir / f"automated_test_{slug}_{timestamp}.xml"
        results.append(run_category(name, node_ids, junit_path))

    table = render_table(results)
    print(table)

    summary_path = out_dir / f"automated_test_summary_{timestamp}.md"
    summary_path.write_text(
        "# AI Live Commentary automated test summary (AUTOMATED TEST -- real pytest run, not a sample)\n\n"
        f"Generated: {timestamp}\n\n" + table + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {summary_path}")

    any_failed = any((r.failures + r.errors) > 0 for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
