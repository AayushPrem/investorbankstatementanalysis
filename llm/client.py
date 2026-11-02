"""The one place this codebase talks to an LLM.

Before Phase 1, eight modules each built ``anthropic.Anthropic()`` with a
hardcoded model id. They now call :func:`get_llm_client` and use
:meth:`LLMClient.complete`. That gives one place for the model id (from
``config.settings``), timeout and retry policy, and usage/latency logging, and
one seam for tests (:class:`FakeLLM`).

Implementations:

* :class:`AnthropicClient`: the real client. ``anthropic`` is imported lazily
  so the package stays optional at import time. Retries with exponential
  backoff on 408/409/429/5xx and connection errors come from the SDK's own
  ``max_retries``.
* :class:`NullClient`: LLM disabled or no key. Every call raises
  :class:`LLMUnavailable`, and callers fall back to their deterministic path
  exactly as they did when the key was missing.
* :class:`FakeLLM`: scripted responses for tests. It records each call.

Logging rule (UPGRADE_GUIDE §1.9): only model id, token counts and
``duration_ms`` are logged, never prompt or response text.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from config.settings import Settings, get_settings

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class LLMUnavailable(RuntimeError):  # noqa: N818 — name fixed by UPGRADE_GUIDE §7
    """Raised when no LLM is configured (disabled, no key, or SDK not installed)."""


class LLMResponseError(ValueError):
    """The model's output could not be parsed or validated (after the one retry)."""


def _validate_json(text: str, schema: type[M]) -> M:
    """Parse *text* as JSON and validate it against *schema*. Raises ValueError."""
    try:
        return schema.model_validate_json(text.strip())
    except ValidationError as exc:
        raise LLMResponseError(f"output failed {schema.__name__} validation") from exc


def _complete_json_with_retry(
    client: LLMClient,
    prompt: str,
    *,
    system: str,
    max_tokens: int,
    temperature: float | None,
    schema: type[M],
) -> M:
    """Shared complete_json behaviour: validate, and retry once on invalid output."""
    text = client.complete(prompt, system=system, max_tokens=max_tokens, temperature=temperature)
    try:
        return _validate_json(text, schema)
    except LLMResponseError:
        log.info("llm json invalid, retrying once", extra={"schema": schema.__name__})
    retry_prompt = (
        f"{prompt}\n\nYour previous reply was not valid JSON for the required schema. "
        "Respond again with ONLY valid JSON."
    )
    text = client.complete(
        retry_prompt, system=system, max_tokens=max_tokens, temperature=temperature
    )
    return _validate_json(text, schema)


@runtime_checkable
class LLMClient(Protocol):
    """What call sites depend on."""

    def complete(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        """Single-turn completion. Returns the (unstripped) text of the first content block."""
        ...

    def complete_json(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        schema: type[M],
        temperature: float | None = None,
    ) -> M:
        """Completion validated against *schema*. Retries once on invalid JSON."""
        ...


class NullClient:
    """LLM disabled: every call raises :class:`LLMUnavailable`."""

    def __init__(self, reason: str = "LLM disabled or ANTHROPIC_API_KEY not set") -> None:
        self.reason = reason

    def complete(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        raise LLMUnavailable(self.reason)

    def complete_json(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        schema: type[M],
        temperature: float | None = None,
    ) -> M:
        raise LLMUnavailable(self.reason)


class AnthropicClient:
    """Claude via the official ``anthropic`` SDK."""

    def __init__(self, settings: Settings | None = None, model: str | None = None) -> None:
        self._settings = settings or get_settings()
        self.model = model or self._settings.llm_model
        self._sdk_client: Any = None

    def _client(self) -> Any:
        if self._sdk_client is None:
            try:
                import anthropic
            except ImportError as exc:  # SDK not installed (or masked in tests)
                raise LLMUnavailable("anthropic package not installed") from exc
            key = self._settings.anthropic_api_key
            self._sdk_client = anthropic.Anthropic(
                api_key=key.get_secret_value() if key else None,
                timeout=self._settings.llm_timeout_s,
                max_retries=self._settings.llm_max_retries,
            )
        return self._sdk_client

    def complete(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Only sent when a caller asks for it. The pre-Phase-1 call sites never
        # sent temperature, and they still don't.
        if temperature is not None:
            kwargs["temperature"] = temperature
        start = time.perf_counter()
        response = client.messages.create(**kwargs)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        usage = getattr(response, "usage", None)
        log.info(
            "llm call complete",
            extra={
                "model": self.model,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "duration_ms": duration_ms,
                "stop_reason": getattr(response, "stop_reason", None),
            },
        )
        # Same contract as the old call sites: text of the first content block.
        # A non-text first block raises AttributeError, which callers catch.
        text: str = response.content[0].text
        return text

    def complete_json(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        schema: type[M],
        temperature: float | None = None,
    ) -> M:
        return _complete_json_with_retry(
            self, prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, schema=schema,
        )


@dataclass
class FakeCall:
    prompt: str
    system: str
    max_tokens: int
    temperature: float | None


@dataclass
class FakeLLM:
    """Scripted LLM for tests.

    ``responses`` is consumed in order. Each item is either a string to
    return or an exception instance to raise. Alternatively pass ``handler``,
    a function ``(prompt, system) -> str``.
    """

    responses: list[str | BaseException] = field(default_factory=list)
    handler: Callable[[str, str], str] | None = None
    calls: list[FakeCall] = field(default_factory=list)

    @classmethod
    def from_json(cls, payloads: Iterable[Any]) -> FakeLLM:
        return cls(responses=[json.dumps(p) for p in payloads])

    def complete(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(FakeCall(prompt, system, max_tokens, temperature))
        if self.handler is not None:
            return self.handler(prompt, system)
        if not self.responses:
            raise AssertionError("FakeLLM: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def complete_json(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        schema: type[M],
        temperature: float | None = None,
    ) -> M:
        return _complete_json_with_retry(
            self, prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, schema=schema,
        )


def get_llm_client(
    enabled: bool = True,
    settings: Settings | None = None,
    model: str | None = None,
) -> LLMClient:
    """Return an :class:`AnthropicClient` if *enabled* and a key is configured, else a :class:`NullClient`.

    *model* overrides ``settings.llm_model`` (used by ``Categoriser(llm_model=...)``).
    """
    s = settings or get_settings()
    if enabled and s.llm_available:
        return AnthropicClient(s, model=model)
    return NullClient()
