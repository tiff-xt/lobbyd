"""The public "try to charge twice" endpoint."""

import asyncio
import uuid

from httpx import AsyncClient


async def test_a_burst_of_the_same_request_makes_one_charge(client: AsyncClient) -> None:
    key = str(uuid.uuid4())
    responses = await asyncio.gather(
        *(
            client.post("/try/charges", json={"item": "ticket"}, headers={"Idempotency-Key": key})
            for _ in range(12)
        )
    )

    assert {response.status_code for response in responses} == {201}
    assert len({response.text for response in responses}) == 1
    assert sum("Idempotent-Replay" not in response.headers for response in responses) == 1

    tally = (await client.get(f"/try/charges/{key}")).json()
    assert tally == {"requests_received": 12, "charges_made": 1}


async def test_a_new_key_is_a_new_charge(client: AsyncClient) -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    for key in (first, second, second):
        await client.post("/try/charges", json={}, headers={"Idempotency-Key": key})

    assert (await client.get(f"/try/charges/{first}")).json()["charges_made"] == 1
    assert (await client.get(f"/try/charges/{second}")).json() == {
        "requests_received": 2,
        "charges_made": 1,
    }


async def test_it_needs_a_key_and_an_unknown_key_has_no_charges(client: AsyncClient) -> None:
    assert (await client.post("/try/charges", json={})).status_code == 400
    assert (await client.get("/try/charges/never-used-key")).json() == {
        "requests_received": 0,
        "charges_made": 0,
    }
