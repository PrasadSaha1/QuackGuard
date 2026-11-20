"""Content checks for caller-supplied text. The default transport is offline."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.parse import urlsplit

from .types import Classification, Direction, Verdict


class Scanner(Protocol):
    def scan(self, direction: Direction, text: str) -> Classification: ...


class DemoScanner:
    """Exact, direction-sensitive fixtures. Unknown items need review."""

    def __init__(self, fixtures: Mapping[tuple[Direction, str], Classification] | None = None):
        if fixtures is None:
            fixtures = {
                (Direction.INPUT, "Summarize the sample syllabus."): Classification(Verdict.SAFE),
                (Direction.OUTPUT, "The sample quiz is on Friday."): Classification(Verdict.SAFE),
                (Direction.INPUT, "[flagged input fixture]"): Classification(Verdict.UNSAFE, ("S7",)),
                (Direction.OUTPUT, "[flagged output fixture]"): Classification(Verdict.UNSAFE, ("S7",)),
            }
        self._fixtures = dict(fixtures)

    def scan(self, direction: Direction, text: str) -> Classification:
        return self._fixtures.get((direction, text), Classification(Verdict.UNAVAILABLE))


Transport = Callable[[str, dict, str, float], object]


def _post(endpoint: str, payload: dict, token: str, timeout: float) -> object:
    import requests  # Installed with the optional llama extra.

    with requests.Session() as session:
        session.trust_env = False
        response = session.post(endpoint, json=payload, headers={"Authorization": f"Bearer {token}"},
                                timeout=timeout, allow_redirects=False)
        if response.status_code != 200:
            return None
        return response.json()


def _parse(response: object) -> Classification:
    if not isinstance(response, dict) or response.get("error") is not None:
        raise ValueError
    choices = response["choices"]
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError
    choice = choices[0]
    if choice.get("finish_reason", "stop") != "stop":
        raise ValueError
    message = choice["message"]
    if (message.get("role", "assistant") != "assistant" or message.get("tool_calls") is not None
            or message.get("refusal") is not None or message.get("function_call") is not None):
        raise ValueError
    content = message["content"].strip()
    if content == "safe":
        return Classification(Verdict.SAFE)
    if content == "unsafe":
        return Classification(Verdict.UNSAFE)
    match = re.fullmatch(r"unsafe\r?\n(S\d+(?:[ \t]*,[ \t]*S\d+)*)", content)
    if not match:
        raise ValueError
    codes = tuple(part.strip() for part in match[1].split(","))
    return Classification(Verdict.UNSAFE, codes)


class LlamaGuardScanner:
    """Opt-in Llama Guard 3 adapter for an HTTPS chat-completions endpoint."""

    def __init__(self, *, endpoint: str, model: str, timeout: float = 10.0,
                 transport: Transport | None = None):
        # Do not repeat configuration values in validation errors.
        try:
            parsed = urlsplit(endpoint)
            valid_url = (parsed.scheme == "https" and bool(parsed.hostname)
                         and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment)
            _ = parsed.port
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url or type(model) is not str or not model.strip():
            raise ValueError("Configure an HTTPS endpoint and a nonempty model ID.")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Configure a finite positive timeout in seconds.")
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout
        self._transport = transport or _post

    def scan(self, direction: Direction, text: str) -> Classification:
        unavailable = Classification(Verdict.UNAVAILABLE)
        token = os.environ.get("QUACKGUARD_API_KEY")
        if not token or type(text) is not str or type(direction) is not Direction:
            return unavailable
        messages = [{"role": "user", "content": text}]
        if direction is Direction.OUTPUT:
            messages = [{"role": "user", "content": "Review the following reply."},
                        {"role": "assistant", "content": text}]
        try:
            response = self._transport(self._endpoint, {"model": self._model, "messages": messages},
                                       token, self._timeout)
            return _parse(response)
        except Exception:
            # Provider errors can carry authorization headers or submitted text.
            return unavailable
