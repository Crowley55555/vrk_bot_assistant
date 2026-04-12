"""
Runtime proof for acoustic branch without size step.

Run:
    python scripts/acoustic_size_proof.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import process_message, _reset_funnel  # noqa: E402
from models import ChatRequest  # noqa: E402


async def main() -> None:
    sid = "acoustic-size-proof"
    _reset_funnel(sid)

    start = await process_message(ChatRequest(message="Вентиляционные решетки", session_id=sid))
    acoustic = await process_message(ChatRequest(message="acoustic", session_id=sid))
    after_material = await process_message(ChatRequest(message="aluminum", session_id=sid))

    report = {
        "start_reply": start.reply,
        "start_buttons": [b.label for b in (start.buttons or [])],
        "acoustic_reply": acoustic.reply,
        "acoustic_buttons": [b.label for b in (acoustic.buttons or [])],
        "after_material_reply": after_material.reply,
        "after_material_action": after_material.action.value,
        "after_material_buttons": [b.label for b in (after_material.buttons or [])],
        "size_question_present_after_material": "Какой примерный размер решетки?"
        in ((after_material.reply or "") + " " + " ".join(b.label for b in (after_material.buttons or []))),
        "products_count": len(after_material.products or []),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
