#!/usr/bin/env python3
"""
Rewrite To My Voice — Supabase edition
--------------------------------------
Takes each viral post's transcript (or caption) and rewrites it into a
ready-to-film English voiceover script in the owner's brand voice (from
brand_voice.md), writing the result to the `my_script` column. Edit
brand_voice.md to tune the persona/tone/red-lines — no code change needed.

Mirrors analyze_posts.py: highest-scoring posts first, HTTP-400 rejections
are marked so the queue moves on, Gemini (free tier) by default / Anthropic
when ANTHROPIC_API_KEY is set. Non-fatal by design: if the `my_script` column
is missing (older schema) it explains the one-line fix and exits 0 so the
Analyze → Deploy chain keeps running.

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY | ANTHROPIC_API_KEY
     NICHE (default: content creation), GEMINI_MODEL (gemini-2.5-flash),
     POST_LIMIT (60), REWRITE_ALL (set to "1" to redo every post after
     changing brand_voice.md)
"""

import json
import os
import re
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
POST_LIMIT = int(os.environ.get("POST_LIMIT", "60"))
REWRITE_ALL = os.environ.get("REWRITE_ALL", "").strip()  # set to redo existing (voice change)
FAIL_MARK = "(rewrite failed)"
SKIP_TR = ("(transcription failed)", "(video unavailable)", "(no speech)")

BRAND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand_voice.md")


def load_brand():
    try:
        txt = open(BRAND_PATH, encoding="utf-8").read()
    except Exception:
        txt = ""
    txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)  # examples are for humans only; not fed to the AI
    m = re.search(r"^## ", txt, flags=re.M)
    if m:
        txt = txt[m.start():]                          # the "how to edit" notes at the top are not fed either
    if "__NICHE__" in txt:
        print(f"brand_voice.md has not been filled in yet; using a generic persona for NICHE=\"{NICHE}\". "
              "After editing the file, run Actions → Rewrite To My Voice (rewrite_all=1) to redo every script.")
    txt = txt.replace("__NICHE__", NICHE).strip()
    if txt:
        return txt
    return (f"You are a short-video creator in \"{NICHE}\". "
            "Your tone is sincere and vivid, with practical steps. Never exaggerate or promise results. "
            "No emoji, no dashes.")


def build_system():
    return (
        "You are a voiceover scriptwriter. The [PERSONA & RULES] below define the brand identity, voice, "
        "audience and red lines you must follow. Every line you write must sound like this person "
        "said it themselves, and you must strictly obey every red line.\n\n"
        "Task: you will get the transcript or caption of one [VIRAL COMPETITOR SHORT VIDEO] "
        "(it may be in Chinese, or from a different field). "
        "Rewrite it into an [ENGLISH VOICEOVER SCRIPT] that can be read straight to camera:\n"
        "1. Keep the original's hook and structure and pacing (borrow the structure only; never copy its sentences)\n"
        "2. Swap the topic, examples and scenes for the audience pain points in [PERSONA & RULES]\n"
        "3. Use the [PERSONA & RULES] voice throughout, and never break a red line\n"
        "4. Follow the length, structure and ending in [PERSONA & RULES]; if none is given, write about 30-60 seconds "
        "of voiceover, open with a strong hook, and close with one soft call to action\n"
        "5. Write in English only, with British spelling. Never output Chinese or any other language, "
        "even if the original post is in Chinese\n"
        "6. Output only the script itself: no explanation, title, preface or sign-off, no \"Here is\" or \"Script:\", "
        "and no confidence tag or notes to the writer\n\n"
        f"[PERSONA & RULES] (field: {NICHE})\n" + load_brand()
    )


SYSTEM = build_system()


def _request(url, method="GET", data=None, headers=None, timeout=120):
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None)
        raw = resp.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("HTTP %s non-JSON body: %r" % (status, raw[:300]))


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def fetch_pending():
    flt = "" if REWRITE_ALL else "&my_script=is.null"
    rows = sb(
        "posts?select=post_id,competitor,caption,transcript,post_type,"
        "likes,comments,followers"
        + flt +
        "&order=viral_score.desc.nullslast"
    )
    # need real material to rewrite: a usable transcript or a decent caption
    # (length filters live here — PostgREST cannot filter on expressions)
    return [p for p in rows
            if len((p.get("transcript") or "") if (p.get("transcript") or "") not in SKIP_TR else "") > 30
            or len(p.get("caption") or "") > 50]


def build_prompt(p):
    tr = p.get("transcript") or ""
    if tr in SKIP_TR:
        tr = ""
    cap = (p.get("caption") or "")[:800]
    body = (f"Competitor account: @{p.get('competitor', '?')}\n"
            f"Stats: Likes {p.get('likes', '?')} | Comments {p.get('comments', '?')} | Followers {p.get('followers', '?')}\n")
    if tr:
        body += f"\n[ORIGINAL VIDEO TRANSCRIPT (main source for the rewrite)]\n{tr[:2800]}\n"
    if cap:
        body += f"\n[ORIGINAL CAPTION (supporting reference)]\n{cap}\n"
    body += "\nFollowing the system instructions, rewrite the viral post above into an English voiceover script for the person in [PERSONA & RULES]."
    return body


def rewrite_gemini(key, p):
    while True:
        model = _gemini_models[0]
        cfg = {"maxOutputTokens": 2000, "temperature": 0.85}
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


def rewrite_anthropic(key, p):
    resp = _request(
        "https://api.anthropic.com/v1/messages", "POST",
        {"model": CLAUDE_MODEL, "max_tokens": 2000, "system": SYSTEM,
         "messages": [{"role": "user", "content": build_prompt(p)}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"}, 90)
    parts = resp.get("content") or []
    return "".join(x.get("text", "") for x in parts if x.get("type") == "text").strip()


def save(post_id, text):
    sb(f"posts?post_id=eq.{urllib.parse.quote(post_id)}", method="PATCH",
       data={"my_script": text[:4000]}, prefer="return=minimal")


def main():
    if not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if anthropic_key:
        provider, delay = "anthropic", 0.5
        call = lambda p: rewrite_anthropic(anthropic_key, p)
    elif gemini_key:
        provider, delay = "gemini", 6.5  # free tier allows about 10 calls/minute; leave some headroom
        call = lambda p: rewrite_gemini(gemini_key, p)
    else:
        print("No ANTHROPIC_API_KEY or GEMINI_API_KEY — skipping (non-fatal).")
        print("Add GEMINI_API_KEY under the repo's Settings → Secrets and variables → Actions (free from Google AI Studio).")
        return
    print(f"[Provider] {provider} | [Niche] {NICHE}" + (" | REWRITE_ALL" if REWRITE_ALL else ""))

    try:
        queue = fetch_pending()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        # older schema without the column: tell the student the one-line fix and
        # exit 0 so the Analyze → Deploy chain is not blocked by this bonus step
        if exc.code == 400 and ("my_script" in body or '"42703"' in body):
            print("The posts table has no my_script column yet (old schema), so \"My Script\" is skipped this run; other steps are unaffected.")
            print("To add it, run this one line in Supabase → SQL Editor:")
            print("  alter table posts add column if not exists my_script text;")
            return
        raise
    print(f"{len(queue)} posts pending rewrite")
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

    print(f"Done: {ok} rewritten, {failed} failed")


if __name__ == "__main__":
    main()
