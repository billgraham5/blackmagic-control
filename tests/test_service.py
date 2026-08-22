"""End-to-end tests: real service, real mock camera, real websocket."""

from __future__ import annotations

import asyncio

import pytest


async def deck(client, path: str):
    return await client.get(f"/deck/{path}")


# ----------------------------------------------------------------- discovery

async def test_probe_drops_endpoints_this_body_lacks(service):
    """The published spec advertises hardware the Micro Studio G2 does not have."""
    supported = set((await service.get("/api/state")).json()["supported"])
    assert "/video/iso" in supported
    assert "/lens/iris" in supported
    assert "/video/ndFilter" not in supported
    assert "/camera/tallyStatus" not in supported
    assert "/monitoring/focusAssist" not in supported


async def test_pushed_and_polled_properties_are_separated(service):
    """Older firmware pushes transport but not exposure, so exposure gets polled."""
    body = (await service.get("/api/state")).json()
    pushed = set(body["pushed"])
    assert "/transports/0/record" in pushed
    assert "/video/iso" not in pushed
    assert set(body["supported"]) - pushed


# -------------------------------------------------------------------- values

async def test_iso_steps_up_and_down(service):
    assert (await deck(service, "iso/400")).text == "ISO 400"
    assert (await deck(service, "iso/up")).text == "ISO 800"
    assert (await deck(service, "iso/up")).text == "ISO 1600"
    assert (await deck(service, "iso/down")).text == "ISO 800"


async def test_iso_stops_at_the_top_of_the_ladder(service):
    await deck(service, "iso/25600")
    assert (await deck(service, "iso/up")).text == "ISO 25600"


async def test_read_back_reports_what_the_camera_actually_took(service):
    """1250 is not on this camera's ladder, so it snaps -- and we report the snap."""
    response = await deck(service, "iso/1250")
    assert response.status_code == 200
    assert response.text == "ISO 1600"


async def test_white_balance_presets_and_clamping(service):
    assert (await deck(service, "wb/preset/tungsten")).text == "3200K"
    assert (await deck(service, "wb/preset/daylight")).text == "5600K"
    # Above the camera's 10000K ceiling; clamped rather than rejected.
    assert (await deck(service, "wb/99999")).text == "10000K"


async def test_white_balance_steps_by_kelvin(service):
    await deck(service, "wb/5600")
    assert (await deck(service, "wb/warmer")).text == "6100K"
    assert (await deck(service, "wb/cooler?by=1000")).text == "5100K"


async def test_shutter_accepts_both_speed_and_angle(service):
    assert (await deck(service, "shutter/50")).text == "1/50"
    assert (await deck(service, "shutter/up")).text == "1/60"
    assert (await deck(service, "shutter/angle/18000")).text == "180deg"
    # Now reporting in angle, stepping should stay in angle.
    assert (await deck(service, "shutter/up")).text == "270deg"


async def test_record_toggles_and_reports_state(service):
    assert (await deck(service, "record/toggle")).text == "REC"
    assert "REC" in (await deck(service, "status")).text
    assert (await deck(service, "record/toggle")).text == "IDLE"


async def test_record_start_accepts_a_clip_name(service):
    assert (await deck(service, "record/start?clip=take-07")).text == "REC"
    await deck(service, "record/stop")


async def test_auto_exposure_toggles(service):
    assert (await deck(service, "ae/toggle")).text == "AE Continuous"
    assert (await deck(service, "ae/toggle")).text == "AE Off"


async def test_preset_recall(service):
    assert (await deck(service, "preset/Interview")).text == "Interview"
    body = (await service.get("/api/state")).json()
    assert body["activePreset"] == "Interview"


async def test_media_summary_is_human_readable(service):
    assert (await deck(service, "media")).text == "2h21m"


async def test_status_line_summarises_the_camera(service):
    await deck(service, "iso/800")
    await deck(service, "shutter/50")
    await deck(service, "wb/5600")
    text = (await deck(service, "status")).text
    assert "ISO 800" in text and "1/50" in text and "5600K" in text


# -------------------------------------------------------------------- errors

async def test_unknown_white_balance_preset_is_explained(service):
    response = await deck(service, "wb/preset/nonsense")
    assert response.status_code == 400
    assert "daylight" in response.text  # tells the caller the valid options


async def test_camera_rejection_is_surfaced_as_plain_text(service):
    """Deck errors land on a button title, so they must not be JSON."""
    response = await deck(service, "iso/50")
    assert response.status_code == 400
    assert not response.text.startswith("{")
    assert "iso out of range" in response.text


async def test_unknown_preset_is_rejected_by_the_camera(service):
    response = await deck(service, "preset/DoesNotExist")
    assert response.status_code == 400


async def test_unknown_control_is_a_404(service):
    assert (await service.get("/api/set/nonsense?v=1")).status_code == 404


# ---------------------------------------------------------------- continuous

async def test_sliders_set_continuous_controls(service):
    assert (await service.get("/api/set/iris?v=0.25")).text == "f/4"
    assert (await service.get("/api/set/focus?v=0.8")).text == "focus 80%"
    assert (await service.get("/api/set/saturation?v=0")).text == "sat 0.00"
    assert (await service.get("/api/set/tint?v=-10")).text == "tint -10"


async def test_raw_passthrough_requires_an_absolute_path(service):
    assert (await service.post("/api/raw", json={"path": "video/iso"})).status_code == 400
    response = await service.post(
        "/api/raw", json={"path": "/video/iso", "body": {"iso": 3200}}
    )
    assert response.status_code == 200
    assert (await deck(service, "status")).text.startswith("ISO 3200")


# ----------------------------------------------------------------- websocket

async def test_state_changes_reach_websocket_clients(service):
    """A Stream Deck press must light up the web page without a refresh."""
    import websockets

    url = str(service.base_url).replace("http://", "ws://") + "/api/ws"
    async with websockets.connect(url) as socket:
        import json

        snapshot = json.loads(await socket.recv())
        assert snapshot["type"] == "snapshot"
        assert "/video/iso" in snapshot["state"]

        await deck(service, "record/toggle")

        async def wait_for_record() -> dict:
            while True:
                message = json.loads(await socket.recv())
                if message.get("property") == "/transports/0/record":
                    return message

        message = await asyncio.wait_for(wait_for_record(), timeout=10)
        assert message["value"]["recording"] is True


async def test_polling_picks_up_changes_made_on_the_camera(service, camera_state):
    """Exposure is not pushed on this firmware, so the poll loop must catch it."""
    camera_state.set("/video/iso", {"iso": 6400})

    async def wait_for_iso() -> None:
        while True:
            body = (await service.get("/api/state")).json()
            if body["state"].get("/video/iso", {}).get("iso") == 6400:
                return
            await asyncio.sleep(0.1)

    await asyncio.wait_for(wait_for_iso(), timeout=10)


# --------------------------------------------------------------- unavailable

@pytest.mark.asyncio
async def test_service_starts_without_a_camera_and_reports_why():
    """The service should be startable at boot, before the camera is powered on."""
    import httpx

    from bmc.app import create_app
    from bmc.config import Settings
    from tests.conftest import Background, free_port

    port = free_port()
    settings = Settings(camera_host=f"127.0.0.1:{free_port()}", port=port)
    async with Background(create_app(settings), port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            body = (await client.get("/api/state")).json()
            assert body["connected"] is False
            response = await client.get("/deck/status")
            assert response.status_code == 503
