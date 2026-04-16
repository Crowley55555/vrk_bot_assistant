"""
Runtime proof for skipping regulated step in outdoor galvanized facade flow.

Run:
    python scripts/facade_galvanized_skip_proof.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import main as main_module  # noqa: E402
from main import _get_session, _reset_funnel, process_message  # noqa: E402
from models import ChatRequest  # noqa: E402


async def _fake_ask_llm(user_message: str, session_id: str, context: str) -> str:
    return "proof-llm-disabled"


main_module._ask_llm = _fake_ask_llm
logging.disable(logging.CRITICAL)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _buttons_payload(response) -> list[dict[str, str]]:
    return [{"label": btn.label, "value": btn.value} for btn in (response.buttons or [])]


def _mentions_regulated_question(reply: str) -> bool:
    return "нужна ли регулировка потока воздуха" in (reply or "").lower()


def _useless_fallback(reply: str) -> bool:
    text = (reply or "").lower()
    return any(
        token in text
        for token in ("уточните категорию", "уточните параметры", "уточните запрос текстом")
    )


async def _walk_path(session_id: str, answers: list[str]) -> list[dict]:
    _reset_funnel(session_id)
    transcript: list[dict] = []
    for answer in ["подобрать", *answers]:
        response = await process_message(ChatRequest(message=answer, session_id=session_id))
        transcript.append(
            {
                "answer": answer,
                "reply": response.reply,
                "action": response.action.value,
                "buttons": _buttons_payload(response),
                "products": [product.get("name", "") for product in (response.products or [])],
                "session_snapshot": {
                    "detail_answers": dict((_get_session(session_id).get("detail_answers") or {})),
                    "active_filters": dict((_get_session(session_id).get("active_filters") or {})),
                },
            }
        )
    return transcript


async def _galvanized_case() -> dict:
    transcript = await _walk_path(
        "facade-galvanized-skip",
        ["grille", "outdoor", "rectangular", "standard", "galvanized", "embedded", "under_2m2"],
    )
    replies = [step["reply"] for step in transcript]
    final_step = transcript[-1]
    galvanized_step = next(step for step in transcript if step["answer"] == "galvanized")
    return {
        "case": "outdoor_galvanized_embedded",
        "reply_after_galvanized": galvanized_step["reply"],
        "action_after_galvanized": galvanized_step["action"],
        "final_action": final_step["action"],
        "final_reply": final_step["reply"],
        "final_products": final_step["products"],
        "regulated_question_seen": any(_mentions_regulated_question(reply) for reply in replies),
        "facade_regulated_in_detail_answers": "facade_regulated" in final_step["session_snapshot"]["detail_answers"],
        "regulated_in_active_filters": "regulated" in final_step["session_snapshot"]["active_filters"],
        "useless_fallback_shown": _useless_fallback(final_step["reply"]),
    }


async def _aluminum_control_case() -> dict:
    transcript = await _walk_path(
        "facade-aluminum-control",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "embedded", "under_2m2"],
    )
    replies = [step["reply"] for step in transcript]
    final_step = transcript[-1]
    aluminum_step = next(step for step in transcript if step["answer"] == "aluminum")
    return {
        "case": "outdoor_aluminum_control",
        "reply_after_aluminum": aluminum_step["reply"],
        "action_after_aluminum": aluminum_step["action"],
        "last_reply_before_final_choice": final_step["reply"],
        "regulated_question_seen": any(_mentions_regulated_question(reply) for reply in replies),
        "facade_regulated_in_detail_answers": "facade_regulated" in final_step["session_snapshot"]["detail_answers"],
    }


async def _stainless_case() -> dict:
    transcript = await _walk_path(
        "facade-stainless-skip",
        ["grille", "outdoor", "rectangular", "standard", "stainless_steel", "embedded", "over_2m2", "yes"],
    )
    replies = [step["reply"] for step in transcript]
    stainless_step = next(step for step in transcript if step["answer"] == "stainless_steel")
    size_step = next(step for step in transcript if step["answer"] == "embedded")
    final_step = transcript[-1]
    size_values = [button["value"] for button in size_step["buttons"]]
    size_labels = [button["label"] for button in size_step["buttons"]]
    return {
        "case": "outdoor_stainless_embedded",
        "reply_after_stainless": stainless_step["reply"],
        "action_after_stainless": stainless_step["action"],
        "size_step_reply": size_step["reply"],
        "size_step_values": size_values,
        "size_step_labels": size_labels,
        "final_action": final_step["action"],
        "final_reply": final_step["reply"],
        "final_products": final_step["products"],
        "regulated_question_seen": any(_mentions_regulated_question(reply) for reply in replies),
        "facade_regulated_in_detail_answers": "facade_regulated" in final_step["session_snapshot"]["detail_answers"],
        "regulated_in_active_filters": "regulated" in final_step["session_snapshot"]["active_filters"],
        "useless_fallback_shown": _useless_fallback(final_step["reply"]),
    }


async def _galvanized_invalid_regulated_case() -> dict:
    transcript = await _walk_path(
        "facade-galvanized-invalid-regulated",
        ["grille", "outdoor", "rectangular", "standard", "galvanized", "embedded", "regulated"],
    )
    before_invalid = transcript[-2]
    after_invalid = transcript[-1]
    return {
        "case": "outdoor_galvanized_invalid_regulated",
        "reply_before_invalid": before_invalid["reply"],
        "reply_after_invalid": after_invalid["reply"],
        "action_after_invalid": after_invalid["action"],
        "detail_answers_before": before_invalid["session_snapshot"]["detail_answers"],
        "detail_answers_after": after_invalid["session_snapshot"]["detail_answers"],
        "active_filters_before": before_invalid["session_snapshot"]["active_filters"],
        "active_filters_after": after_invalid["session_snapshot"]["active_filters"],
    }


async def _stainless_invalid_regulated_case() -> dict:
    transcript = await _walk_path(
        "facade-stainless-invalid-regulated",
        ["grille", "outdoor", "rectangular", "standard", "stainless_steel", "embedded", "regulated"],
    )
    before_invalid = transcript[-2]
    after_invalid = transcript[-1]
    return {
        "case": "outdoor_stainless_invalid_regulated",
        "reply_before_invalid": before_invalid["reply"],
        "reply_after_invalid": after_invalid["reply"],
        "action_after_invalid": after_invalid["action"],
        "detail_answers_before": before_invalid["session_snapshot"]["detail_answers"],
        "detail_answers_after": after_invalid["session_snapshot"]["detail_answers"],
        "active_filters_before": before_invalid["session_snapshot"]["active_filters"],
        "active_filters_after": after_invalid["session_snapshot"]["active_filters"],
    }


async def _stainless_invalid_over4_case() -> dict:
    transcript = await _walk_path(
        "facade-stainless-invalid-over4",
        ["grille", "outdoor", "rectangular", "standard", "stainless_steel", "embedded", "over_4m2"],
    )
    before_invalid = transcript[-2]
    after_invalid = transcript[-1]
    return {
        "case": "outdoor_stainless_invalid_over4m2",
        "reply_before_invalid": before_invalid["reply"],
        "reply_after_invalid": after_invalid["reply"],
        "action_after_invalid": after_invalid["action"],
        "detail_answers_before": before_invalid["session_snapshot"]["detail_answers"],
        "detail_answers_after": after_invalid["session_snapshot"]["detail_answers"],
        "active_filters_before": before_invalid["session_snapshot"]["active_filters"],
        "active_filters_after": after_invalid["session_snapshot"]["active_filters"],
    }


async def main() -> None:
    report = [
        await _galvanized_case(),
        await _stainless_case(),
        await _galvanized_invalid_regulated_case(),
        await _stainless_invalid_regulated_case(),
        await _stainless_invalid_over4_case(),
        await _aluminum_control_case(),
    ]
    by_case = {item["case"]: item for item in report}
    _assert(
        by_case["outdoor_galvanized_embedded"]["action_after_galvanized"] == "ask_question",
        "Galvanized path must continue with the next question",
    )
    _assert(
        "встраиваемая или накладная" in by_case["outdoor_galvanized_embedded"]["reply_after_galvanized"].lower(),
        "Galvanized path should go to mount question right after material",
    )
    _assert(
        not by_case["outdoor_galvanized_embedded"]["regulated_question_seen"],
        "Galvanized path must not ask regulated question",
    )
    _assert(
        not by_case["outdoor_galvanized_embedded"]["facade_regulated_in_detail_answers"],
        "Galvanized path must not store facade_regulated answer",
    )
    _assert(
        not by_case["outdoor_galvanized_embedded"]["regulated_in_active_filters"],
        "Galvanized path must not write regulated filter",
    )
    _assert(
        by_case["outdoor_galvanized_embedded"]["final_action"] == "show_product",
        "Galvanized path must still return products",
    )
    _assert(
        not by_case["outdoor_galvanized_embedded"]["useless_fallback_shown"],
        "Galvanized path showed useless fallback",
    )
    _assert(
        by_case["outdoor_stainless_embedded"]["action_after_stainless"] == "ask_question",
        "Stainless path must continue with the next question",
    )
    _assert(
        "встраиваемая или накладная" in by_case["outdoor_stainless_embedded"]["reply_after_stainless"].lower(),
        "Stainless path should go to mount question right after material",
    )
    _assert(
        "over_4m2" not in by_case["outdoor_stainless_embedded"]["size_step_values"],
        "Stainless size step must not expose over_4m2 option",
    )
    _assert(
        not any("более 4 м²" == label.lower() for label in by_case["outdoor_stainless_embedded"]["size_step_labels"]),
        "Stainless size step must not show 'Более 4 м²'",
    )
    _assert(
        not by_case["outdoor_stainless_embedded"]["regulated_question_seen"],
        "Stainless path must not ask regulated question",
    )
    _assert(
        not by_case["outdoor_stainless_embedded"]["facade_regulated_in_detail_answers"],
        "Stainless path must not store facade_regulated answer",
    )
    _assert(
        not by_case["outdoor_stainless_embedded"]["regulated_in_active_filters"],
        "Stainless path must not write regulated filter",
    )
    _assert(
        by_case["outdoor_stainless_embedded"]["final_action"] == "show_product",
        "Stainless path must still return products",
    )
    _assert(
        not by_case["outdoor_stainless_embedded"]["useless_fallback_shown"],
        "Stainless path showed useless fallback",
    )
    _assert(
        by_case["outdoor_galvanized_invalid_regulated"]["action_after_invalid"] == "ask_question",
        "Manual regulated for galvanized must be rejected",
    )
    _assert(
        by_case["outdoor_galvanized_invalid_regulated"]["detail_answers_before"]
        == by_case["outdoor_galvanized_invalid_regulated"]["detail_answers_after"],
        "Manual regulated for galvanized must not change detail_answers",
    )
    _assert(
        by_case["outdoor_galvanized_invalid_regulated"]["active_filters_before"]
        == by_case["outdoor_galvanized_invalid_regulated"]["active_filters_after"],
        "Manual regulated for galvanized must not change active_filters",
    )
    _assert(
        by_case["outdoor_stainless_invalid_regulated"]["action_after_invalid"] == "ask_question",
        "Manual regulated for stainless must be rejected",
    )
    _assert(
        by_case["outdoor_stainless_invalid_regulated"]["detail_answers_before"]
        == by_case["outdoor_stainless_invalid_regulated"]["detail_answers_after"],
        "Manual regulated for stainless must not change detail_answers",
    )
    _assert(
        by_case["outdoor_stainless_invalid_regulated"]["active_filters_before"]
        == by_case["outdoor_stainless_invalid_regulated"]["active_filters_after"],
        "Manual regulated for stainless must not change active_filters",
    )
    _assert(
        by_case["outdoor_stainless_invalid_over4m2"]["action_after_invalid"] == "ask_question",
        "Manual over_4m2 for stainless must be rejected",
    )
    _assert(
        by_case["outdoor_stainless_invalid_over4m2"]["detail_answers_before"]
        == by_case["outdoor_stainless_invalid_over4m2"]["detail_answers_after"],
        "Manual over_4m2 for stainless must not change detail_answers",
    )
    _assert(
        by_case["outdoor_stainless_invalid_over4m2"]["active_filters_before"]
        == by_case["outdoor_stainless_invalid_over4m2"]["active_filters_after"],
        "Manual over_4m2 for stainless must not change active_filters",
    )
    _assert(
        by_case["outdoor_aluminum_control"]["regulated_question_seen"],
        "Aluminum control path must still ask regulated question",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
