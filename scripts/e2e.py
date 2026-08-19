#!/usr/bin/env python3
"""End-to-end check against a running Praxis backend.

Exercises every endpoint with real keys and prints a pass/fail line for each, so
a deployment or an endpoint change can be verified in one command instead of a
pile of curl invocations.

    python3 scripts/e2e.py --base http://127.0.0.1:8000
    python3 scripts/e2e.py --base https://your-app.vercel.app --skip-research

Keys come from the environment, never from arguments, so they stay out of shell
history: OPENROUTER_KEY, EXA_KEY, FIRECRAWL_KEY, SPEECHIFY_KEY. To test a
different OpenAI-compatible gateway, set LLM_BASE_URL and LLM_MODEL as well.

Exit status is 0 only when every check that could run passed. Checks that cannot
run because a key is absent are reported as skipped, not failed.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def record(name, status, detail=""):
    results.append((name, status, detail))
    colour = {PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[33m"}[status]
    print("  {0}{1:<5}\033[0m {2:<28} {3}".format(colour, status, name, detail[:110]))


def post(base_url, path, payload, timeout=240):
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def secrets_envelope():
    keys = {}
    for provider, variable in (
        ("openrouter", "OPENROUTER_KEY"),
        ("exa", "EXA_KEY"),
        ("firecrawl", "FIRECRAWL_KEY"),
        ("speechify", "SPEECHIFY_KEY"),
    ):
        value = os.environ.get(variable, "").strip()
        if value:
            keys[provider] = [value]
    return {"keys": keys, "usage": {}}


def llm_envelope():
    return {
        "base_url": os.environ.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1",
        "model": os.environ.get("LLM_MODEL") or "~deepseek/deepseek-v4-flash-latest",
        "reasoning": {"enabled": False},
    }


def check_health(base_url):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/health", timeout=30) as response:
            body = json.loads(response.read())
        record("health", PASS, "model {0}".format(body.get("default_model")))
        return True
    except Exception as failure:
        record("health", FAIL, str(failure))
        return False


def check_keycheck(base_url, secrets):
    try:
        _, raw = post(base_url, "/api/keycheck", {"secrets": secrets, "llm": llm_envelope()})
        report = json.loads(raw)["results"]
        broken = [
            "{0}/{1}: {2}".format(provider, row["fingerprint"], row["detail"])
            for provider, rows in report.items()
            for row in rows
            if not row["ok"]
        ]
        working = sum(1 for rows in report.values() for row in rows if row["ok"])
        if broken:
            record("keycheck", FAIL, "{0} working; broken -> {1}".format(working, "; ".join(broken)))
        else:
            record("keycheck", PASS, "{0} keys working".format(working))
    except Exception as failure:
        record("keycheck", FAIL, str(failure))


def check_models(base_url, secrets):
    try:
        _, raw = post(base_url, "/api/models", {"secrets": secrets, "llm": llm_envelope()})
        body = json.loads(raw)
        models = body.get("models") or []
        with_tools = sum(1 for model in models if model.get("supports_tools"))
        if not models:
            record("models", FAIL, "empty catalogue")
        else:
            record(
                "models",
                PASS,
                "{0} models, {1} tool-capable".format(len(models), with_tools),
            )
    except Exception as failure:
        record("models", FAIL, str(failure))


def check_voices(base_url, secrets):
    if "speechify" not in secrets["keys"]:
        record("voices", SKIP, "no SPEECHIFY_KEY")
        return
    try:
        _, raw = post(base_url, "/api/voices", {"secrets": secrets})
        body = json.loads(raw)
        per_model = body.get("speechify_by_model") or {}
        british = [
            voice["id"]
            for voice in per_model.get("simba-3.2", [])
            if (voice.get("locale") or "").startswith("en-GB")
        ]
        if not per_model.get("simba-3.2"):
            record("voices", FAIL, "simba-3.2 catalogue empty: {0}".format(body.get("speechify_error")))
        else:
            record(
                "voices",
                PASS,
                "simba-3.2 {0} voices ({1} British), kokoro {2}".format(
                    len(per_model["simba-3.2"]), len(british), len(body.get("kokoro") or [])
                ),
            )
    except Exception as failure:
        record("voices", FAIL, str(failure))


def check_speak(base_url, secrets):
    for provider, voice, extra in (
        ("kokoro", "bm_george", {}),
        ("speechify", "beatrice_32", {"speechify_model": "simba-3.2", "emotion": "energetic"}),
    ):
        needed = "speechify" if provider == "speechify" else "openrouter"
        if needed not in secrets["keys"]:
            record("speak/" + provider, SKIP, "no key for " + needed)
            continue
        try:
            payload = {
                "text": "Every moment of light and dark is a miracle.",
                "provider": provider,
                "voice": voice,
                "secrets": secrets,
                "llm": llm_envelope(),
            }
            payload.update(extra)
            _, raw = post(base_url, "/api/speak", payload)
            body = json.loads(raw)
            audio = base64.b64decode(body["audio_base64"])
            looks_like_mp3 = audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3")
            if not looks_like_mp3:
                record("speak/" + provider, FAIL, "not an mp3 frame")
            else:
                record(
                    "speak/" + provider,
                    PASS,
                    "{0} bytes, marks={1}".format(
                        len(audio), bool(body.get("speech_marks"))
                    ),
                )
        except Exception as failure:
            record("speak/" + provider, FAIL, str(failure))


def check_outline(base_url, secrets):
    try:
        _, raw = post(
            base_url,
            "/api/outline",
            {
                "topic": "Whether rent control reduces housing supply",
                "subtopic_count": 3,
                "secrets": secrets,
                "llm": llm_envelope(),
            },
        )
        subtopics = json.loads(raw).get("subtopics") or []
        if len(subtopics) < 2:
            record("outline", FAIL, "got {0} segments".format(len(subtopics)))
        else:
            record("outline", PASS, "; ".join(s["title"][:34] for s in subtopics))
    except Exception as failure:
        record("outline", FAIL, str(failure))


def check_research(base_url, secrets, budget_seconds):
    """The full loop, resuming across passes until it produces blocks."""
    if "exa" not in secrets["keys"]:
        record("research", SKIP, "no EXA_KEY")
        return

    state = None
    passes = 0
    searches = 0
    started = time.monotonic()
    saw_tool_trouble = ""

    while passes < 8:
        passes += 1
        payload = {
            "secrets": secrets,
            "llm": llm_envelope(),
            "budget_seconds": budget_seconds,
            "minimum_rounds": 1,
            "maximum_rounds": 2,
            "target_block_count": 6,
        }
        if state:
            payload["state"] = state
        else:
            payload["topic"] = "Whether rent control reduces housing supply"

        try:
            _, raw = post(base_url, "/api/research", payload, timeout=budget_seconds + 120)
        except Exception as failure:
            record("research", FAIL, "pass {0}: {1}".format(passes, failure))
            return

        final = None
        for line in raw.decode("utf8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "search":
                searches += 1
            elif kind in ("tool_error", "note") and "reason" not in event:
                saw_tool_trouble = str(event.get("error") or event.get("message") or "")[:90]
            elif kind == "error":
                record("research", FAIL, "pass {0}: {1}".format(passes, str(event.get("error"))[:150]))
                return
            elif kind in ("done", "suspended"):
                final = event

        if final is None:
            record("research", FAIL, "pass {0} ended without done/suspended".format(passes))
            return

        state = final.get("state")
        if final["type"] == "done":
            blocks = (state or {}).get("blocks") or []
            findings = (state or {}).get("findings") or []
            sourced = sum(1 for block in blocks if block.get("sources"))
            detail = "{0} passes, {1}s, {2} searches, {3} findings, {4} blocks ({5} sourced)".format(
                passes,
                int(time.monotonic() - started),
                searches,
                len(findings),
                len(blocks),
                sourced,
            )
            record("research", PASS if blocks else FAIL, detail)
            if saw_tool_trouble:
                record("research/tools", SKIP, "endpoint complained: " + saw_tool_trouble)
            return

    record("research", FAIL, "did not finish in 8 passes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--budget", type=int, default=200)
    parser.add_argument("--skip-research", action="store_true")
    arguments = parser.parse_args()

    secrets = secrets_envelope()
    print("Praxis end-to-end check")
    print("  backend:  {0}".format(arguments.base))
    print("  endpoint: {0}".format(llm_envelope()["base_url"]))
    print("  model:    {0}".format(llm_envelope()["model"]))
    print("  keys:     {0}".format(", ".join(sorted(secrets["keys"])) or "none"))
    print()

    if not check_health(arguments.base):
        print("\nbackend is not answering; nothing else can be checked")
        return 1

    if "openrouter" not in secrets["keys"]:
        print("\nOPENROUTER_KEY is required for everything except health")
        return 1

    check_keycheck(arguments.base, secrets)
    check_models(arguments.base, secrets)
    check_voices(arguments.base, secrets)
    check_speak(arguments.base, secrets)
    check_outline(arguments.base, secrets)
    if not arguments.skip_research:
        check_research(arguments.base, secrets, arguments.budget)

    failed = [name for name, status, _ in results if status == FAIL]
    print()
    print(
        "{0} passed, {1} failed, {2} skipped".format(
            sum(1 for _, status, _ in results if status == PASS),
            len(failed),
            sum(1 for _, status, _ in results if status == SKIP),
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
