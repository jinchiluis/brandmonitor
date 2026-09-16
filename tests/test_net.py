"""The preflight that decides whether a scheduled run starts at all."""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.net import OFFLINE_EXIT, PROBE_TARGETS, ROUTE_TARGET, check_online


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def wire(monkeypatch, *, resolves: bool, connects) -> list[str]:
    """Install a fake stack and return the hosts it was asked to connect to.

    ``connects`` is either a bool for every host or a set of hosts that answer.
    """
    asked: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        if not resolves:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    def create_connection(address, timeout=None, *args, **kwargs):
        host = address[0]
        asked.append(host)
        ok = connects if isinstance(connects, bool) else host in connects
        if not ok:
            raise OSError(10060, "connection attempt failed")
        return FakeSocket()

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return asked


def test_one_endpoint_answering_is_enough(monkeypatch):
    """The question is whether this host has internet, not whether all three are up."""
    asked = wire(monkeypatch, resolves=True, connects={PROBE_TARGETS[0][0]})

    status = check_online(timeout=0.01)

    assert status.online
    assert asked == [PROBE_TARGETS[0][0]], "stops at the first success"


def test_a_total_outage_reports_no_route_and_no_resolution(monkeypatch):
    wire(monkeypatch, resolves=False, connects=False)

    status = check_online(timeout=0.01)

    assert not status.online
    assert (status.resolved, status.routed) == (False, False)
    assert "no route and no resolution" in status.detail


def test_dns_failing_alone_is_named_as_such(monkeypatch):
    """The partial outage a single ping would call healthy.

    Routing works, so packets leave the machine; nothing resolves, so no crawl
    can start. Saying which half failed is the point of probing both.
    """
    wire(monkeypatch, resolves=False, connects={ROUTE_TARGET[0]})

    status = check_online(timeout=0.01)

    assert not status.online and status.routed
    assert "routing works" in status.detail
    assert "name lookup failed" in status.detail


def test_names_resolving_while_nothing_connects_is_named_separately(monkeypatch):
    """A captive portal or a firewall, not a dead link.

    Found by running the batch file against an unroutable address: `resolved`
    used to be hardcoded False here, so this reported "no name resolved" while
    every name had in fact resolved.
    """
    wire(monkeypatch, resolves=True, connects=False)

    status = check_online(timeout=0.01)

    assert not status.online
    assert (status.resolved, status.routed) == (True, False)
    assert "names resolve but nothing connects" in status.detail


def test_every_probe_target_is_tried_before_giving_up(monkeypatch):
    asked = wire(monkeypatch, resolves=True, connects={ROUTE_TARGET[0]})

    check_online(timeout=0.01)

    assert asked[:len(PROBE_TARGETS)] == [host for host, _ in PROBE_TARGETS]


@pytest.mark.parametrize("online,expected", [(True, 0), (False, OFFLINE_EXIT)])
def test_netcheck_exits_four_only_when_offline(monkeypatch, capsys, online, expected):
    """The exit code the batch files branch on, so it is worth pinning."""
    import run as entry

    wire(monkeypatch, resolves=online, connects=online)
    args = entry.build_parser().parse_args(["netcheck", "--timeout", "0.01"])

    assert args.func(args) == expected
    assert ("online" if online else "OFFLINE") in capsys.readouterr().out
