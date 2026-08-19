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

from praxis.graph import (
    answer_inline_question,
    new_research_state,
    run_research_slice,
)
from praxis.keys import KeyExhausted, SecretVault
from praxis.llm import (
    DEFAULT_CHAT_MODEL,
    KOKORO_VOICES,
    list_speechify_voices,
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

    speechify_voices: List[Dict[str, Any]] = []
    speechify_error: Optional[str] = None
    if vault.pool("speechify").is_configured():
        try:
            speechify_voices = list_speechify_voices(vault)
        except (KeyExhausted, Exception) as voice_error:  # noqa: BLE001
            speechify_error = str(voice_error)

    return {
        "kokoro": KOKORO_VOICES,
        "speechify": speechify_voices,
        "speechify_error": speechify_error,
        "usage": vault.export_usage(),
    }


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
        )

    budget_seconds = _clamped_budget(request_body.get("budget_seconds"))
    model_identifier = request_body.get("model") or DEFAULT_CHAT_MODEL

    def event_stream() -> Iterator[str]:
        try:
            for event in run_research_slice(
                vault,
                state=incoming_state,
                budget_seconds=budget_seconds,
                model_identifier=model_identifier,
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
                model_identifier=request_body.get("model") or DEFAULT_CHAT_MODEL,
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
                voice_identifier=voice_identifier or "alec",
                model_identifier=request_body.get("speechify_model") or "simba-3.0",
                language=request_body.get("language"),
            )
        else:
            _require_providers(vault, ["openrouter"])
            speech_result = synthesize_speech_kokoro(
                vault,
                text=block_text,
                voice_identifier=voice_identifier or "af_heart",
            )
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))

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
) -> Dict[str, Any]:
    """Voice question in, text out.

    Multipart because Whisper on OpenRouter lives on /audio/transcriptions and
    takes a file, not a JSON message list. Secrets ride along as a JSON string
    field since this request cannot have a JSON body.
    """
    try:
        secrets_payload = json.loads(secrets or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="secrets field was not valid JSON.")

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
        )
    except KeyExhausted as exhausted:
        raise HTTPException(status_code=402, detail=str(exhausted))

    return {
        "text": (transcription.get("text") or "").strip(),
        "usage": vault.export_usage(),
    }
