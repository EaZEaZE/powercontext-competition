"""MiniMax 调用封装（OpenAI 兼容 Chat Completions 优先，失败回退原生接口）。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List

DEFAULT_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2")


def _post(url: str, payload: Dict, api_key: str, timeout: int = 300) -> Dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _extract_openai(body: Dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or choices[0].get("text") or ""


def _extract_native(body: Dict) -> str:
    choices = body.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""
    return body.get("reply") or ""


def chat(prompt: str, system: str | None = None, model: str | None = None,
         temperature: float = 0.0, max_tokens: int = 8000,
         api_key: str | None = None, base_url: str | None = None,
         log_path: str | None = None) -> str:
    key = api_key or os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 MINIMAX_API_KEY")
    base = (base_url or DEFAULT_BASE).rstrip("/")
    mdl = model or DEFAULT_MODEL

    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    errors = []
    for path, extract in (("/chat/completions", _extract_openai),
                          ("/text/chatcompletion_v2", _extract_native)):
        url = base + path
        payload = {"model": mdl, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        started = time.time()
        try:
            body = _post(url, payload, key)
            text = extract(body)
            if not text:
                errors.append("%s -> 空响应 %s" % (url, json.dumps(body, ensure_ascii=False)[:400]))
                continue
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "url": url, "model": mdl, "elapsed_s": round(time.time() - started, 2),
                        "usage": body.get("usage"), "request": payload,
                        "response": body,
                    }, ensure_ascii=False) + "\n")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            errors.append("%s -> HTTP %s %s" % (url, e.code, detail))
        except Exception as e:  # noqa: BLE001
            errors.append("%s -> %s: %s" % (url, type(e).__name__, e))
    raise RuntimeError("MiniMax 调用失败:\n" + "\n".join(errors))
