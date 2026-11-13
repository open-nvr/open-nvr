"""
A bounded yes/no re-rank of search's first page by the box's own language
model.

Fusion ranks by agreement between arms; it cannot read. "Two men looking
at a laptop" fuses to rows the words and the vectors each half-liked —
a man, a laptop, two people — and the caption that says exactly that
sits fourth. The captioner's model (Gemma on the Hexagon, through the
litertlm adapter's OpenAI-shaped ``/v1/chat/completions``) can read ten
captions and say which ones show what was asked, in one short call.
The rows it says yes to move to the front, in their fused order; the
rest keep theirs behind them. Nothing is dropped, nothing is invented.

Bounded on purpose: at most ``top`` captions, one call, a few tokens of
answer, a hard timeout, one call in flight at a time — and off unless an
operator turns it on (``SEARCH_RERANK_TOP``) or a caller asks
(``?rerank=10``), because every search pays its latency.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Task names an adapter advertises when it answers chat. The litertlm
#: captioner answers chat beside its captions without advertising it, so
#: the captioner's URL is the fallback, probed once.
CHAT_TASK_NAMES = {"chat", "llm", "text_generation", "chat_completion"}
CAPTION_TASK_NAMES = {"scene_caption", "image_captioning", "caption"}
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
MAX_CAPTION_CHARS = 240
_PROBE_TTL_S = 60.0
_probe_cache: dict[str, tuple[float, bool]] = {}
_one_at_a_time = asyncio.Semaphore(1)

#: Strict on purpose. The first wording ("a caption matches when it shows
#: what the search asks for") had Gemma say yes to nearly every caption
#: and the re-rank LOST ground on the labelled eval (MRR 0.762 → 0.731,
#: Ventuno Q 2026-11-11); this one, asking for every part of the search
#: and "when unsure, it is not a match", gained a little (0.762 → 0.775,
#: P@5 0.450 → 0.457) for ~250 ms a search. A best-three ranking prompt
#: scored the same (0.777). Small, measured, and why this is off by
#: default.
SYSTEM_PROMPT = (
    "You judge security-camera captions against a search. Be strict: a caption matches "
    "only when it clearly shows EVERY part of the search — the object, the colour, the "
    "clothing, the action, the count. Sharing a word is not a match; when unsure, it is "
    "not a match. Answer with the numbers of the matching captions, comma-separated, or "
    "the single word none. No other words."
)


def build_prompt(query: str, captions: list[str]) -> str:
    lines = [f"Search: {query.strip()}", ""]
    for i, cap in enumerate(captions, 1):
        text = " ".join((cap or "").split())[:MAX_CAPTION_CHARS]
        lines.append(f"{i}. {text or '(no caption)'}")
    lines.append("")
    lines.append("Matching captions:")
    return "\n".join(lines)


def parse_answer(text: str, n: int) -> list[int] | None:
    """The 1-based numbers the model named, in order, de-duplicated and
    within range; ``[]`` for "none"; ``None`` when the answer is not an
    answer (so the caller keeps the fused order and says so)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\W*none\W*", t):
        return []
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if not nums:
        return None
    seen: list[int] = []
    for x in nums:
        if 1 <= x <= n and x not in seen:
            seen.append(x)
    return seen


def apply(order: list[Any], yes: list[int]) -> list[Any]:
    """``order`` with the 1-based ``yes`` rows first (in their fused order),
    then the rest in theirs."""
    front = [order[i - 1] for i in sorted(yes)]
    back = [row for i, row in enumerate(order, 1) if i not in set(yes)]
    return front + back


async def _chat(url: str, payload: dict[str, Any], *, token: str, timeout_s: float) -> dict[str, Any]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def _probe(base: str, token: str) -> bool:
    now = time.monotonic()
    hit = _probe_cache.get(base)
    if hit and now - hit[0] < _PROBE_TTL_S:
        return hit[1]
    ok = False
    try:
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(base.rstrip("/") + MODELS_PATH, headers=headers)
            ok = r.status_code == 200
    except Exception:
        ok = False
    _probe_cache[base] = (now, ok)
    return ok


async def resolve_chat_base() -> str | None:
    """The base URL of an adapter that answers chat: one advertising a
    chat task, else the captioner (litertlm answers chat beside its
    captions), each confirmed by a cached ``/v1/models`` probe."""
    try:
        from routers.skills import _kai_c_view

        _health, caps = await _kai_c_view()
    except Exception:
        return None
    if not isinstance(caps, dict):
        return None
    from core.config import settings

    token = str(getattr(settings, "internal_api_key", "") or "")
    candidates: list[str] = []
    for want in (CHAT_TASK_NAMES, CAPTION_TASK_NAMES):
        for name in sorted(caps):
            entry = caps.get(name) or {}
            c = entry.get("capabilities") if isinstance(entry, dict) else None
            tasks = c.get("tasks_advertised") if isinstance(c, dict) else None
            url = entry.get("url") if isinstance(entry, dict) else None
            if isinstance(tasks, list) and isinstance(url, str) and url and any(
                    isinstance(t, str) and t.lower() in want for t in tasks):
                candidates.append(url.rstrip("/"))
    for base in candidates:
        if await _probe(base, token):
            return base
    return None


async def rerank(query: str, captions: list[str], *, timeout_s: float = 8.0,
                 base: str | None = None) -> tuple[list[int] | None, dict[str, Any]]:
    """Ask once which of ``captions`` show ``query``. Returns ``(yes, info)``:
    the 1-based matching rows, or ``None`` when the re-rank could not run
    (no model, timeout, an unreadable answer) — and ``info`` says which."""
    info: dict[str, Any] = {"asked": len(captions), "kept": None, "ms": None}
    if not captions or not (query or "").strip():
        info["reason"] = "nothing-to-rank"
        return None, info
    base = base or await resolve_chat_base()
    if not base:
        info["reason"] = "no-model"
        return None, info
    from core.config import settings

    token = str(getattr(settings, "internal_api_key", "") or "")
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": build_prompt(query, captions)}],
        "max_tokens": 32,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        async with _one_at_a_time:
            body = await asyncio.wait_for(
                _chat(base + CHAT_PATH, payload, token=token, timeout_s=timeout_s), timeout_s)
    except TimeoutError:
        info.update(reason="timeout", ms=round((time.perf_counter() - t0) * 1000))
        return None, info
    except Exception as exc:
        logger.debug("search re-rank: chat failed (%s)", exc)
        info.update(reason="error", ms=round((time.perf_counter() - t0) * 1000))
        return None, info
    info["ms"] = round((time.perf_counter() - t0) * 1000)
    text = ""
    try:
        text = str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except Exception:
        text = ""
    yes = parse_answer(text, len(captions))
    info["answer"] = text.strip()[:80]
    if yes is None:
        info["reason"] = "unreadable"
        return None, info
    info["kept"] = len(yes)
    return yes, info
