"""Fan-out to web clients.

A page that silently stops receiving updates is worse than one that visibly
disconnects: it keeps showing stale values and looks fine doing it.
"""

from __future__ import annotations

import asyncio

from bmc.camera import Camera
from bmc.config import Settings


def _drain(queue: asyncio.Queue) -> list[dict]:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def _apply(messages: list[dict]) -> dict:
    """Replay messages the way the web page does, and return the state it holds."""
    state: dict = {}
    for message in messages:
        if message.get("type") == "snapshot":
            state = dict(message["state"])
        elif message.get("type") == "state":
            state[message["property"]] = message["value"]
    return state


async def test_a_backed_up_client_is_resynced_not_dropped():
    """A burst bigger than the queue must not cost the client every future update.

    Firmware 9.6 pushes ~90 properties, so a burst during a slider drag can
    outrun a browser that is momentarily busy.
    """
    camera = Camera(Settings())
    camera._connected = True
    camera._supported = {"/video/iso"}

    async with camera.listen() as queue:
        for value in range(500):  # comfortably past the queue bound
            camera._update("/video/iso", {"iso": value})

        messages = _drain(queue)
        assert messages, "client was cut off instead of resynchronised"

        # However much was dropped, replaying what arrived must land on the
        # current value -- a resync snapshot followed by whatever came after it.
        assert any(m.get("type") == "snapshot" for m in messages), (
            "client got no snapshot, so it can never recover what it missed"
        )
        assert _apply(messages)["/video/iso"] == {"iso": 499}

        # And it must still be subscribed for what comes next.
        camera._update("/video/iso", {"iso": 1000})
        followup = _drain(queue)
        assert followup, "client stopped receiving updates after the burst"
        assert _apply(followup)["/video/iso"] == {"iso": 1000}
