"""
llm.py — pluggable JSON-mode LLM client. Provider chosen by config.LLM_PROVIDER:

- "anthropic"          : hosted Claude (default for the SaaS). Cheap Haiku by default.
- "openai_compatible"  : a self-hosted vLLM/Ollama OpenAI-compatible endpoint
                         (the master build's path; needs enable_thinking:false).
- "none"               : LLM features disabled — every call returns None, and the
                         list + sync + cycle recommendations work fully without it.

Every caller must tolerate None (provider down/absent/garbage). Enrichment,
recipes, and diversity are the only LLM-dependent features.
"""
import json
import logging

import httpx

import config

log = logging.getLogger("thincart.llm")


async def _anthropic(prompt: str, max_tokens: int, temperature: float) -> dict | list | None:
    if not config.ANTHROPIC_API_KEY:
        log.warning("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
        return None
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        # Ask for pure JSON; prefill "{" to force a JSON object and skip preamble.
        msg = await client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt + "\nRespond with ONLY the JSON object."},
                {"role": "assistant", "content": "{"},
            ],
        )
        text = "{" + "".join(b.text for b in msg.content if b.type == "text")
        return json.loads(text)
    except Exception as e:
        log.warning("anthropic call failed: %s", e)
        return None


async def _openai_compatible(prompt: str, max_tokens: int, temperature: float):
    payload = {
        "model": config.OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},  # Qwen3.5 empties output otherwise
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions", json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"].get("content")
        return json.loads(content) if content else None
    except Exception as e:
        log.warning("openai-compatible call failed: %s", e)
        return None


async def chat_json(prompt: str, max_tokens: int = 300, timeout: float = 60,
                    temperature: float = 0.1) -> dict | list | None:
    """Provider-agnostic JSON call. Returns parsed JSON or None on any failure."""
    if config.LLM_PROVIDER == "anthropic":
        return await _anthropic(prompt, max_tokens, temperature)
    if config.LLM_PROVIDER == "openai_compatible":
        return await _openai_compatible(prompt, max_tokens, temperature)
    return None  # "none" — features gracefully absent


def available() -> bool:
    if config.LLM_PROVIDER == "anthropic":
        return bool(config.ANTHROPIC_API_KEY)
    return config.LLM_PROVIDER == "openai_compatible"
