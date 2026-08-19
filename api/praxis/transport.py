"""HTTP plumbing shared by every provider call.

One job: run a request against a key pool and, when a key is rejected for a
reason that means "this key is finished", move to the next key and try again.
Everything provider-specific — which URL, which auth header, how spend is
reported — stays in the caller.
"""

import json
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from .keys import KeyExhausted, ProviderKeyPool, RETIRING_STATUS_CODES

DEFAULT_TIMEOUT_SECONDS = 90.0


class ProviderHttpError(Exception):
    """A provider answered with a non-2xx status."""

    def __init__(self, status_code: int, body_text: str):
        super().__init__("HTTP {0}: {1}".format(status_code, body_text[:400]))
        self.status_code = status_code
        self.body_text = body_text

    def is_transport_failure(self) -> bool:
        """A timeout or connection error, not a verdict from the provider."""
        return self.status_code in (503, 504)

    def retires_key(self) -> bool:
        # Status 0 is this module's own refusal to send a blank key. It is not a
        # provider verdict on the key's validity, but the key is still unusable,
        # so it is taken out of rotation with an accurate reason.
        return self.status_code in RETIRING_STATUS_CODES or self.status_code == 0

    def diagnosis(self) -> str:
        """The failure in terms of what to actually do about it.

        Providers word authentication failures in ways that point at very
        different causes, and the difference matters. Measured against
        OpenRouter: a blank value after "Bearer" returns "Missing Authentication
        header", a genuinely wrong key returns "User not found", and no header at
        all returns "No cookie auth credentials found". Reporting all three as
        "rejected" hides which one happened.
        """
        lowered = (self.body_text or "").lower()
        if self.status_code == 0:
            return self.body_text
        if "missing authentication header" in lowered:
            return (
                "HTTP 401 — the provider saw a malformed Authorization header, "
                "which means the key value sent was blank or not a plain token. "
                "Re-enter this key in Settings."
            )
        if "user not found" in lowered or "invalid api key" in lowered:
            return "HTTP 401 — the provider does not recognise this key. It is wrong or revoked."
        if self.status_code == 402:
            return "HTTP 402 — this key is out of credit."
        if self.status_code == 429:
            return "HTTP 429 — this key is rate limited right now."
        if self.is_transport_failure():
            return self.body_text
        return "HTTP {0}: {1}".format(self.status_code, (self.body_text or "")[:200])


# A provider attempt takes the key it should use and returns
# (parsed_result, spend_record). The spend record is passed straight to
# ProviderKeyPool.record_spend, so its keys must match that signature.
ProviderAttempt = Callable[[str], Tuple[Any, Dict[str, Any]]]


def bearer_header(api_key: str) -> str:
    """Build an Authorization value, refusing to build a useless one.

    A blank key produces the header `Bearer ` — which providers do not read as
    "no credentials" but as a malformed one. OpenRouter answers 401 "Missing
    Authentication header", which the rotation layer would then record as a
    rejected key and report as "all keys are exhausted or rejected". That message
    sends you looking at your account when the real problem is an empty string in
    settings. Failing here instead keeps the diagnosis accurate.
    """
    if not api_key or not api_key.strip():
        raise ProviderHttpError(
            0,
            "refusing to send a blank API key: the value was empty or whitespace, "
            "which providers report as a malformed Authorization header rather "
            "than a missing one",
        )
    return "Bearer {0}".format(api_key.strip())


def plain_key(api_key: str) -> str:
    """Validate a key sent in a bare header, such as Exa's `x-api-key`.

    Same reasoning as bearer_header: an empty value is not read as "absent" but
    as "wrong", so the resulting error blames the account instead of the setting.
    """
    if not api_key or not api_key.strip():
        raise ProviderHttpError(
            0, "refusing to send a blank API key: the value was empty or whitespace"
        )
    return api_key.strip()


def run_with_rotation(pool: ProviderKeyPool, attempt: ProviderAttempt) -> Any:
    """Try `attempt` against each usable key until one succeeds.

    A key is only abandoned on 401/402/403/429. Any other failure — a 500 from
    the provider, a timeout, malformed JSON — is a problem with the request or
    the provider, not the key, so rotating would just burn every key on the same
    broken call. Those propagate immediately.
    """
    if not pool.is_configured():
        raise KeyExhausted(
            pool.provider_name, "no keys configured in settings", attempted=[]
        )

    candidate_keys = pool.available_keys()
    if not candidate_keys:
        raise KeyExhausted(
            pool.provider_name,
            pool.last_error_text,
            attempted=sorted(pool.retired_fingerprints),
        )

    attempted_fingerprints = []
    for api_key in candidate_keys:
        attempted_fingerprints.append(pool.fingerprint(api_key))
        try:
            result, spend_record = attempt(api_key)
        except ProviderHttpError as provider_error:
            if provider_error.retires_key():
                pool.retire(api_key, provider_error.diagnosis())
                continue
            raise
        pool.record_spend(api_key, **spend_record)
        return result

    raise KeyExhausted(
        pool.provider_name, pool.last_error_text, attempted=attempted_fingerprints
    )


def _send(request_description: str, send_request: Callable[[], httpx.Response]) -> httpx.Response:
    """Perform a request, turning transport failures into readable errors.

    An httpx timeout surfaces as `ReadTimeout: The read operation timed out`,
    which says nothing about who timed out or what was being asked of them. It is
    also emphatically not the API key's fault, so it must not retire a key — one
    slow gateway would otherwise burn the whole pool.
    """
    try:
        return send_request()
    except httpx.TimeoutException as timeout_error:
        raise ProviderHttpError(
            504,
            "{0} timed out after waiting for a response ({1}). The endpoint "
            "accepted the request but never finished answering.".format(
                request_description, type(timeout_error).__name__
            ),
        )
    except httpx.RequestError as request_error:
        raise ProviderHttpError(
            503,
            "{0} could not be reached: {1}: {2}".format(
                request_description, type(request_error).__name__, str(request_error)[:160]
            ),
        )


def collapse_streamed_completion(body_text: str) -> Optional[Dict[str, Any]]:
    """Rebuild one chat-completion object from a server-sent-event stream.

    Some OpenAI-compatible gateways answer `text/event-stream` regardless of the
    `stream` flag. The chunks are the same completion split into deltas, so they
    can be folded back into the non-streamed shape the rest of this code expects.

    Tool calls are the part that needs care: they arrive as deltas addressed by
    index, with the function name usually in the first delta and `arguments`
    dribbling in across later ones, so they are accumulated per index rather than
    overwritten. Getting that wrong would silently truncate every tool call, which
    is the whole research loop.
    """
    aggregated_content = []
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    finish_reason = None
    usage = None
    model_name = None
    saw_any_chunk = False

    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data_text = line[5:].strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            chunk = json.loads(data_text)
        except json.JSONDecodeError:
            continue

        saw_any_chunk = True
        model_name = chunk.get("model") or model_name
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("content"):
                aggregated_content.append(delta["content"])

            for tool_delta in delta.get("tool_calls") or []:
                index = int(tool_delta.get("index") or 0)
                entry = tool_calls_by_index.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool_delta.get("id"):
                    entry["id"] = tool_delta["id"]
                function_delta = tool_delta.get("function") or {}
                if function_delta.get("name"):
                    entry["function"]["name"] = function_delta["name"]
                if function_delta.get("arguments"):
                    entry["function"]["arguments"] += function_delta["arguments"]

    if not saw_any_chunk:
        return None

    assistant_message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(aggregated_content),
    }
    if tool_calls_by_index:
        assistant_message["tool_calls"] = [
            tool_calls_by_index[index] for index in sorted(tool_calls_by_index)
        ]

    return {
        "id": "reassembled-stream",
        "object": "chat.completion",
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason or "stop",
                "message": assistant_message,
            }
        ],
        "usage": usage or {},
    }


def post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    response = _send(
        "POST {0}".format(url),
        lambda: httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds),
    )
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    try:
        return response.json()
    except json.JSONDecodeError as decode_error:
        # A gateway that streams when it was not asked to produces a 200 whose
        # body begins "data: {...}", and `.json()` fails at character 0. That is
        # recoverable: fold the deltas back into a normal completion.
        content_type = response.headers.get("content-type", "")
        body_text = response.text or ""
        if "text/event-stream" in content_type or body_text.lstrip().startswith("data:"):
            reassembled = collapse_streamed_completion(body_text)
            if reassembled is not None:
                return reassembled
        raise ProviderHttpError(
            response.status_code,
            "expected JSON but the endpoint sent {0} ({1} bytes): {2}. "
            "The body starts: {3!r}".format(
                content_type or "no content-type",
                len(body_text),
                str(decode_error),
                body_text[:120],
            ),
        )


def get_json(
    url: str,
    headers: Dict[str, str],
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    response = _send(
        "GET {0}".format(url),
        lambda: httpx.get(url, headers=headers, timeout=timeout_seconds),
    )
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    return response.json()


def post_for_bytes(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bytes, str]:
    response = _send(
        "POST {0}".format(url),
        lambda: httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds),
    )
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    return response.content, response.headers.get("content-type", "application/octet-stream")


def post_multipart_for_json(
    url: str,
    headers: Dict[str, str],
    files: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    response = _send(
        "POST {0}".format(url),
        lambda: httpx.post(
            url, headers=headers, files=files, data=data or {}, timeout=timeout_seconds
        ),
    )
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    return response.json()
