"""FastAPI surface for the Praxis backend.

Deployed as a Vercel Python function. Two things shape every decision here.

Streaming: research takes minutes, so the research and ask endpoints stream
server-sent events. The browser shows searches as they happen instead of staring
at a spinner, and the same event stream is what drives the shimmer on a block
being answered.

No ambient authority: this service holds no secrets of its own. Every request
carries the keys it needs, and the response hands back updated per-key usage
counters for the browser to store. That is why permissive CORS is safe here —
there is nothing a cross-origin caller could borrow. It also means the frontend
can live on a different origin, which keeps both deployment shapes open: one
Vercel project using Services, or two projects with the backend URL configured
in settings.
"""

import json
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .graph import (
    answer_inline_question,
    new_research_state,
    propose_subtopics,
    run_research_slice,
)
from .vault_store import load_vault, rows_as_list, upsert_rows
from .keys import KeyExhausted, SecretVault
from .transport import (
    ProviderHttpError,
    bearer_header,
    get_json,
    plain_key,
    post_json,
)
from .llm import (
    DEFAULT_CHAT_MODEL,
    KOKORO_VOICES,
    SPEECHIFY_EMOTIONS,
    SPEECHIFY_MODELS,
    LlmProfile,
    list_chat_models,
    list_speechify_voices,
    probe_openrouter_audio,
    synthesize_speech_kokoro,
    synthesize_speech_speechify,
    transcribe_audio,
)

app = FastAPI(title="Praxis research backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hobby terminates an invocation at 300 seconds, and the budget is not the whole
# story: a research round that gets cut off still spends one more turn writing up
# what it found, and that turn was measured overrunning the budget by about 30
# seconds. So the default leaves room for the overrun plus the response itself.
# A 240 budget was observed finishing at 272s, which is closer to the wall than
# anything should be.
DEFAULT_RESEARCH_BUDGET_SECONDS = 200.0
HARD_BUDGET_CEILING_SECONDS = 240.0


def _server_sent_event(event_payload: Dict[str, Any]) -> str:
    return "data: {0}\n\n".format(json.dumps(event_payload, ensure_ascii=False))


def _build_vault(request_body: Dict[str, Any]) -> SecretVault:
    return SecretVault(request_body.get("secrets") or {})


def _build_profile(request_body: Dict[str, Any]) -> LlmProfile:
    """Endpoint, model, and reasoning setting for this request.

    The browser sends this because the browser is what holds the model catalogue
    and therefore knows which reasoning shape the chosen model actually accepts.
    """
    return LlmProfile.from_payload(request_body.get("llm"))


def _require_providers(vault: SecretVault, required: List[str]) -> None:
    missing = vault.missing_providers(required)
    if missing:
        raise HTTPException(
            status_code=400,
            detail="No API key configured for: {0}. Add one in Settings.".format(
                ", ".join(missing)
            ),
        )


def _clamped_budget(requested: Any) -> float:
    try:
        budget = float(requested or DEFAULT_RESEARCH_BUDGET_SECONDS)
    except (TypeError, ValueError):
        budget = DEFAULT_RESEARCH_BUDGET_SECONDS
    return max(30.0, min(budget, HARD_BUDGET_CEILING_SECONDS))


def _clean_vault_id(vault_id: str) -> str:
    cleaned = (vault_id or "").strip().lower()
    if not cleaned or len(cleaned) < 16 or len(cleaned) > 128:
        raise HTTPException(status_code=400, detail="Invalid vault id.")
    if any(character not in "0123456789abcdef" for character in cleaned):
        raise HTTPException(status_code=400, detail="Invalid vault id.")
    return cleaned


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "praxis",
        "default_model": DEFAULT_CHAT_MODEL,
        "research_budget_seconds": DEFAULT_RESEARCH_BUDGET_SECONDS,
    }


@app.post("/api/voices")
async def voices(request: Request) -> Dict[str, Any]:
    """Voice catalogue for the settings screen.

    Kokoro's list is hardcoded because OpenRouter exposes no voice endpoint for
    it — /audio/voices is a 404, and an unrecognised voice ID fails the whole
    synthesis call, so guessing is not an option. Speechify does publish a list,
    so that half is live.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)

    # One request per Speechify model, because each model has its own catalogue
    # rather than a filtered view of a shared one, and a voice from the wrong
    # catalogue is rejected at synthesis time.
    speechify_by_model: Dict[str, List[Dict[str, Any]]] = {}
    speechify_error: Optional[str] = None
    if vault.pool("speechify").is_configured():
        for model_entry in SPEECHIFY_MODELS:
            model_identifier = model_entry["id"]
            try:
                speechify_by_model[model_identifier] = list_speechify_voices(
                    vault, model_name=model_identifier
                )
            except KeyExhausted as exhausted:
                speechify_error = str(exhausted)
                break
            except Exception as voice_error:  # noqa: BLE001
                # One model failing should not hide the others.
                speechify_by_model[model_identifier] = []
                speechify_error = "{0}: {1}".format(model_identifier, str(voice_error))

    return {
        "kokoro": KOKORO_VOICES,
        "speechify_models": SPEECHIFY_MODELS,
        "speechify_emotions": SPEECHIFY_EMOTIONS,
        "speechify_by_model": speechify_by_model,
        # Kept so an older cached frontend still finds a usable list.
        "speechify": speechify_by_model.get("simba-3.2")
        or speechify_by_model.get("simba-3.0")
        or [],
        "speechify_error": speechify_error,
        "usage": vault.export_usage(),
    }


@app.post("/api/keycheck")
async def keycheck(request: Request) -> Dict[str, Any]:
    """Test every configured key individually and say which ones work.

    Rotation deliberately hides individual failures — it moves to the next key and
    only complains once nothing is left. That is right for a research run and
    useless for debugging, because "no usable openrouter key" does not say which
    of your three keys is wrong, or whether the problem is a typo, no credit, or a
    rate limit. This endpoint answers that directly, one cheap authenticated call
    per key.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)
    profile = _build_profile(request_body)

    probes = {
        "openrouter": lambda key: get_json(
            "{0}/key".format(profile.base_url),
            headers={"Authorization": bearer_header(key)},
        ),
        "exa": lambda key: post_json(
            "https://api.exa.ai/search",
            headers={"x-api-key": plain_key(key), "Content-Type": "application/json"},
            payload={"query": "test", "type": "instant", "numResults": 1},
        ),
        "firecrawl": lambda key: get_json(
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers={"Authorization": bearer_header(key)},
        ),
        "speechify": lambda key: get_json(
            "https://api.sws.speechify.com/v1/voices",
            headers={"Authorization": bearer_header(key)},
        ),
    }

    report: Dict[str, List[Dict[str, Any]]] = {}
    for provider_name, probe in probes.items():
        pool = vault.pool(provider_name)
        rows: List[Dict[str, Any]] = []
        for api_key in pool.api_keys:
            fingerprint = pool.fingerprint(api_key)
            try:
                probe(api_key)
                rows.append({"fingerprint": fingerprint, "ok": True, "detail": "working"})
            except ProviderHttpError as http_error:
                rows.append(
                    {
                        "fingerprint": fingerprint,
                        "ok": False,
                        "detail": http_error.diagnosis(),
                    }
                )
            except Exception as unexpected:  # noqa: BLE001
                rows.append(
                    {
                        "fingerprint": fingerprint,
                        "ok": False,
                        "detail": "{0}: {1}".format(
                            type(unexpected).__name__, str(unexpected)[:160]
                        ),
                    }
                )
        report[provider_name] = rows

    return {"results": report, "usage": vault.export_usage()}


@app.post("/api/audiocheck")
async def audiocheck(request: Request) -> Dict[str, Any]:
    """Hit speech and transcription for real, on this OpenRouter key."""
    request_body = await request.json()
    vault = _build_vault(request_body)
    _require_providers(vault, ["openrouter"])
    try:
        report = probe_openrouter_audio(vault, _build_profile(request_body))
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))
    report["usage"] = vault.export_usage()
    return report


@app.post("/api/models")
async def models(request: Request) -> Dict[str, Any]:
    """The configured endpoint's model catalogue, for the searchable picker.

    Proxied rather than fetched from the browser because the key must not be
    exposed to a cross-origin request and many OpenAI-compatible servers send no
    CORS headers at all.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)
    _require_providers(vault, ["openrouter"])

    try:
        catalogue = list_chat_models(vault, _build_profile(request_body))
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))
    except Exception as failure:  # noqa: BLE001 - surfaced to the settings screen
        raise HTTPException(
            status_code=502,
            detail="Could not read the model list: {0}".format(str(failure)),
        )

    return {"models": catalogue, "count": len(catalogue), "usage": vault.export_usage()}


@app.get("/api/vault/{vault_id}")
def read_vault(vault_id: str) -> Dict[str, Any]:
    """Return the opaque ciphertext rows for this vault. No plaintext here."""
    cleaned = _clean_vault_id(vault_id)
    rows = rows_as_list(load_vault(cleaned))
    return {"rows": rows}


@app.put("/api/vault/{vault_id}")
async def write_vault(vault_id: str, request: Request) -> Dict[str, Any]:
    """Merge incoming ciphertext rows. Newer updated_at wins per id."""
    cleaned = _clean_vault_id(vault_id)
    request_body = await request.json()
    incoming = request_body.get("rows") or []
    if not isinstance(incoming, list):
        raise HTTPException(status_code=400, detail="rows must be an array.")
    try:
        merged = upsert_rows(cleaned, incoming)
    except Exception as failure:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="Vault storage failed: {0}".format(str(failure)[:200]),
        )
    return {"rows": rows_as_list(merged)}


@app.post("/api/outline")
async def outline(request: Request) -> Dict[str, Any]:
    """Split a subject into an even spine of segments, before research starts.

    Cheap and synchronous: it is one model call with no tools, and the listener is
    looking at a modal waiting to accept or reject the result.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)
    _require_providers(vault, ["openrouter"])

    topic = (request_body.get("topic") or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Provide a topic to break up.")

    try:
        subtopics = propose_subtopics(
            vault,
            topic=topic,
            subtopic_count=int(request_body.get("subtopic_count") or 5),
            profile=_build_profile(request_body),
        )
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))
    except ValueError as parse_failure:
        raise HTTPException(
            status_code=502,
            detail="The model did not return a usable outline: {0}".format(
                str(parse_failure)
            ),
        )

    if not subtopics:
        raise HTTPException(
            status_code=502, detail="The model returned no subtopics for that subject."
        )

    return {"subtopics": subtopics, "usage": vault.export_usage()}


@app.post("/api/research")
async def research(request: Request) -> StreamingResponse:
    """Advance a lesson's research by one slice, streaming progress.

    Send `{topic}` to begin, or `{state}` from a previous slice's `suspended`
    event to continue. The final event is either `done` or `suspended`; on
    `suspended` the client posts the returned state straight back.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)
    _require_providers(vault, ["openrouter", "exa"])

    incoming_state = request_body.get("state")
    if not incoming_state:
        topic = (request_body.get("topic") or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="Provide a topic or a state.")
        incoming_state = new_research_state(
            topic=topic,
            target_block_count=int(request_body.get("target_block_count") or 12),
            minimum_rounds=int(request_body.get("minimum_rounds") or 2),
            maximum_rounds=int(request_body.get("maximum_rounds") or 5),
            subtopics=request_body.get("subtopics") or None,
        )

    budget_seconds = _clamped_budget(request_body.get("budget_seconds"))
    profile = _build_profile(request_body)

    def event_stream() -> Iterator[str]:
        try:
            for event in run_research_slice(
                vault,
                state=incoming_state,
                budget_seconds=budget_seconds,
                profile=profile,
            ):
                if event.get("type") in ("done", "suspended"):
                    event["usage"] = vault.export_usage()
                yield _server_sent_event(event)
        except KeyExhausted as exhausted:
            yield _server_sent_event(
                {
                    "type": "error",
                    "error": str(exhausted),
                    "provider": exhausted.provider_name,
                    "usage": vault.export_usage(),
                }
            )
        except Exception as unexpected:  # noqa: BLE001 - must reach the client
            yield _server_sent_event(
                {
                    "type": "error",
                    "error": "{0}: {1}".format(
                        type(unexpected).__name__, str(unexpected)
                    ),
                    "usage": vault.export_usage(),
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ask")
async def ask(request: Request) -> StreamingResponse:
    """Answer a question the listener typed or spoke into the lesson.

    Faster than the research graph on purpose — the cursor is blinking and
    someone is waiting — but the fabrication rules do not relax.
    """
    request_body = await request.json()
    vault = _build_vault(request_body)
    _require_providers(vault, ["openrouter", "exa"])

    question_text = (request_body.get("question") or "").strip()
    if not question_text:
        raise HTTPException(status_code=400, detail="Provide a question.")

    def event_stream() -> Iterator[str]:
        try:
            for event in answer_inline_question(
                vault,
                topic=request_body.get("topic") or "",
                question_text=question_text,
                surrounding_context=request_body.get("context") or "",
                findings=request_body.get("findings") or [],
                budget_seconds=_clamped_budget(
                    request_body.get("budget_seconds") or 150.0
                ),
                profile=_build_profile(request_body),
            ):
                if event.get("type") == "done":
                    event["usage"] = vault.export_usage()
                yield _server_sent_event(event)
        except KeyExhausted as exhausted:
            yield _server_sent_event(
                {
                    "type": "error",
                    "error": str(exhausted),
                    "provider": exhausted.provider_name,
                    "usage": vault.export_usage(),
                }
            )
        except Exception as unexpected:  # noqa: BLE001
            yield _server_sent_event(
                {
                    "type": "error",
                    "error": "{0}: {1}".format(
                        type(unexpected).__name__, str(unexpected)
                    ),
                    "usage": vault.export_usage(),
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/api/speak")
async def speak(request: Request) -> JSONResponse:
    """Synthesize one block of audio.

    Base64 in a JSON envelope rather than raw bytes, so Speechify's speech marks
    can travel alongside the audio for word-level highlighting. One block at a
    time also keeps every response well under the 4.5 MB body ceiling — a whole
    episode in one call would exceed it.
    """
    import base64

    request_body = await request.json()
    vault = _build_vault(request_body)

    block_text = (request_body.get("text") or "").strip()
    if not block_text:
        raise HTTPException(status_code=400, detail="Nothing to speak.")

    provider_name = (request_body.get("provider") or "kokoro").lower()
    voice_identifier = request_body.get("voice") or ""

    try:
        if provider_name == "speechify":
            _require_providers(vault, ["speechify"])
            speech_result = synthesize_speech_speechify(
                vault,
                text=block_text,
                voice_identifier=voice_identifier or "beatrice_32",
                model_identifier=request_body.get("speechify_model") or "simba-3.2",
                language=request_body.get("language"),
                emotion=request_body.get("emotion"),
            )
        else:
            _require_providers(vault, ["openrouter"])
            speech_result = synthesize_speech_kokoro(
                vault,
                text=block_text,
                voice_identifier=voice_identifier or "af_heart",
                profile=_build_profile(request_body),
            )
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))
    except ProviderHttpError as provider_error:
        # Uncaught, this became a FastAPI 500 with no body the player could
        # display — the UI then showed a bare "HTTP 500". Surface the provider's
        # own diagnosis so a bad voice, a 4xx from Kokoro, or an upstream 500
        # is distinguishable.
        raise HTTPException(
            status_code=502,
            detail="{0} speech failed: {1}".format(
                provider_name, provider_error.diagnosis()
            ),
        )
    except Exception as unexpected:  # noqa: BLE001 - must not become a blank 500
        raise HTTPException(
            status_code=502,
            detail="{0} speech failed: {1}: {2}".format(
                provider_name, type(unexpected).__name__, str(unexpected)[:240]
            ),
        )

    audio_bytes = speech_result.get("audio_bytes") or b""
    if not audio_bytes:
        raise HTTPException(
            status_code=502,
            detail="{0} returned no audio for that block.".format(provider_name),
        )

    return JSONResponse(
        {
            "audio_base64": base64.b64encode(speech_result["audio_bytes"]).decode(),
            "content_type": speech_result["content_type"],
            "speech_marks": speech_result.get("speech_marks"),
            "billable_characters": speech_result.get("billable_characters"),
            "provider": provider_name,
            "usage": vault.export_usage(),
        }
    )


@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    secrets: str = Form(...),
    llm: str = Form("{}"),
) -> Dict[str, Any]:
    """Voice question in, text out.

    Multipart because Whisper on OpenRouter lives on /audio/transcriptions and
    takes a file, not a JSON message list. Since the request cannot have a JSON
    body, the two JSON envelopes every other endpoint takes arrive as string
    form fields instead.
    """
    try:
        secrets_payload = json.loads(secrets or "{}")
        llm_payload = json.loads(llm or "{}")
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="secrets or llm field was not valid JSON."
        )

    vault = SecretVault(secrets_payload)
    _require_providers(vault, ["openrouter"])

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty recording.")

    try:
        transcription = transcribe_audio(
            vault,
            audio_bytes=audio_bytes,
            filename=audio.filename or "question.webm",
            content_type=audio.content_type or "audio/webm",
            profile=LlmProfile.from_payload(llm_payload),
        )
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))
    except ProviderHttpError as provider_error:
        raise HTTPException(
            status_code=502,
            detail="transcription failed: {0}".format(provider_error.diagnosis()),
        )
    except Exception as unexpected:  # noqa: BLE001 - must not become a blank 500
        raise HTTPException(
            status_code=502,
            detail="transcription failed: {0}: {1}".format(
                type(unexpected).__name__, str(unexpected)[:240]
            ),
        )

    return {
        "text": (transcription.get("text") or "").strip(),
        "usage": vault.export_usage(),
    }
