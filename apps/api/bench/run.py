"""Run one load-test scenario end to end: `python bench/run.py sale`.

Prepares the data, starts the API, a worker and the payment simulator as
real processes, drives them with Locust, then checks every invariant and
writes a JSON summary to bench/results/. Nothing is mocked.
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API_ROOT = HERE.parent
RESULTS = HERE / "results"

# name -> (prepare arguments, extra server settings, locust users, spawn rate, seconds)
SCENARIOS: dict[str, tuple[list[str], dict[str, str], int, int, int]] = {
    # The reference sale, scaled to this machine: far more buyers than seats.
    "sale": (["--seats", "2000", "--buyers", "1500"], {}, 1500, 150, 75),
    "sale-no-cache": (
        ["--seats", "2000", "--buyers", "1500"],
        {"LOBBYD_CACHE_ENABLED": "false"},
        1500,
        150,
        75,
    ),
    "sale-waiting-room": (
        ["--seats", "2000", "--buyers", "1500", "--waiting-room"],
        {"LOBBYD_WAITING_ROOM_ADMIT_PER_SECOND": "40"},
        1500,
        150,
        75,
    ),
    # The waiting room again, admitting at a rate chosen from the "steady"
    # result, and run for long enough to let everyone through.
    "sale-waiting-room-tuned": (
        ["--seats", "2000", "--buyers", "1500", "--waiting-room"],
        {"LOBBYD_WAITING_ROOM_ADMIT_PER_SECOND": "20"},
        1500,
        150,
        110,
    ),
    # The same sale at a pace this machine can serve: what latency looks
    # like when the system is not overloaded.
    "steady": (["--seats", "2000", "--buyers", "400"], {}, 400, 15, 60),
    # Everyone at once on very few seats, each wanting two to four of them,
    # clicked in any order: the claim path under maximum contention.
    **{
        f"rush-{strategy}": (
            ["--seats", "250", "--buyers", "500"],
            {"LOBBYD_CLAIM_STRATEGY": strategy, "BENCH_GROUP": "2-4"},
            500,
            500,
            30,
        )
        for strategy in ("lock_then_claim", "claim_only", "skip_locked", "redis_lock")
    },
    # A few seconds at low load, for CI: catches crashes and broken invariants.
    "smoke": (["--seats", "250", "--buyers", "60"], {}, 60, 30, 15),
}


def wait_for(url: str, seconds: float = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    sys.exit(f"{url} did not come up")


def psql(query: str) -> str:
    out = subprocess.run(
        ["psql", os.environ["BENCH_PSQL_URL"], "-Atc", query],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--workers", type=int, default=3, help="API processes")
    args = parser.parse_args()
    prepare_args, settings, users, spawn_rate, seconds = SCENARIOS[args.scenario]

    RESULTS.mkdir(exist_ok=True)
    state = HERE / ".state.json"
    env = {
        **os.environ,
        "LOBBYD_LOG_JSON": "true",
        # Every simulated buyer shares this machine's address.
        "LOBBYD_RATE_LIMIT_ENABLED": "false",
        "PAYSIM_DELAY_MS": "300",
        "BENCH_STATE": str(state),
        "BENCH_OUTCOMES": str(RESULTS / f"{args.scenario}.outcomes.json"),
        **settings,
    }
    database = env.get(
        "LOBBYD_DATABASE_URL", "postgresql+asyncpg://lobbyd:lobbyd@localhost:5432/lobbyd"
    )
    os.environ["BENCH_PSQL_URL"] = database.replace("+asyncpg", "")

    def run(*command: str) -> None:
        subprocess.run(command, cwd=API_ROOT, env=env, check=True)

    run("uv", "run", "alembic", "upgrade", "head")
    run("uv", "run", "python", "bench/prepare.py", "--out", str(state), *prepare_args)
    deadlocks_before = int(
        psql("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
    )

    logs = open(RESULTS / f"{args.scenario}.server.log", "w")
    servers = [
        subprocess.Popen(command, cwd=API_ROOT, env=env, stdout=logs, stderr=logs)
        for command in (
            [
                "uv",
                "run",
                "uvicorn",
                "lobbyd.main:app",
                "--port",
                "8000",
                "--workers",
                str(args.workers),
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            ["uv", "run", "uvicorn", "paysim.main:app", "--port", "8001", "--log-level", "warning"],
            ["uv", "run", "lobbyd-worker"],
        )
    ]
    try:
        wait_for("http://localhost:8000/readyz")
        wait_for("http://localhost:8001/healthz")
        prefix = RESULTS / args.scenario
        locust = subprocess.run(
            [
                "uv",
                "run",
                "locust",
                "-f",
                "bench/locustfile.py",
                "--headless",
                "--host",
                "http://localhost:8000",
                "-u",
                str(users),
                "-r",
                str(spawn_rate),
                "-t",
                f"{seconds}s",
                "--csv",
                str(prefix),
                "--only-summary",
                "--stop-timeout",
                "5",
            ],
            cwd=API_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        time.sleep(4)  # let the last payments settle
    finally:
        for server in servers:
            server.send_signal(signal.SIGTERM)
        for server in servers:
            server.wait(timeout=20)
        logs.close()

    # The numbers that matter more than speed.
    sys.path.insert(0, str(API_ROOT / "src"))
    from lobbyd import reconcile
    from lobbyd.config import get_settings
    from lobbyd.platform.db import create_engine, create_sessionmaker

    async def check() -> dict[str, list[tuple[object, ...]]]:
        engine = create_engine(get_settings())
        async with create_sessionmaker(engine)() as session:
            broken = await reconcile.violations(session, settled_grace=None)
        await engine.dispose()
        return broken

    broken = asyncio.run(check())
    rows = list(csv.DictReader(open(f"{prefix}_stats.csv")))
    summary = {
        "scenario": args.scenario,
        "users": users,
        "seconds": seconds,
        "api_workers": args.workers,
        "settings": settings,
        "requests": {
            row["Name"]: {
                "count": int(row["Request Count"]),
                "failures": int(row["Failure Count"]),
                "p50_ms": float(row["50%"] or 0),
                "p95_ms": float(row["95%"] or 0),
                "p99_ms": float(row["99%"] or 0),
                "per_second": round(float(row["Requests/s"]), 1),
            }
            for row in rows
        },
        "outcomes": json.load(open(env["BENCH_OUTCOMES"])),
        "seats_sold": int(psql("SELECT count(*) FROM tournament_seats WHERE status = 'sold'")),
        "seats_total": int(psql("SELECT count(*) FROM tournament_seats")),
        "orders_paid": int(psql("SELECT count(*) FROM orders WHERE status = 'paid'")),
        "tickets": int(psql("SELECT count(*) FROM tickets")),
        "deadlocks": int(
            psql("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        )
        - deadlocks_before,
        "server_errors_logged": sum(
            1
            for line in open(RESULTS / f"{args.scenario}.server.log")
            if '"level": "error"' in line
        ),
        "invariants_broken": {rule: [str(r) for r in rows] for rule, rows in broken.items()},
    }
    with open(RESULTS / f"{args.scenario}.json", "w") as file:
        json.dump(summary, file, indent=2)

    total = summary["requests"]["Aggregated"]
    holds = summary["requests"].get("POST holds", {})
    print(
        f"{args.scenario}: {total['count']} requests, {total['per_second']}/s, "
        f"{total['failures']} failures | holds p50 {holds.get('p50_ms')} p95 {holds.get('p95_ms')} "
        f"p99 {holds.get('p99_ms')} ms | sold {summary['seats_sold']}/{summary['seats_total']} | "
        f"deadlocks {summary['deadlocks']} | invariants broken {len(broken)}"
    )
    if locust.returncode not in (0, 1):  # 1 means "some requests failed", reported above
        print(locust.stderr[-2000:])
    if broken or (args.scenario == "smoke" and total["failures"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
