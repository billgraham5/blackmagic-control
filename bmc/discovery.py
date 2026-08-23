"""Work out what a camera can do by asking the camera, not by guessing.

Three sources, best first:

1. The OpenAPI documents the camera serves at ``/control/documentation.html``.
   These are authoritative for this exact body and firmware, and carry the bit
   nothing else does: the shape and legal values of what you can write.
2. ``/event/list``, which names every property the camera will push. Firmware
   9.6 lists well over a hundred, including endpoints absent from any published
   spec dump.
3. The built-in list, as a floor for firmware that serves neither.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
import yaml

log = logging.getLogger("bmc.discovery")


class _SpecLoader(yaml.SafeLoader):
    """YAML loader that does not turn ``Off`` into ``False``.

    YAML 1.1 resolves Off/On/Yes/No as booleans. This API uses ``Off`` as a
    real enum value -- auto exposure mode is Off, Continuous or OneShot -- so
    the default loader silently replaces a legal setting with a boolean.
    """


_SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
for _first_char, _resolvers in list(_SpecLoader.yaml_implicit_resolvers.items()):
    _SpecLoader.yaml_implicit_resolvers[_first_char] = [
        (tag, regexp)
        for tag, regexp in _resolvers
        if not (tag == "tag:yaml.org,2002:bool" and _first_char not in "tTfF")
    ]

#: Where cameras have been seen to serve their OpenAPI documents.
SPEC_DIRECTORIES: tuple[str, ...] = ("/control/", "/control/api/v1/", "/")


@dataclass
class Endpoint:
    """One path, and what the camera says can be done with it."""

    path: str
    methods: set[str] = field(default_factory=set)
    summary: str = ""
    #: Property name -> JSON-schema-ish description, for anything writable.
    write_schema: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return "get" in self.methods

    @property
    def writable(self) -> bool:
        return "put" in self.methods


def _resolve(node: Any, root: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    """Follow $ref links, refusing to loop on a self-referential schema."""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen or not ref.startswith("#/"):
            return {}
        target: Any = root
        for part in ref.lstrip("#/").split("/"):
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        return _resolve(target, root, seen | {ref})
    return node


#: This API nests one level at most (auto exposure's mode object). A bound stops
#: a schema that refers to itself from recursing forever.
MAX_DEPTH = 3


def _properties(
    schema: Any, root: dict[str, Any], depth: int = 0
) -> dict[str, dict[str, Any]]:
    """Flatten a request schema into the fields a form would need."""
    if depth >= MAX_DEPTH:
        return {}
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return {}

    for combinator in ("oneOf", "anyOf", "allOf"):
        options = schema.get(combinator)
        if isinstance(options, list):
            merged: dict[str, dict[str, Any]] = {}
            for option in options:
                merged.update(_properties(option, root, depth + 1))
            if merged:
                return merged

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        # A bare scalar body, e.g. a boolean. Name it after nothing so the
        # caller knows to send the value unwrapped.
        if schema.get("type") in ("boolean", "number", "integer", "string"):
            return {"": _describe(schema, root, depth + 1)}
        return {}
    return {
        name: _describe(value, root, depth + 1) for name, value in properties.items()
    }


def _describe(node: Any, root: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    node = _resolve(node, root)
    if not isinstance(node, dict):
        return {"type": "string"}
    described: dict[str, Any] = {"type": node.get("type", "string")}
    for key in ("enum", "minimum", "maximum", "default", "description"):
        if key in node:
            described[key] = node[key]
    if described["type"] == "object":
        nested = _properties(node, root, depth)
        if nested:
            described["properties"] = nested
    return described


def parse_openapi(document: dict[str, Any]) -> dict[str, Endpoint]:
    """Turn one OpenAPI document into endpoints keyed by path."""
    endpoints: dict[str, Endpoint] = {}
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return endpoints

    for path, operations in paths.items():
        if not isinstance(path, str) or not isinstance(operations, dict):
            continue
        endpoint = endpoints.setdefault(f"/{path.lstrip('/')}", Endpoint(path=f"/{path.lstrip('/')}"))
        for method, operation in operations.items():
            if method.lower() not in ("get", "put", "post", "delete"):
                continue
            endpoint.methods.add(method.lower())
            if not isinstance(operation, dict):
                continue
            if not endpoint.summary and isinstance(operation.get("summary"), str):
                endpoint.summary = operation["summary"]
            if method.lower() == "put":
                body = operation.get("requestBody")
                if isinstance(body, dict):
                    content = body.get("content")
                    if isinstance(content, dict):
                        for media in content.values():
                            if isinstance(media, dict) and "schema" in media:
                                endpoint.write_schema.update(
                                    _properties(media["schema"], document)
                                )
    return endpoints


def expand_templates(path: str, displays: Iterable[str], channels: Iterable[int]) -> list[str]:
    """Turn ``/monitoring/{displayName}/zebra`` into one path per real display."""
    if "{" not in path:
        return [path]
    placeholder = re.search(r"\{(\w+)\}", path)
    if placeholder is None:
        return [path]
    name = placeholder.group(1).lower()
    if "display" in name:
        return [re.sub(r"\{\w+\}", display, path, count=1) for display in displays]
    if "channel" in name or "index" in name:
        return [re.sub(r"\{\w+\}", str(channel), path, count=1) for channel in channels]
    # A name, filename or device we cannot enumerate: not discoverable.
    return []


async def fetch_specs(client: httpx.AsyncClient, base_url: str) -> dict[str, Endpoint]:
    """Download and parse whatever OpenAPI documents the camera serves.

    Best effort throughout: a camera that serves no documentation is normal, and
    discovery falls back to the other two sources.
    """
    page = await _get_text(client, f"{base_url}/control/documentation.html")
    if page is None:
        return {}

    names = sorted(set(re.findall(r"[\w./-]+\.yaml", page)))
    if not names:
        log.debug("documentation page named no specs")
        return {}

    endpoints: dict[str, Endpoint] = {}
    for name in names:
        text = await _fetch_spec(client, base_url, name)
        if text is None:
            continue
        try:
            document = yaml.load(text, Loader=_SpecLoader)
        except yaml.YAMLError as exc:
            log.debug("could not parse %s: %s", name, exc)
            continue
        if not isinstance(document, dict):
            continue
        for path, endpoint in parse_openapi(document).items():
            existing = endpoints.get(path)
            if existing is None:
                endpoints[path] = endpoint
            else:
                existing.methods |= endpoint.methods
                existing.write_schema.update(endpoint.write_schema)
                existing.summary = existing.summary or endpoint.summary

    log.info("camera documentation describes %d endpoints", len(endpoints))
    return endpoints


async def _fetch_spec(client: httpx.AsyncClient, base_url: str, name: str) -> str | None:
    leaf = name.lstrip("./")
    for directory in SPEC_DIRECTORIES:
        text = await _get_text(client, f"{base_url}{directory}{leaf}")
        if text is not None:
            return text
    return None


async def _get_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or not response.text.strip():
        return None
    return response.text
