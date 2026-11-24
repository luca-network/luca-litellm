"""luca-litellm: what leaves the proxy, batching, retries, and a real LiteLLM call through the callback."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import litellm
import pytest

from luca_litellm import LucaLogger, to_record

PAYLOAD = {
    "id": "call-1", "status": "success", "call_type": "acompletion", "startTime": 1791072000.5, "endTime": 1791072001.0,
    "model": "gpt-4o", "model_group": "gpt-4o", "custom_llm_provider": "openai",
    "api_base": "https://myco.openai.azure.com/openai/deployments/prod?api-key=SECRETKEY",
    "response_cost": 0.0123, "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cache_hit": False,
    "end_user": "acme-prod", "request_tags": ["batch"],
    "messages": [{"role": "user", "content": "secret prompt"}], "response": {"text": "secret answer"},
    "model_parameters": {"temperature": 0}, "requester_ip_address": "10.0.0.8",
    "metadata": {
        "user_api_key_hash": "hash1", "user_api_key_team_id": "t_1", "user_api_key_team_alias": "Support",
        "user_api_key_user_id": "u_9", "user_api_key_user_email": "person@myco.example",
        "usage_object": {"prompt_tokens_details": {"cached_tokens": 20}, "completion_tokens": 30},
        "mcp_tool_call_metadata": {"name": "search", "mcp_server_name": "github", "arguments": {"q": "secret"}, "result": {"r": "secret"}},
        "requester_metadata": {"note": "secret"},
    },
}


def test_only_the_fields_accounting_needs_leave_the_proxy():
    r = to_record(PAYLOAD)
    text = json.dumps(r)
    assert "secret" not in text.lower() and "SECRETKEY" not in text, "no prompt, response, tool arguments or key"
    assert "person@myco.example" not in text, "no email address unless asked for"
    assert "10.0.0.8" not in text
    assert r["api_base"] == "https://myco.openai.azure.com"
    assert r["response_cost"] == 0.0123 and r["prompt_tokens"] == 120
    assert r["end_user"] == "acme-prod" and r["request_tags"] == ["batch"]
    assert r["metadata"]["user_api_key_team_alias"] == "Support"
    assert r["metadata"]["usage_object"] == {"prompt_tokens_details": {"cached_tokens": 20}}
    assert r["metadata"]["mcp_tool_call_metadata"] == {"mcp_server_name": "github", "name": "search"}
    assert to_record(PAYLOAD, include_email=True)["metadata"]["user_api_key_user_email"] == "person@myco.example"
    assert to_record({**PAYLOAD, "status": "failure"}) is None
    assert to_record(None) is None and to_record({"status": "success"}) is None


def test_litellm_customer_hierarchy_is_kept_without_the_members_name():
    """LiteLLM's customer hierarchy (v1.111): organization and team with their aliases, the member's ID only."""
    md = {"user_api_key_customer_org_id": "org-acme", "user_api_key_customer_org_alias": "Acme",
          "user_api_key_customer_team_id": "acme-support", "user_api_key_customer_team_alias": "Support",
          "user_api_key_customer_member_id": "acme-alice", "user_api_key_customer_member_alias": "Alice"}
    r = to_record({**PAYLOAD, "end_user": None, "metadata": {**PAYLOAD["metadata"], **md}})
    kept = {k: v for k, v in r["metadata"].items() if "customer" in k}
    assert kept == {k: v for k, v in md.items() if k != "user_api_key_customer_member_alias"}
    assert "Alice" not in json.dumps(r)


class Receiver:
    """A local stand-in for Luca's webhook: records each delivery, and can fail the first few."""

    def __init__(self, fail_first=0, status=503):
        self.deliveries, self.headers, self.fail_left, self.status = [], [], fail_first, status
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if outer.fail_left > 0:
                    outer.fail_left -= 1
                    self.send_response(outer.status); self.end_headers(); return
                outer.deliveries.append(json.loads(body)); outer.headers.append(dict(self.headers))
                self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
                self.wfile.write(b'{"stored":1}')

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/hooks/litellm/conn_1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_batches_retries_and_never_sends_without_settings(monkeypatch):
    monkeypatch.delenv("LUCA_LITELLM_URL", raising=False)
    monkeypatch.delenv("LUCA_LITELLM_TOKEN", raising=False)
    idle = LucaLogger()
    idle.log_success_event({"standard_logging_object": PAYLOAD}, None, None, None)
    idle.flush()  # no URL or token: nothing is sent, nothing raises
    assert len(idle._queue) == 1

    rx = Receiver(fail_first=1)
    try:
        lg = LucaLogger(url=rx.url, token="t" * 40, batch_size=2)
        lg.log_success_event({"standard_logging_object": PAYLOAD}, None, None, None)
        lg.log_success_event({"standard_logging_object": PAYLOAD}, None, None, None)  # the same call twice: kept once
        assert rx.deliveries == [] and len(lg._queue) == 1
        lg.log_success_event({"standard_logging_object": {**PAYLOAD, "id": "call-2"}}, None, None, None)
        assert rx.deliveries == [], "Luca was down: the batch is kept"
        assert len(lg._queue) == 2
        lg.flush()
        assert [r["id"] for r in rx.deliveries[0]] == ["call-1", "call-2"]
        assert rx.headers[0]["Authorization"] == "Bearer " + "t" * 40
        assert lg._queue == []
    finally:
        rx.close()

    bad = Receiver(fail_first=1, status=401)
    try:
        lg = LucaLogger(url=bad.url, token="t" * 40)
        lg.log_success_event({"standard_logging_object": PAYLOAD}, None, None, None)
        lg.flush()
        assert lg._queue == [], "a refused delivery (wrong token) is not retried for ever"
    finally:
        bad.close()


def test_a_real_litellm_call_through_the_callback():
    rx = Receiver()
    try:
        lg = LucaLogger(url=rx.url, token="t" * 40, flush_seconds=1)

        async def go():
            await litellm.acompletion(
                model="gpt-4o", messages=[{"role": "user", "content": "a secret question"}],
                mock_response="a secret answer", callbacks=[lg], user="acme-prod",
                metadata={"tags": ["batch"]},
            )
            for _ in range(50):
                await asyncio.sleep(0.1)
                if lg._queue or rx.deliveries:
                    break
            await lg.async_flush()

        asyncio.run(go())
        sent = [r for d in rx.deliveries for r in d]
        assert len(sent) == 1, sent
        r = sent[0]
        assert r["model"] == "gpt-4o" and r["status"] == "success" and r["prompt_tokens"] > 0
        assert "secret" not in json.dumps(r)
        out = os.environ.get("LUCA_LITELLM_SAMPLE")
        if out:  # the delivery, kept for luca-core's own test of reading it
            with open(out, "w") as f:
                json.dump(rx.deliveries[0], f, indent=1)
    finally:
        rx.close()


class CheckReceiver:
    """Luca's answer to a setup check: the check sentence for the right token, 401 for any other."""

    def __init__(self, token="tok_ok", answer=b'{"received":1,"stored":0,"check":"Luca received the check: this proxy\'s address and token work"}'):
        self.bodies = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                good = self.headers.get("Authorization") == f"Bearer {token}"
                self.send_response(200 if good else 401); self.send_header("content-type", "application/json"); self.end_headers()
                self.wfile.write(answer if good else b'{"error":"invalid token"}')

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/hooks/litellm/rc_1"


def test_check_proves_the_address_and_token_and_sends_no_call():
    from luca_litellm import check
    from luca_litellm.__main__ import main
    r = CheckReceiver()
    ok, said = check(r.url, "tok_ok")
    assert ok and "address and token work" in said
    assert r.bodies[0][0]["luca_check"] is True and set(r.bodies[0][0]) == {"luca_check", "id"}, "only the check, no call"
    ok, said = check(r.url, "tok_wrong")
    assert not ok and "LUCA_LITELLM_TOKEN" in said
    assert check(None, None)[0] is False
    assert main(["help"]) == 2
