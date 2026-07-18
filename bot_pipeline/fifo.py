"""Cancel-safe FIFO admission for per-channel bot turns."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field


@dataclass(eq=False, slots=True)
class AdmissionTicket:
    """A synchronously-reserved queue position owned by one turn."""

    gate: "FifoAdmission"
    sequence: int
    position: int
    _ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    acquired: bool = False
    released: bool = False

    async def wait(self) -> None:
        if self.released:
            raise RuntimeError("admission ticket is no longer active")
        try:
            await self._ready.wait()
        except BaseException:
            self.cancel()
            raise
        if self.released:
            raise asyncio.CancelledError
        self.acquired = True

    def cancel(self) -> None:
        if not self.released:
            self.gate._remove(self)

    def release(self) -> None:
        if not self.released:
            self.gate._remove(self)


class FifoAdmission:
    """Reserve arrival order before any status or reaction I/O occurs."""

    def __init__(self) -> None:
        self._tickets: deque[AdmissionTicket] = deque()
        self._sequence = 0
        self._context_tickets: dict[asyncio.Task[object], AdmissionTicket] = {}

    async def __aenter__(self) -> "FifoAdmission":
        """Acquire one FIFO position owned by the current async context."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("FIFO admission requires an asyncio task")
        if task in self._context_tickets:
            raise RuntimeError("FIFO admission is already entered by this task")
        ticket = self.issue()
        self._context_tickets[task] = ticket
        try:
            await ticket.wait()
        except BaseException:
            self._context_tickets.pop(task, None)
            ticket.cancel()
            raise
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        task = asyncio.current_task()
        ticket = self._context_tickets.pop(task, None) if task is not None else None
        if ticket is not None:
            ticket.release()
        return False

    def issue(self) -> AdmissionTicket:
        self._sequence += 1
        ticket = AdmissionTicket(
            gate=self,
            sequence=self._sequence,
            position=len(self._tickets) + 1,
        )
        self._tickets.append(ticket)
        if len(self._tickets) == 1:
            ticket._ready.set()
        return ticket

    async def acquire(self) -> None:
        """Compatibility adapter for callers expecting asyncio.Semaphore."""
        await self.issue().wait()

    def release(self) -> None:
        """Release the active compatibility or explicitly-issued ticket."""
        if not self._tickets or not self._tickets[0].acquired:
            raise ValueError("admission gate is not acquired")
        self._remove(self._tickets[0])

    def locked(self) -> bool:
        return bool(self._tickets)

    @property
    def idle(self) -> bool:
        return not self._tickets

    def _remove(self, ticket: AdmissionTicket) -> None:
        if ticket.released:
            return
        was_head = bool(self._tickets) and self._tickets[0] is ticket
        try:
            self._tickets.remove(ticket)
        except ValueError:
            ticket.released = True
            return
        ticket.released = True
        ticket._ready.set()
        if was_head and self._tickets:
            self._tickets[0]._ready.set()
