"""End-to-end tests: real service, real mock camera, real websocket."""

from __future__ import annotations

import asyncio

import pytest


async def deck(client, path: str):
    return await client.get(f"/deck/{path}")


# ----------------------------------------------------------------- discovery

async def test_probe_drops_endpoints_this_body_lacks(service):
    """The published spec advertises hardware the Micro Studio G2 does not have.

    ND filter is the clear case: the endpoint is in the camera's own OpenAPI
    documentation, and the body has no ND filter behind it.
    """
    supported = set((await service.get("/api/state")).json()["supported"])
    assert "/video/iso" in supported
    assert "/lens/iris" in supported
    assert "/video/ndFilter" not in supported
    assert "/clips" not in supported


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


# ------------------------------------------------------- monitoring overlays

async def test_monitoring_displays_are_discovered(service):
    body = (await service.get("/api/state")).json()
    assert body["displays"] == ["MainSDI", "HDMI", "FrontUSBC"]
    assert "/monitoring/MainSDI/falseColor" in body["supported"]
    assert "/monitoring/FrontUSBC/falseColor" in body["supported"]
    # Only the SDI output carries the full overlay set on this mock, so the
    # others must not have it claimed for them.
    assert "/monitoring/MainSDI/displayLUT" in body["supported"]
    assert "/monitoring/HDMI/displayLUT" not in body["supported"]


async def test_overlay_toggles_without_dropping_its_other_settings(service, camera_state):
    """Zebra carries a level; a toggle that sent only the flag would lose it."""
    assert (await deck(service, "monitor/zebra/toggle")).text == "zebra on"
    assert camera_state.state["/monitoring/MainSDI/zebra"] == {"enabled": True, "level": 75}
    assert (await deck(service, "monitor/zebra/toggle")).text == "zebra off"


async def test_overlay_can_be_set_explicitly_and_per_display(service, camera_state):
    assert (await deck(service, "monitor/falseColor/on")).text == "falseColor on"
    assert (await deck(service, "monitor/zebra/on?display=HDMI")).text == "zebra on"
    assert camera_state.state["/monitoring/HDMI/zebra"]["enabled"] is True
    assert camera_state.state["/monitoring/MainSDI/zebra"]["enabled"] is False


async def test_unknown_overlay_says_so(service):
    response = await deck(service, "monitor/nonsense/toggle")
    assert response.status_code == 400
    assert "not available" in response.text


async def test_colour_bars_and_tally(service):
    assert (await deck(service, "colorbars/toggle")).text == "colorBars on"
    assert (await deck(service, "tally")).text == "off"


async def test_the_cameras_own_iso_list_drives_stepping(service):
    """/video/supportedISOs is reported as a bare list here, not an object."""
    body = (await service.get("/api/state")).json()
    assert "/video/supportedISOs" in body["supported"]
    await deck(service, "iso/400")
    assert (await deck(service, "iso/up")).text == "ISO 800"


async def test_default_overlay_display_follows_the_cameras_own_order(service):
    """The camera lists displays in its own order; alphabetical is not it.

    The web page and the deck must resolve to the same display, or a button
    toggles one output while showing the state of another.
    """
    body = (await service.get("/api/state")).json()
    assert body["displays"] == ["MainSDI", "HDMI", "FrontUSBC"]

    await deck(service, "monitor/zebra/toggle")
    state = (await service.get("/api/state")).json()["state"]
    assert state["/monitoring/MainSDI/zebra"]["enabled"] is True
    assert state["/monitoring/HDMI/zebra"]["enabled"] is False
    assert state["/monitoring/FrontUSBC/zebra"]["enabled"] is False


async def test_each_display_is_addressable(service, camera_state):
    for display in ("HDMI", "FrontUSBC", "MainSDI"):
        assert (await deck(service, f"monitor/cleanFeed/on?display={display}")).text == "cleanFeed on"
        assert camera_state.state[f"/monitoring/{display}/cleanFeed"]["enabled"] is True


# ------------------------------------------------------------------- caching

async def test_page_assets_are_never_served_from_a_stale_cache(service):
    """A cached app.js survives an upgrade and silently breaks the page.

    Without a Cache-Control header a browser may reuse the old JavaScript
    indefinitely: the page loads, buttons still fire, and nothing updates --
    until someone thinks to hard refresh.
    """
    for path in ("/", "/static/app.js", "/static/style.css"):
        response = await service.get(path)
        assert response.status_code == 200, path
        assert "no-cache" in response.headers.get("cache-control", ""), path


async def test_asset_urls_carry_a_build_tag(service):
    """Belt and braces: a changed asset gets a changed URL."""
    html = (await service.get("/")).text
    import re

    tags = re.findall(r"/static/\w+\.\w+\?v=([0-9a-f]{12})", html)
    assert len(tags) >= 2, html[:400]
    assert len(set(tags)) == 1, "assets should share one build tag"


# ------------------------------------------------------------------- iris

async def test_iris_is_set_by_f_number(service, camera_state):
    """f/8 is APEX 6.0 on the wire; sending 8 straight through would be f/16."""
    assert (await deck(service, "iris/fstop/8")).text == "f/8"
    assert round(camera_state.state["/lens/iris"]["apertureStop"], 2) == 6.0

    assert (await deck(service, "iris/fstop/2.8")).text == "f/2.8"
    assert round(camera_state.state["/lens/iris"]["apertureStop"], 2) == 2.97


async def test_iris_uses_aperture_stop_not_the_integer_ordinal(service, camera_state):
    """apertureNumber is an integer index into the lens's steps, not an f-number."""
    await deck(service, "iris/fstop/5.6")
    assert camera_state.state["/lens/iris"]["apertureNumber"] == 8  # untouched
    assert round(camera_state.state["/lens/iris"]["apertureStop"], 2) == 4.97


async def test_iris_is_clamped_to_what_the_lens_can_do(service):
    """A 12-35mm f/2.8 cannot be asked for f/1.4 or f/32."""
    assert (await deck(service, "iris/fstop/1.4")).text == "f/2.8"
    assert (await deck(service, "iris/fstop/32")).text == "f/16"


async def test_iris_range_is_reported_as_f_numbers(service):
    iris = (await service.get("/api/state")).json()["iris"]
    low, high = iris["range"]
    assert round(low, 1) == 2.8
    assert round(high, 1) == 16.0


async def test_iris_reports_the_f_number_the_lens_actually_holds(service):
    await deck(service, "iris/fstop/11")
    iris = (await service.get("/api/state")).json()["iris"]
    assert round(iris["fstop"], 1) == 11.0


async def test_a_nonsense_f_number_is_rejected(service):
    response = await deck(service, "iris/fstop/0")
    assert response.status_code == 400
    assert "greater than zero" in response.text


async def test_iris_can_still_be_set_normalised(service):
    """The normalised path stays, for the nudge endpoints and any lens without
    an aperture description."""
    assert (await service.get("/api/set/iris?v=0.25")).status_code == 200


# --------------------------------------------------- discovery and schema

async def test_endpoints_are_found_from_the_cameras_own_documentation(service):
    """These exist only in the camera's OpenAPI, not in any built-in list."""
    supported = set((await service.get("/api/state")).json()["supported"])
    assert "/camera/id" in supported
    assert "/media/slots" in supported
    # A templated path, expanded against the displays the camera reported.
    assert "/monitoring/MainSDI/brightness" in supported
    assert "/monitoring/FrontUSBC/brightness" in supported


async def test_schema_describes_what_can_be_written(service):
    schema = (await service.get("/api/schema")).json()

    brightness = schema["/monitoring/MainSDI/brightness"]
    assert brightness["writable"]
    assert brightness["fields"]["brightness"] == {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
    }

    # Nested objects and their enums survive, Off included.
    mode = schema["/video/autoExposure"]["fields"]["mode"]["properties"]["mode"]
    assert mode["enum"] == ["Off", "Continuous", "OneShot"]


async def test_read_only_endpoints_are_not_offered_as_settings(service):
    """The camera documents /media/slots as GET only."""
    schema = (await service.get("/api/schema")).json()
    assert schema["/media/slots"]["writable"] is False
    assert schema["/media/slots"]["fields"] == {}


async def test_schema_marks_which_values_the_camera_pushes(service):
    schema = (await service.get("/api/schema")).json()
    assert schema["/transports/0/record"]["pushed"] is True
    assert schema["/video/iso"]["pushed"] is False


async def test_anything_discovered_can_be_written_through_the_passthrough(service):
    response = await service.post(
        "/api/raw", json={"path": "/camera/id", "body": {"id": "studio-left-2"}}
    )
    assert response.status_code == 200
    state = (await service.get("/api/state")).json()["state"]
    assert state["/camera/id"] == {"id": "studio-left-2"}


async def test_an_overlay_the_camera_ignores_is_reported_not_silently_dropped(service):
    """A camera can answer 204 and change nothing.

    An overlay may not apply to the output it was addressed to. Left unchecked
    that reads as a button that does nothing at all, with no clue why.
    """
    response = await deck(service, "monitor/cleanFeed/on?display=FrontUSBC")
    assert response.status_code == 400
    assert "left cleanFeed off" in response.text
    assert "FrontUSBC" in response.text


async def test_an_overlay_that_does_apply_still_works(service, camera_state):
    assert (await deck(service, "monitor/cleanFeed/on?display=MainSDI")).text == "cleanFeed on"
    assert camera_state.state["/monitoring/MainSDI/cleanFeed"]["enabled"] is True
