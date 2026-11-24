"""Send the cost of each LiteLLM call to Luca, without prompts or responses.

Add to the proxy's config.yaml:

    litellm_settings:
      callbacks: ["luca_litellm.handler"]

and set two environment variables on the proxy:

    LUCA_LITELLM_URL    the webhook URL Luca shows for the connection
    LUCA_LITELLM_TOKEN  the ingest token you gave the connection

Only the fields accounting needs leave the proxy: when the call was made, the provider and model, the cost and
token counts, and the team, key, project, customer, tags and MCP server LiteLLM records with it. The prompt, the
response, tool arguments and tool results are dropped here, before anything is sent.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx
from litellm.integrations.custom_logger import CustomLogger

__all__ = ["LucaLogger", "to_record", "handler", "__version__"]
__version__ = "0.1.1"

log = logging.getLogger("luca_litellm")

# The top-level fields of LiteLLM's standard logging payload that Luca reads. Nothing else is sent.
_TOP = (
    "id", "status", "call_type", "startTime", "endTime", "model", "model_group", "model_id", "custom_llm_provider",
    "response_cost", "prompt_tokens", "completion_tokens", "total_tokens", "cache_hit", "end_user",
)
# From the call's metadata: who made it and for whom. The user's email address is left out unless
# LUCA_LITELLM_INCLUDE_EMAIL is set; the user's ID is enough for Luca to split usage by user.
_META = (
    "user_api_key_hash", "user_api_key_alias", "user_api_key_team_id", "user_api_key_team_alias",
    "user_api_key_user_id", "user_api_key_project_id", "user_api_key_project_alias", "user_api_key_end_user_id",
    "user_api_key_org_id", "user_api_key_org_alias", "team_id", "team_alias",
    # LiteLLM's customer hierarchy (v1.111): the customer organization, its team and the member's ID (not the member's
    # alias, which is often a person's name).
    "user_api_key_customer_org_id", "user_api_key_customer_org_alias", "user_api_key_customer_team_id",
    "user_api_key_customer_team_alias", "user_api_key_customer_member_id",
)
# From the usage object: only the cached-token counts, which change the cost.
_USAGE = ("cache_read_input_tokens", "cache_creation_input_tokens")

_MAX_QUEUE = 20_000


def _host(api_base: Any) -> str | None:
    """The provider's address without its path or query, which can hold a deployment name or a key."""
    if not isinstance(api_base, str) or not api_base:
        return None
    p = urlsplit(api_base if "://" in api_base else f"https://{api_base}")
    return f"{p.scheme}://{p.hostname}" if p.hostname else None


def to_record(payload: Any, include_email: bool = False) -> dict | None:
    """The part of one call that Luca keeps, or None when there is nothing to send (no payload, a failed call)."""
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    if payload.get("status") not in (None, "success"):
        return None
    rec: dict[str, Any] = {k: payload[k] for k in _TOP if payload.get(k) is not None}
    host = _host(payload.get("api_base"))
    if host:
        rec["api_base"] = host
    tags = payload.get("request_tags")
    # LiteLLM adds the client's User-Agent as tags; those say nothing about who the call served.
    tags = [str(t) for t in tags if not str(t).startswith("User-Agent:")] if isinstance(tags, list) else []
    if tags:
        rec["request_tags"] = tags
    md = payload.get("metadata") or {}
    meta: dict[str, Any] = {k: md[k] for k in _META if md.get(k) is not None}
    if include_email and md.get("user_api_key_user_email"):
        meta["user_api_key_user_email"] = md["user_api_key_user_email"]
    usage = md.get("usage_object") or {}
    if isinstance(usage, dict):
        u: dict[str, Any] = {k: usage[k] for k in _USAGE if usage.get(k) is not None}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is not None:
            u["prompt_tokens_details"] = {"cached_tokens": cached}
        if u:
            meta["usage_object"] = u
    mcp = md.get("mcp_tool_call_metadata") or {}
    if isinstance(mcp, dict) and (mcp.get("mcp_server_name") or mcp.get("name")):
        meta["mcp_tool_call_metadata"] = {k: mcp[k] for k in ("mcp_server_name", "name") if mcp.get(k)}
    if meta:
        rec["metadata"] = meta
    return rec


class LucaLogger(CustomLogger):
    """A LiteLLM callback that batches each successful call's cost fields and posts them to Luca."""

    def __init__(self, url: str | None = None, token: str | None = None, batch_size: int = 200,
                 flush_seconds: float = 10.0, timeout: float = 10.0, include_email: bool | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._url = url
        self._token = token
        self.batch_size = max(1, min(int(batch_size), 1000))
        self.flush_seconds = max(1.0, float(flush_seconds))
        self.timeout = float(timeout)
        self._include_email = include_email
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._warned = False
        self._seen: dict[str, None] = {}
        atexit.register(self.flush)

    # Settings are read when first needed, so the module can be imported before the environment is set.
    @property
    def url(self) -> str | None:
        return self._url or os.environ.get("LUCA_LITELLM_URL")

    @property
    def token(self) -> str | None:
        return self._token or os.environ.get("LUCA_LITELLM_TOKEN")

    @property
    def include_email(self) -> bool:
        if self._include_email is not None:
            return self._include_email
        return os.environ.get("LUCA_LITELLM_INCLUDE_EMAIL", "").lower() in ("1", "true", "yes")

    def _ready(self) -> bool:
        if self.url and self.token:
            return True
        if not self._warned:
            log.warning("luca_litellm: set LUCA_LITELLM_URL and LUCA_LITELLM_TOKEN; nothing is sent until both are set")
            self._warned = True
        return False

    def _add(self, kwargs: dict) -> bool:
        rec = to_record(kwargs.get("standard_logging_object"), self.include_email)
        if rec is None:
            return False
        with self._lock:
            # LiteLLM can report one call to both the sync and the async hook; send it once.
            if rec["id"] in self._seen:
                return False
            self._seen[rec["id"]] = None
            if len(self._seen) > _MAX_QUEUE:
                del self._seen[next(iter(self._seen))]
            self._queue.append(rec)
            if len(self._queue) > _MAX_QUEUE:
                dropped = len(self._queue) - _MAX_QUEUE
                del self._queue[:dropped]
                log.warning("luca_litellm: Luca could not be reached; %d oldest calls dropped from the queue", dropped)
            return len(self._queue) >= self.batch_size

    def _take(self) -> list[dict]:
        with self._lock:
            batch, self._queue = self._queue[: self.batch_size], self._queue[self.batch_size:]
            return batch

    def _put_back(self, batch: list[dict]) -> None:
        with self._lock:
            self._queue[:0] = batch

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": f"luca-litellm/{__version__}"}

    @staticmethod
    def _ok(res: httpx.Response) -> bool:
        if res.status_code < 300:
            return True
        # A rejected delivery (bad token, wrong URL) would fail again; keep it only for errors that may pass.
        if res.status_code in (408, 429) or res.status_code >= 500:
            return False
        log.error("luca_litellm: Luca refused the delivery (%s): %s", res.status_code, res.text[:300])
        return True

    async def async_flush(self) -> None:
        if not self._ready():
            return
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                batch = self._take()
                if not batch:
                    return
                try:
                    if not self._ok(await client.post(self.url, json=batch, headers=self._headers())):
                        self._put_back(batch)
                        return
                except httpx.HTTPError as e:
                    log.warning("luca_litellm: could not reach Luca (%s); will retry", e)
                    self._put_back(batch)
                    return

    def flush(self) -> None:
        if not self._queue or not self._ready():
            return
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                batch = self._take()
                if not batch:
                    return
                try:
                    if not self._ok(client.post(self.url, json=batch, headers=self._headers())):
                        self._put_back(batch)
                        return
                except httpx.HTTPError as e:
                    log.warning("luca_litellm: could not reach Luca (%s); will retry", e)
                    self._put_back(batch)
                    return

    async def _every(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            try:
                await self.async_flush()
            except Exception:  # never let the background loop die
                log.exception("luca_litellm: flush failed")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            full = self._add(kwargs)
            if self._task is None or self._task.done():
                self._task = asyncio.get_running_loop().create_task(self._every())
            if full:
                await self.async_flush()
        except Exception:  # logging must never affect the call itself
            log.exception("luca_litellm: could not record a call")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            if self._add(kwargs):
                self.flush()
        except Exception:
            log.exception("luca_litellm: could not record a call")


# The instance the proxy's config names: callbacks: ["luca_litellm.handler"]
handler = LucaLogger()
