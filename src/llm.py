"""
OpenRouter client.

Three things this does beyond wrapping an HTTP call, each of which exists
because of a way the measurements could otherwise go wrong.

**It caches.** Keyed on a hash of (model, temperature, messages). Stages 6-11
re-run the same gold set many times while the evaluator, prompts and parsing
get debugged. Without a cache you pay for every identical call again, and a
sub-$10 budget becomes a $100 one. The cache is on disk in sqlite so it
survives between sessions.

**It logs the resolved provider.** OpenRouter serves the same model slug from
several providers, and they do not always run identical quantisations. If the
provider changes between the "glossary off" run and the "glossary on" run,
part of the measured delta is provider drift rather than the glossary. The
provider is recorded on every response so that can be checked rather than
assumed.

**It records usage.** Prompt and completion tokens on every call, and cost
where OpenRouter reports it. Cost per query is a headline metric for this
project and it cannot be reconstructed afterwards.

Nothing here retries into a different model. A failed call returns an error
and the runner records it; silently substituting a model would corrupt the
comparison being measured.

Requires: pip install requests python-dotenv

Smoke test:
    python src/llm.py --model <slug>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
CACHE_PATH = Path("data/llm_cache.sqlite")
DEFAULT_TIMEOUT = 90
MAX_RETRIES = 7
RATE_LIMIT_BASE_WAIT = 4.0


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_chars: int = 0
    cost: float | None = None
    finish_reason: str = ""
    cached: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:
        if not self.ok:
            return f"<LLMResponse error: {self.error}>"
        tag = " cached" if self.cached else ""
        return (f"<LLMResponse {self.prompt_tokens}+{self.completion_tokens}tok "
                f"{self.elapsed_ms:.0f}ms{tag} via {self.provider or '?'}>")


class Cache:
    """Disk-backed response cache. Key covers model, temperature and messages."""

    def __init__(self, path: Path = CACHE_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "  key TEXT PRIMARY KEY, payload TEXT NOT NULL,"
            "  created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.con.commit()

    # Version tag. Bump to invalidate every entry when the stored payload
    # shape changes - old rows lack fields added later and would replay as
    # zeros.
    SCHEMA_VERSION = 2

    @staticmethod
    def key(model: str, temperature: float, messages: list[dict],
            max_tokens: int) -> str:
        """Every parameter that can change the response must be in the key.

        max_tokens was originally omitted. Raising it therefore replayed the
        old responses from disk and the change appeared to do nothing - an
        experiment that silently measured nothing at all.
        """
        blob = json.dumps([Cache.SCHEMA_VERSION, model, temperature,
                           max_tokens, messages], sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        row = self.con.execute(
            "SELECT payload FROM responses WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, payload: dict) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO responses (key, payload) VALUES (?, ?)",
            (key, json.dumps(payload)))
        self.con.commit()

    def count(self) -> int:
        return self.con.execute("SELECT count(*) FROM responses").fetchone()[0]

    def close(self) -> None:
        self.con.close()


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY not set. Put it in .env as\n"
            "  OPENROUTER_API_KEY=sk-or-...\n"
            ".env is already in .gitignore - confirm before pasting a key.")
    return key


def complete(messages: list[dict],
             model: str,
             temperature: float = 0.0,
             max_tokens: int = 4000,
             cache: Cache | None = None,
             provider: str | None = None,
             max_price_per_m: float | None = None,
             pace_s: float = 0.0) -> LLMResponse:
    """One chat completion. Returns an error rather than raising.

    `provider` pins routing to a single upstream. Leave it unset only for
    exploratory calls: OpenRouter will otherwise spread a run across several
    providers, which may serve different quantisations of the same slug, and
    any accuracy delta measured across such runs is partly routing noise.
    """
    own_cache = cache is None
    cache = cache or Cache()
    key = Cache.key(f"{model}@{provider or 'any'}", temperature, messages,
                    max_tokens)

    hit = cache.get(key)
    if hit is not None:
        if own_cache:
            cache.close()
        return LLMResponse(cached=True, **hit)

    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }
    routing: dict = {}
    if provider:
        routing["order"] = [provider]
        routing["allow_fallbacks"] = False
    if max_price_per_m is not None:
        # Hard ceiling: the request fails rather than overspending.
        routing["max_price"] = {"prompt": max_price_per_m,
                                "completion": max_price_per_m}
    if routing:
        body["provider"] = routing

    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "X-Title": "conversational-analytics",
    }

    if pace_s:
        time.sleep(pace_s)          # only reached on a cache miss

    start = time.perf_counter()
    last_error = "unknown"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(ENDPOINT, headers=headers, json=body,
                              timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"network: {exc}"
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 429 or r.status_code >= 500:
            last_error = f"http {r.status_code}"
            # Honour Retry-After when the provider sends it; otherwise back
            # off geometrically. 429 means "slow down", so the wait has to be
            # long enough to actually clear the window - 2s, 4s, 8s gives up
            # while still inside it.
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = min(float(retry_after), 60.0)
            else:
                wait = min(RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60.0)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            body_text = r.text[:200].replace("\n", " ")
            return LLMResponse("", error=f"http {r.status_code}: {body_text}",
                               elapsed_ms=(time.perf_counter() - start) * 1000)

        data = r.json()
        if "error" in data:
            return LLMResponse("", error=str(data["error"])[:200],
                               elapsed_ms=(time.perf_counter() - start) * 1000)

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        choice = data["choices"][0]
        payload = {
            "text": (choice["message"].get("content") or "").strip(),
            "finish_reason": choice.get("finish_reason", "") or "",
            # Some models put everything in a reasoning channel and leave
            # content empty. If that is happening, this is non-zero while the
            # text is blank.
            "reasoning_chars": len(choice["message"].get("reasoning") or ""),
            "model": data.get("model", model),
            "provider": data.get("provider", ""),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": details.get("reasoning_tokens", 0),
            "cost": usage.get("cost"),
            "elapsed_ms": (time.perf_counter() - start) * 1000,
            "error": None,
        }
        cache.put(key, payload)
        if own_cache:
            cache.close()
        return LLMResponse(cached=False, **payload)

    if own_cache:
        cache.close()
    return LLMResponse("", error=f"gave up after {MAX_RETRIES} attempts: "
                                 f"{last_error}",
                       elapsed_ms=(time.perf_counter() - start) * 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test one model")
    ap.add_argument("--model", required=True,
                    help="OpenRouter slug, e.g. vendor/model-name")
    ap.add_argument("--prompt", default="Reply with exactly: OK")
    ap.add_argument("--provider", default=None,
                    help="pin routing, e.g. Groq. Omit to let OpenRouter choose.")
    args = ap.parse_args()

    cache = Cache()
    print(f"cache holds {cache.count()} responses\n")

    res = complete([{"role": "user", "content": args.prompt}],
                   model=args.model, cache=cache, provider=args.provider)
    cache.close()

    if not res.ok:
        print(f"FAILED: {res.error}")
        return 1

    print(f"reply:      {res.text[:200]}")
    print(f"model:      {res.model}")
    print(f"provider:   {res.provider or '(not reported)'}")
    print(f"tokens:     {res.prompt_tokens} in, {res.completion_tokens} out"
          f"{f' ({res.reasoning_tokens} reasoning)' if res.reasoning_tokens else ''}")
    print(f"finish:     {res.finish_reason or '(not reported)'}")
    print(f"cost:       {res.cost if res.cost is not None else '(not reported)'}")
    print(f"latency:    {res.elapsed_ms:.0f} ms")
    print(f"cached:     {res.cached}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
