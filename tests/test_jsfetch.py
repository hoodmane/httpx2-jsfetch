from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from httpx2 import URL
from pytest_pyodide.decorator import run_in_pyodide_coverage
from pytest_pyodide.runner import SeleniumChromeRunner


def run_in_pyodide(func: Callable[..., Any]) -> Callable[..., Any]:
    args = {"include": ["*/httpx2_jsfetch/*", "*/tests/*"]}
    return run_in_pyodide_coverage(coverage_args=args)(func)  # type: ignore[no-any-return]


@pytest.fixture
def timeout_url(server_url: URL, request: pytest.FixtureRequest) -> URL:
    # The query string keeps each parametrization from hitting a cached response.
    return server_url.copy_with(path="/slow_response", query=request.node.callspec.id.encode("UTF-8"))


@run_in_pyodide
def test_transports_are_installed(selenium_runner: SeleniumChromeRunner) -> None:
    import httpx2

    import httpx2_jsfetch

    # On Emscripten httpx2 dispatches to the transports in this package.
    assert httpx2.HTTPTransport is httpx2_jsfetch.JavascriptFetchTransport  # type: ignore[comparison-overlap]
    assert httpx2.AsyncHTTPTransport is httpx2_jsfetch.AsyncJavascriptFetchTransport  # type: ignore[comparison-overlap]


@run_in_pyodide
def test_get(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    response = httpx2.get(server_url)
    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    assert response.text == "Hello, world!"
    assert response.http_version == "HTTP/1.1"


@run_in_pyodide
def test_get_worker(selenium_worker_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    response = httpx2.get(server_url)
    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    assert response.text == "Hello, world!"


@run_in_pyodide
def test_post(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    response = httpx2.post(server_url.copy_with(path="/echo_body"), content=b"Hello, world!")
    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    assert response.content == b"Hello, world!"


@run_in_pyodide
def test_get_status_code(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    response = httpx2.get(server_url.copy_with(path="/status/404"))
    assert response.status_code == 404


@run_in_pyodide
def test_stream_response(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    with httpx2.stream("GET", server_url) as response:
        chunks = list(response.iter_bytes())
    assert b"".join(chunks) == b"Hello, world!"


@run_in_pyodide
async def test_async_get(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    async with httpx2.AsyncClient() as client:
        response = await client.get(server_url)
        assert response.status_code == 200
        assert response.text == "Hello, world!"
        assert response.http_version == "HTTP/1.1"
        assert response.headers
        assert repr(response) == "<Response [200 OK]>"


@run_in_pyodide
async def test_async_post_json(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    async with httpx2.AsyncClient() as client:
        response = await client.post(server_url.copy_with(path="/echo_body"), json={"text": "Hello, world!"})
        assert response.status_code == 200
        assert response.json() == {"text": "Hello, world!"}


@run_in_pyodide
async def test_async_stream_response(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2

    async with httpx2.AsyncClient() as client:
        async with client.stream("GET", server_url) as response:
            chunks = [chunk async for chunk in response.aiter_bytes()]
    assert b"".join(chunks) == b"Hello, world!"


@run_in_pyodide
def test_sync_get_timeout(selenium_runner: SeleniumChromeRunner, has_jspi: bool, timeout_url: URL) -> None:
    import httpx2
    import pytest

    if not has_jspi:
        # Requires JSPI b/c otherwise if we are using XMLHttpRequest in a main
        # browser thread then this will never timeout, or at least it will use the
        # default browser timeout which is VERY long!
        pytest.skip("Requires JSPI")

    with pytest.raises(httpx2.TimeoutException):
        httpx2.get(timeout_url, timeout=0.1)


@run_in_pyodide
def test_sync_get_timeout_worker(selenium_worker_runner: SeleniumChromeRunner, timeout_url: URL) -> None:
    import httpx2
    import pytest

    with pytest.raises(httpx2.TimeoutException):
        httpx2.get(timeout_url, timeout=0.1)


@run_in_pyodide
async def test_async_get_timeout(selenium_runner: SeleniumChromeRunner, timeout_url: URL) -> None:
    import httpx2
    import pytest

    async with httpx2.AsyncClient() as client:
        with pytest.raises(httpx2.TimeoutException):
            await client.get(timeout_url, timeout=0.1)


@run_in_pyodide
def test_sync_get_error(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2
    import pytest

    # test connection error
    # 255.255.255.255 should always return an error
    error_url = str(server_url).split(":")[0] + "://255.255.255.255/"
    with pytest.raises(httpx2.ConnectError):
        httpx2.get(error_url)


@run_in_pyodide
async def test_async_get_error(selenium_runner: SeleniumChromeRunner, server_url: URL) -> None:
    import httpx2
    import pytest

    # test connection error
    # 255.255.255.255 should always return an error
    error_url = str(server_url).split(":")[0] + "://255.255.255.255/"
    with pytest.raises(httpx2.ConnectError):
        async with httpx2.AsyncClient(timeout=1.0) as client:
            await client.get(error_url)


@run_in_pyodide
def test_ignored_options_warn(selenium_runner: SeleniumChromeRunner) -> None:
    import warnings

    import httpx2
    import pytest

    # No warning when using defaults.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        httpx2.HTTPTransport()
        httpx2.AsyncHTTPTransport()

    # Each unsupported option should produce a single UserWarning naming it.
    cases: list[tuple[str, Any]] = [
        ("verify", False),
        ("cert", "client.pem"),
        ("http2", True),
        ("proxy", "http://localhost:8080"),
        ("uds", "/tmp/sock"),
        ("local_address", "127.0.0.1"),
        ("retries", 3),
        ("socket_options", []),
    ]
    for key, value in cases:
        match = rf"The following transport option\(s\) are not supported on Emscripten and will be ignored: {key}."
        with pytest.warns(UserWarning, match=match):
            httpx2.HTTPTransport(**{key: value})
        with pytest.warns(UserWarning, match=match):
            httpx2.AsyncHTTPTransport(**{key: value})
        if key in ("verify", "cert", "http2", "proxy"):
            # Warning is also surfaced when constructing a Client.
            with pytest.warns(UserWarning, match=match):
                httpx2.Client(**{key: value})
