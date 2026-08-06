"""Process-local outbound network guard for frozen research replay."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import requests


class ReleaseNetworkError(RuntimeError):
    """A sanitized signal that offline research attempted network access."""

    def __init__(self, code: str = "network_access_forbidden"):
        self.code = code
        super().__init__(code)


@contextmanager
def offline_network_guard() -> Iterator[None]:
    """Block process-local socket, DNS, and Requests exits."""

    def blocked(*_args, **_kwargs):
        raise ReleaseNetworkError()

    with ExitStack() as stack:
        for method_name in (
            "connect",
            "connect_ex",
            "send",
            "sendall",
            "sendfile",
            "sendmsg",
            "sendto",
        ):
            if hasattr(socket.socket, method_name):
                stack.enter_context(
                    patch.object(socket.socket, method_name, blocked)
                )
        for resolver_name in (
            "getaddrinfo",
            "gethostbyaddr",
            "gethostbyname",
            "gethostbyname_ex",
            "getnameinfo",
        ):
            stack.enter_context(patch.object(socket, resolver_name, blocked))
        stack.enter_context(patch.object(socket, "create_connection", blocked))
        stack.enter_context(patch.object(requests.Session, "request", blocked))
        yield
