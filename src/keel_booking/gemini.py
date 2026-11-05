"""The package's one Gemini client: the model list, timeouts, the fallback between models and the usage log.

The booking assistant calls ``generate`` and nothing else. Each free model has its own quota, so a model that is out
of quota (429), busy (5xx) or slower than ``TIMEOUT`` passes the call to the next one in ``ASSISTANT_MODELS``. A model
that failed rests in this process: for Google's retryDelay after a 429, ``BUSY_REST`` seconds otherwise, and a busy
one is still tried last. ``Unavailable`` means no model answered.

Caches: the request keeps its fixed part first (system instruction, then the tools) and byte-identical between turns,
so Gemini's implicit cache can reuse it; every call logs ``cachedContentTokenCount``. No response cache: a
conversation is personal and runs tools (model-api-caching rule 3.2).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import conf

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 25
DEADLINE = 50
BUSY_REST = 30
QUOTA_REST = 60
_resting: dict[str, tuple[float, bool]] = {}  # model -> (until, out of quota)


class Unavailable(Exception):
    pass


def _retry_delay(body: dict) -> int:
    for detail in (body.get("error") or {}).get("details") or []:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", "")))
        if match:
            return int(float(match.group(1))) + 1
    return QUOTA_REST


def _post(model: str, key: str, payload: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(API.format(model=model), data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def generate(system: str, contents: list[dict], tools: list[dict] | None = None, temperature: float = 0.6) -> dict:
    """One model turn: the candidate's content ({"role": "model", "parts": [...]}), its parts kept whole so function
    calls carry their thought signatures back on the next round."""
    key = conf.get("ASSISTANT_GEMINI_KEY")
    if not key:
        raise Unavailable("no ASSISTANT_GEMINI_KEY")
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 4096,
                                 "thinkingConfig": {"thinkingLevel": "low"}}}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.monotonic()
    now = time.time()
    models = list(conf.get("ASSISTANT_MODELS"))
    ready = [m for m in models if _resting.get(m, (0, False))[0] <= now]
    busy = [m for m in models if m not in ready and not _resting[m][1]]
    tried = []
    for model in ready + busy:
        if time.monotonic() - started > DEADLINE:
            break
        began = time.monotonic()
        try:
            status, data = _post(model, key, payload)
        except Exception as error:  # timeout or network
            tried.append(f"{model}:{type(error).__name__}")
            _resting[model] = (time.time() + BUSY_REST, False)
            continue
        ms = int((time.monotonic() - began) * 1000)
        if status != 200:
            tried.append(f"{model}:{status}:{ms}ms")
            if status == 429:
                _resting[model] = (time.time() + _retry_delay(data), True)
            elif status >= 500:
                _resting[model] = (time.time() + BUSY_REST, False)
            else:
                log.warning("gemini %s answered %s: %s", model, status, str(data)[:500])
            continue
        candidate = (data.get("candidates") or [{}])[0]
        content = candidate.get("content") or {}
        if not content.get("parts"):
            tried.append(f"{model}:empty:{candidate.get('finishReason')}")
            continue
        _resting.pop(model, None)
        usage = data.get("usageMetadata") or {}
        tried.append(f"{model}:ok:{ms}ms")
        log.info(json.dumps({
            "event": "assistant_call", "model": data.get("modelVersion", model), "turns": len(contents),
            "input_tokens": usage.get("promptTokenCount", 0), "cached_tokens": usage.get("cachedContentTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0), "thinking_tokens": usage.get("thoughtsTokenCount", 0),
            "cost_usd": 0, "response_cache": "none", "finish": candidate.get("finishReason"), "tried": tried}))
        return {"role": "model", "parts": content["parts"]}
    log.warning(json.dumps({"event": "assistant_unavailable", "tried": tried}))
    raise Unavailable(", ".join(tried) or "every model is resting")
