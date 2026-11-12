"""Turn evals/results/*.json into the scorecard table for docs/assistant.md.

uv run python evals/report.py
"""

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def main() -> None:
    runs = [json.loads(path.read_text()) for path in sorted(RESULTS.glob("*.json"))]
    if not runs:
        print("No results yet. Run the evals first (see tests/evals/test_scenarios.py).")
        return
    names = [scenario["name"] for scenario in runs[0]["scenarios"]]
    about = {scenario["name"]: scenario["about"] for scenario in runs[0]["scenarios"]}
    header = "| Scenario | What it tests | " + " | ".join(run["model"] for run in runs) + " |"
    print(header)
    print("| --- | --- | " + " | ".join("---" for _ in runs) + " |")
    for name in names:
        cells = []
        for run in runs:
            scenario = next((s for s in run["scenarios"] if s["name"] == name), None)
            if scenario is None:
                cells.append("not run")
                continue
            failed = [label for label, ok in scenario["checks"].items() if not ok]
            cells.append(
                "pass" if scenario["passed"] else "FAIL: " + "; ".join(failed or ["error"])
            )
        print(f"| `{name}` | {about[name]} | " + " | ".join(cells) + " |")
    totals = []
    for run in runs:
        passed = sum(1 for s in run["scenarios"] if s["passed"])
        seconds = sum(s["seconds"] for s in run["scenarios"]) / max(len(run["scenarios"]), 1)
        totals.append(f"**{passed} / {len(run['scenarios'])}**, {seconds:.1f} s per scenario")
    print("| | **Total** | " + " | ".join(totals) + " |")


if __name__ == "__main__":
    main()
