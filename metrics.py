from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx
import logging
log = logging.getLogger("metrics")


def _build_url(base: Optional[str]) -> Optional[str]:
    if not base:
        return None
    base = base.rstrip("/")
    if base.startswith("http://") or base.startswith("https://"):
        return base if base.endswith("/event") else f"{base}/event"
    url = f"https://{base}"
    return url if url.endswith("/event") else f"{url}/event"


async def track_event(
    event_name: str,
    user_id: int,
    session_id: str | None = None,
    game_id: str | None = None,
    properties: dict | None = None,
    payment: dict | None = None,
) -> None:
    url = _build_url(os.getenv("API_URL") or os.getenv("METRICS_URL"))
    api_key = os.getenv("METRICS_API_KEY")
    if not url or not api_key:
        return

    payload: dict[str, Any] = {
        "event_name": event_name,
        "user_id": user_id,
        "timestamp": int(time.time() * 1000),
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if game_id is not None:
        payload["game_id"] = game_id
    if properties is not None:
        payload["properties"] = properties
    if payment is not None:
        payload["payment"] = payment

    headers = {"X-API-Key": api_key}
    log.info("metrics payload: %s", payload)

    async def _send() -> None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(url, json=payload, headers=headers)

    try:
        await _send()
    except Exception:
        try:
            await asyncio.sleep(0.2)
            await _send()
        except Exception:
            return
