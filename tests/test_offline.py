"""PRD §6: the runtime never touches the network (the demo runs with wifi off).

Every socket connection or DNS lookup to anything but loopback is blocked *and recorded* (the brain
swallows some exceptions, so a raise alone could go unnoticed). Then we import every brain/eval module,
run every eval case end to end and router-only, and play the demo rehearsal, all without a model, and
assert nothing tried to leave the machine. The planner is only ever pointed at loopback by default.
"""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import pkgutil
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest

import brain
import eval as eval_pkg
from brain.config import Settings
from eval.cases import load_cases
from eval.e2e import real_brain_factory, run_case_e2e
from eval.rehearse import FixtureStage, rehearse
from eval.runner import Components, run_case
from eval.scoring import score

ROOT = Path(__file__).resolve().parents[1]


def _is_loopback(host: object) -> bool:
    if host is None or host == "":
        return True
    name = str(host).strip("[]").lower()
    if name in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(name.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def network_attempts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Block + record any non-loopback connect / create_connection / getaddrinfo."""
    attempts: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def guard(address: object) -> None:
        host = address[0] if isinstance(address, tuple) and address else None
        if isinstance(address, tuple) and not _is_loopback(host):
            attempts.append(repr(address))
            raise OSError(f"network access blocked by test_offline: {address!r}")

    def connect(self: socket.socket, address: object) -> None:
        guard(address)
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        guard(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    def create_connection(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
        guard(address)
        return real_create(address, *args, **kwargs)  # type: ignore[arg-type]

    def getaddrinfo(host: object, port: object, *args: object, **kwargs: object) -> object:
        guard((host, port))
        return real_gai(host, port, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield attempts


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_guard_blocks_and_records_internet(network_attempts: list[str]) -> None:
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(OSError, match="blocked"), socket.socket() as s:
        s.connect(("93.184.216.34", 80))
    with pytest.raises(OSError, match="blocked"):
        socket.getaddrinfo("pypi.org", 443)
    assert len(network_attempts) == 3


def test_guard_allows_loopback(network_attempts: list[str]) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted = threading.Thread(target=lambda: server.accept()[0].close(), daemon=True)
        accepted.start()
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        accepted.join(timeout=2)
    assert network_attempts == []


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


def _modules(pkg: object) -> list[str]:
    path = getattr(pkg, "__path__", [])
    name = getattr(pkg, "__name__", "")
    return [m.name for m in pkgutil.walk_packages(path, prefix=name + ".")]


def test_every_module_imports_offline(network_attempts: list[str]) -> None:
    names = _modules(brain) + _modules(eval_pkg)
    assert "brain.pipeline" in names and "eval.run_eval" in names
    for name in names:
        importlib.import_module(name)
    spec = importlib.util.spec_from_file_location("demo_rehearse", ROOT / "scripts" / "demo" / "rehearse.py")
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert network_attempts == []


def test_every_case_end_to_end_offline(network_attempts: list[str]) -> None:
    factory = real_brain_factory(None)
    statuses = {}
    for case in load_cases():
        out = run_case_e2e(case, factory, has_planner=False)
        statuses[case.id] = score(case, out, "e2e").status
    assert network_attempts == []
    assert "ERROR" not in statuses.values(), {k: v for k, v in statuses.items() if v == "ERROR"}
    assert list(statuses.values()).count("PASS") >= 30  # the model-free part of the suite really ran


def test_every_case_router_only_offline(network_attempts: list[str]) -> None:
    from brain.fastpath import FastPath
    from brain.fixtures import load_apps
    from brain.flows import Flow, FlowStore
    from brain.router import Router

    fastpath = FastPath(load_apps())

    def make_router(seed: object) -> Router:
        store = FlowStore(None)
        for f in seed:  # type: ignore[attr-defined]
            store.save(Flow.from_dict(f))
        return Router(fastpath, store, None)

    comps = Components(make_router=make_router)
    for case in load_cases():
        if "router" in case.modes:
            assert score(case, run_case(case, comps), "router").status != "ERROR", case.id
    assert network_attempts == []


def test_demo_rehearsal_offline(network_attempts: list[str]) -> None:
    stage = FixtureStage(None)
    try:
        results = rehearse(stage, has_planner=False)
    finally:
        stage.close()
    assert network_attempts == []
    assert [r.status for r in results if r.step.key in ("task", "hijack", "teach", "away", "replay")] == [
        "PASS"
    ] * 5


def test_default_endpoints_are_loopback() -> None:
    settings = Settings()
    assert _is_loopback(urlparse(settings.bonsai_url).hostname), settings.bonsai_url
    assert _is_loopback(settings.host), settings.host
