import asyncio
import warnings

from http.cookies import SimpleCookie
from inspect import isawaitable
from typing import Any, Awaitable, Callable, MutableMapping, Union
from urllib.parse import quote

from multidict import CIMultiDict

from sanic.exceptions import InvalidUsage, ServerError
from sanic.log import logger
from sanic.request import Request
from sanic.response import HTTPResponse, StreamingHTTPResponse
from sanic.server import StreamBuffer
from sanic.websocket import WebSocketConnection


ASGIScope = MutableMapping[str, Any]
ASGIMessage = MutableMapping[str, Any]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]


class MockProtocol:
    def __init__(self, transport: "MockTransport", loop):
        self.transport = transport
        self._not_paused = asyncio.Event(loop=loop)
        self._not_paused.set()
        self._complete = asyncio.Event(loop=loop)

    def pause_writing(self) -> None:
        self._not_paused.clear()

    def resume_writing(self) -> None:
        self._not_paused.set()

    async def complete(self) -> None:
        self._not_paused.set()
        await self.transport.send(
            {"type": "http.response.body", "body": b"", "more_body": False}
        )

    @property
    def is_complete(self) -> bool:
        return self._complete.is_set()

    async def push_data(self, data: bytes) -> None:
        if not self.is_complete:
            await self.transport.send(
                {"type": "http.response.body", "body": data, "more_body": True}
            )

    async def drain(self) -> None:
        await self._not_paused.wait()


class MockTransport:
    def __init__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self._protocol = None
        self.loop = None

    def get_protocol(self) -> MockProtocol:
        if not self._protocol:
            self._protocol = MockProtocol(self, self.loop)
        return self._protocol

    def get_extra_info(self, info: str) -> Union[str, bool]:
        if info == "peername":
            return self.scope.get("server")
        elif info == "sslcontext":
            return self.scope.get("scheme") in ["https", "wss"]

    def get_websocket_connection(self) -> WebSocketConnection:
        try:
            return self._websocket_connection
        except AttributeError:
            raise InvalidUsage("Improper websocket connection.")

    def create_websocket_connection(
        self, send: ASGISend, receive: ASGIReceive
    ) -> WebSocketConnection:
        self._websocket_connection = WebSocketConnection(send, receive)
        return self._websocket_connection

    def add_task(self) -> None:
        raise NotImplementedError

    async def send(self, data) -> None:
        # TODO:
        # - Validation on data and that it is formatted properly and is valid
        await self._send(data)

    async def receive(self) -> ASGIMessage:
        return await self._receive()


class Lifespan:
    def __init__(self, asgi_app: "ASGIApp") -> None:
        self.asgi_app = asgi_app

        if "before_server_start" in self.asgi_app.sanic_app.listeners:
            warnings.warn(
                'You have set a listener for "before_server_start" '
                "in ASGI mode. "
                "It will be executed as early as possible, but not before "
                "the ASGI server is started."
            )
        if "after_server_stop" in self.asgi_app.sanic_app.listeners:
            warnings.warn(
                'You have set a listener for "after_server_stop" '
                "in ASGI mode. "
                "It will be executed as late as possible, but not after "
                "the ASGI server is stopped."
            )

    async def pre_startup(self) -> None:
        for handler in self.asgi_app.sanic_app.listeners[
            "before_server_start"
        ]:
            response = handler(
                self.asgi_app.sanic_app, self.asgi_app.sanic_app.loop
            )
            if isawaitable(response):
                await response

    async def startup(self) -> None:
        for handler in self.asgi_app.sanic_app.listeners[
            "before_server_start"
        ]:
            response = handler(
                self.asgi_app.sanic_app, self.asgi_app.sanic_app.loop
            )
            if isawaitable(response):
                await response

        for handler in self.asgi_app.sanic_app.listeners["after_server_start"]:
            response = handler(
                self.asgi_app.sanic_app, self.asgi_app.sanic_app.loop
            )
            if isawaitable(response):
                await response

    async def shutdown(self) -> None:
        for handler in self.asgi_app.sanic_app.listeners["before_server_stop"]:
            response = handler(
                self.asgi_app.sanic_app, self.asgi_app.sanic_app.loop
            )
            if isawaitable(response):
                await response

        for handler in self.asgi_app.sanic_app.listeners["after_server_stop"]:
            response = handler(
                self.asgi_app.sanic_app, self.asgi_app.sanic_app.loop
            )
            if isawaitable(response):
                await response

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await self.startup()
            await send({"type": "lifespan.startup.complete"})

        message = await receive()
        if message["type"] == "lifespan.shutdown":
            await self.shutdown()
            await send({"type": "lifespan.shutdown.complete"})


class ASGIApp:
    def __init__(self) -> None:
        self.ws = None

    @classmethod
    async def create(
        cls, sanic_app, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> "ASGIApp":
        instance = cls()
        instance.sanic_app = sanic_app
        instance.transport = MockTransport(scope, receive, send)
        instance.transport.add_task = sanic_app.loop.create_task
        instance.transport.loop = sanic_app.loop

        headers = CIMultiDict(
            [
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in scope.get("headers", [])
            ]
        )
        instance.do_stream = (
            True if headers.get("expect") == "100-continue" else False
        )
        instance.lifespan = Lifespan(instance)

        if scope["type"] == "lifespan":
            await instance.lifespan(scope, receive, send)
        else:
            url_bytes = scope.get("root_path", "") + quote(scope["path"])
            url_bytes = url_bytes.encode("latin-1")
            url_bytes += b"?" + scope["query_string"]

            if scope["type"] == "http":
                version = scope["http_version"]
                method = scope["method"]
            elif scope["type"] == "websocket":
                version = "1.1"
                method = "GET"

                instance.ws = instance.transport.create_websocket_connection(
                    send, receive
                )
                await instance.ws.accept()
            else:
                pass
                # TODO:
                # - close connection

            instance.request = Request(
                url_bytes,
                headers,
                version,
                method,
                instance.transport,
                sanic_app,
            )

            if sanic_app.is_request_stream:
                instance.request.stream = StreamBuffer()

        return instance

    async def read_body(self) -> bytes:
        """
        Read and return the entire body from an incoming ASGI message.
        """
        body = b""
        more_body = True
        while more_body:
            message = await self.transport.receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        return body

    async def stream_body(self) -> None:
        """
        Read and stream the body in chunks from an incoming ASGI message.
        """
        more_body = True

        while more_body:
            message = await self.transport.receive()
            chunk = message.get("body", b"")
            await self.request.stream.put(chunk)

            more_body = message.get("more_body", False)

        await self.request.stream.put(None)

    async def __call__(self) -> None:
        """
        Handle the incoming request.
        """
        if not self.do_stream:
            self.request.body = await self.read_body()
        else:
            self.sanic_app.loop.create_task(self.stream_body())

        handler = self.sanic_app.handle_request
        callback = None if self.ws else self.stream_callback
        await handler(self.request, None, callback)

    async def stream_callback(self, response: HTTPResponse) -> None:
        """
        Write the response.
        """

        try:
            headers = [
                (str(name).encode("latin-1"), str(value).encode("latin-1"))
                for name, value in response.headers.items()
            ]
        except AttributeError:
            logger.error(
                "Invalid response object for url %s, "
                "Expected Type: HTTPResponse, Actual Type: %s",
                self.request.url,
                type(response),
            )
            exception = ServerError("Invalid response type")
            response = self.sanic_app.error_handler.response(
                self.request, exception
            )
            headers = [
                (str(name).encode("latin-1"), str(value).encode("latin-1"))
                for name, value in response.headers.items()
                if name not in (b"Set-Cookie",)
            ]

        if "content-length" not in response.headers and not isinstance(
            response, StreamingHTTPResponse
        ):
            headers += [
                (b"content-length", str(len(response.body)).encode("latin-1"))
            ]

        if response.cookies:
            cookies = SimpleCookie()
            cookies.load(response.cookies)
            headers += [
                (b"set-cookie", cookie.encode("utf-8"))
                for name, cookie in response.cookies.items()
            ]

        await self.transport.send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": headers,
            }
        )

        if isinstance(response, StreamingHTTPResponse):
            response.protocol = self.transport.get_protocol()
            await response.stream()
            await response.protocol.complete()

        else:
            await self.transport.send(
                {
                    "type": "http.response.body",
                    "body": response.body,
                    "more_body": False,
                }
            )
