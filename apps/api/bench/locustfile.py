"""The sale, from the buyers' side. Run through bench/run.py.

Each simulated buyer behaves like a person on the tournament page:

1. open the page (tournament, seat map, availability)
2. if there is a waiting room, queue until admitted
3. pick two neighbouring seats that look free and try to hold them; on a
   refusal, look at availability again and pick others (up to five tries)
4. pay, and poll the order until it settles
5. then stay on the page, polling availability like an open browser tab

A refused hold (409) is a correct answer, not a failure: most buyers in an
oversubscribed sale must lose. Only server errors and timeouts count as failures.
"""

import json
import os
import random
import time
import uuid
from collections import Counter
from itertools import count

from locust import FastHttpUser, between, events, task

STATE = json.load(open(os.environ.get("BENCH_STATE", "bench/.state.json")))
SLUG = STATE["slug"]
ROWS = STATE["rows"]
_next_buyer = count()
outcomes: Counter[str] = Counter()


def _report(name: str, started: float) -> None:
    """Record a whole user journey as its own line in the results."""
    events.request.fire(
        request_type="FLOW",
        name=name,
        response_time=(time.perf_counter() - started) * 1000,
        response_length=0,
        exception=None,
        context={},
    )


class Buyer(FastHttpUser):
    wait_time = between(2, 4)
    network_timeout = 30.0
    connection_timeout = 30.0

    def on_start(self) -> None:
        token = STATE["tokens"][next(_next_buyer) % len(STATE["tokens"])]
        self.auth = {"Authorization": f"Bearer {token}"}
        self.done = False
        self.taken: set[str] = set()

    def _availability(self) -> None:
        with self.client.get(
            f"/tournaments/{SLUG}/availability", name="GET availability", catch_response=True
        ) as response:
            if response.status_code == 200:
                self.taken = set(response.json()["taken_seat_ids"])
            else:
                response.failure(f"status {response.status_code}")

    def _pick(self) -> list[str] | None:
        for _ in range(40):
            row = random.choice(ROWS)
            at = random.randrange(len(row) - 1)
            pair = row[at : at + 2]
            if not self.taken.intersection(pair):
                return pair
        return None

    def _admission(self) -> str | None:
        """Queue until it is our turn. None if there is no waiting room."""
        started = time.perf_counter()
        while True:
            with self.client.post(
                f"/tournaments/{SLUG}/queue",
                headers=self.auth,
                name="POST queue",
                catch_response=True,
            ) as response:
                if response.status_code != 200:
                    response.failure(f"status {response.status_code}")
                    return None
                status = response.json()
            if status["status"] == "not_required":
                return None
            if status["status"] == "admitted":
                _report("time in waiting room", started)
                return str(status["admission_token"])
            time.sleep(1)

    @task
    def shop(self) -> None:
        if self.done:
            # An open tab: keeps polling, as real ones do during a sale.
            self._availability()
            return
        self.done = True
        started = time.perf_counter()

        self.client.get(f"/tournaments/{SLUG}", name="GET tournament")
        self.client.get(f"/venues/{STATE['venue_id']}/seats", name="GET seat map")
        self._availability()
        admission = self._admission()

        hold_id = None
        for _ in range(5):
            seats = self._pick()
            if seats is None:
                outcomes["nothing left to pick"] += 1
                return
            headers = {**self.auth, "Idempotency-Key": str(uuid.uuid4())}
            if admission:
                headers["X-Admission-Token"] = admission
            with self.client.post(
                "/holds",
                json={"tournament": SLUG, "seat_ids": seats},
                headers=headers,
                name="POST holds",
                catch_response=True,
            ) as response:
                if response.status_code == 201:
                    outcomes["hold won"] += 1
                    hold_id = response.json()["id"]
                elif response.status_code == 409:
                    outcomes["hold refused"] += 1
                    response.success()  # a correct answer
                else:
                    outcomes[f"hold error {response.status_code}"] += 1
                    response.failure(f"status {response.status_code}")
            if hold_id:
                break
            self.taken.update(seats)
            self._availability()
        if hold_id is None:
            outcomes["gave up"] += 1
            return
        _report("page open to seats held", started)

        with self.client.post(
            "/orders",
            json={"hold_id": hold_id, "payment_method": "pm_success"},
            headers={**self.auth, "Idempotency-Key": str(uuid.uuid4())},
            name="POST orders",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                outcomes[f"order error {response.status_code}"] += 1
                response.failure(f"status {response.status_code}")
                return
            order_id = response.json()["id"]

        # Ask again after half a second, then back off: a client that keeps
        # hammering a busy server is part of what keeps it busy.
        pause, waited = 0.5, 0.0
        while waited < 45:
            time.sleep(pause)
            waited += pause
            pause = min(pause * 1.5, 3.0)
            response = self.client.get(f"/orders/{order_id}", headers=self.auth, name="GET order")
            if response.status_code == 200 and response.json()["status"] != "pending":
                outcomes[f"order {response.json()['status']}"] += 1
                _report("page open to tickets", started)
                return
        outcomes["order still pending"] += 1


@events.quitting.add_listener
def _save_outcomes(environment, **_: object) -> None:  # type: ignore[no-untyped-def]
    path = os.environ.get("BENCH_OUTCOMES")
    if path:
        with open(path, "w") as file:
            json.dump(outcomes, file)
