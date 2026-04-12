#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fetch_transfer_results() -> list[dict]:
    filters = {
        "product_type": "grille",
        "location": "indoor",
        "material": "aluminum",
        "size_group": "small",
    }
    scenario = main.FUNNEL_SCENARIOS["grille"]
    return main._search_with_fallback(
        "переточная решетка",
        filters,
        scenario,
        allowed_subcats=["reshetki-peretochnye"],
        detail_branch="indoor",
        n_results=12,
    )


def _markers(results: list[dict]) -> list[str]:
    return sorted({
        main._transfer_execution_marker((r.get("metadata", {}) or {}))
        for r in results
    })


def test_execution_modes() -> None:
    base = _fetch_transfer_results()
    base_markers = _markers(base)
    _assert(len(base) >= 3, f"Expected at least 3 transfer results, got {len(base)}")
    _assert(
        {"acoustic", "no_frame", "standard"}.issubset(set(base_markers)),
        f"Expected all execution markers in base results, got {base_markers}",
    )

    acoustic = main._filter_transfer_results_by_execution(base, "acoustic")
    no_frame = main._filter_transfer_results_by_execution(base, "no_frame")
    standard = main._filter_transfer_results_by_execution(base, "standard")
    any_mode = main._filter_transfer_results_by_execution(base, "any")

    _assert(_markers(acoustic) == ["acoustic"], f"acoustic markers mismatch: {_markers(acoustic)}")
    _assert(_markers(no_frame) == ["no_frame"], f"no_frame markers mismatch: {_markers(no_frame)}")
    _assert(_markers(standard) == ["standard"], f"standard markers mismatch: {_markers(standard)}")
    _assert(
        set(_markers(any_mode)) == set(base_markers),
        f"any markers mismatch: {_markers(any_mode)} vs {base_markers}",
    )


def test_multiple_items_not_truncated() -> None:
    synthetic = [
        {"metadata": {"name": "ПР-АКУСТИК 1", "article": "A1", "raw_attrs_json": ""}},
        {"metadata": {"name": "ПР-АКУСТИК 2", "article": "A2", "raw_attrs_json": ""}},
        {"metadata": {"name": "Переточная решетка без ответной рамки ПР-БР", "article": "B1", "raw_attrs_json": ""}},
    ]
    filtered = main._filter_transfer_results_by_execution(synthetic, "acoustic")
    _assert(len(filtered) == 2, f"Expected 2 acoustic items, got {len(filtered)}")


def test_material_size_constraints_preserved() -> None:
    filters = {
        "product_type": "grille",
        "location": "indoor",
        "material": "aluminum",
        "size_group": "small",
    }
    scenario = main.FUNNEL_SCENARIOS["grille"]
    base = main._search_with_fallback(
        "переточная решетка",
        filters,
        scenario,
        allowed_subcats=["reshetki-peretochnye"],
        detail_branch="indoor",
        n_results=12,
    )
    acoustic = main._filter_transfer_results_by_execution(base, "acoustic")
    _assert(len(acoustic) >= 1, "Expected at least 1 acoustic result")
    by_id = {
        (r.get("id") or (r.get("metadata", {}) or {}).get("url") or f"idx-{i}"): r
        for i, r in enumerate(base)
    }
    for i, r in enumerate(acoustic):
        rid = r.get("id") or (r.get("metadata", {}) or {}).get("url") or f"ac-{i}"
        _assert(rid in by_id, "Transfer execution filter introduced a non-base result")
        base_meta = (by_id[rid].get("metadata", {}) or {})
        cur_meta = (r.get("metadata", {}) or {})
        _assert(
            (base_meta.get("material", "") or "").strip() == (cur_meta.get("material", "") or "").strip(),
            "Material changed after transfer execution filtering",
        )
        _assert(
            (base_meta.get("size_group", "") or "").strip() == (cur_meta.get("size_group", "") or "").strip(),
            "Size group changed after transfer execution filtering",
        )


def main_test() -> int:
    tests = [
        test_execution_modes,
        test_multiple_items_not_truncated,
        test_material_size_constraints_preserved,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("ALL OK: transfer execution tests passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
