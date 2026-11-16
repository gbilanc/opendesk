"""Characterisation tests for ``_RelaySession._run_host`` / ``_run_client``.

Scopo: dare una rete di sicurezza alla macchina a stati dei due ruoli prima
di estrarre il boilerplate comune (connect/register, loop di ricezione,
teardown).  A differenza di ``test_relay_integration.py`` — che verifica il
protocollo a mano sul filo — qui girano i veri ``_run_host`` e ``_run_client``
contro un relay finto che implementa il minimo pairing necessario.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

import pytest

from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayRole, _RelaySession

_TIMEOUT = 5.0


class _ScriptedRelay:
    """Relay finto: risponde al RELAY_REGISTER e poi inoltra ai due peer.

    ``_run_host``/``_run_client`` non leggono alcun greeting dal relay.
    """

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._peers: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._paired: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter | None, msg: Message) -> None:
        if writer is None:
            return
        writer.write(msg.encode())
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            register = await asyncio.wait_for(Message.from_reader(reader), timeout=_TIMEOUT)
            assert register.type == MessageType.RELAY_REGISTER
            if register.payload.get("lookup_device"):
                role = "client"
                await self._send(writer, Message(MessageType.RELAY_REGISTER, {"paired": True}))
            else:
                role = "host"
                await self._send(writer, Message(MessageType.RELAY_REGISTER, {"mode": "host"}))
            self._peers[role] = (reader, writer)
            assert self._paired is not None
            self._paired.set()

            other = "host" if role == "client" else "client"
            while True:
                msg = await Message.from_reader(reader)
                peer = self._peers.get(other)
                await self._send(peer[1] if peer else None, msg)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _run(self) -> None:
        self._paired = asyncio.Event()
        self._stop = asyncio.Event()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        async with self._server:
            await self._stop.wait()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()
            self._loop = None

    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=_TIMEOUT), "relay finto non partito"

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=_TIMEOUT)
            self._thread = None


class _Session:
    """Wrapper: ``_RelaySession`` su un thread, con inbox ispezionabile."""

    def __init__(self, relay: _ScriptedRelay, role: RelayRole) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.session = _RelaySession(
            host="127.0.0.1",
            port=relay.port,
            session_id="SID123",
            password="pw",
            role=role,
            inbox=self.inbox,
            device_id="dev-host" if role is RelayRole.HOST else "dev-client",
            device_name=role.name,
            e2ee_enabled=False,
            p2p_enabled=False,
        )
        self._thread = threading.Thread(target=self.session.start, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_event(self, name: str) -> tuple:
        """Attende il primo evento ``name`` nell'inbox (gli altri sono scartati)."""
        deadline = time.monotonic() + _TIMEOUT
        while time.monotonic() < deadline:
            try:
                event = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if event[0] == name:
                return event
        raise AssertionError(f"evento '{name}' non ricevuto entro {_TIMEOUT}s")

    def stop(self) -> None:
        if self._thread.is_alive():
            self.session.stop()
        self._thread.join(timeout=_TIMEOUT)


@pytest.fixture
def relay() -> _ScriptedRelay:
    rel = _ScriptedRelay()
    rel.start_thread()
    yield rel
    rel.stop()


@pytest.mark.parametrize("role", [RelayRole.HOST, RelayRole.CLIENT])
def test_role_connects_and_registers(relay: _ScriptedRelay, role: RelayRole) -> None:
    """Ogni ruolo si connette, si registra e riceve l'evento 'connected'."""
    other_role = RelayRole.CLIENT if role is RelayRole.HOST else RelayRole.HOST
    other = _Session(relay, other_role)
    session = _Session(relay, role)
    other.start()
    session.start()
    try:
        event = session.wait_event("connected")
        assert event[1][0] == role.name.lower()
    finally:
        session.stop()
        other.stop()


@pytest.mark.parametrize("role", [RelayRole.HOST, RelayRole.CLIENT])
def test_role_ping_pong_round_trip(relay: _ScriptedRelay, role: RelayRole) -> None:
    """I due ruoli si scambiano PING/PONG attraverso il loop di ricezione."""
    other_role = RelayRole.CLIENT if role is RelayRole.HOST else RelayRole.HOST
    other = _Session(relay, other_role)
    session = _Session(relay, role)
    other.start()
    session.start()
    try:
        # Entrambi devono essere connessi prima di scambiare traffico.
        session.wait_event("connected")
        other.wait_event("connected")

        ping_sender = other
        assert ping_sender.session.send_message(Message.ping(seq=7)) is True

        # Il PONG torna al mittente del PING: _last_pong_time si aggiorna lì.
        deadline = time.monotonic() + _TIMEOUT
        while time.monotonic() < deadline:
            if ping_sender.session._last_pong_time > 0:
                break
            time.sleep(0.05)
        assert ping_sender.session._last_pong_time > 0
    finally:
        session.stop()
        other.stop()


@pytest.mark.parametrize("role", [RelayRole.HOST, RelayRole.CLIENT])
def test_role_disconnect_emits_disconnected(relay: _ScriptedRelay, role: RelayRole) -> None:
    """La chiusura della sessione emette 'disconnected' e ferma il thread."""
    other_role = RelayRole.CLIENT if role is RelayRole.HOST else RelayRole.HOST
    other = _Session(relay, other_role)
    session = _Session(relay, role)
    other.start()
    session.start()
    try:
        session.wait_event("connected")
        session.session.stop()
        session.wait_event("disconnected")
        # Il thread di sessione deve uscire da solo dopo lo stop.
        session._thread.join(timeout=_TIMEOUT)
        assert not session._thread.is_alive()
    finally:
        session.stop()
        other.stop()


def test_unreachable_relay_reports_error_and_disconnected() -> None:
    """Relay irraggiungibile: evento 'error' seguito da 'disconnected'."""
    session = _Session.__new__(_Session)
    session.inbox = queue.Queue()
    session.session = _RelaySession(
        host="127.0.0.1",
        port=1,  # porta chiusa
        session_id="SID123",
        password="pw",
        role=RelayRole.CLIENT,
        inbox=session.inbox,
        e2ee_enabled=False,
        p2p_enabled=False,
    )
    session._thread = threading.Thread(target=session.session.start, daemon=True)
    session.start()
    try:
        assert session.wait_event("error")[0] == "error"
        assert session.wait_event("disconnected")[0] == "disconnected"
    finally:
        session._thread.join(timeout=_TIMEOUT)
