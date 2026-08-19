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

    def retires_key(self) -> bool:
        return self.status_code in RETIRING_STATUS_CODES


# A provider attempt takes the key it should use and returns
# (parsed_result, spend_record). The spend record is passed straight to
# ProviderKeyPool.record_spend, so its keys must match that signature.
ProviderAttempt = Callable[[str], Tuple[Any, Dict[str, Any]]]


def run_with_rotation(pool: ProviderKeyPool, attempt: ProviderAttempt) -> Any:
    """Try `attempt` against each usable key until one succeeds.

    A key is only abandoned on 401/402/403/429. Any other failure — a 500 from
    the provider, a timeout, malformed JSON — is a problem with the request or
    the provider, not the key, so rotating would just burn every key on the same
    broken call. Those propagate immediately.
    """
    if not pool.is_configured():
        raise KeyExhausted(pool.provider_name, "no keys configured in settings")

    candidate_keys = pool.available_keys()
    if not candidate_keys:
        raise KeyExhausted(pool.provider_name, pool.last_error_text)

    for api_key in candidate_keys:
        try:
            result, spend_record = attempt(api_key)
        except ProviderHttpError as provider_error:
            if provider_error.retires_key():
                pool.retire(api_key, str(provider_error))
                continue
            raise
        pool.record_spend(api_key, **spend_record)
        return result

    raise KeyExhausted(pool.provider_name, pool.last_error_text)


def post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    response = httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds)
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    try:
        return response.json()
    except json.JSONDecodeError as decode_error:
        raise ProviderHttpError(
            response.status_code,
            "provider returned non-JSON body: {0}".format(str(decode_error)),
        )


def get_json(
    url: str,
    headers: Dict[str, str],
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    response = httpx.get(url, headers=headers, timeout=timeout_seconds)
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    return response.json()


def post_for_bytes(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bytes, str]:
    response = httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds)
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
    response = httpx.post(
        url, headers=headers, files=files, data=data or {}, timeout=timeout_seconds
    )
    if response.status_code >= 300:
        raise ProviderHttpError(response.status_code, response.text)
    return response.json()
