"""
Runtime proof for LLM provider failover.

Run:
    python scripts/llm_failover_proof.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_factory  # noqa: E402
import main as main_module  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from main import _ask_llm, _reset_funnel  # noqa: E402


class FakeProvider:
    def __init__(
        self,
        name: str,
        behavior: str,
        answer: str,
        events: list[str],
    ) -> None:
        self.name = name
        self.behavior = behavior
        self.answer = answer
        self.events = events
        self.calls = 0
        self.cancelled = 0
        self.late_response_attempted = False

    async def ainvoke(self, messages) -> AIMessage:  # noqa: ANN001
        _ = messages
        self.calls += 1
        self.events.append(f"{self.name}:start")

        if self.behavior == "success":
            await asyncio.sleep(0)
            self.events.append(f"{self.name}:success")
            return AIMessage(content=self.answer)

        if self.behavior == "error":
            self.events.append(f"{self.name}:error")
            raise RuntimeError(f"{self.name} failed")

        if self.behavior == "timeout":
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled += 1
                self.events.append(f"{self.name}:cancelled")
                raise

        if self.behavior == "late_after_cancel":
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled += 1
                self.late_response_attempted = True
                self.events.append(f"{self.name}:cancelled")
                await asyncio.sleep(0)
                self.events.append(f"{self.name}:late_response")
                return AIMessage(content=self.answer)

        raise RuntimeError(f"Unsupported fake behavior: {self.behavior}")


async def _run_fake_chain_case(
    case: str,
    providers: list[tuple[str, FakeProvider]],
) -> dict:
    chain = [(name, provider) for name, provider in providers]
    events = chain[0][1].events if chain else []
    sid = f"llm-proof-{case}"
    _reset_funnel(sid)

    original_get_llm = main_module.get_llm
    original_get_llm_chain = main_module.get_llm_chain
    original_timeout = main_module.LLM_PROVIDER_TIMEOUT_SECONDS

    main_module.get_llm = lambda: chain[0][1]
    main_module.get_llm_chain = lambda: list(chain)
    main_module.LLM_PROVIDER_TIMEOUT_SECONDS = 0.05

    try:
        answer = await _ask_llm("proof request", sid, "proof context")
    finally:
        main_module.get_llm = original_get_llm
        main_module.get_llm_chain = original_get_llm_chain
        main_module.LLM_PROVIDER_TIMEOUT_SECONDS = original_timeout

    return {
        "case": case,
        "final_answer": answer,
        "events": events,
        "providers": [
            {
                "name": provider.name,
                "calls": provider.calls,
                "cancelled": provider.cancelled,
                "late_response_attempted": provider.late_response_attempted,
            }
            for _, provider in chain
        ],
    }


async def main() -> None:
    llm_factory.reset_llm_cache()
    detected_chain = []
    detection_error = ""
    try:
        detected_chain = [name for name, _ in llm_factory.get_llm_chain()]
    except Exception as exc:  # pragma: no cover - proof only
        detection_error = str(exc)

    primary_success_events: list[str] = []
    primary_success = await _run_fake_chain_case(
        "primary_success",
        [
            ("GigaChat", FakeProvider("GigaChat", "success", "primary answer", primary_success_events)),
            ("Yandex GPT", FakeProvider("Yandex GPT", "success", "secondary answer", primary_success_events)),
        ],
    )

    timeout_failover_events: list[str] = []
    timeout_failover = await _run_fake_chain_case(
        "timeout_failover",
        [
            ("GigaChat", FakeProvider("GigaChat", "late_after_cancel", "late primary answer", timeout_failover_events)),
            ("Yandex GPT", FakeProvider("Yandex GPT", "success", "secondary answer", timeout_failover_events)),
        ],
    )

    single_provider_events: list[str] = []
    single_provider = await _run_fake_chain_case(
        "single_provider",
        [
            ("Yandex GPT", FakeProvider("Yandex GPT", "success", "single provider answer", single_provider_events)),
        ],
    )

    report = {
        "actual_provider_detection": {
            "detected_chain": detected_chain,
            "yandex_available": "Yandex GPT" in detected_chain,
            "gigachat_available": "GigaChat" in detected_chain,
            "openrouter_available": "OpenRouter" in detected_chain,
            "openai_available": "OpenAI" in detected_chain,
            "detection_error": detection_error,
        },
        "scenarios": [
            primary_success,
            timeout_failover,
            single_provider,
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
