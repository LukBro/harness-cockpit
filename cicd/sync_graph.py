#!/usr/bin/env python3
"""Sync the code dependency graph extracted by graphify to Monolynx.

This is a thin mapper, not an analyzer: it reads graphify-out/graph.json
(NetworkX node-link format), maps the graphify taxonomy onto Monolynx node
and edge types, and pushes the result with the MCP tool `replace_graph`
(full replacement, idempotent).

Missing graph.json or missing token ends with exit 0 so the sync never
breaks the project build.

Usage:
    graphify update .
    python cicd/sync_graph.py --dry-run
    python cicd/sync_graph.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("sync_graph")

DEFAULT_URL = "https://monolynx.com"
PROTOCOL_VERSION = "2025-03-26"
MAX_NODES = 20_000
MAX_EDGES = 60_000
RETRIES = 3

# Relations outside this map (references, rationale_for) are dropped - v1 is code-only.
RELATION_TO_EDGE_TYPE = {
    "calls": "CALLS",
    "indirect_call": "CALLS",
    "contains": "CONTAINS",
    "method": "CONTAINS",
    "imports": "IMPORTS",
    "imports_from": "IMPORTS",
    "re_exports": "IMPORTS",
    "inherits": "INHERITS",
    "uses": "USES",
}

# Source file extensions of this project (Python backend, TypeScript frontend).
FILE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs")

# CamelCase only: UPPER_SNAKE constants and single-word ALLCAPS fall through to Const.
CAMEL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*$")


class MonolynxError(RuntimeError):
    pass


class MonolynxClient:
    """Minimal MCP Streamable HTTP client (JSON-RPC over urllib, stdlib only)."""

    # replace_graph with clear_first can take over 120 s server-side on ~1500 nodes;
    # a shorter timeout drops the response and triggers a needless full resend.
    def __init__(self, url: str, token: str, project_slug: str, timeout: float = 300.0) -> None:
        self.endpoint = url.rstrip("/") + "/mcp/"
        self.token = token
        self.project_slug = project_slug
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        data = json.dumps(payload).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    return (json.loads(body) if body.strip() else None), response_headers
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                    raise MonolynxError(f"HTTP {exc.code}: {detail}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < RETRIES:
                delay = 2 ** attempt
                log.warning("Request failed (%s), retry %d/%d in %ds", last_error, attempt, RETRIES - 1, delay)
                time.sleep(delay)
        raise MonolynxError(f"Request failed after {RETRIES} attempts: {last_error}")

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._next_id,
        }
        body, headers = self._post(payload)
        if "mcp-session-id" in headers:
            self.session_id = headers["mcp-session-id"]
        if body is None:
            raise MonolynxError(f"Empty response to {method}")
        if "error" in body:
            raise MonolynxError(f"{method}: {body['error']}")
        return body.get("result")

    def initialize(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sync_graph", "version": "1.0"},
            },
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.session_id is None:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = (result or {}).get("content") or []
        text = content[0].get("text", "") if content else ""
        if (result or {}).get("isError"):
            raise MonolynxError(f"{name}: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def replace_graph(self, nodes: list[dict], edges: list[dict], clear_first: bool = True) -> Any:
        return self.call_tool(
            "replace_graph",
            {
                "project_slug": self.project_slug,
                "nodes": nodes,
                "edges": edges,
                "clear_first": clear_first,
            },
        )


def _skip_node(node: dict[str, Any]) -> bool:
    return (
        node.get("file_type") == "rationale"
        or not node.get("source_file")
        or node.get("type") == "package"
    )


def _infer_type(node: dict[str, Any], method_sources: set[str], method_targets: set[str]) -> str:
    node_id = node["id"]
    label = str(node.get("label") or node_id)
    if label.endswith(FILE_EXTENSIONS):
        return "File"
    if node_id in method_targets or label.startswith("."):
        return "Method"
    if node_id in method_sources:
        return "Class"
    if label.endswith("()"):
        return "Function"
    if CAMEL_CASE.match(label):
        return "Class"
    return "Const"


def _clean_name(label: str) -> str:
    if label.endswith("()"):
        label = label[:-2]
    return label.lstrip(".")


def _line_number(location: Any) -> int | None:
    if location is None:
        return None
    match = re.match(r"^L?(\d+)", str(location))
    return int(match.group(1)) if match else None


def map_graph(graph: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Map graphify node-link JSON onto Monolynx nodes and edges."""
    raw_nodes = graph.get("nodes", [])
    raw_links = graph.get("links", [])

    method_sources: set[str] = set()
    method_targets: set[str] = set()
    for link in raw_links:
        if link.get("relation") == "method":
            method_sources.add(str(link.get("source")))
            method_targets.add(str(link.get("target")))

    nodes: list[dict] = []
    kept_ids: set[str] = set()
    for raw in raw_nodes:
        if "id" not in raw or _skip_node(raw):
            continue
        raw = {**raw, "id": str(raw["id"])}
        node: dict[str, Any] = {
            "id": raw["id"],
            "type": _infer_type(raw, method_sources, method_targets),
            "name": _clean_name(str(raw.get("label") or raw["id"])),
            "file_path": raw.get("source_file"),
        }
        line = _line_number(raw.get("source_location"))
        if line is not None:
            node["line_number"] = line
        if raw.get("community") is not None:
            node["metadata"] = {"community": raw["community"]}
        nodes.append(node)
        kept_ids.add(raw["id"])

    edges: list[dict] = []
    for link in raw_links:
        relation = link.get("relation")
        edge_type = RELATION_TO_EDGE_TYPE.get(relation)
        if edge_type is None:
            continue
        source, target = str(link.get("source")), str(link.get("target"))
        # Endpoint dropped by the node filter (external symbol, rationale) - skip the edge.
        if source not in kept_ids or target not in kept_ids:
            continue
        edges.append(
            {
                "source_id": source,
                "target_id": target,
                "type": edge_type,
                "metadata": {
                    "source_relation": relation,
                    "confidence": link.get("confidence", "EXTRACTED"),
                },
            }
        )
    return nodes, edges


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync graphify code graph to Monolynx")
    parser.add_argument("--monolynx-url", default=os.environ.get("MONOLYNX_URL") or DEFAULT_URL)
    parser.add_argument("--token", default=os.environ.get("MONOLYNX_GRAPH_TOKEN"))
    parser.add_argument("--project-slug", default=os.environ.get("MONOLYNX_PROJECT_SLUG"))
    parser.add_argument("--graph-json", default="graphify-out/graph.json")
    parser.add_argument("--dry-run", action="store_true", help="map and count only, send nothing")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    graph_path = Path(args.graph_json)
    if not graph_path.is_file():
        log.error("Missing %s - run graphify update .", graph_path)
        return 0

    if not args.project_slug:
        log.error("Missing project slug - set MONOLYNX_PROJECT_SLUG or pass --project-slug")
        return 1

    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot read %s: %s", graph_path, exc)
        return 1

    nodes, edges = map_graph(graph)
    log.info("Mapped %d nodes and %d edges from %s", len(nodes), len(edges), graph_path)
    if args.verbose:
        by_type: dict[str, int] = {}
        for node in nodes:
            by_type[node["type"]] = by_type.get(node["type"], 0) + 1
        for edge in edges:
            by_type[edge["type"]] = by_type.get(edge["type"], 0) + 1
        for key in sorted(by_type):
            log.debug("  %-10s %d", key, by_type[key])

    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        log.error(
            "Payload too large (%d nodes / %d edges, limits %d / %d) - tighten .graphifyignore",
            len(nodes), len(edges), MAX_NODES, MAX_EDGES,
        )
        return 1

    if args.dry_run:
        log.info("Dry run - nothing sent")
        return 0

    if not args.token:
        log.error("Missing MONOLYNX_GRAPH_TOKEN - skipping sync")
        return 0

    client = MonolynxClient(args.monolynx_url, args.token, args.project_slug)
    try:
        result = client.replace_graph(nodes, edges, clear_first=True)
    except MonolynxError as exc:
        log.error("replace_graph failed: %s", exc)
        return 1
    log.info("replace_graph OK: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
