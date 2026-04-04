#!/usr/bin/env python3
"""
Smoke: POST /api/chat — action, карточки, проверка Konika.

  set SMOKE_BASE_URL=http://127.0.0.1:8000
  python scripts/smoke_chat.py
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Запросы из runtime-proof
MESSAGES: list[str] = [
    "расскажи про Konika",
    "что такое VLL-S",
    "нужен PV",
    "покажи диффузоры",
    "аналоги Konika",
]


def _product_has_konika(p: dict[str, Any]) -> bool:
    blob = " ".join(
        str(p.get(k, "") or "")
        for k in ("name", "article", "url")
    ).lower()
    return "konika" in blob


def _print_products(products: list[dict[str, Any]]) -> None:
    for i, p in enumerate(products, 1):
        print(
            f"    [{i}] name={p.get('name')!r}\n"
            f"        article={p.get('article')!r}\n"
            f"        url={p.get('url')!r}"
        )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    url = f"{BASE_URL}/api/chat"
    print(f"BASE_URL={BASE_URL}")
    print(f"POST {url}\n")

    konika_ok = True
    for msg in MESSAGES:
        sid = str(uuid.uuid4())
        payload = {"message": msg, "session_id": sid, "source": "smoke"}
        try:
            r = httpx.post(url, json=payload, timeout=180.0)
        except httpx.RequestError as e:
            print(f"FAIL request: {msg!r}\n  {e}")
            return 2

        if r.status_code != 200:
            print(f"FAIL {r.status_code}: {msg!r}\n  {r.text[:800]}")
            return 1

        data = r.json()
        action = data.get("action", "?")
        reply = data.get("reply") or ""
        products = data.get("products") or []
        if not isinstance(products, list):
            products = []
        n = len(products)

        print(f"message: {msg!r}")
        print(f"  action={action}")
        print(f"  products_count={n}")
        print(f"  reply_preview: {reply[:200]!r}{'...' if len(reply) > 200 else ''}")
        if products:
            print("  cards:")
            _print_products(products)
        else:
            print("  cards: (none)")

        if msg == "расскажи про Konika" and products:
            bad = [p for p in products if not _product_has_konika(p)]
            if bad:
                konika_ok = False
                print(f"  KONIKA CHECK FAIL: {len(bad)} card(s) without konika in name/article/url")
                _print_products(bad[:5])
            else:
                print("  KONIKA CHECK OK: all cards contain 'konika' in name, article, or url")
        elif msg == "расскажи про Konika" and not products:
            print("  KONIKA CHECK: no products (expected if no match or ASK_QUESTION-only path)")
        print()

    if not konika_ok:
        print("RESULT: FAILED — Konika assertion (foreign cards).")
        return 1
    print("RESULT: OK — all HTTP 200; Konika cards OK or empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
