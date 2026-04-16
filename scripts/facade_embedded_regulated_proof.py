"""
Runtime proof for facade outdoor embedded regulated flow.

Run:
    python scripts/facade_embedded_regulated_proof.py
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
from main import _reset_funnel, process_message  # noqa: E402
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
            }
        )
    return transcript


async def _case_small_regulated() -> dict:
    transcript = await _walk_path(
        "facade-embedded-small",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "embedded", "under_2m2", "regulated"],
    )
    final_step = transcript[-1]
    products = final_step["products"]
    return {
        "case": "embedded_regulated_under_2m2",
        "final_action": final_step["action"],
        "final_reply": final_step["reply"],
        "products": products,
        "contains_vrn_r": any("ВРН-Р" in product for product in products),
        "contains_vrn_ur": any("ВРН-УР" in product for product in products),
        "useless_fallback_shown": _useless_fallback(final_step["reply"]),
    }


async def _case_large_regulated() -> dict:
    transcript = await _walk_path(
        "facade-embedded-large",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "embedded", "over_2m2", "yes", "regulated"],
    )
    final_step = transcript[-1]
    products = final_step["products"]
    return {
        "case": "embedded_regulated_over_2m2",
        "final_action": final_step["action"],
        "final_reply": final_step["reply"],
        "products": products,
        "top_product": products[0] if products else "",
        "contains_vrn_r": any("ВРН-Р" in product for product in products),
        "contains_vrn_ur": any("ВРН-УР" in product for product in products),
        "useless_fallback_shown": _useless_fallback(final_step["reply"]),
    }


async def _case_fixed_control() -> dict:
    transcript = await _walk_path(
        "facade-embedded-fixed",
        ["grille", "outdoor", "rectangular", "standard", "aluminum", "embedded", "under_2m2", "fixed"],
    )
    final_step = transcript[-1]
    return {
        "case": "embedded_fixed_control",
        "final_action": final_step["action"],
        "final_reply": final_step["reply"],
        "products": final_step["products"],
        "useless_fallback_shown": _useless_fallback(final_step["reply"]),
    }


async def main() -> None:
    report = [
        await _case_small_regulated(),
        await _case_large_regulated(),
        await _case_fixed_control(),
    ]
    by_case = {item["case"]: item for item in report}
    _assert(by_case["embedded_regulated_under_2m2"]["final_action"] == "show_product", "Small regulated facade path must end with products")
    _assert(by_case["embedded_regulated_under_2m2"]["contains_vrn_r"], "Small regulated facade path must include ВРН-Р")
    _assert(by_case["embedded_regulated_under_2m2"]["contains_vrn_ur"], "Small regulated facade path must include ВРН-УР")
    _assert(by_case["embedded_regulated_over_2m2"]["final_action"] == "show_product", "Large regulated facade path must end with products")
    _assert(by_case["embedded_regulated_over_2m2"]["contains_vrn_r"], "Large regulated facade path must still include ВРН-Р")
    _assert(by_case["embedded_regulated_over_2m2"]["contains_vrn_ur"], "Large regulated facade path must include ВРН-УР")
    _assert("ВРН-УР" in by_case["embedded_regulated_over_2m2"]["top_product"], "Large regulated facade path must rank ВРН-УР first")
    _assert(not by_case["embedded_regulated_under_2m2"]["useless_fallback_shown"], "Small regulated facade path showed useless fallback")
    _assert(not by_case["embedded_regulated_over_2m2"]["useless_fallback_shown"], "Large regulated facade path showed useless fallback")
    _assert(by_case["embedded_fixed_control"]["final_action"] == "show_product", "Fixed facade control must still return products")
    _assert(not by_case["embedded_fixed_control"]["useless_fallback_shown"], "Fixed facade control showed useless fallback")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
