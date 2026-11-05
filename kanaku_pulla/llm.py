"""One call shape for whichever models are configured: give it a system prompt, a user message and a
Pydantic model, get back a validated instance of that model and the name of the provider that answered.

Providers are tried in order (Gemini, then OpenRouter, then Claude, for whichever keys are set), so a
failing key or an outage falls through to the next. If all fail, the caller uses the rule-based path.
The model only reads emails and writes the labelled summary. It never decides a payment.
"""

import json
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

from . import config

NAMES = {"gemini": "Gemini", "openrouter": "OpenRouter", "claude": "Claude"}
RETRYABLE = {429, 500, 502, 503, 504}


def name(provider: str) -> str:
    return NAMES.get(provider, "the rule-based reader")


def structured(system: str, user: str, schema: type[BaseModel]) -> tuple[BaseModel, str]:
    """Return (validated instance, provider). Raises RuntimeError if every configured provider fails."""
    failures = []
    for provider in config.llm_providers():
        try:
            if provider == "gemini":
                return schema.model_validate_json(_gemini_json(system, user, schema)), provider
            if provider == "openrouter":
                return _openrouter(system, user, schema), provider
            if provider == "claude":
                return _claude(system, user, schema), provider
        except Exception as exc:  # bad key, quota, network, refusal, schema mismatch: try the next one
            failures.append(f"{name(provider)}: {exc.__class__.__name__}: {str(exc)[:160]}")
            print(f"[llm] {failures[-1]}")
    raise RuntimeError("; ".join(failures) or "no model configured")


def _clean_json(text: str) -> str:
    """Models sometimes wrap JSON in code fences or put reasoning first; keep only the JSON object."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


# --- Gemini (Google AI Studio key) --------------------------------------------------------------

def _gemini_json(system: str, user: str, schema: type[BaseModel]) -> str:
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=config.gemini_key(), http_options=types.HttpOptions(timeout=45_000))
    request = dict(
        model=config.GEMINI_MODEL,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            temperature=0,
        ),
    )
    for attempt in range(2):  # one retry for rate limits and server hiccups
        try:
            response = client.models.generate_content(**request)
            if not response.text:
                raise RuntimeError("empty response (possibly blocked)")
            return response.text
        except errors.APIError as exc:
            if attempt == 0 and getattr(exc, "code", None) in RETRYABLE:
                time.sleep(2)
                continue
            raise
    raise RuntimeError("unreachable")


# --- OpenRouter (OpenAI-compatible; e.g. NVIDIA Nemotron 3 Ultra) ----------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _openrouter(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user + "\n\nReply with one JSON object only, matching the schema."}]
    body = {
        "model": config.OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": schema.__name__, "strict": False,
                                            "schema": schema.model_json_schema()}},
    }
    headers = {"Authorization": f"Bearer {config.openrouter_key()}", "Content-Type": "application/json",
               "X-Title": "Kanaku Pulla"}
    for attempt in range(2):
        resp = httpx.post(OPENROUTER_URL, json=body, headers=headers, timeout=90)
        if resp.status_code in RETRYABLE and attempt == 0:
            time.sleep(3)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:200])
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            return schema.model_validate_json(_clean_json(content))
        except ValidationError as exc:
            if attempt == 0:  # one repair round with the validation error spelled out
                body["messages"] = messages + [{"role": "assistant", "content": content},
                                               {"role": "user", "content": "That did not match the schema: "
                                                + str(exc)[:600] + "\nReturn the corrected JSON object only."}]
                continue
            raise
    raise RuntimeError("no usable answer")


# --- Claude (Anthropic key) ------------------------------------------------------------------------

def _claude(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    import anthropic

    response = anthropic.Anthropic().messages.parse(
        model=config.MODEL, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": user}], output_format=schema,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"no parsed output (stop_reason={response.stop_reason})")
    return response.parsed_output
