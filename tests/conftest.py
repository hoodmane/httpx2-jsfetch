from __future__ import annotations

import asyncio
import socket
import threading
import time
import typing
from pathlib import Path

import httpx2
import pytest
from pytest_pyodide.runner import SeleniumChromeRunner

Message = typing.Dict[str, typing.Any]
Receive = typing.Callable[[], typing.Awaitable[Message]]
Send = typing.Callable[[typing.Dict[str, typing.Any]], typing.Coroutine[None, None, None]]
Scope = typing.Dict[str, typing.Any]

# The test server is always on a different origin than the page that Pyodide is
# loaded from, so every response has to opt in to cross origin requests.
CORS_HEADERS: list[list[bytes]] = [
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"PUT, GET, HEAD, POST, DELETE, OPTIONS"],
    [b"access-control-allow-headers", b"*"],
]


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    assert scope["type"] == "http"
    if scope["path"].startswith("/slow_response"):
        await slow_response(scope, receive, send)
    elif scope["path"].startswith("/status"):
        await status_code(scope, receive, send)
    elif scope["path"].startswith("/echo_body"):
        await echo_body(scope, receive, send)
    elif scope["path"].startswith("/wheel_download"):
        await wheel_download(scope, receive, send)
    else:
        await hello_world(scope, receive, send)


async def hello_world(scope: Scope, receive: Receive, send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/plain"], *CORS_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": b"Hello, world!"})


# Serving the wheels from `dist/` lets us install them with micropip, which is
# also the only way to get them into a web worker.
async def wheel_download(scope: Scope, receive: Receive, send: Send) -> None:
    wheel_file = Path("dist") / scope["path"].rpartition("/")[2]
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/x-wheel"], *CORS_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": wheel_file.read_bytes()})


async def slow_response(scope: Scope, receive: Receive, send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/plain"],
                *CORS_HEADERS,
                # Don't let the runtime serve a cached response, it would never
                # time out.
                [b"cache-control", b"no-store,private,no-cache,must-revalidate"],
            ],
        }
    )
    await asyncio.sleep(1.0)  # Allow triggering a read timeout.
    await send({"type": "http.response.body", "body": b"Hello, world!"})


async def status_code(scope: Scope, receive: Receive, send: Send) -> None:
    status_code = int(scope["path"].replace("/status/", ""))
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [[b"content-type", b"text/plain"], *CORS_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": b"Hello, world!"})


async def echo_body(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    more_body = True

    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/octet-stream"], *CORS_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": body})


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def server_url() -> typing.Iterator[httpx2.URL]:
    """Run the test app in a background thread and yield its base URL."""
    from uvicorn.config import Config
    from uvicorn.server import Server

    class TestServer(Server):
        def install_signal_handlers(self) -> None:
            # Signal handlers can only be installed from the main thread.
            pass  # pragma: no cover

    host, port = "127.0.0.1", free_tcp_port()
    server = TestServer(config=Config(app=app, lifespan="off", loop="asyncio", host=host, port=port))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        while not server.started:
            time.sleep(1e-3)
        yield httpx2.URL(f"http://{host}:{port}/")
    finally:
        server.should_exit = True
        thread.join()


@pytest.fixture(scope="session")
def wheel_urls(server_url: httpx2.URL) -> list[str]:
    """URLs of the wheels that the tests install with micropip.

    `httpx2_jsfetch` comes first so that micropip has it in hand before it
    resolves the `httpx2-jsfetch` requirement of the `httpx2` wheel.
    """
    wheels = sorted(Path("dist").glob("httpx2_jsfetch-*.whl")) + sorted(Path("dist").glob("httpx2-*.whl"))
    assert len(wheels) == 2, f"expected an httpx2 and an httpx2_jsfetch wheel in dist/, found {wheels}"
    return [str(server_url.copy_with(path=f"/wheel_download/{wheel.name}")) for wheel in wheels]


def _patch_javascript_setup(
    orig: typing.Callable[[SeleniumChromeRunner], None],
) -> typing.Callable[[SeleniumChromeRunner], None]:
    """Remove WebAssembly.Suspending when jspi is False

    Pyodide uses WebAssembly.Suspending to feature detect JSPI. Removing it
    ensures that we actually use the no-JSPI code path when self.jspi is False.
    """

    def javascript_setup(self: SeleniumChromeRunner) -> None:
        orig(self)
        if not self.jspi:
            self.run_js(
                "delete WebAssembly.Suspending;",
                pyodide_checks=False,
            )

    return javascript_setup


SeleniumChromeRunner.javascript_setup = _patch_javascript_setup(SeleniumChromeRunner.javascript_setup)


def _runner(
    request: pytest.FixtureRequest,
    has_jspi: bool,
    wheel_urls: list[str],
    is_worker: bool,
) -> SeleniumChromeRunner:
    fixture_name = "selenium_jspi" if has_jspi else "selenium"
    if is_worker:
        fixture_name += "_worker"
    runner = request.getfixturevalue(fixture_name)
    # `h2` is installed so that `httpx2.Client(http2=True)` gets far enough to
    # warn that HTTP/2 isn't supported on Emscripten.
    requirements = [*wheel_urls, "h2"]
    runner.run_js(
        f"""
        await pyodide.loadPackage("micropip");
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install({requirements!r})
        `);
        """
    )
    return runner


@pytest.fixture
def selenium_runner(
    request: pytest.FixtureRequest,
    runtime: str,
    has_jspi: bool,
    wheel_urls: list[str],
) -> typing.Any:
    return _runner(request, has_jspi, wheel_urls, is_worker=False)


@pytest.fixture
def selenium_worker_runner(
    request: pytest.FixtureRequest,
    runtime: str,
    has_jspi: bool,
    wheel_urls: list[str],
) -> typing.Any:
    return _runner(request, has_jspi, wheel_urls, is_worker=True)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Generate WebAssembly JavaScript Promise Integration based tests
    only for platforms that support it.

    Currently:
    1) NodeJS requires JSPI because it doesn't support XMLHttpRequest
    2) Firefox doesn't support JSPI
    3) Chrome supports JSPI on or off.
    """
    if "has_jspi" in metafunc.fixturenames:  # pragma: no cover
        if metafunc.config.getoption("--runtime").startswith("node"):
            metafunc.parametrize("has_jspi", [True])
        elif metafunc.config.getoption("--runtime").startswith("firefox"):
            metafunc.parametrize("has_jspi", [False])
        else:
            metafunc.parametrize("has_jspi", [True, False])
