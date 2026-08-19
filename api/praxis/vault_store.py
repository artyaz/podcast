"""Opaque encrypted blobs for the browser vault.

The server never sees plaintext. Each row is ciphertext the client produced
with a key that never leaves the browser. Lookup is by a hash of that key
(`vault_id`), which is unguessable and not sufficient to decrypt.

Storage backends, in order of preference:

  1. Upstash / Vercel KV, when KV_REST_API_URL and KV_REST_API_TOKEN are set
  2. A JSON file at PRAXIS_VAULT_PATH (defaults to api/.vault.json locally)

Vercel functions have no durable disk, so production needs the KV pair.
"""

import json
import os
import threading
from typing import Any, Dict, List, Optional

import httpx

_file_lock = threading.Lock()


def _vault_path() -> str:
    configured = os.environ.get("PRAXIS_VAULT_PATH")
    if configured:
        return configured
    beside_package = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".vault.json")
    )
    # Vercel Python functions are read-only except /tmp. Writing next to the
    # package raises Errno 30 and the browser saw HTTP 503 on every unlock.
    parent = os.path.dirname(beside_package)
    if os.access(parent, os.W_OK):
        return beside_package
    return "/tmp/praxis-vault.json"


def _kv_configured() -> bool:
    return bool(os.environ.get("KV_REST_API_URL") and os.environ.get("KV_REST_API_TOKEN"))


def _kv_headers() -> Dict[str, str]:
    return {
        "Authorization": "Bearer {0}".format(os.environ["KV_REST_API_TOKEN"]),
        "Content-Type": "application/json",
    }


def _kv_key(vault_id: str) -> str:
    return "praxis:vault:{0}".format(vault_id)


def _load_file() -> Dict[str, Any]:
    path = os.path.abspath(_vault_path())
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        return parsed if isinstance(parsed, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _save_file(payload: Dict[str, Any]) -> None:
    path = os.path.abspath(_vault_path())
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(tmp_path, path)


def _kv_get(vault_id: str) -> Dict[str, Any]:
    url = os.environ["KV_REST_API_URL"].rstrip("/") + "/get/" + _kv_key(vault_id)
    response = httpx.get(url, headers=_kv_headers(), timeout=20.0)
    response.raise_for_status()
    body = response.json()
    result = body.get("result") if isinstance(body, dict) else None
    if not result:
        return {}
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _kv_set(vault_id: str, payload: Dict[str, Any]) -> None:
    url = os.environ["KV_REST_API_URL"].rstrip("/") + "/set/" + _kv_key(vault_id)
    response = httpx.post(
        url, headers=_kv_headers(), content=json.dumps(payload), timeout=20.0
    )
    response.raise_for_status()


def load_vault(vault_id: str) -> Dict[str, Dict[str, Any]]:
    """Return {row_id: {ciphertext, updated_at}} for this vault."""
    if _kv_configured():
        stored = _kv_get(vault_id)
    else:
        with _file_lock:
            stored = (_load_file().get("vaults") or {}).get(vault_id) or {}
    rows = stored.get("rows") if isinstance(stored, dict) else None
    return rows if isinstance(rows, dict) else {}


def merge_rows(
    existing: Dict[str, Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    merged = dict(existing)
    for raw_row in incoming or []:
        if not isinstance(raw_row, dict):
            continue
        row_id = str(raw_row.get("id") or "").strip()
        ciphertext = raw_row.get("ciphertext")
        if not row_id or not isinstance(ciphertext, str) or not ciphertext:
            continue
        try:
            updated_at = int(raw_row.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        held = merged.get(row_id) or {}
        try:
            held_updated = int(held.get("updated_at") or 0)
        except (TypeError, ValueError):
            held_updated = 0
        if updated_at >= held_updated:
            merged[row_id] = {"ciphertext": ciphertext, "updated_at": updated_at}
    return merged


def save_vault(vault_id: str, rows: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    payload = {"rows": rows}
    if _kv_configured():
        _kv_set(vault_id, payload)
        return rows
    try:
        with _file_lock:
            stored = _load_file()
            vaults = stored.get("vaults") if isinstance(stored.get("vaults"), dict) else {}
            vaults[vault_id] = payload
            stored["vaults"] = vaults
            _save_file(stored)
    except OSError:
        # IndexedDB is the durable copy. A read-only host without KV should not
        # fail the request — the next unlock will retry the push.
        return rows
    return rows


def upsert_rows(vault_id: str, incoming: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    existing = load_vault(vault_id)
    merged = merge_rows(existing, incoming)
    return save_vault(vault_id, merged)


def rows_as_list(rows: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    listed = []
    for row_id, body in rows.items():
        listed.append(
            {
                "id": row_id,
                "ciphertext": body.get("ciphertext") or "",
                "updated_at": int(body.get("updated_at") or 0),
            }
        )
    return listed
