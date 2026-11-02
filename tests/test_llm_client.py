"""llm/client.py: NullClient, FakeLLM, factory gating, complete_json, Anthropic wiring.

No real API calls are made. The Anthropic SDK client is replaced with a stub.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from config.settings import DEFAULT_LLM_MODEL, Settings, get_settings
from llm.client import (
    AnthropicClient,
    FakeLLM,
    LLMClient,
    LLMResponseError,
    LLMUnavailable,
    NullClient,
    get_llm_client,
)


class _Out(BaseModel):
    label: str
    score: float


# ── Settings ────────────────────────────────────────────────────────────────

class TestSettings:
    def test_default_model_is_pre_phase1_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_MODEL", raising=False)
        assert get_settings().llm_model == DEFAULT_LLM_MODEL == "claude-haiku-4-5-20251001"

    def test_key_read_live_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert get_settings().llm_available is False
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert get_settings().llm_available is True

    def test_llm_enabled_false_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_ENABLED", "false")
        assert get_settings().llm_available is False

    def test_empty_key_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert get_settings().llm_available is False

    def test_key_is_secret_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
        assert "sk-very-secret" not in repr(get_settings())

    def test_dotenv_disabled_in_tests(self) -> None:
        import os

        assert os.environ.get("BSAA_ENV_FILE") == ""


# ── Factory ─────────────────────────────────────────────────────────────────

class TestFactory:
    def test_no_key_gives_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert isinstance(get_llm_client(), NullClient)

    def test_disabled_gives_null_even_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert isinstance(get_llm_client(enabled=False), NullClient)

    def test_key_gives_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        client = get_llm_client()
        assert isinstance(client, AnthropicClient)
        assert client.model == DEFAULT_LLM_MODEL

    def test_model_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        client = get_llm_client(model="some-other-model")
        assert isinstance(client, AnthropicClient)
        assert client.model == "some-other-model"

    def test_all_implement_protocol(self) -> None:
        for c in (NullClient(), FakeLLM(), AnthropicClient(Settings())):
            assert isinstance(c, LLMClient)


# ── NullClient / FakeLLM ────────────────────────────────────────────────────

class TestNullAndFake:
    def test_null_raises(self) -> None:
        with pytest.raises(LLMUnavailable):
            NullClient().complete("p", system="s", max_tokens=10)
        with pytest.raises(LLMUnavailable):
            NullClient().complete_json("p", system="s", max_tokens=10, schema=_Out)

    def test_fake_scripted_and_records(self) -> None:
        fake = FakeLLM(responses=["one", "two"])
        assert fake.complete("a", system="s", max_tokens=5) == "one"
        assert fake.complete("b", system="s", max_tokens=6) == "two"
        assert [c.prompt for c in fake.calls] == ["a", "b"]
        assert fake.calls[1].max_tokens == 6

    def test_fake_raises_scripted_exception(self) -> None:
        fake = FakeLLM(responses=[RuntimeError("boom")])
        with pytest.raises(RuntimeError, match="boom"):
            fake.complete("a", system="s", max_tokens=5)

    def test_fake_exhausted(self) -> None:
        with pytest.raises(AssertionError):
            FakeLLM().complete("a", system="s", max_tokens=5)


# ── complete_json ───────────────────────────────────────────────────────────

class TestCompleteJson:
    def test_valid_first_try(self) -> None:
        fake = FakeLLM.from_json([{"label": "x", "score": 0.5}])
        out = fake.complete_json("p", system="s", max_tokens=10, schema=_Out)
        assert out == _Out(label="x", score=0.5)
        assert len(fake.calls) == 1

    def test_retries_once_on_invalid(self) -> None:
        fake = FakeLLM(responses=["not json", '{"label": "y", "score": 1}'])
        out = fake.complete_json("p", system="s", max_tokens=10, schema=_Out)
        assert out.label == "y"
        assert len(fake.calls) == 2
        assert "not valid JSON" in fake.calls[1].prompt

    def test_gives_up_after_one_retry(self) -> None:
        fake = FakeLLM(responses=["nope", '{"label": "missing score"}', "never used"])
        with pytest.raises(LLMResponseError):
            fake.complete_json("p", system="s", max_tokens=10, schema=_Out)
        assert len(fake.calls) == 2


# ── AnthropicClient with a stubbed SDK client ───────────────────────────────

class _StubMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(text=self.text)],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            stop_reason="end_turn",
        )


class TestAnthropicClient:
    def _client(self, text: str) -> tuple[AnthropicClient, _StubMessages]:
        client = AnthropicClient(Settings(anthropic_api_key="test-key"))  # type: ignore[arg-type]
        stub = _StubMessages(text)
        client._sdk_client = SimpleNamespace(messages=stub)
        return client, stub

    def test_request_shape_matches_pre_phase1_call_sites(self) -> None:
        client, stub = self._client("hello")
        assert client.complete("the prompt", system="the system", max_tokens=256) == "hello"
        assert stub.kwargs == {
            "model": DEFAULT_LLM_MODEL,
            "max_tokens": 256,
            "system": "the system",
            "messages": [{"role": "user", "content": "the prompt"}],
        }

    def test_temperature_only_sent_when_given(self) -> None:
        client, stub = self._client("x")
        client.complete("p", system="s", max_tokens=1, temperature=0)
        assert stub.kwargs["temperature"] == 0

    def test_logs_usage_not_content(self, caplog: pytest.LogCaptureFixture) -> None:
        client, _ = self._client("SECRET RESPONSE TEXT")
        with caplog.at_level(logging.INFO, logger="llm.client"):
            client.complete("SECRET PROMPT TEXT", system="s", max_tokens=5)
        rec = next(r for r in caplog.records if r.getMessage() == "llm call complete")
        assert rec.input_tokens == 11  # type: ignore[attr-defined]
        assert rec.output_tokens == 7  # type: ignore[attr-defined]
        assert rec.duration_ms >= 0  # type: ignore[attr-defined]
        dumped = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert "SECRET PROMPT TEXT" not in dumped
        assert "SECRET RESPONSE TEXT" not in dumped

    def test_sdk_constructed_with_timeout_and_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        seen: dict[str, Any] = {}

        def fake_ctor(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(messages=_StubMessages("ok"))

        monkeypatch.setattr(anthropic, "Anthropic", fake_ctor)
        s = Settings(anthropic_api_key="k", llm_timeout_s=12.5, llm_max_retries=3)  # type: ignore[arg-type]
        assert AnthropicClient(s).complete("p", system="s", max_tokens=1) == "ok"
        assert seen == {"api_key": "k", "timeout": 12.5, "max_retries": 3}

    def test_missing_sdk_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
        with pytest.raises(LLMUnavailable):
            AnthropicClient(Settings(anthropic_api_key="k")).complete(  # type: ignore[arg-type]
                "p", system="s", max_tokens=1
            )


# ── Call sites route through the seam ───────────────────────────────────────

class TestCallSitesUseSeam:
    """With a key set, every call site calls get_llm_client(). A FakeLLM stands in."""

    def test_risk_narrative_via_fake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from analysis import risk

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        fake = FakeLLM(responses=["  narrative from fake  "])
        monkeypatch.setattr(risk, "get_llm_client", lambda *a, **k: fake)
        flag = risk.Flag(
            detector_name="structuring", severity=risk.Severity.HIGH,
            description="d", triggering_transaction_ids=[],
        )
        out = risk.RiskAnalyst()._generate_narrative([flag])
        assert out == "narrative from fake"
        assert fake.calls[0].max_tokens == 400

    def test_risk_narrative_unavailable_uses_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from analysis import risk

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(risk, "get_llm_client", lambda *a, **k: NullClient())
        flag = risk.Flag(
            detector_name="structuring", severity=risk.Severity.HIGH,
            description="d", triggering_transaction_ids=[],
        )
        assert risk.RiskAnalyst()._generate_narrative([flag]) == risk._fallback_narrative([flag])

    def test_report_narrative_via_fake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from analysis import report_narrative as rn

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        fake = FakeLLM(responses=["summary"])
        monkeypatch.setattr(rn, "get_llm_client", lambda *a, **k: fake)
        data = rn.ReportNarrativeInput(
            company_name="Acme", total_revenue=1.0, avg_monthly_burn=1.0,
            runway_months=None, avg_mom_growth=None, active_customers=None, latest_nrr=None,
            risk_flag_counts={}, health_alert_counts={}, compliance_exception_counts={},
            reconciliation_finding_counts=None, top_risk_descriptions=[], top_health_descriptions=[],
        )
        assert rn.generate_report_narrative(data) == "summary"

    def test_categoriser_uses_configured_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from analysis.categoriser import Categoriser

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "configured-model")
        assert Categoriser()._llm_model == "configured-model"
        assert Categoriser(llm_model="explicit")._llm_model == "explicit"
