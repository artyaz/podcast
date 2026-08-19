"""OpenRouter chat, transcription, and speech.

Three things worth knowing, all established by probing the live API rather than
reading documentation:

  * Chat is the only thing on /chat/completions. Whisper answers HTTP 400 there
    and tells you to use /audio/transcriptions; Kokoro answers 400 and asks for
    `input` instead of `messages`. Three endpoints, not one.
  * Reasoning is off unless asked for. The default model reports
    mandatory=false with default_enabled=true, so omitting the parameter would
    silently switch reasoning on and bill for it. Some models refuse to have it
    disabled at all and answer 400 "Reasoning is mandatory", which is handled.
  * Tool calls come back one at a time from this model, so the loop below does
    not assume a parallel batch — it handles a list of any length.
"""

import base64
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .keys import SecretVault
from .transport import (
    ProviderHttpError,
    bearer_header,
    get_json,
    post_for_bytes,
    post_json,
    post_multipart_for_json,
    run_with_rotation,
)
from .tools import TOOL_SCHEMAS, dispatch_tool_call

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
SPEECHIFY_SPEECH_URL = "https://api.sws.speechify.com/v1/audio/speech"

DEFAULT_CHAT_MODEL = "~deepseek/deepseek-v4-flash-latest"
DEFAULT_TRANSCRIBE_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_KOKORO_MODEL = "hexgrad/kokoro-82m"

# Reasoning off by default. The default model reports mandatory=false with
# default_enabled=true, so leaving the parameter out would silently turn reasoning
# on and bill for it. Sending it explicitly is the only way to hold the
# non-reasoning behaviour.
DEFAULT_REASONING = {"enabled": False}


class LlmProfile:
    """Which endpoint to talk to, as which model, with what reasoning setting.

    The endpoint is configurable because any OpenAI-compatible server exposes the
    same four paths — chat completions, transcriptions, speech, and a model list.
    OpenRouter is just the default one.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_CHAT_MODEL,
        reasoning: Optional[Dict[str, Any]] = None,
        transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
        speech_model: str = DEFAULT_KOKORO_MODEL,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_CHAT_MODEL
        self.reasoning = DEFAULT_REASONING if reasoning is None else reasoning
        self.transcribe_model = transcribe_model or DEFAULT_TRANSCRIBE_MODEL
        self.speech_model = speech_model or DEFAULT_KOKORO_MODEL

    @classmethod
    def from_payload(cls, payload: Optional[Dict[str, Any]]) -> "LlmProfile":
        payload = payload or {}
        # A present-but-null reasoning value means "send no reasoning parameter",
        # which is what a model with no reasoning support wants. An absent key
        # means "use the default", which is reasoning off.
        reasoning = payload["reasoning"] if "reasoning" in payload else None
        if "reasoning" in payload and payload["reasoning"] is None:
            reasoning = {}
        return cls(
            base_url=payload.get("base_url") or DEFAULT_BASE_URL,
            model=payload.get("model") or DEFAULT_CHAT_MODEL,
            reasoning=reasoning,
            transcribe_model=payload.get("transcribe_model") or DEFAULT_TRANSCRIBE_MODEL,
            speech_model=payload.get("speech_model") or DEFAULT_KOKORO_MODEL,
        )

    def reasoning_is_active(self) -> bool:
        """Whether this profile will actually produce reasoning tokens."""
        if not self.reasoning:
            return False
        return self.reasoning.get("enabled") is not False

    def reasoning_token_allowance(self) -> int:
        """Extra output budget to request when reasoning is on.

        Reasoning tokens are spent out of `max_tokens`, not from a separate pool,
        and `exclude: true` hides the trace without stopping the thinking — a run
        with effort=low and exclude=true was measured truncating a JSON response
        at 1460 characters that completed cleanly with reasoning off. So the
        ceiling has to be raised to cover the thinking, or structured output gets
        cut off mid-string.
        """
        if not self.reasoning_is_active():
            return 0
        explicit_budget = self.reasoning.get("max_tokens")
        if isinstance(explicit_budget, int) and explicit_budget > 0:
            return explicit_budget
        return {
            "minimal": 512,
            "low": 1024,
            "medium": 2048,
            "high": 4096,
            "max": 6144,
        }.get(str(self.reasoning.get("effort") or "high"), 4096)

    @property
    def chat_url(self) -> str:
        return "{0}/chat/completions".format(self.base_url)

    @property
    def transcribe_url(self) -> str:
        return "{0}/audio/transcriptions".format(self.base_url)

    @property
    def speech_url(self) -> str:
        return "{0}/audio/speech".format(self.base_url)

    @property
    def models_url(self) -> str:
        return "{0}/models".format(self.base_url)

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

SPEECHIFY_VOICES_URL = "https://api.sws.speechify.com/v1/voices"

# Speechify's voice catalogue is model-dependent, and not as a subset. The bare
# GET /v1/voices returns 50 voices of which only 5 are English, and none of them
# are tagged simba-3.2; GET /v1/voices?model=simba-3.2 returns a different set of
# 8 English voices that the bare call never mentions. Asking for a 3.2 voice from
# the bare list therefore fails with "the selected voice is not available for
# simba-3.2", which is why the model has to be part of the query.
SPEECHIFY_MODELS = [
    {
        "id": "simba-3.2",
        "label": "Simba 3.2 — streaming-native, emotional control",
        "supports_emotion": True,
        "languages": "English only",
    },
    {
        "id": "simba-3.0",
        "label": "Simba 3.0 — 50 voices, many languages",
        "supports_emotion": False,
        "languages": "Multilingual",
    },
    {
        "id": "simba-english",
        "label": "Simba English",
        "supports_emotion": False,
        "languages": "English",
    },
    {
        "id": "simba-multilingual",
        "label": "Simba Multilingual",
        "supports_emotion": False,
        "languages": "Multilingual",
    },
]

# The five emotions Speechify documents for SSML control. Confirmed to produce
# audibly different audio on simba-3.2. An undocumented value is NOT rejected —
# "furious" returned a short clip rather than an error — so an invalid emotion
# fails silently and the list must stay closed.
SPEECHIFY_EMOTIONS = ["neutral", "calm", "cheerful", "energetic", "sad"]

MAX_TOOL_ITERATIONS_DEFAULT = 14


def chat_completion(
    vault: SecretVault,
    messages: List[Dict[str, Any]],
    profile: Optional[LlmProfile] = None,
    tools_enabled: bool = False,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    force_json_object: bool = False,
) -> Dict[str, Any]:
    """One round trip to the model. Returns the raw response body."""
    profile = profile or LlmProfile()

    def build_payload(include_reasoning: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": profile.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Sent explicitly because some OpenAI-compatible gateways stream by
            # default. One was measured answering text/event-stream to a request
            # that never mentioned streaming, which made the JSON parse fail at
            # character 0 with "Expecting value". Asking for stream:false fixes it
            # at the source; the transport layer can still fold a stream back
            # together for gateways that ignore the flag.
            "stream": False,
        }
        if include_reasoning and profile.reasoning:
            payload["reasoning"] = profile.reasoning
        if tools_enabled:
            payload["tools"] = TOOL_SCHEMAS
            payload["tool_choice"] = "auto"
        if force_json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def attempt(api_key: str):
        headers = {
            "Authorization": bearer_header(api_key),
            "Content-Type": "application/json",
        }
        try:
            response_body = post_json(
                profile.chat_url, headers=headers, payload=build_payload(True)
            )
        except ProviderHttpError as http_error:
            # Some models refuse to have reasoning switched off: OpenRouter answers
            # 400 "Reasoning is mandatory for this endpoint and cannot be disabled."
            # The UI hides the off switch for those, but a stale saved setting or a
            # third-party endpoint can still produce it, and dropping the parameter
            # is strictly better than failing the whole run.
            if http_error.status_code == 400 and "reasoning is mandatory" in (
                http_error.body_text or ""
            ).lower():
                response_body = post_json(
                    profile.chat_url, headers=headers, payload=build_payload(False)
                )
            else:
                raise
        reported_cost = float((response_body.get("usage") or {}).get("cost") or 0.0)
        return response_body, {"dollars": reported_cost}

    return run_with_rotation(vault.pool("openrouter"), attempt)


def list_chat_models(vault: SecretVault, profile: Optional[LlmProfile] = None) -> List[Dict[str, Any]]:
    """The endpoint's model catalogue, trimmed to what the picker needs.

    The `reasoning` descriptor is the important part and is passed through
    untouched. It is what lets the settings screen build an honest reasoning
    control: whether reasoning exists at all, whether it can be turned off
    (`mandatory`), and which effort values this specific model actually accepts
    (`supported_efforts` — deepseek-v4-flash takes max/high/low with no medium,
    while gpt-5 takes high/medium/low/minimal, so a hardcoded list would send
    values the model never advertised).

    A plain OpenAI-compatible server returns only `id`, so everything else is
    optional and the picker degrades to a searchable list of names.
    """
    profile = profile or LlmProfile()

    def attempt(api_key: str):
        response_body = get_json(
            profile.models_url,
            headers={"Authorization": bearer_header(api_key)},
            timeout_seconds=30.0,
        )
        return response_body, {"dollars": 0.0}

    response_body = run_with_rotation(vault.pool("openrouter"), attempt)
    raw_models = response_body.get("data") if isinstance(response_body, dict) else None
    if not isinstance(raw_models, list):
        raw_models = response_body if isinstance(response_body, list) else []

    catalogue: List[Dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict) or not raw_model.get("id"):
            continue

        # Two catalogue dialects seen in the wild. OpenRouter describes a model
        # with `supported_parameters` plus a `reasoning` descriptor. Other
        # OpenAI-compatible gateways use a `capabilities` object instead — one was
        # measured returning {"tool_calling": true, "reasoning": true, ...} and no
        # supported_parameters at all. Reading only the first dialect would report
        # every model on such a gateway as having neither tools nor reasoning,
        # which would steer you away from models that work perfectly well.
        supported_parameters = raw_model.get("supported_parameters") or []
        capabilities = raw_model.get("capabilities") or {}
        pricing = raw_model.get("pricing") or {}

        if supported_parameters:
            supports_tools = "tools" in supported_parameters
            supports_reasoning = "reasoning" in supported_parameters
            supports_effort = "reasoning_effort" in supported_parameters
            supports_include = "include_reasoning" in supported_parameters
        elif capabilities:
            supports_tools = bool(capabilities.get("tool_calling"))
            supports_reasoning = bool(
                capabilities.get("reasoning") or capabilities.get("thinking")
            )
            # This dialect says whether reasoning exists but not how it is asked
            # for, so no effort list is claimed and the reasoning control falls
            # back to a plain on/off.
            supports_effort = False
            supports_include = False
        else:
            # A bare /models listing gives only ids. Assume tools work rather than
            # hiding every model: the research loop will fail loudly on the first
            # call if they do not, which is more informative than an empty picker.
            supports_tools = True
            supports_reasoning = False
            supports_effort = False
            supports_include = False

        catalogue.append(
            {
                "id": raw_model["id"],
                "name": raw_model.get("name") or raw_model["id"],
                "context_length": raw_model.get("context_length")
                or raw_model.get("max_input_tokens"),
                "prompt_price": pricing.get("prompt"),
                "supports_tools": supports_tools,
                "supports_reasoning": supports_reasoning,
                "supports_reasoning_effort": supports_effort,
                "supports_include_reasoning": supports_include,
                "reasoning": raw_model.get("reasoning"),
            }
        )
    return catalogue


def run_tool_loop(
    vault: SecretVault,
    messages: List[Dict[str, Any]],
    profile: Optional[LlmProfile] = None,
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

        try:
            response_body = chat_completion(
                vault,
                messages=messages,
                profile=profile,
                tools_enabled=True,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except ProviderHttpError as call_failure:
            # A gateway that cannot serve a tool call — one was measured answering
            # 422 for tool requests and timing out on others — should not destroy
            # the round. Stop the loop and let the caller write up whatever
            # evidence was already gathered.
            yield {
                "type": "stopped_early",
                "reason": call_failure.diagnosis(),
                "messages": messages,
            }
            return
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


def _extract_json_object(
    text: str, allow_brace_slice: bool = True
) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a JSON object out of model output.

    Necessary because `response_format: {"type": "json_object"}` is honoured
    structurally by this model but not reliably: it truncates when it runs out of
    tokens, and it has been observed emitting a bare float inside an array of
    strings.

    `allow_brace_slice` is refused when the response was truncated. Recovering a
    cut-off response by slicing between its outermost braces can only ever produce
    a partial object, and a partial object that happens to parse is worse than a
    parse error because the caller believes it. Truncation is better handled by
    retrying with a bigger ceiling, which is what the caller does.
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

    if not allow_brace_slice:
        return None

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
    profile: Optional[LlmProfile] = None,
    max_tokens: int = 3000,
    temperature: float = 0.4,
    attempts: int = 3,
    required_keys: Optional[List[str]] = None,
    required_non_empty: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Ask for a JSON object and keep asking until a complete one parses.

    `max_tokens` defaults high on purpose: the observed failure mode is not bad
    syntax, it is a response cut off mid-string by the token ceiling. Reasoning
    makes that worse because reasoning tokens come out of the same budget, so the
    profile's allowance is added on top.

    The two `required_*` arguments guard the quieter failure. This model will
    happily return syntactically perfect JSON with the right keys and nothing in
    them — a scoping call was observed returning an empty governing axis and an
    empty question list, which the graph then carried forward as though research
    had been scoped. `required_keys` checks a key is present, which is what a
    legitimately-false boolean like "ready" needs; `required_non_empty` checks it
    actually has content, which is what a list of questions or blocks needs.
    """
    profile = profile or LlmProfile()
    conversation = list(messages)
    last_raw_text = ""
    effective_max_tokens = max_tokens + profile.reasoning_token_allowance()

    for attempt_index in range(attempts):
        response_body = chat_completion(
            vault,
            messages=conversation,
            profile=profile,
            tools_enabled=False,
            max_tokens=effective_max_tokens,
            temperature=temperature,
            force_json_object=True,
        )
        choices = response_body.get("choices") or []
        if not choices:
            continue
        was_truncated = choices[0].get("finish_reason") == "length"
        last_raw_text = (choices[0].get("message") or {}).get("content") or ""

        parsed_object = _extract_json_object(
            last_raw_text, allow_brace_slice=not was_truncated
        )
        if parsed_object is not None:
            missing_keys = [
                key for key in (required_keys or []) if key not in parsed_object
            ]
            empty_keys = [
                key for key in (required_non_empty or []) if not parsed_object.get(key)
            ]
            if not missing_keys and not empty_keys:
                return parsed_object
            last_raw_text = "missing {0}, empty {1}; got {2}".format(
                missing_keys, empty_keys, sorted(parsed_object.keys())
            )

        if was_truncated:
            effective_max_tokens = min(int(effective_max_tokens * 2), 16000)
        if attempt_index < attempts - 1:
            conversation = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "That response was not usable"
                        + (" (it was cut off)." if was_truncated else ".")
                        + " Return one complete, valid JSON object with every key"
                        + (
                            " including {0}".format(", ".join(required_keys))
                            if required_keys
                            else ""
                        )
                        + (
                            ". These came back empty and must be filled: {0}".format(
                                ", ".join(required_non_empty)
                            )
                            if required_non_empty
                            else ""
                        )
                        + " Return nothing else. Keep it compact enough to finish."
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
    profile: Optional[LlmProfile] = None,
) -> Dict[str, Any]:
    """Voice question in, text out. Multipart, not JSON."""

    profile = profile or LlmProfile()

    def attempt(api_key: str):
        response_body = post_multipart_for_json(
            profile.transcribe_url,
            headers={"Authorization": bearer_header(api_key)},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": profile.transcribe_model},
        )
        reported_cost = float((response_body.get("usage") or {}).get("cost") or 0.0)
        return response_body, {"dollars": reported_cost}

    return run_with_rotation(vault.pool("openrouter"), attempt)


def synthesize_speech_kokoro(
    vault: SecretVault,
    text: str,
    voice_identifier: str = "af_heart",
    profile: Optional[LlmProfile] = None,
) -> Dict[str, Any]:
    """Kokoro answers with raw MP3 bytes and no timing information."""
    profile = profile or LlmProfile()

    def attempt(api_key: str):
        audio_bytes, content_type = post_for_bytes(
            profile.speech_url,
            headers={
                "Authorization": bearer_header(api_key),
                "Content-Type": "application/json",
            },
            payload={
                "model": profile.speech_model,
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


def _wrap_with_emotion(text: str, emotion: Optional[str]) -> str:
    """Wrap spoken text in Speechify's SSML emotion tag.

    Only for emotions Speechify documents. An unrecognised value is accepted by
    the API rather than rejected, so passing one through would silently change the
    delivery in an unpredictable way instead of failing loudly.
    """
    if not emotion or emotion not in SPEECHIFY_EMOTIONS:
        return text
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return '<speak><speechify:emotion emotion="{0}">{1}</speechify:emotion></speak>'.format(
        emotion, escaped
    )


def synthesize_speech_speechify(
    vault: SecretVault,
    text: str,
    voice_identifier: str = "beatrice_32",
    model_identifier: str = "simba-3.2",
    language: Optional[str] = None,
    emotion: Optional[str] = None,
) -> Dict[str, Any]:
    """Speechify returns base64 audio in a JSON envelope, plus timings.

    The `speech_marks` payload carries sentence and word offsets with start and
    end times, which is what lets the player highlight words as a block is read
    aloud. Kokoro gives nothing equivalent, so that feature degrades to
    block-level highlighting on the cheaper provider.
    """

    def attempt(api_key: str):
        request_payload: Dict[str, Any] = {
            "input": _wrap_with_emotion(text, emotion),
            "voice_id": voice_identifier,
            "audio_format": "mp3",
            "model": model_identifier,
        }
        if language:
            request_payload["language"] = language

        response_body = post_json(
            SPEECHIFY_SPEECH_URL,
            headers={
                "Authorization": bearer_header(api_key),
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


def list_speechify_voices(
    vault: SecretVault, model_name: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Speechify's voices for one model, or the default catalogue.

    `model_name` does not filter a single list — it selects a different one. The
    bare call returns 50 voices, of which only 5 are English and none are tagged
    simba-3.2. Asking for simba-3.2 returns 8 English voices the bare call never
    mentions, four of them British. Synthesis rejects any pairing the chosen model
    does not list ("the selected voice is not available for simba-3.2"), so the
    picker has to be built per model or the newest voices are invisible.
    """
    request_url = SPEECHIFY_VOICES_URL
    if model_name:
        request_url = "{0}?model={1}".format(SPEECHIFY_VOICES_URL, model_name)

    def attempt(api_key: str):
        response_body = get_json(
            request_url, headers={"Authorization": bearer_header(api_key)}
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
