#!/usr/bin/env python3
"""End-to-end test for relevance pruning — gateway routes + trainers + judge loop.

Exercises the *real* HTTP routes (via Starlette's TestClient) plus both trainer
scripts, with everything isolated in a throwaway temp dir so it never touches
your real ~/.acm. No API key and no external services: it runs the encoder in
its dependency-free lexical backend.

Run:

    cd extension
    uv run python scripts/e2e_relevance.py

Exits 0 when every step passes, non-zero otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_EXT = Path(__file__).resolve().parents[1]
_BACKEND = _EXT.parent / "backend"


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="acm_e2e_"))
    os.environ.update(
        ACM_CONFIG=str(d / "acm.config.json"),
        ACM_DROPLIST_PATH=str(d / "dropped.json"),
        ACM_RELEVANCE_AUDIT_PATH=str(d / "audits.jsonl"),
        ACM_RELEVANCE_FEEDBACK_PATH=str(d / "feedback.jsonl"),
        ACM_JUDGE_FEWSHOT_PATH=str(d / "fewshot.json"),
        ACM_PROVIDERS_PATH=str(d / "providers.json"),
    )

    # Import only after env is set (engine reads default paths at import time).
    from acm_engine import parse_profile

    prof = parse_profile(
        {
            "tool_surface": "tool_calling",
            "context_management": {
                "relevance_pruning": {"enabled": True, "mode": "encoder", "keep_recent": 0}
            },
        }
    )
    Path(os.environ["ACM_CONFIG"]).write_text(json.dumps(prof.model_dump()))

    from fastapi.testclient import TestClient
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    import relevance as R
    from acm_gateway import app as A

    client = TestClient(A.app)

    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(("  PASS " if cond else "  FAIL ") + name)

    conv = "c_demo"
    A._DROP.record_seen(
        conv,
        [
            SystemMessage(content="You are a coding agent."),
            HumanMessage(content="Build a CSV export button in export.py"),
            AIMessage(content="Added export button in export.py using to_csv"),
            HumanMessage(content="Fix an unrelated typo in README.md"),
            AIMessage(content="Fixed the typo in README.md"),
            HumanMessage(content="Now build a dark mode toggle in ui.py theme switch"),
            AIMessage(content="Started dark mode toggle in ui.py"),
        ],
    )

    print("\n[1] GET /relevance/suggest (encoder mode, no API key)")
    r = client.get(f"/relevance/suggest?conv={conv}")
    data = r.json()
    sugs = data.get("suggestions", [])
    labels = {s["episode_id"]: s["label"] for s in sugs}
    print("    status", r.status_code, "| labels:", labels)
    check("suggest returned 200", r.status_code == 200)
    check(
        "export + readme -> DROP, dark-mode -> KEEP",
        labels.get("ep0") == "DROP" and labels.get("ep1") == "DROP" and labels.get("ep2") == "KEEP",
    )

    print("\n[2] POST /messages/drop_many (accept ep0)")
    ep0 = next(s for s in sugs if s["episode_id"] == "ep0")
    r2 = client.post("/messages/drop_many", json={"conv": conv, "fps": ep0["member_fps"]})
    check("drop_many ok", r2.json().get("ok") is True)

    print("\n[3] verify the tombstone filters the forwarded view")
    _filtered, removed = A._DROP.apply(conv, A._DROP.seen_full(conv))
    print(f"    removed {removed} message(s) from the model's view")
    check("episode removed from forwarded messages", removed >= 1)

    print("\n[4] POST /relevance/feedback (accept ep0=DROP, reject ep1 -> override)")
    client.post("/relevance/feedback", json={"conv": conv, "episode_id": "ep0", "title": "CSV export", "shown_label": "DROP", "user_action": "accept_drop", "final_label": "DROP", "source": "encoder"})
    client.post("/relevance/feedback", json={"conv": conv, "episode_id": "ep1", "title": "README typo", "shown_label": "DROP", "user_action": "reject", "final_label": "KEEP", "source": "encoder"})
    auds = R.load_audits(Path(os.environ["ACM_RELEVANCE_AUDIT_PATH"]))
    fbs = R.load_feedback(Path(os.environ["ACM_RELEVANCE_FEEDBACK_PATH"]))
    print(f"    audit rows: {len(auds)} | feedback rows: {len(fbs)}")
    check("audit + feedback logged", len(auds) >= 2 and len(fbs) == 2)

    print("\n[5] trainer 2 (judge) on the real logs from this session")
    out = subprocess.run(
        [sys.executable, str(_BACKEND / "training" / "train_judge.py"),
         "--audits", os.environ["ACM_RELEVANCE_AUDIT_PATH"],
         "--feedback", os.environ["ACM_RELEVANCE_FEEDBACK_PATH"],
         "--fewshot-out", os.environ["ACM_JUDGE_FEWSHOT_PATH"],
         "--dpo-out", str(d / "dpo.jsonl")],
        capture_output=True, text=True,
    )
    print("   ", [l for l in out.stdout.splitlines() if "corrections" in l or "few-shot bank" in l or "DPO pairs" in l])
    fs = json.loads(Path(os.environ["ACM_JUDGE_FEWSHOT_PATH"]).read_text())
    check("few-shot exemplar built from the ep1 override", len(fs["examples"]) >= 1 and fs["examples"][0]["label"] == "KEEP")

    print("\n[6] confirm the judge would inject that exemplar")
    block = R._load_fewshot()
    check("judge loads the few-shot bank", "user overrode" in block and "KEEP" in block)

    print("\n[7] trainer 1 (encoder) dry-run on the same logs")
    out2 = subprocess.run(
        [sys.executable, str(_BACKEND / "training" / "train_encoder.py"), "--dry-run",
         "--audits", os.environ["ACM_RELEVANCE_AUDIT_PATH"],
         "--feedback", os.environ["ACM_RELEVANCE_FEEDBACK_PATH"]],
        capture_output=True, text=True,
    )
    check("encoder dataset built", "label balance" in out2.stdout)

    print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
    print(f"(temp state in {d})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
