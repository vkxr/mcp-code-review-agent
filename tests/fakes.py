"""A scripted stand-in for anthropic.Anthropic used by the tests (no network, no API key)."""

from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace

_ids = itertools.count()


def tool_use(name: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=payload)


class FakeClient:
    """Routes each call to a responder chosen by the system prompt.

    `responders` maps a substring of the system prompt to a list of content-block
    lists, returned one per call for that agent.
    """

    def __init__(self, responders: dict[str, list[list]]):
        self.responders = {k: list(v) for k, v in responders.items()}
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        with self._lock:
            self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
            for key, queue in self.responders.items():
                if key in kwargs["system"]:
                    if not queue:
                        raise AssertionError(f"No scripted response left for {key!r}")
                    content = queue.pop(0)
                    break
            else:
                raise AssertionError(f"Unexpected system prompt: {kwargs['system'][:60]}")
        return SimpleNamespace(content=content, stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))
