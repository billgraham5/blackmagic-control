"""Discovering what a camera can do from what the camera says."""

from __future__ import annotations

import yaml

from bmc.discovery import _SpecLoader, expand_templates, parse_openapi


def load(text: str) -> dict:
    return yaml.load(text, Loader=_SpecLoader)


def test_off_is_a_value_not_a_boolean():
    """YAML 1.1 resolves Off/On/Yes/No as booleans.

    Auto exposure mode is Off, Continuous or OneShot, so the default loader
    silently replaces a legal setting with False and the option disappears.
    """
    document = load("enum: [Off, On, Yes, No, Continuous]")
    assert document["enum"] == ["Off", "On", "Yes", "No", "Continuous"]
    # For contrast, and to show the trap is real:
    assert yaml.safe_load("enum: [Off]")["enum"] == [False]


def test_real_booleans_and_numbers_still_parse():
    document = load("a: true\nb: false\nc: 1.5\nd: 3\ne: hello")
    assert document == {"a": True, "b": False, "c": 1.5, "d": 3, "e": "hello"}


def test_methods_and_summaries_are_read():
    document = load("""
    paths:
      /video/iso:
        get: {summary: Get current ISO}
        put:
          summary: Set current ISO
          requestBody:
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    iso: {type: integer, minimum: 100, maximum: 25600}
    """)
    endpoints = parse_openapi(document)
    iso = endpoints["/video/iso"]
    assert iso.methods == {"get", "put"}
    assert iso.readable and iso.writable
    assert iso.write_schema["iso"] == {"type": "integer", "minimum": 100, "maximum": 25600}


def test_refs_are_followed():
    document = load("""
    paths:
      /video/autoExposure:
        put:
          requestBody:
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    mode: {$ref: '#/components/schemas/Mode'}
    components:
      schemas:
        Mode:
          type: string
          enum: [Off, Continuous]
    """)
    schema = parse_openapi(document)["/video/autoExposure"].write_schema
    assert schema["mode"]["enum"] == ["Off", "Continuous"]


def test_a_self_referential_schema_does_not_hang():
    document = load("""
    paths:
      /loop:
        put:
          requestBody:
            content:
              application/json:
                schema: {$ref: '#/components/schemas/Loop'}
    components:
      schemas:
        Loop:
          type: object
          properties:
            self: {$ref: '#/components/schemas/Loop'}
    """)
    assert parse_openapi(document)["/loop"].write_schema is not None


def test_a_get_only_endpoint_is_not_offered_for_writing():
    document = load("paths:\n  /system/product:\n    get: {summary: Product name}")
    endpoint = parse_openapi(document)["/system/product"]
    assert endpoint.readable
    assert not endpoint.writable


def test_templates_expand_to_real_paths():
    assert expand_templates(
        "/monitoring/{displayName}/zebra", ["MainSDI", "HDMI"], [0, 1]
    ) == ["/monitoring/MainSDI/zebra", "/monitoring/HDMI/zebra"]
    assert expand_templates("/audio/channel/{channelIndex}/level", [], [0, 1]) == [
        "/audio/channel/0/level",
        "/audio/channel/1/level",
    ]


def test_templates_we_cannot_enumerate_are_dropped():
    """A preset name is not discoverable, so probing one would be a guess."""
    assert expand_templates("/presets/{presetName}", ["HDMI"], [0]) == []


def test_a_document_without_paths_is_harmless():
    assert parse_openapi({"openapi": "3.0.1"}) == {}
    assert parse_openapi({"paths": "nonsense"}) == {}
