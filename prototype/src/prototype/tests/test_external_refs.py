"""4.1.2.1: external $ref resolution at load time.

External http(s) $refs are fetched once through the bounded download path,
cached per document, expanded at load time and bounded (document limit,
recursion budgets). Render/repair paths operate on the already-normalized
document and never touch the network (covered in test_generator_context).

Servers that reference themselves by URL need their own port, so the
self-referential scenarios run a local ThreadingHTTPServer inline; the
parameterized fixture covers documents without self-references.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from prototype.parser.contract import (
    ContractInputError,
    load_contract_document,
    save_contract_snapshot,
)


def _spec(paths=None):
    return {"openapi": "3.0.3", "info": {"title": "T", "version": "1"},
            "paths": paths or {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}}}


def _write_contract(tmp_path: Path, spec, name="contract.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _ref_schema_spec(base: str, ref: str):
    return _spec({"/items": {"get": {"responses": {"200": {"description": "OK", "content": {
        "application/json": {"schema": {"$ref": ref}}}}}}}})


@pytest.fixture
def ext_server(request):
    """Serve the docs from request.param; yield (base_url, request list)."""
    docs = getattr(request, "param", {})
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            entry = docs.get(self.path)
            if entry is None:
                status, payload = 404, b"missing"
            else:
                status, payload = entry
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _self_referencing_server(docs_for_port):
    """Start a server whose docs embed their own port; return (server, thread, base)."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, payload = docs_for_port(self.server.server_port).get(self.path, (404, b"missing"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}", requests


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


SCHEMA_YAML = ("type: object\nrequired: [id]\nproperties:\n  id: {type: string}\n"
               "  qty: {type: integer, minimum: 1}\n")


@pytest.mark.parametrize("ext_server", [{
    "/items.yaml": (200, SCHEMA_YAML.encode()),
}], indirect=True)
def test_external_yaml_ref_whole_doc_and_pointer_share_one_fetch(ext_server, tmp_path):
    base, requests = ext_server
    spec = _spec({"/items": {"get": {
        "parameters": [{"name": "qty", "in": "query",
                        "schema": {"$ref": base + "/items.yaml#/properties/qty"}}],
        "responses": {"200": {"description": "OK", "content": {
            "application/json": {"schema": {"$ref": base + "/items.yaml"}}}}}}}})
    document = load_contract_document(_write_contract(tmp_path, spec))
    schema = document.data["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["id"]
    assert schema["properties"]["qty"] == {"type": "integer", "minimum": 1}
    parameter = document.data["paths"]["/items"]["get"]["parameters"][0]
    assert parameter["schema"] == {"type": "integer", "minimum": 1}
    # One distinct external document: fetched once despite two refs.
    assert requests == ["/items.yaml"]
    assert document.external_refs == (base + "/items.yaml",)


def test_external_ref_chain_is_fully_resolved(tmp_path):
    def docs_for_port(port):
        return {
            "/a.yaml": (200, json.dumps({"schemas": {"Outer": {"type": "object",
                "properties": {"inner": {"$ref": f"http://127.0.0.1:{port}/b.yaml#/schemas/Inner"}}}}}).encode()),
            "/b.yaml": (200, json.dumps({"schemas": {"Inner": {"type": "object",
                "properties": {"leaf": {"$ref": f"http://127.0.0.1:{port}/c.yaml#/schemas/Leaf"}}}}}).encode()),
            "/c.yaml": (200, json.dumps({"schemas": {"Leaf": {"type": "string"}}}).encode()),
        }

    server, thread, base, requests = _self_referencing_server(docs_for_port)
    try:
        document = load_contract_document(_write_contract(tmp_path, _ref_schema_spec(base, base + "/a.yaml#/schemas/Outer")))
        schema = document.data["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["properties"]["inner"]["properties"]["leaf"] == {"type": "string"}
        assert requests == ["/a.yaml", "/b.yaml", "/c.yaml"]
        assert document.external_refs == tuple(base + p for p in ("/a.yaml", "/b.yaml", "/c.yaml"))
    finally:
        _stop(server, thread)


def test_external_ref_cycle_keeps_the_marker_without_hanging(tmp_path):
    def docs_for_port(port):
        return {
            "/a.yaml": (200, json.dumps({"schemas": {"A": {"type": "object",
                "properties": {"b": {"$ref": f"http://127.0.0.1:{port}/b.yaml#/schemas/B"}}}}}).encode()),
            "/b.yaml": (200, json.dumps({"schemas": {"B": {"type": "object",
                "properties": {"a": {"$ref": f"http://127.0.0.1:{port}/a.yaml#/schemas/A"}}}}}).encode()),
        }

    server, thread, base, requests = _self_referencing_server(docs_for_port)
    try:
        document = load_contract_document(_write_contract(tmp_path, _ref_schema_spec(base, base + "/a.yaml#/schemas/A")))
        schema = document.data["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["properties"]["b"]["properties"]["a"] == {"$ref": base + "/a.yaml#/schemas/A"}
        # Each document fetched exactly once despite the cycle.
        assert requests == ["/a.yaml", "/b.yaml"]
    finally:
        _stop(server, thread)


@pytest.mark.parametrize("ref", ["file:///etc/passwd", "ftp://example.com/items.yaml", "gopher://example.com/x"])
def test_non_http_external_ref_is_rejected_without_network(tmp_path, ref):
    with pytest.raises(ContractInputError, match=r"HTTP\(S\) URL"):
        load_contract_document(_write_contract(tmp_path, _ref_schema_spec("", ref)))


def test_unavailable_external_ref_is_an_input_error(ext_server, tmp_path):
    base, _ = ext_server
    with pytest.raises(ContractInputError, match="HTTP"):
        load_contract_document(_write_contract(tmp_path, _ref_schema_spec(base, base + "/missing.yaml")))


def test_connection_refused_external_ref_is_an_input_error(tmp_path):
    with pytest.raises(ContractInputError):
        load_contract_document(
            _write_contract(tmp_path, _ref_schema_spec("", "http://127.0.0.1:1/items.yaml")))


@pytest.mark.parametrize("ext_server", [{
    "/bad.yaml": (200, b"[1, 2]"),
}], indirect=True)
def test_external_ref_document_must_be_a_json_object(ext_server, tmp_path):
    base, _ = ext_server
    with pytest.raises(ContractInputError, match="объект"):
        load_contract_document(_write_contract(tmp_path, _ref_schema_spec(base, base + "/bad.yaml")))


def test_document_limit_stops_expansion(tmp_path):
    def docs_for_port(port):
        return {f"/d{index}.yaml": (200, b'{"type": "string"}') for index in range(20)}

    server, thread, base, requests = _self_referencing_server(docs_for_port)
    try:
        paths = {f"/p{index}": {"get": {"responses": {"200": {"description": "OK", "content": {
            "application/json": {"schema": {"$ref": base + f"/d{index}.yaml"}}}}}}}
            for index in range(20)}
        with pytest.raises(ContractInputError, match="лимит внешних"):
            load_contract_document(_write_contract(tmp_path, _spec(paths)))
        assert len(requests) <= 17  # bounded before the error
    finally:
        _stop(server, thread)


@pytest.mark.parametrize("ext_server", [{
    "/items.yaml": (200, SCHEMA_YAML.encode()),
}], indirect=True)
def test_snapshot_provenance_records_fetched_external_refs(ext_server, tmp_path):
    base, _ = ext_server
    document = load_contract_document(_write_contract(tmp_path, _ref_schema_spec(base, base + "/items.yaml")))
    snapshot = save_contract_snapshot(document, tmp_path / "snapshot")
    assert snapshot["external_refs"] == [base + "/items.yaml"]
    assert snapshot["sha256"]
