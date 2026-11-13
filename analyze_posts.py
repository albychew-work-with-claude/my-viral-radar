#!/usr/bin/env python3
"""
AI Viral-Post Breakdown — Supabase edition
-------------------------------------------
Feeds each viral post (stats + caption + transcript) to an LLM and writes a
structured breakdown to the `ai_breakdown` column. Highest-scoring posts
first so the free model budget goes to the best material; a
permanently-rejected post is marked (HTTP 400) so the queue always moves on.

Provider: Gemini (free tier, GEMINI_API_KEY from Google AI Studio) by default;
Anthropic API when ANTHROPIC_API_KEY is set. (GitHub Models was retired on
2026-07-30 and is no longer supported.)

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY | ANTHROPIC_API_KEY
     NICHE (default: content creation), GEMINI_MODEL (gemini-2.5-flash), POST_LIMIT (80)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
NICHE = os.environ.get("NICHE") or "content creation"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Google keeps retiring older free-tier models (gemini-2.0-flash is already gone).
# On a 404 it switches to the next one automatically; set GEMINI_MODEL to pick one.
GEMINI_FALLBACKS = ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash")
_gemini_models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
POST_LIMIT = int(os.environ.get("POST_LIMIT", "80"))
FAIL_MARK = "(breakdown failed)"
SKIP_TR = ("(transcription failed)", "(video unavailable)", "(no speech)")

SYSTEM = (
    f"You are an expert at breaking down viral short videos, working for a team in the \"{NICHE}\" business "
    "(Singapore market). You will get one viral competitor post. The post may be in Chinese or another "
    "language, but you must write your breakdown in English only, using British spelling. "
    "Output a tight breakdown in exactly these four lines, each line under 30 words, with no other text:\n"
    "Hook type: <e.g. counter-intuitive / direct pain point / suspense / number promise / identity resonance / showing results>\n"
    "Structure: <summarise as A → B → C>\n"
    "Why it works: <one sentence>\n"
    f"Adaptation angle: <one concrete suggestion for adapting it to {NICHE}, one sentence>"
)


def _request(url, method="GET", data=None, headers=None, timeout=120):
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def fetch_pending():
    rows = sb(
        "posts?select=post_id,competitor,caption,transcript,post_type,"
        "likes,comments,followers"
        "&ai_breakdown=is.null"
        "&order=viral_score.desc.nullslast"
    )
    # enough material to analyse: a real transcript or a decent caption
    # (length filters live here — PostgREST cannot filter on expressions)
    return [p for p in rows
            if len((p.get("transcript") or "") if (p.get("transcript") or "") not in SKIP_TR else "") > 30
            or len(p.get("caption") or "") > 50]


def build_prompt(p):
    tr = p.get("transcript") or ""
    if tr in SKIP_TR:
        tr = ""
    return (
        f"Account: @{p.get('competitor', '?')} (followers {p.get('followers', '?')})\n"
        f"Type: {p.get('post_type', '?')} | Likes {p.get('likes', '?')} | Comments {p.get('comments', '?')}\n"
        f"Caption:\n{(p.get('caption') or '')[:1200]}\n"
        + (f"\nVideo transcript:\n{tr[:2500]}" if tr else "")
    )


def analyze_gemini(key, p):
    while True:
        model = _gemini_models[0]
        cfg = {"maxOutputTokens": 1200, "temperature": 0.6}
        if model.startswith("gemini-2.5-flash"):
            cfg["thinkingConfig"] = {"thinkingBudget": 0}  # 2.5 "thinks" by default, which eats the whole output budget
        try:
            resp = _request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                "POST",
                {"system_instruction": {"parts": [{"text": SYSTEM}]},
                 "contents": [{"role": "user", "parts": [{"text": build_prompt(p)}]}],
                 "generationConfig": cfg},
                {"x-goog-api-key": key}, 90)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and len(_gemini_models) > 1:
                _gemini_models.pop(0)
                print(f"  model {model} not found (retired?) -> switching to {_gemini_models[0]}")
                continue
            raise
        cands = resp.get("candidates") or []
        if not cands:
            return ""
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(x.get("text", "") for x in parts if not x.get("thought")).strip()


def analyze_anthropic(key, p):
    resp = _request(
        "https://api.anthropic.com/v1/messages", "POST",
        {"model": CLAUDE_MODEL, "max_tokens": 400, "system": SYSTEM,
         "messages": [{"role": "user", "content": build_prompt(p)}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"}, 90)
    parts = resp.get("content") or []
    return "".join(x.get("text", "") for x in parts if x.get("type") == "text").strip()


def save(post_id, text):
    sb(f"posts?post_id=eq.{urllib.parse.quote(post_id)}", method="PATCH",
       data={"ai_breakdown": text[:1500]}, prefer="return=minimal")


def main():
    if not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if anthropic_key:
        provider, delay = "anthropic", 0.5
        call = lambda p: analyze_anthropic(anthropic_key, p)
    elif gemini_key:
        provider, delay = "gemini", 6.5  # free tier allows about 10 calls/minute; leave some headroom
        call = lambda p: analyze_gemini(gemini_key, p)
    else:
        print("No ANTHROPIC_API_KEY or GEMINI_API_KEY — skipping (non-fatal).")
        print("Add GEMINI_API_KEY under the repo's Settings → Secrets and variables → Actions (free from Google AI Studio).")
        return
    print(f"[Provider] {provider} | [Niche] {NICHE}")

    queue = fetch_pending()
    print(f"{len(queue)} posts pending breakdown")
    if not queue:
        return
    if len(queue) > POST_LIMIT:
        print(f"capping at {POST_LIMIT} ({len(queue) - POST_LIMIT} left for next run)")
        queue = queue[:POST_LIMIT]

    ok = failed = 0
    for i, p in enumerate(queue, 1):
        who = p.get("competitor", "?")
        try:
            text = call(p)
            if not text:
                raise ValueError("empty response")
            save(p["post_id"], text)
            ok += 1
            print(f"[{i}/{len(queue)}] @{who} OK: {text[:50]}")
        except urllib.error.HTTPError as exc:
            failed += 1
            if exc.code == 400:
                try:
                    save(p["post_id"], FAIL_MARK)
                except Exception:
                    pass
                print(f"[{i}/{len(queue)}] @{who} REJECTED (marked)")
            elif exc.code == 429:
                print(f"[{i}/{len(queue)}] @{who} RATE LIMITED (wait 30s; leftovers next run)")
                time.sleep(30)
            else:
                print(f"[{i}/{len(queue)}] @{who} FAILED: HTTP {exc.code} (retry)")
        except Exception as exc:
            failed += 1
            print(f"[{i}/{len(queue)}] @{who} FAILED: {exc} (retry)")
        time.sleep(delay)

    print(f"Done: {ok} analyzed, {failed} failed")


if __name__ == "__main__":
    main()
