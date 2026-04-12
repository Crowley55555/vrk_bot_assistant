"""
Regression matrix for ceiling_air_direction + direction family filter.
Run: python scripts/ceiling_direction_regression.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import (  # noqa: E402
    _FOUR_WAY_PRIMARY_MARKERS,
    _build_search_query,
    _ceiling_direction_allowed_primary_markers,
    _ceiling_diversity_rerank,
    _ceiling_only_guard,
    _ceiling_primary_marker,
    _ceiling_query_mode,
    _filter_ceiling_results_by_direction_allowed,
    _filter_ceiling_results_by_marker,
    _get_scenario,
    _get_session,
    _product_data_list,
    _recommend_series,
    _reset_funnel,
    _resolve_ceiling_model_for_search,
    _search_with_fallback,
)

ALL_MARKERS = frozenset({"1pr", "2pr", "2prm", "3pr", "4pr", "4pr-s", "4sa", "4pp", "4ps", "4a"})


def _setup_session(sid: str, detail_answers: dict) -> None:
    _reset_funnel(sid)
    s = _get_session(sid)
    da = dict(detail_answers)
    da["ceiling_model"] = _resolve_ceiling_model_for_search(da)
    s["detail_branch"] = "indoor"
    s["detail_answers"] = da
    s["scenario_key"] = "grille"
    s["funnel_phase"] = "detail"
    s["active_filters"] = {
        "product_type": "grille",
        "location": "indoor",
        "size_group": "small",
        "material": da.get("ceiling_material") or "aluminum",
        "regulated": "fixed"
        if (da.get("ceiling_valve") or "") == "no"
        else "regulated"
        if (da.get("ceiling_valve") or "") == "yes"
        else None,
    }
    if s["active_filters"]["regulated"] is None:
        s["active_filters"].pop("regulated", None)
    s["allowed_subcats"] = ["reshetki-potolochnye"]


def _indoor_query_additions_for_ceiling(da: dict) -> list[str]:
    parts = ["потолочная решетка"]
    size_hint_map = {
        "armstrong_600": "600x600 595x595 armstrong",
        "up_to_1000": "до 1000 мм",
        "over_1000": "более 1000 мм",
    }
    mat_hint_map = {
        "aluminum": "алюминиевая",
        "galvanized": "стальная оцинкованная",
    }
    valve_hint_map = {
        "yes": "с клапаном крв",
        "no": "без клапана",
    }
    sb = (da.get("ceiling_size_bucket") or "").strip()
    if size_hint_map.get(sb):
        parts.append(size_hint_map[sb])
    cm = (da.get("ceiling_material") or "").strip()
    if mat_hint_map.get(cm):
        parts.append(mat_hint_map[cm])
    cv = (da.get("ceiling_valve") or "").strip()
    if valve_hint_map.get(cv):
        parts.append(valve_hint_map[cv])
    model = (da.get("ceiling_model") or "").strip()
    if model and model != "any":
        parts.append(model.replace("-", " "))
    return parts


def run_ceiling_pipeline(sid: str) -> dict:
    """Mirror _detail_search ceiling chain: search -> guard -> recovery -> direction -> marker -> diversity."""
    s = _get_session(sid)
    da = dict(s.get("detail_answers") or {})
    da["ceiling_model"] = _resolve_ceiling_model_for_search(da)
    s["detail_answers"] = da

    indoor_query_additions = _indoor_query_additions_for_ceiling(da)
    recommendation = _recommend_series(sid)
    query = _build_search_query(sid)
    if indoor_query_additions:
        query += " " + " ".join(indoor_query_additions)
    if recommendation:
        query += " " + recommendation.split(":")[1].strip() if ":" in recommendation else ""

    scenario = _get_scenario(sid)
    subcats = s.get("allowed_subcats") or None
    detail_n_results = 25

    results = _search_with_fallback(
        query,
        s["active_filters"],
        scenario,
        subcats,
        n_results=detail_n_results,
        detail_branch=s.get("detail_branch"),
    )
    ceiling_recovery_reason = ""
    answers = s.get("detail_answers") or {}
    selected_model = (answers.get("ceiling_model") or "").strip()
    selected_valve = (answers.get("ceiling_valve") or "").strip()
    selected_size_bucket = (answers.get("ceiling_size_bucket") or "").strip()
    query_mode = _ceiling_query_mode(selected_model, selected_valve, selected_size_bucket)
    broad_ceiling_mode = query_mode != "model_specific"

    base_count = len(results)
    results = _ceiling_only_guard(results)

    if query_mode == "model_specific" and len(results) <= 1:
        relaxed_filters = dict(s["active_filters"])
        for k in ("size_group", "material", "regulated"):
            relaxed_filters.pop(k, None)
        recovered = _search_with_fallback(
            query,
            relaxed_filters,
            scenario,
            subcats,
            n_results=detail_n_results,
            detail_branch=s.get("detail_branch"),
        )
        recovered = _ceiling_only_guard(recovered)
        if len(recovered) > len(results):
            results = recovered
            ceiling_recovery_reason = "overconstrained_metadata"
    elif broad_ceiling_mode and len(results) <= 1:
        relaxed_filters = dict(s["active_filters"])
        if "size_group" in relaxed_filters:
            relaxed_filters.pop("size_group", None)
        if selected_valve != "yes":
            relaxed_filters.pop("regulated", None)
        recovered = _search_with_fallback(
            query,
            relaxed_filters,
            scenario,
            subcats,
            n_results=detail_n_results,
            detail_branch=s.get("detail_branch"),
        )
        recovered = _ceiling_only_guard(recovered)
        if len(recovered) > len(results):
            results = recovered
            ceiling_recovery_reason = "overconstrained_metadata"

    before_dir = len(results)
    allowed_dir = _ceiling_direction_allowed_primary_markers(answers)
    if allowed_dir is not None:
        results = _filter_ceiling_results_by_direction_allowed(results, allowed_dir)
    after_dir = len(results)

    retrieval_count_before = len(results)
    results, matched_markers, ceiling_stats = _filter_ceiling_results_by_marker(
        results,
        selected_model,
        selected_valve,
    )

    diversity_applied = False
    final_result_markers: list[str] = sorted(
        {
            _ceiling_primary_marker((r.get("metadata", {}) or {}))
            for r in results
            if _ceiling_primary_marker((r.get("metadata", {}) or {})) != "_other"
        }
    )
    if broad_ceiling_mode:
        results, diversity_applied, final_result_markers = _ceiling_diversity_rerank(results)

    products = _product_data_list(results, n=5)
    # Те же строки, что и в _product_data_list (dedupe по url), для проверки маркеров на карточках
    card_markers: list[str] = []
    seen_urls: set[str] = set()
    for r in results:
        if len(card_markers) >= 5:
            break
        meta = r.get("metadata") or {}
        url = meta.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        card_markers.append(_ceiling_primary_marker(meta))
    return {
        "query_mode": query_mode,
        "broad_ceiling_mode": broad_ceiling_mode,
        "before_direction_filter": before_dir,
        "after_direction_filter": after_dir,
        "allowed_dir": sorted(allowed_dir) if allowed_dir else None,
        "final_result_markers": final_result_markers,
        "diversity_applied": diversity_applied,
        "retrieval_before_marker": retrieval_count_before,
        "products_count": len(products),
        "card_markers": card_markers,
        "ceiling_recovery_reason": ceiling_recovery_reason,
    }


def main() -> None:
    base = {
        "indoor_type": "ceiling",
        "ceiling_size_bucket": "armstrong_600",
        "ceiling_material": "aluminum",
        "ceiling_valve": "no",
    }
    cases = [
        ("1_one", {**base, "ceiling_air_direction": "one"}, frozenset({"1pr", "2prm"}), frozenset({"2pr", "3pr", "4pr", "4pp", "4ps", "4sa", "4a", "4pr-s"})),
        ("2_two", {**base, "ceiling_air_direction": "two"}, frozenset({"2pr"}), frozenset({"1pr", "2prm", "3pr", "4pr", "4pp", "4ps", "4sa", "4a", "4pr-s"})),
        ("3_three", {**base, "ceiling_air_direction": "three"}, frozenset({"3pr"}), frozenset({"1pr", "2pr", "2prm", "4pr", "4pp", "4ps", "4sa", "4a", "4pr-s"})),
        ("4_four_ordinary", {**base, "ceiling_air_direction": "four", "ceiling_face_type": "ordinary"}, frozenset({"4pr", "4sa", "4a"}), frozenset({"1pr", "2pr", "2prm", "3pr", "4pp", "4ps", "4pr-s"})),
        ("5_four_perf", {**base, "ceiling_air_direction": "four", "ceiling_face_type": "perforated"}, frozenset({"4pp"}), ALL_MARKERS - frozenset({"4pp"})),
        ("6_four_honey", {**base, "ceiling_air_direction": "four", "ceiling_face_type": "honeycomb"}, frozenset({"4ps"}), ALL_MARKERS - frozenset({"4ps"})),
        ("7_four_steel", {**base, "ceiling_air_direction": "four", "ceiling_face_type": "steel_strong"}, frozenset({"4pr-s"}), ALL_MARKERS - frozenset({"4pr-s"})),
        (
            "8_four_unknown_face",
            {**base, "ceiling_air_direction": "four", "ceiling_face_type": "unknown"},
            _FOUR_WAY_PRIMARY_MARKERS,
            frozenset({"1pr", "2pr", "2prm", "3pr"}),
        ),
        ("9_unknown_dir", {**base, "ceiling_air_direction": "unknown"}, None, None),
    ]

    print("ceiling direction regression matrix\n")
    for name, da, exp_allowed, forbidden in cases:
        sid = f"reg-{name}"
        _setup_session(sid, da)
        got_allowed = _ceiling_direction_allowed_primary_markers(_get_session(sid)["detail_answers"] or {})
        if exp_allowed is not None and got_allowed != exp_allowed:
            print(f"{name} ALLOWED_MISMATCH expected={sorted(exp_allowed)} got={sorted(got_allowed) if got_allowed else None}")
        out = run_ceiling_pipeline(sid)
        markers_union = set(out["final_result_markers"]) | {m for m in out["card_markers"] if m != "_other"}
        fail = False
        if forbidden is not None:
            if markers_union & forbidden:
                fail = True
        if exp_allowed is not None and markers_union - exp_allowed - {"_other"}:
            fail = True
        status = "FAIL" if fail else "PASS"
        print(f"=== {name} === [{status}]")
        print(f"  input: direction={da.get('ceiling_air_direction')} face={da.get('ceiling_face_type')}")
        print(f"  allowed families (code): {out['allowed_dir']}")
        print(f"  before direction filter: {out['before_direction_filter']}  after: {out['after_direction_filter']}")
        print(f"  final_result_markers: {out['final_result_markers']}")
        print(f"  products_count: {out['products_count']}  card_markers: {out['card_markers']}")
        print(f"  diversity_applied: {out['diversity_applied']}  query_mode: {out['query_mode']}")
        print()


if __name__ == "__main__":
    main()
