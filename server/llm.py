"""
llm.py — vLLM Qwen3.5-122B client (OpenAI-compatible), JSON-mode only.

CRITICAL (verified empirically on this box, 2026-06-29 + re-verified 2026-07-03):
Qwen3.5 defaults to "thinking mode" which burns the whole token budget on a
hidden reasoning trace and returns EMPTY content — chat_template_kwargs
.enable_thinking=false is mandatory. response_format json_object verified:
curry-roux enrichment returned clean parseable JSON in 2.0 s.

Every caller must tolerate None (LLM down/slow/garbage) — the list and sync
never depend on this module.
"""
import json
import logging

import httpx

log = logging.getLogger("plantcart.llm")

VLLM_URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "Intel/Qwen3.5-122B-A10B-int4-AutoRound"


async def chat_json(
    prompt: str, max_tokens: int = 300, timeout: float = 60, temperature: float = 0.1
) -> dict | list | None:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(VLLM_URL, json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"].get("content")
        return json.loads(content) if content else None
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None
