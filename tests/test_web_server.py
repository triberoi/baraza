"""Tests for the socket half of the local server (``baraza.web.server``).

Short file, one real subject: **the tool ships with no login, and that is only correct
while the socket cannot be reached from off the machine.** Everything here defends
that, including the absence of a way to change it — a `--host` flag added later to
make it work in a container would turn a private guest list into a LAN service, and
the code would still look fine.
"""

import inspect
import socket

import pytest

from baraza.web import server


def test_the_bound_address_is_loopback() -> None:
    sock = server.bind()
    try:
        host, port = sock.getsockname()[:2]
        assert host == server.LOOPBACK == "127.0.0.1"
        assert port != 0, "the kernel must have assigned a real port"
    finally:
        sock.close()


def test_there_is_no_way_to_ask_for_another_address() -> None:
    """The boundary is the absence of the parameter, so the absence is the assertion.
    A host argument would have exactly one safe value, and a parameter with one safe
    value is one somebody eventually widens.

    Swept over **everything this module publishes** rather than a written-down pair. It
    used to name `bind` and `serve`; `serve` has since gone (nothing
    called it), and a list of names is a list that goes stale exactly when the surface
    changes, which is the moment this check matters most.
    """
    for name in server.__all__:
        published = getattr(server, name)
        if not callable(published):
            continue
        parameters = inspect.signature(published).parameters
        assert "host" not in parameters, f"{name}() grew a host parameter — see the module docstring"
        assert "interface" not in parameters and "address" not in parameters


def test_the_module_names_no_address_but_loopback() -> None:
    """Catches the other shape of the same mistake: a literal `0.0.0.0` or an empty
    host passed straight to `bind()`, which binds every interface on every platform."""
    source = inspect.getsource(server)
    for wildcard in ("0.0.0.0", '"::"', "'::'"):
        assert wildcard not in source, f"{wildcard} appears in baraza.web.server — it must never bind a wildcard"


def test_the_url_is_read_back_from_the_socket() -> None:
    """With an ephemeral port there is nothing to assume, and the caller needs the real
    one *before* serving starts so it can open a browser without racing startup."""
    sock = server.bind()
    try:
        url = server.url_for(sock)
        assert url == f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


def test_two_servers_do_not_collide_on_a_port() -> None:
    """Why the port is ephemeral: an organizer running this twice, or running anything
    else on a fixed port, must not get a crash they cannot interpret."""
    first, second = server.bind(), server.bind()
    try:
        assert first.getsockname()[1] != second.getsockname()[1]
    finally:
        first.close()
        second.close()


def test_the_socket_really_is_unreachable_from_a_non_loopback_address() -> None:
    """The property itself rather than the string: connecting to the bound port on this
    machine's own routable address must fail. Asserted last because it is the one that
    would still catch a mistake if every check above were edited away."""
    sock = server.bind()
    port = sock.getsockname()[1]
    routable = socket.gethostbyname(socket.gethostname())
    if routable.startswith("127."):
        pytest.skip("this machine resolves its own hostname to loopback — nothing to prove against")
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        with pytest.raises(OSError):
            probe.connect((routable, port))
        probe.close()
    finally:
        sock.close()
