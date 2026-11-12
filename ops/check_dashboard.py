"""Every metric the dashboard and the alerts ask for must be one the code defines.

A renamed metric would otherwise leave a panel quietly empty, or an alert
that can never fire. Run from the repository root: python3 ops/check_dashboard.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "apps/api/src/lobbyd/platform/metrics.py").read_text()
defined = set(re.findall(r'"(lobbyd_[a-z_]+)"', SOURCE))
# Counters gain _total only if not already named so; histograms gain these.
known = defined | {f"{name}{suffix}" for name in defined for suffix in ("_bucket", "_count", "_sum")}

dashboard = json.loads((ROOT / "ops/grafana/dashboards/lobbyd.json").read_text())
queries = [
    target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
]
queries.append((ROOT / "ops/prometheus/alerts.yml").read_text())

used = set(re.findall(r"\blobbyd_[a-z_]+", "\n".join(queries)))
unknown = sorted(used - known)
if unknown:
    sys.exit(f"queried but not defined in metrics.py: {', '.join(unknown)}")
print(f"{len(used)} metrics queried, all defined")
