"""Multi-key rotation with real quota awareness.

Vercel functions are stateless, so rotation state cannot live on the server.
The browser owns the key list and the usage counters; it sends both with every
request and stores whatever the response hands back. That keeps rotation honest
across invocations without a database, which matters on the Hobby plan.

Quota is discovered differently per provider, because the providers differ:

  firecrawl   exposes real remaining credits    -> ask it
  openrouter  exposes usage and an optional cap -> ask it
  exa         exposes no balance endpoint at all -> accumulate reported cost
  speechify   exposes no balance endpoint at all -> count characters spoken

A key is retired for the rest of the request when the provider answers with
401 (bad key), 402 (out of money), or 429 (rate limited). Those three are the
only signals that actually mean "stop using this key".
"""

from typing import Any, Dict, List, Optional

RETIRING_STATUS_CODES = (401, 402, 403, 429)


class KeyExhausted(Exception):
    """No usable key remained for a provider during this request.

    The message names which keys were tried and what the provider actually said,
    because "all keys are exhausted or rejected" is unactionable when you hold
    several keys — it does not say which one broke, or whether the problem is
    credit, a typo, or a rate limit.
    """

    def __init__(self, provider_name: str, last_error: str, attempted=None):
        attempted = attempted or []
        if not attempted:
            summary = "no usable {0} key: {1}".format(provider_name, last_error)
        else:
            summary = "no usable {0} key after trying {1} ({2}). Last failure: {3}".format(
                provider_name,
                "1 key" if len(attempted) == 1 else "{0} keys".format(len(attempted)),
                ", ".join(attempted),
                last_error,
            )
        super().__init__(summary)
        self.provider_name = provider_name
        self.last_error = last_error
        self.attempted = attempted


class ProviderKeyPool:
    """An ordered pool of interchangeable API keys for one provider.

    The pool never mutates the caller's list. It tracks, per key fingerprint,
    how much has been spent and whether the key was retired mid-request, and it
    exposes that back to the browser so the next request starts where this one
    finished.
    """

    def __init__(
        self,
        provider_name: str,
        api_keys: Optional[List[str]] = None,
        usage_by_fingerprint: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.provider_name = provider_name
        self.api_keys = [key.strip() for key in (api_keys or []) if key and key.strip()]
        self.usage_by_fingerprint = dict(usage_by_fingerprint or {})
        self.retired_fingerprints = set()
        self.last_error_text = "no attempt was made"

    @staticmethod
    def fingerprint(api_key: str) -> str:
        """A short, non-secret handle for a key.

        Rotation counters must be storable and loggable. Storing the key itself
        as a dictionary key would put the secret in every log line and every
        localStorage dump, so keys are identified by their first and last few
        characters instead.
        """
        if len(api_key) <= 12:
            return "key-{0}".format(len(api_key))
        return "{0}...{1}".format(api_key[:6], api_key[-4:])

    def is_configured(self) -> bool:
        return bool(self.api_keys)

    def usage_for(self, api_key: str) -> Dict[str, Any]:
        return self.usage_by_fingerprint.setdefault(
            self.fingerprint(api_key),
            {"spent_dollars": 0.0, "calls": 0, "characters": 0, "remaining_credits": None},
        )

    def available_keys(self) -> List[str]:
        """Keys still usable, best first.

        Ordering rule: a key whose provider reports real remaining credits
        sorts by that number, descending, so the healthiest key goes first.
        Keys with no known credit figure fall back to least-spent-first, which
        spreads load evenly instead of draining one key while others idle.
        """
        usable = [
            api_key
            for api_key in self.api_keys
            if self.fingerprint(api_key) not in self.retired_fingerprints
        ]

        def sort_key(api_key: str):
            usage = self.usage_for(api_key)
            remaining = usage.get("remaining_credits")
            has_known_credits = remaining is not None
            # Negative remaining sorts descending; spent ascending.
            return (
                0 if has_known_credits else 1,
                -float(remaining) if has_known_credits else 0.0,
                float(usage.get("spent_dollars") or 0.0),
            )

        return sorted(usable, key=sort_key)

    def retire(self, api_key: str, reason: str) -> None:
        self.retired_fingerprints.add(self.fingerprint(api_key))
        self.last_error_text = reason

    def record_spend(
        self,
        api_key: str,
        dollars: float = 0.0,
        characters: int = 0,
        remaining_credits: Optional[float] = None,
    ) -> None:
        usage = self.usage_for(api_key)
        usage["calls"] = int(usage.get("calls") or 0) + 1
        usage["spent_dollars"] = round(float(usage.get("spent_dollars") or 0.0) + dollars, 6)
        usage["characters"] = int(usage.get("characters") or 0) + characters
        if remaining_credits is not None:
            usage["remaining_credits"] = remaining_credits

    def export_usage(self) -> Dict[str, Dict[str, Any]]:
        """Counters to hand back to the browser."""
        return self.usage_by_fingerprint


class SecretVault:
    """Every provider pool for one request, built from the browser's payload.

    Nothing here is ever read from the environment. The whole point of the
    settings screen is that keys travel with the request and are never baked
    into the deployment, so an accidental `os.environ` read would quietly break
    that promise.
    """

    PROVIDER_NAMES = ("openrouter", "exa", "firecrawl", "speechify")

    def __init__(self, secrets_payload: Optional[Dict[str, Any]] = None):
        payload = secrets_payload or {}
        key_lists = payload.get("keys") or {}
        usage_records = payload.get("usage") or {}
        self.pools = {}
        for provider_name in self.PROVIDER_NAMES:
            raw_keys = key_lists.get(provider_name) or []
            if isinstance(raw_keys, str):
                raw_keys = [raw_keys]
            self.pools[provider_name] = ProviderKeyPool(
                provider_name=provider_name,
                api_keys=raw_keys,
                usage_by_fingerprint=usage_records.get(provider_name) or {},
            )

    def pool(self, provider_name: str) -> ProviderKeyPool:
        if provider_name not in self.pools:
            raise KeyError("unknown provider: {0}".format(provider_name))
        return self.pools[provider_name]

    def missing_providers(self, required: List[str]) -> List[str]:
        return [name for name in required if not self.pool(name).is_configured()]

    def export_usage(self) -> Dict[str, Dict[str, Any]]:
        return {
            provider_name: pool.export_usage()
            for provider_name, pool in self.pools.items()
        }
