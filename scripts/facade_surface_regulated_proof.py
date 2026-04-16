"""
Runtime proof for facade outdoor surface regulation step.

Run:
    python scripts/facade_surface_regulated_proof.py
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
from models import ChatAction, ChatRequest  # noqa: E402


async def _fake_ask_llm(user_message: str, session_id: str, context: str) -> str:
    return "proof-llm-disabled"


main_module._ask_llm = _fake_ask_llm
logging.disable(logging.CRITICAL)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _buttons_payload(response) -> list[dict[str, str]]:
    return [{"label": btn.label, "value": btn.value} for btn in (response.buttons or [])]


def _contains_regulated_question(reply: str, buttons: list[dict[str, str]]) -> bool:
    reply = (reply or "").lower()
    buttons_text = " ".join(btn["label"].lower() for btn in buttons)
    return (
        "нужна ли регулировка потока воздуха" in reply
        and "нерегулируемая" in buttons_text
        and "регулируемая" in buttons_text
    )


def _useless_fallback(reply: str) -> bool:
    text = (reply or "").lower()
    return any(
        token in text
        for token in ("уточните категорию", "уточните параметры", "уточните запрос текстом")
    )


async def _walk_path(session_id: str, answers: list[str]) -> dict:
    _reset_funnel(session_id)
    transcript: list[dict] = []
    response = None
    scripted_answers = ["подобрать", *answers]
    for answer in scripted_answers:
        response = await process_message(ChatRequest(message=answer, session_id=session_id))
        transcript.append(
            {
                "answer": answer,
                "reply": response.reply,
                "action": response.action.value,
                "buttons": _buttons_payload(response),
                "products_count": len(response.products or []),
            }
        )
    session = _get_session(session_id)
    return {
        "transcript": transcript,
        "last_response": transcript[-1] if transcript else {},
        "session_snapshot": {
            "detail_branch": session.get("detail_branch"),
            "detail_answers": dict(session.get("detail_answers") or {}),
            "active_filters": dict(session.get("active_filters") or {}),
            "allowed_subcats": list(session.get("allowed_subcats") or []),
        },
        "last_action": response.action if response else None,
        "last_products_count": len((response.products or [])) if response else 0,
        "last_reply": response.reply if response else "",
    }


async def _case_surface_question() -> dict:
    run = await _walk_path(
        "facade-surface-question",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "surface"],
    )
    last = run["transcript"][-1]
    return {
        "case": "surface_question_after_mount",
        "after_surface_action": last["action"],
        "after_surface_reply": last["reply"],
        "after_surface_buttons": last["buttons"],
        "regulated_question_shown": _contains_regulated_question(last["reply"], last["buttons"]),
        "search_started_before_regulated": last["action"] != ChatAction.ASK_QUESTION.value,
        "detail_answers_after_surface": run["session_snapshot"]["detail_answers"],
    }


async def _case_surface_result(choice: str) -> dict:
    run = await _walk_path(
        f"facade-surface-{choice}",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "surface", choice, "under_2m2"],
    )
    return {
        "case": f"surface_{choice}_result",
        "final_action": run["last_action"].value if run["last_action"] else "",
        "final_reply": run["last_reply"],
        "products_count": run["last_products_count"],
        "active_filters": run["session_snapshot"]["active_filters"],
        "detail_answers": run["session_snapshot"]["detail_answers"],
        "useless_fallback_shown": _useless_fallback(run["last_reply"]),
    }


async def _case_round_regression() -> dict:
    run = await _walk_path("facade-round-control", ["grille", "outdoor", "round"])
    transcript = run["transcript"]
    replies = [item["reply"] for item in transcript]
    return {
        "case": "round_control",
        "asked_replies": replies,
        "regulated_question_seen": any("нужна ли регулировка потока воздуха" in (reply or "").lower() for reply in replies),
        "mount_question_seen": any("встраиваемая или накладная" in (reply or "").lower() for reply in replies),
        "final_action": run["last_action"].value if run["last_action"] else "",
    }


async def _case_embedded_control() -> dict:
    run = await _walk_path(
        "facade-embedded-control",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "embedded"],
    )
    last = run["transcript"][-1]
    return {
        "case": "embedded_control",
        "after_embedded_action": last["action"],
        "after_embedded_reply": last["reply"],
        "after_embedded_buttons": last["buttons"],
        "regulated_question_shown": _contains_regulated_question(last["reply"], last["buttons"]),
    }


async def main() -> None:
    report = [
        await _case_surface_question(),
        await _case_surface_result("regulated"),
        await _case_surface_result("fixed"),
        await _case_round_regression(),
        await _case_embedded_control(),
    ]
    by_case = {item["case"]: item for item in report}
    _assert(by_case["surface_question_after_mount"]["regulated_question_shown"], "Surface path must ask regulated question right after mount")
    _assert(not by_case["surface_question_after_mount"]["search_started_before_regulated"], "Surface path must not start search before regulated question")
    _assert(by_case["surface_regulated_result"]["final_action"] != "ask_question", "Surface regulated path must complete search after size")
    _assert(by_case["surface_fixed_result"]["final_action"] != "ask_question", "Surface fixed path must complete search after size")
    _assert(not by_case["surface_regulated_result"]["useless_fallback_shown"], "Surface regulated path showed useless fallback")
    _assert(not by_case["surface_fixed_result"]["useless_fallback_shown"], "Surface fixed path showed useless fallback")
    _assert(not by_case["round_control"]["regulated_question_seen"], "Round facade control must not ask regulated question")
    _assert(not by_case["round_control"]["mount_question_seen"], "Round facade control must not ask mount question")
    _assert(not by_case["embedded_control"]["regulated_question_shown"], "Embedded control should keep its next step unchanged")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
