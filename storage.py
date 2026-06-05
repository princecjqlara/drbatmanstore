import json
import os
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any


def supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL", "").strip() and supabase_key())


def supabase_key() -> str:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.getenv("SUPABASE_ANON_KEY", "").strip()
    )


def supabase_url(path: str, query: dict[str, str] | None = None) -> str:
    base_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    encoded_query = urllib.parse.urlencode(query or {})
    return f"{base_url}{path}" + (f"?{encoded_query}" if encoded_query else "")


def supabase_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    key = supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    headers.update(extra or {})
    return headers


def store_state_id() -> str:
    return os.getenv("SUPABASE_STORE_STATE_ID", "main").strip() or "main"


def supabase_request(method: str, path: str, payload: Any | None = None, query: dict[str, str] | None = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        supabase_url(path, query),
        data=body,
        headers=supabase_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase request failed with status {error.code}: {details}") from error


def seed_data(default_data: dict[str, Any], seed_path: Path | None = None) -> dict[str, Any]:
    if seed_path and seed_path.exists():
        return json.loads(seed_path.read_text(encoding="utf-8-sig"))
    return deepcopy(default_data)


def load_store_state(default_data: dict[str, Any], seed_path: Path | None = None) -> dict[str, Any] | None:
    if not supabase_configured():
        return None
    state_id = store_state_id()
    rows = supabase_request(
        "GET",
        "/rest/v1/store_state",
        query={"id": f"eq.{state_id}", "select": "data"},
    )
    if isinstance(rows, list) and rows:
        data = rows[0].get("data")
        if isinstance(data, dict):
            return data
    data = seed_data(default_data, seed_path)
    save_store_state(data)
    return data


def save_store_state(data: dict[str, Any]) -> None:
    if not supabase_configured():
        return
    supabase_request(
        "POST",
        "/rest/v1/store_state",
        {"id": store_state_id(), "data": data},
        query={"on_conflict": "id"},
    )
