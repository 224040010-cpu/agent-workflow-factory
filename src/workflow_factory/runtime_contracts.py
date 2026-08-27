from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RuntimeCapabilities:
    durable_sessions: bool
    append_only_events: bool
    human_gate: bool
    scheduled_loops: bool
    sandbox_network_allowlist: bool


class AgentProvider(Protocol):
    def start(self, profile: dict, route: dict) -> str: ...
    def resume(self, handle: str, route: dict) -> None: ...
    def interrupt(self, handle: str, reason: str) -> None: ...


class ToolProvider(Protocol):
    def invoke(self, descriptor: dict, request: dict, idempotency_key: str) -> dict: ...
    def inspect(self, idempotency_key: str) -> dict: ...


class EventStore(Protocol):
    def append(self, run_id: str, events: list[dict]) -> None: ...
    def read(self, run_id: str, after_seq: int = 0) -> list[dict]: ...


def negotiate(required: dict[str, str], available: RuntimeCapabilities) -> list[str]:
    missing: list[str] = []
    for capability, level in required.items():
        if level == "required" and not getattr(available, capability, False):
            missing.append(capability)
    return missing
