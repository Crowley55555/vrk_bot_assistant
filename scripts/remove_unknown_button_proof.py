"""
Runtime proof for removing the exact "Не знаю" button.

Run:
    python scripts/remove_unknown_button_proof.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import (  # noqa: E402
    _activate_scenario,
    _detail_step_response,
    _get_session,
    _goto_main_menu,
    _reset_funnel,
)


def _buttons_payload(response) -> dict:
    labels = [btn.label for btn in (response.buttons or [])]
    return {
        "reply": response.reply,
        "button_labels": labels,
        "has_exact_unknown_button": "Не знаю" in labels,
    }


def main() -> None:
    report: dict[str, object] = {}

    start_sid = "proof-unknown-start"
    _reset_funnel(start_sid)
    report["start_step"] = _buttons_payload(_goto_main_menu(start_sid))

    funnel_sid = "proof-unknown-funnel"
    _reset_funnel(funnel_sid)
    report["scenario_step"] = _buttons_payload(_activate_scenario(funnel_sid, "grille"))

    detail_sid = "proof-unknown-detail"
    _reset_funnel(detail_sid)
    detail_session = _get_session(detail_sid)
    detail_session["detail_branch"] = "diffuser"
    detail_session["funnel_phase"] = "detail"
    detail_session["detail_step_idx"] = 0
    detail_session["detail_answers"] = {"diffuser_purpose": "unknown"}
    report["detail_step"] = _buttons_payload(_detail_step_response(detail_sid))

    transfer_sid = "proof-unknown-transfer"
    _reset_funnel(transfer_sid)
    transfer_session = _get_session(transfer_sid)
    transfer_session["detail_branch"] = "indoor"
    transfer_session["funnel_phase"] = "detail"
    transfer_session["detail_step_idx"] = 0
    transfer_session["detail_answers"] = {"indoor_type": "transfer"}
    report["transfer_step_control"] = _buttons_payload(_detail_step_response(transfer_sid))

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
