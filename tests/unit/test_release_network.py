from __future__ import annotations

import socket

import pytest
import requests

from aquant.release_network import (
    ReleaseNetworkError,
    offline_network_guard,
)


def _assert_forbidden(operation) -> None:
    with pytest.raises(ReleaseNetworkError) as error:
        operation()
    assert error.value.code == "network_access_forbidden"
    assert str(error.value) == "network_access_forbidden"


def test_guard_blocks_socket_connect_and_connect_ex():
    connection = socket.socket()
    try:
        with offline_network_guard():
            _assert_forbidden(
                lambda: connection.connect(("127.0.0.1", 9))
            )
            _assert_forbidden(
                lambda: connection.connect_ex(("127.0.0.1", 9))
            )
    finally:
        connection.close()


def test_guard_blocks_socket_factory_and_requests_without_leaking_target():
    with offline_network_guard():
        _assert_forbidden(
            lambda: socket.create_connection(("secret.invalid", 443))
        )
        _assert_forbidden(
            lambda: requests.Session().request(
                "GET",
                "https://secret.invalid/private?token=value",
            )
        )


def test_guard_restores_original_network_callables():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_request = requests.Session.request

    with offline_network_guard():
        assert socket.socket.connect is not original_connect
        assert socket.socket.connect_ex is not original_connect_ex
        assert socket.create_connection is not original_create_connection
        assert requests.Session.request is not original_request

    assert socket.socket.connect is original_connect
    assert socket.socket.connect_ex is original_connect_ex
    assert socket.create_connection is original_create_connection
    assert requests.Session.request is original_request


def test_nested_guards_restore_the_outer_guard_before_originals():
    original_create_connection = socket.create_connection

    with offline_network_guard():
        outer = socket.create_connection
        with offline_network_guard():
            inner = socket.create_connection
            assert inner is not outer
        assert socket.create_connection is outer
    assert socket.create_connection is original_create_connection
