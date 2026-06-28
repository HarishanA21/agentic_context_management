"""Wire-path integration test for the gateway — no API key needed.

Spins up a mock upstream, drives the gateway via an in-process TestClient, and
checks the full proxy path end to end: visual-method pagination on the wire, the
context token meter, /messages/images, /relevance/summarize (drop + inject), and
manual drop. State is isolated to a temp dir so it never touches ~/.acm.

    cd extension
    uv run python scripts/wire_test.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

_work = tempfile.mkdtemp()
os.environ["ACM_DROPLIST_PATH"] = _work + "/dropped.json"
os.environ["ACM_SUMMARY_PATH"] = _work + "/summaries.json"
os.environ["ACM_PROVIDERS_PATH"] = _work + "/providers.json"  # no real default provider
_cfg = _work + "/acm.config.json"
shutil.copy(
    os.path.join(os.path.dirname(__file__), "..", "config", "acm.config.example.json"),
    _cfg,
)
os.environ["ACM_CONFIG"] = _cfg
os.environ["ACM_UPSTREAM_BASE_URL"] = "http://127.0.0.1:8899/v1"
os.environ["ACM_UPSTREAM_API_KEY"] = "dummy"

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_mock = FastAPI()
LAST: dict = {"body": None}


@_mock.post("/v1/chat/completions")
async def _mock_chat(request: Request):
    LAST["body"] = await request.json()
    return JSONResponse(
        {
            "id": "mock",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "OK done."},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
    )


def main() -> int:
    srv = uvicorn.Server(
        uvicorn.Config(_mock, host="127.0.0.1", port=8899, log_level="error")
    )
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)

    from starlette.testclient import TestClient

    from acm_gateway.app import app

    c = TestClient(app)
    passed = failed = 0

    def ok(m: str) -> None:
        nonlocal passed
        passed += 1
        print("  PASS", m)

    def no(m: str) -> None:
        nonlocal failed
        failed += 1
        print("  FAIL", m)

    s = c.get("/status").json()
    ok("/status ok + context block") if s.get("ok") and "context" in s else no("/status")
    prof = c.get("/profile").json()["active"]
    c.post("/profile", json={"body": prof, "visual_method": {
        "enabled": True, "trigger_tokens": 300, "only_tools": [], "exclude_tools": []}})
    ok("visual_method enabled") if c.get("/status").json()["techniques"]["visual_method"] else no("visual_method")

    big = "\n".join(f"Line {i}: detail detail detail {i}" for i in range(400))
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": big},
    ]
    data = c.post("/v1/chat/completions", json={"model": "x", "stream": False, "messages": msgs}).json()
    ok("chat round-trips through mock") if data.get("choices") else no(f"chat: {str(data)[:160]}")
    fwd = LAST["body"]["messages"] if LAST["body"] else []
    img = sum(
        sum(1 for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "image_url")
        for m in fwd if isinstance(m.get("content"), list)
    )
    ok(f"visual method on the wire: {img} image block(s)") if img >= 2 else no(f"visual on wire ({img})")
    tok = c.get("/status").json().get("context", {}).get("tokens", 0)
    ok(f"context tokens = {tok}") if tok > 0 else no("context tokens")

    mres = c.get("/messages").json()
    conv = mres["conversation"]
    tool_row = next((m for m in mres["messages"] if m["role"] == "tool"), None)
    user_row = next((m for m in mres["messages"] if m["role"] in ("human", "user")), None)
    if tool_row:
        imgs = c.get(f"/messages/images?conv={conv}&fp={tool_row['fp']}").json()
        ok(f"/messages/images -> {imgs.get('count')} page(s)") if imgs.get("count", 0) >= 1 else no(f"images {imgs}")
    else:
        no("no tool message recorded")
    sumr = c.post("/relevance/summarize", json={
        "conv": conv, "member_fps": [tool_row["fp"]] if tool_row else [],
        "title": "read step", "model": "x"}).json()
    ok("relevance/summarize (drop+inject)") if sumr.get("ok") and sumr.get("summary") else no(f"summarize {sumr}")
    if user_row:
        c.post("/messages/drop", json={"conv": conv, "fp": user_row["fp"]})
        c.post("/v1/chat/completions", json={"model": "x", "stream": False, "messages": msgs})
        fwd2 = LAST["body"]["messages"]
        gone = not any(isinstance(m.get("content"), str) and "read the file" in m.get("content", "") for m in fwd2)
        inj = any(isinstance(m.get("content"), str) and "Summary of earlier step" in m.get("content", "") for m in fwd2)
        ok("dropped message excluded on the wire") if gone else no("drop on wire")
        ok("summary note injected on the wire") if inj else no("summary on wire")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
