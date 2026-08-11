"""HTTP facade tests using a local server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from astrbot.script_runtime.errors import (
    HttpInvalidRequestError,
)
from astrbot.script_runtime.http import ScriptHttpClient, response_json
from astrbot.script_runtime.values import SafeValue


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"price": 3899}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Custom", "yes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b'{"ok": true}'
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server_url():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url
    server.shutdown()
    server.server_close()


def _sv(text: str) -> SafeValue:
    return SafeValue("str", text)


@pytest.mark.asyncio
async def test_get_and_json(server_url):
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 5.0)
    response = await client.request(
        [_sv("GET"), _sv(server_url + "/")],
        {"use_proxy": SafeValue("bool", False)},
    )
    assert response.kind == "http_response"
    value = response.value
    assert value.status == 200
    assert value.headers.get("X-Custom") == "yes"
    assert "price" in value.text
    json_value = response_json(response.value)
    assert json_value.value["price"] == 3899


@pytest.mark.asyncio
async def test_post_json_body(server_url):
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 5.0)
    response = await client.request(
        [_sv("POST"), _sv(server_url + "/")],
        {
            "json": SafeValue("dict", {"a": 1}),
            "use_proxy": SafeValue("bool", False),
        },
    )
    assert response.value.status == 201


@pytest.mark.asyncio
async def test_connection_error_mapped():
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 5.0)
    with pytest.raises(Exception) as excinfo:
        await client.request(
            [_sv("GET"), _sv("http://127.0.0.1:1/")],
            {"use_proxy": SafeValue("bool", False)},
        )
    assert type(excinfo.value).__name__ == "HttpConnectionError"


@pytest.mark.asyncio
async def test_invalid_method():
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 5.0)
    with pytest.raises(HttpInvalidRequestError):
        await client.request([_sv("GET"), _sv("")], {})


@pytest.mark.asyncio
async def test_content_json_data_mutually_exclusive(server_url):
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 5.0)
    with pytest.raises(HttpInvalidRequestError):
        await client.request(
            [_sv("POST"), _sv(server_url + "/")],
            {
                "content": _sv("x"),
                "json": SafeValue("dict", {}),
                "use_proxy": SafeValue("bool", False),
            },
        )


@pytest.mark.asyncio
async def test_timeout_mapped(server_url):
    client = ScriptHttpClient(proxy_snapshot=None, remaining_seconds=lambda: 0.0001)
    with pytest.raises(Exception) as excinfo:
        await client.request(
            [_sv("GET"), _sv(server_url + "/")],
            {"use_proxy": SafeValue("bool", False)},
        )
    assert type(excinfo.value).__name__ in {
        "HttpTimeoutError",
        "HttpConnectionError",
    }
