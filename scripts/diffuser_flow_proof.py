"""
Runtime proof for diffuser guided flow.

Run:
    python scripts/diffuser_flow_proof.py
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

import main as main_module  # noqa: E402
from main import (  # noqa: E402
    _extract_diffuser_hints,
    _extract_filters_from_text,
    _get_session,
    _reset_funnel,
    process_message,
)
from models import ChatAction, ChatRequest  # noqa: E402

PROOF_CASES = [
    {"case": "button_exhaust", "query": "диффузор", "purpose_choice": "exhaust"},
    {"case": "button_safe_mixed", "query": "диффузор", "purpose_choice": "unknown"},
    {"case": "text_exhaust", "query": "вытяжной диффузор"},
    {"case": "text_supply", "query": "приточный диффузор"},
    {"case": "text_supply_exhaust", "query": "приточно-вытяжной диффузор"},
    {"case": "broad_diffuser", "query": "диффузор"},
]


async def _fake_ask_llm(user_message: str, session_id: str, context: str) -> str:
    return "proof-llm-disabled"


main_module._ask_llm = _fake_ask_llm
logging.disable(logging.CRITICAL)


def _combined_extracted(query: str) -> dict[str, str]:
    extracted = _extract_filters_from_text(query)
    diffuser_hints = _extract_diffuser_hints(query)
    if diffuser_hints:
        extracted.update({k: v for k, v in diffuser_hints.items() if v})
        if extracted.get("product_type") == "diffuser":
            extracted.pop("grille_mount", None)
            extracted.pop("grille_feature", None)
    return extracted


def _pick_button(
    reply: str,
    buttons: list[dict],
    extracted: dict[str, str],
    case: dict,
) -> str | None:
    if not buttons:
        return None

    by_value = {btn["value"]: btn["label"] for btn in buttons}
    available_values = [btn["value"] for btn in buttons]
    reply_lower = reply.lower()

    def choose(preferred: list[str]) -> str | None:
        for value in preferred:
            if value in by_value:
                return value
        if "unknown" in by_value:
            return "unknown"
        for value in available_values:
            if value != "unknown":
                return value
        return buttons[0]["value"]

    if "какой тип диффузора" in reply_lower:
        return choose([extracted.get("diffuser_type", "")])
    if "для какого типа монтажа нужен диффузор скрытого монтажа" in reply_lower:
        return choose([extracted.get("diffuser_shadow_mount", "")])
    if "для чего нужен диффузор" in reply_lower:
        if case.get("purpose_choice"):
            return choose([str(case["purpose_choice"])])
        return choose([extracted.get("diffuser_purpose", "")])
    if "где будет установлен диффузор" in reply_lower:
        return choose([extracted.get("diffuser_install", "")])
    if "какая форма нужна" in reply_lower:
        return choose([extracted.get("diffuser_form", "")])
    if "какой размер подключения нужен" in reply_lower:
        return choose([extracted.get("diffuser_swirl_round_size", "")])
    if "какой размер подключения" in reply_lower:
        return choose([extracted.get("diffuser_diameter", "")])
    if "нужна ли регулировка" in reply_lower:
        return choose([extracted.get("diffuser_adjustable", "")])
    return choose([])


def _buttons_to_dicts(response) -> list[dict]:
    return [{"label": btn.label, "value": btn.value} for btn in (response.buttons or [])]


async def _run_single(case: dict, index: int) -> dict:
    query = str(case["query"])
    sid = f"diffuser-proof-{index}"
    _reset_funnel(sid)
    extracted = _combined_extracted(query)

    asked_questions: list[str] = []
    chosen_answers: list[str] = []
    purpose_step_button_labels: list[str] = []
    purpose_step_button_values: list[str] = []
    first_reply = ""
    first_session_snapshot: dict[str, str] = {}

    response = await process_message(ChatRequest(message=query, session_id=sid))
    final_response = response
    first_reply = response.reply
    session = _get_session(sid)
    first_session_snapshot = {
        "scenario_key": str(session.get("scenario_key") or ""),
        "funnel_phase": str(session.get("funnel_phase") or ""),
        "detail_branch": str(session.get("detail_branch") or ""),
        "active_product_type": str((session.get("active_filters") or {}).get("product_type") or ""),
    }

    for _ in range(8):
        if response.action != ChatAction.ASK_QUESTION or not response.buttons:
            break
        asked_questions.append(response.reply)
        current_buttons = _buttons_to_dicts(response)
        if "для чего нужен диффузор" in response.reply.lower() and not purpose_step_button_labels:
            purpose_step_button_labels = [btn["label"] for btn in current_buttons]
            purpose_step_button_values = [btn["value"] for btn in current_buttons]
        answer = _pick_button(response.reply, current_buttons, extracted, case)
        if not answer:
            break
        chosen_answers.append(answer)
        response = await process_message(ChatRequest(message=answer, session_id=sid))
        final_response = response

    final_response = response
    products = final_response.products or []
    final_families = sorted({p.get("category", "") for p in products if p.get("category")})
    final_names = [p.get("name", "") for p in products]
    useless_fallback = any(
        token in (final_response.reply or "").lower()
        for token in ("уточните категорию", "уточните параметры", "уточните запрос текстом")
    )
    grille_intercept = (
        first_session_snapshot.get("scenario_key") == "grille"
        or first_session_snapshot.get("active_product_type") == "grille"
        or "решетк" in first_reply.lower()
        or "решётк" in first_reply.lower()
    )
    first_question = asked_questions[0] if asked_questions else ""
    purpose_mentions = sum("для чего нужен диффузор" in question.lower() for question in asked_questions)
    form_mentions = sum("какая форма нужна" in question.lower() for question in asked_questions)
    swirl_round_size_mentions = sum("какой размер подключения нужен" in question.lower() for question in asked_questions)
    top_result_name = final_names[0] if final_names else ""

    return {
        "case": case["case"],
        "query": query,
        "recognized_intent": extracted.get("product_type") or "unknown",
        "extracted_facets": extracted,
        "first_reply": first_reply,
        "first_session_snapshot": first_session_snapshot,
        "asked_questions_sequence": asked_questions,
        "chosen_answers": chosen_answers,
        "purpose_step_button_labels": purpose_step_button_labels,
        "purpose_step_button_values": purpose_step_button_values,
        "final_action": final_response.action.value,
        "final_result_families": final_families,
        "final_result_names": final_names,
        "top_result_name": top_result_name,
        "products_count": len(products),
        "was_useless_fallback_shown": "yes" if useless_fallback else "no",
        "grille_intercept": "yes" if grille_intercept else "no",
        "first_question": first_question,
        "purpose_is_first_question": "yes"
        if "для чего нужен диффузор" in first_question.lower()
        else "no",
        "purpose_question_asked_count": purpose_mentions,
        "purpose_step_button_count": len(purpose_step_button_labels),
        "was_form_question_shown": "yes" if form_mentions else "no",
        "was_swirl_round_size_step_shown": "yes" if swirl_round_size_mentions else "no",
    }


async def main() -> None:
    report = []
    for idx, case in enumerate(PROOF_CASES, start=1):
        report.append(await _run_single(case, idx))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
