"""OpenRouter chat, transcription, and speech.

Three things worth knowing, all established by probing the live API rather than
reading documentation:

  * Chat is the only thing on /chat/completions. Whisper answers HTTP 400 there
    and tells you to use /audio/transcriptions; Kokoro answers 400 and asks for
    `input` instead of `messages`. Three endpoints, not one.
  * The model runs with reasoning explicitly disabled. It is the non-reasoning
    variant by request, and passing `reasoning: {"enabled": false}` is what
    actually holds it there.
  * Tool calls come back one at a time from this model, so the loop below does
    not assume a parallel batch — it handles a list of any length.
"""

import base64
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .keys import SecretVault
from .transport import (
    post_for_bytes,
    post_json,
    post_multipart_for_json,
    run_with_rotation,
)
from .tools import TOOL_SCHEMAS, dispatch_tool_call

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TRANSCRIBE_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
OPENROUTER_SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
SPEECHIFY_SPEECH_URL = "https://api.sws.speechify.com/v1/audio/speech"

DEFAULT_CHAT_MODEL = "~deepseek/deepseek-v4-flash-latest"
DEFAULT_TRANSCRIBE_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_KOKORO_MODEL = "hexgrad/kokoro-82m"

# Kokoro exposes no voice listing endpoint — /audio/voices is a 404. These IDs
# were confirmed one by one against the live endpoint; an unknown ID returns
# HTTP 400, so this list is the contract. The prefix encodes accent and gender:
# af/am = American female/male, bf/bm = British female/male.
KOKORO_VOICES = [
    {"id": "af_heart", "label": "Heart", "accent": "American", "gender": "female"},
    {"id": "af_bella", "label": "Bella", "accent": "American", "gender": "female"},
    {"id": "af_nicole", "label": "Nicole", "accent": "American", "gender": "female"},
    {"id": "am_michael", "label": "Michael", "accent": "American", "gender": "male"},
    {"id": "am_adam", "label": "Adam", "accent": "American", "gender": "male"},
    {"id": "bf_emma", "label": "Emma", "accent": "British", "gender": "female"},
    {"id": "bm_george", "label": "George", "accent": "British", "gender": "male"},
]

MAX_TOOL_ITERATIONS_DEFAULT = 14


def chat_completion(
    vault: SecretVault,
    messages: List[Dict[str, Any]],
    model_identifier: str = DEFAULT_CHAT_MODEL,
    tools_enabled: bool = False,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    force_json_object: bool = False,
) -> Dict[str, Any]:
    """One round trip to the model. Returns the raw assistant message."""
    request_payload: Dict[str, Any] = {
        "model": model_identifier,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning": {"enabled": False},
    }
    if tools_enabled:
        request_payload["tools"] = TOOL_SCHEMAS
        request_payload["tool_choice"] = "auto"
    if force_json_object:
        request_payload["response_format"] = {"type": "json_object"}

    def attempt(api_key: str):
        response_body = post_json(
            OPENROUTER_CHAT_URL,
            headers={
                "Authorization": "Bearer {0}".format(api_key),
                "Content-Type": "application/json",
            },
            payload=request_payload,
        )
        reported_cost = float((response_body.get("usage") or {}).get("cost") or 0.0)
        return response_body, {"dollars": reported_cost}

    return run_with_rotation(vault.pool("openrouter"), attempt)


def run_tool_loop(
    vault: SecretVault,
    messages: List[Dict[str, Any]],
    model_identifier: str = DEFAULT_CHAT_MODEL,
    max_iterations: int = MAX_TOOL_ITERATIONS_DEFAULT,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    deadline: Optional[Any] = None,
    tool_reserve_seconds: float = 55.0,
) -> Iterator[Dict[str, Any]]:
    """Drive the model until it stops asking for tools.

    Yields events as it goes so the caller can stream progress to the browser:
    `tool_call`, `tool_result`, `assistant_text`, and finally `finished` with the
    full message list. The conversation is mutated in place, so the caller ends
    up holding the transcript either way.

    `tool_reserve_seconds` is the important parameter. The loop stops issuing new
    tool calls once the remaining budget drops below it, leaving the caller time
    to spend one final turn turning the gathered evidence into prose. Without that
    reserve the loop runs right up to the deadline and dies mid-search, having
    spent real money on searches whose results were never written down — the
    caller sees `stopped_early` with no `assistant_text` and, unless it is careful,
    silently discards the whole round.
    """
    for iteration_index in range(max_iterations):
        if deadline is not None and not deadline.has_room_for(tool_reserve_seconds):
            yield {
                "type": "stopped_early",
                "reason": "tool budget reserved for write-up",
                "messages": messages,
            }
            return

        response_body = chat_completion(
            vault,
            messages=messages,
            model_identifier=model_identifier,
            tools_enabled=True,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choices = response_body.get("choices") or []
        if not choices:
            yield {"type": "error", "error": "model returned no choices", "messages": messages}
            return

        assistant_message = choices[0].get("message") or {}
        requested_tool_calls = assistant_message.get("tool_calls") or []

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.get("content") or "",
                "tool_calls": requested_tool_calls,
            }
            if requested_tool_calls
            else {"role": "assistant", "content": assistant_message.get("content") or ""}
        )

        if not requested_tool_calls:
            yield {
                "type": "assistant_text",
                "text": assistant_message.get("content") or "",
                "iterations_used": iteration_index + 1,
            }
            yield {"type": "finished", "messages": messages}
            return

        for tool_call in requested_tool_calls:
            function_block = tool_call.get("function") or {}
            tool_name = function_block.get("name") or ""
            try:
                tool_arguments = json.loads(function_block.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_arguments = {}

            yield {"type": "tool_call", "tool": tool_name, "arguments": tool_arguments}
            tool_result = dispatch_tool_call(vault, tool_name, tool_arguments)
            yield {"type": "tool_result", "tool": tool_name, "result": tool_result}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id") or "",
                    "content": json.dumps(tool_result)[:20000],
                }
            )

    yield {"type": "stopped_early", "reason": "max_iterations", "messages": messages}


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a JSON object out of model output.

    Necessary because `response_format: {"type": "json_object"}` is honoured
    structurally by this model but not reliably: it truncates when it runs out of
    tokens, and it has been observed emitting a bare float inside an array of
    strings. So parse optimistically, then fall back to slicing out the outermost
    braces, then give up and let the caller retry.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        newline_index = candidate.find("\n")
        if newline_index != -1:
            candidate = candidate[newline_index + 1 :]
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    opening_index = candidate.find("{")
    closing_index = candidate.rfind("}")
    if opening_index == -1 or closing_index <= opening_index:
        return None
    try:
        parsed = json.loads(candidate[opening_index : closing_index + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def chat_json(
    vault: SecretVault,
    messages: List[Dict[str, Any]],
    model_identifier: str = DEFAULT_CHAT_MODEL,
    max_tokens: int = 3000,
    temperature: float = 0.4,
    attempts: int = 3,
) -> Dict[str, Any]:
    """Ask for a JSON object and keep asking until one parses.

    `max_tokens` defaults high on purpose. The observed failure mode is not bad
    syntax, it is a response cut off mid-string by the token ceiling.
    """
    conversation = list(messages)
    last_raw_text = ""
    for attempt_index in range(attempts):
        response_body = chat_completion(
            vault,
            messages=conversation,
            model_identifier=model_identifier,
            tools_enabled=False,
            max_tokens=max_tokens,
            temperature=temperature,
            force_json_object=True,
        )
        choices = response_body.get("choices") or []
        if not choices:
            continue
        last_raw_text = (choices[0].get("message") or {}).get("content") or ""
        parsed_object = _extract_json_object(last_raw_text)
        if parsed_object is not None:
            return parsed_object

        was_truncated = choices[0].get("finish_reason") == "length"
        if was_truncated:
            max_tokens = min(int(max_tokens * 2), 12000)
        if attempt_index < attempts - 1:
            conversation = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "That response was not parseable JSON"
                        + (" (it was cut off)." if was_truncated else ".")
                        + " Return one complete, valid JSON object and nothing else. "
                        "Keep it compact enough to finish."
                    ),
                }
            ]

    raise ValueError(
        "model did not return parseable JSON after {0} attempts; last output began: {1}".format(
            attempts, last_raw_text[:200]
        )
    )


def transcribe_audio(
    vault: SecretVault,
    audio_bytes: bytes,
    filename: str = "question.webm",
    content_type: str = "audio/webm",
    model_identifier: str = DEFAULT_TRANSCRIBE_MODEL,
) -> Dict[str, Any]:
    """Voice question in, text out. Multipart, not JSON."""

    def attempt(api_key: str):
        response_body = post_multipart_for_json(
            OPENROUTER_TRANSCRIBE_URL,
            headers={"Authorization": "Bearer {0}".format(api_key)},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": model_identifier},
        )
        reported_cost = float((response_body.get("usage") or {}).get("cost") or 0.0)
        return response_body, {"dollars": reported_cost}

    return run_with_rotation(vault.pool("openrouter"), attempt)


def synthesize_speech_kokoro(
    vault: SecretVault,
    text: str,
    voice_identifier: str = "af_heart",
    model_identifier: str = DEFAULT_KOKORO_MODEL,
) -> Dict[str, Any]:
    """Kokoro answers with raw MP3 bytes and no timing information."""

    def attempt(api_key: str):
        audio_bytes, content_type = post_for_bytes(
            OPENROUTER_SPEECH_URL,
            headers={
                "Authorization": "Bearer {0}".format(api_key),
                "Content-Type": "application/json",
            },
            payload={
                "model": model_identifier,
                "input": text,
                "voice": voice_identifier,
                "response_format": "mp3",
            },
        )
        result = {
            "audio_bytes": audio_bytes,
            "content_type": content_type or "audio/mpeg",
            "speech_marks": None,
            "billable_characters": len(text),
        }
        return result, {"characters": len(text)}

    return run_with_rotation(vault.pool("openrouter"), attempt)


def synthesize_speech_speechify(
    vault: SecretVault,
    text: str,
    voice_identifier: str = "alec",
    model_identifier: str = "simba-3.0",
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Speechify returns base64 audio in a JSON envelope, plus timings.

    The `speech_marks` payload carries sentence and word offsets with start and
    end times, which is what lets the player highlight words as a block is read
    aloud. Kokoro gives nothing equivalent, so that feature degrades to
    block-level highlighting on the cheaper provider.
    """

    def attempt(api_key: str):
        request_payload: Dict[str, Any] = {
            "input": text,
            "voice_id": voice_identifier,
            "audio_format": "mp3",
            "model": model_identifier,
        }
        if language:
            request_payload["language"] = language

        response_body = post_json(
            SPEECHIFY_SPEECH_URL,
            headers={
                "Authorization": "Bearer {0}".format(api_key),
                "Content-Type": "application/json",
            },
            payload=request_payload,
        )
        audio_bytes = base64.b64decode(response_body.get("audio_data") or "")
        billable_characters = int(
            response_body.get("billable_characters_count") or len(text)
        )
        result = {
            "audio_bytes": audio_bytes,
            "content_type": "audio/mpeg",
            "speech_marks": response_body.get("speech_marks"),
            "billable_characters": billable_characters,
        }
        return result, {"characters": billable_characters}

    return run_with_rotation(vault.pool("speechify"), attempt)


def list_speechify_voices(vault: SecretVault) -> List[Dict[str, Any]]:
    """Speechify does publish a voice list, unlike Kokoro."""
    from .transport import get_json

    def attempt(api_key: str):
        response_body = get_json(
            "https://api.sws.speechify.com/v1/voices",
            headers={"Authorization": "Bearer {0}".format(api_key)},
        )
        return response_body, {"dollars": 0.0}

    response_body = run_with_rotation(vault.pool("speechify"), attempt)
    raw_voices = (
        response_body.get("voices") if isinstance(response_body, dict) else response_body
    ) or []

    simplified_voices = []
    for raw_voice in raw_voices:
        model_names = [model.get("name") for model in (raw_voice.get("models") or [])]
        simplified_voices.append(
            {
                "id": raw_voice.get("id"),
                "label": raw_voice.get("display_name"),
                "gender": raw_voice.get("gender"),
                "locale": raw_voice.get("locale"),
                "models": [name for name in model_names if name],
                "preview_audio": raw_voice.get("preview_audio"),
            }
        )
    return simplified_voices
