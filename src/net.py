"""Is this host actually on the internet?

A scheduled run that starts during an outage is worse than one that does not
start. It stores a partial day, spends every stage's timeouts, and - before the
transport rule in ``src/bodies.py`` - burned the body queue's retry budget on
failures that were never the publishers' fault. Nothing is gained by trying:
every collector resumes from its own watermark and every queue is durable, so a
skipped pass is re-covered by the next one.

So the batch files ask this module first and skip the whole run when the answer
is no. There is deliberately no wait-and-retry loop: news collection runs nine
times a day, and a waiter would hold ``data/run.lock`` and make the next slot
exit 3 instead.

Two things have to work before a crawl can, and they fail independently:
resolution and routing. Checking both is what separates "the DNS server is
gone" from "nothing leaves this machine" in the log and the health email - the
partial-outage mode that a single ping would call healthy.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from src.logger import get_logger

logger = get_logger(__name__)

# The exit code the batch files use for "offline, nothing attempted". It extends
# the 0/1/2/3 contract documented in run_daily.bat, and health/check.py reads it
# back out of the run marker as the `offline` status.
OFFLINE_EXIT = 4

# Endpoints that exist to be reached, are independent of each other, and are not
# sources we crawl. Probing a publisher nine times a day for our own diagnostics
# would be rude and would read as bot traffic; these do not care.
PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("one.one.one.one", 443),
    ("dns.google", 443),
    ("www.cloudflare.com", 443),
)

# A literal address needs no resolver, so reaching it while every name lookup
# above failed means the route is fine and DNS is not.
ROUTE_TARGET: tuple[str, int] = ("1.1.1.1", 443)

DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True)
class NetStatus:
    online: bool
    detail: str
    resolved: bool
    routed: bool


def _reach(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    """Probe one endpoint.

    Returns (did the name resolve, why the endpoint was not reached). Resolution
    is reported separately even when the connection then fails: names resolving
    while nothing connects is a different outage - a captive portal, a firewall -
    from no DNS at all, and the operator reads this string.
    """
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"{host}: name lookup failed ({exc.strerror or exc})"
    except OSError as exc:  # noqa: BLE001 - any resolver failure is a failure
        return False, f"{host}: name lookup failed ({type(exc).__name__}: {exc})"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return True, f"{host}:{port}: {type(exc).__name__}: {exc}"


def _routes(timeout: float) -> bool:
    host, port = ROUTE_TARGET
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_online(timeout: float = DEFAULT_TIMEOUT) -> NetStatus:
    """Probe until one endpoint answers; classify the failure when none does.

    One clean resolve-and-connect is enough - the question is whether this host
    has internet at all, not whether every endpoint is up.
    """
    failures: list[str] = []
    resolved = False
    for host, port in PROBE_TARGETS:
        answered, failure = _reach(host, port, timeout)
        resolved = resolved or answered
        if failure is None:
            return NetStatus(True, f"reached {host}:{port}", True, True)
        failures.append(failure)

    routed = _routes(timeout)
    if resolved and routed:
        head = (f"names resolve and {ROUTE_TARGET[0]} answers, but no endpoint accepted "
                "a connection - a captive portal or a blocking firewall")
    elif resolved:
        head = f"names resolve but nothing connects, {ROUTE_TARGET[0]} included"
    elif routed:
        head = f"routing works ({ROUTE_TARGET[0]} answered) but no name resolved - DNS"
    else:
        head = f"no route and no resolution ({ROUTE_TARGET[0]} unreachable)"
    return NetStatus(False, f"{head}: " + "; ".join(failures), resolved, routed)
