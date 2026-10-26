"""Conversation-key test — one Claude Code session ⇒ one context window.

Regression guard for the "one HI created 10+ windows" bug: Claude Code sends
``metadata.user_id`` as a JSON blob whose ``session_id`` is constant across a
chat's main turn AND its auxiliary title/topic/quota calls (which each carry a
different system prefix + first message). ``session_namespace`` must read that
session id so every request of one session resolves to a single key — otherwise
each auxiliary call falls through to the prefix hash and mints its own window.

    cd extension
    uv run python scripts/session_key_test.py
"""

from __future__ import annotations

import sys

from langchain_core.messages import HumanMessage, SystemMessage

from acm_gateway.app import _is_probe
from acm_gateway.droplist import conversation_key, session_namespace

ok = True


def check(label: str, cond: bool) -> None:
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# The real user_id shape captured from live Claude Code traffic.
_REAL_UID = (
    '{"device_id":"ece3934dea8782b42198d0a8af13202295b9207d51b7b1b3fac3c82f6c71a2dc",'
    '"account_uuid":"6f6f309e-b6a2-4fb5-839e-b45769da8461",'
    '"session_id":"58fda58c-1c33-4bab-8c4f-7593529b6dea"}'
)
_EXPECTED_NS = "s58fda58c1c334bab8c4f7593"


def main() -> int:
    print("[1] session_namespace parses the JSON user_id")
    ns = session_namespace({"metadata": {"user_id": _REAL_UID}})
    check("real Claude Code user_id -> session-id namespace", ns == _EXPECTED_NS)

    print("\n[2] one session collapses to one key across differing prefixes")
    # Main turn and two auxiliary calls: different system prompt + first message,
    # same session. All three must share one key.
    k_main = conversation_key(
        [SystemMessage(content="You are Claude Code ... big prompt"),
         HumanMessage(content="HI")],
        namespace=ns,
    )
    k_title = conversation_key(
        [SystemMessage(content="Generate a short title for this chat"),
         HumanMessage(content="HI")],
        namespace=ns,
    )
    k_topic = conversation_key(
        [SystemMessage(content="Is this a new topic? Answer yes/no"),
         HumanMessage(content="unrelated text entirely")],
        namespace=ns,
    )
    check("main == title == topic == namespace",
          k_main == k_title == k_topic == _EXPECTED_NS)

    print("\n[3] a new session (new session_id) gets a new window")
    other_uid = _REAL_UID.replace("58fda58c-1c33-4bab-8c4f-7593529b6dea",
                                  "99999999-0000-0000-0000-000000000000")
    ns2 = session_namespace({"metadata": {"user_id": other_uid}})
    k_other = conversation_key(
        [SystemMessage(content="You are Claude Code ... big prompt"),
         HumanMessage(content="HI")],
        namespace=ns2,
    )
    check("distinct session_id -> distinct key", ns2 != ns and k_other != k_main)

    print("\n[4] an explicit client id always wins")
    check("x-acm-conversation header overrides namespace",
          conversation_key([HumanMessage(content="x")], explicit="my-id", namespace=ns)
          == "my-id")

    print("\n[5] fallbacks and absent metadata")
    check("bare session_<uuid> form still parses",
          session_namespace({"metadata": {"user_id": "session_abc123def456"}})
          == "sabc123def456")
    check("no metadata -> None", session_namespace({}) is None)
    check("non-json plain user_id (no session id) -> None",
          session_namespace({"metadata": {"user_id": "plainuser42"}}) is None)
    check("malformed json user_id -> None (no crash)",
          session_namespace({"metadata": {"user_id": '{"session_id": '}}) is None)

    print("\n[6] no namespace -> prefix hash, and it distinguishes openings")
    a = conversation_key([SystemMessage(content="sys"), HumanMessage(content="first A")])
    b = conversation_key([SystemMessage(content="sys"), HumanMessage(content="first B")])
    check("prefix-hash keys use c_ prefix", a.startswith("c_") and b.startswith("c_"))
    check("different first message -> different prefix key", a != b)

    print("\n[7] token-counting probes are detected (skip window creation)")
    # Claude Code's measurement calls: single message, max_tokens=1, own
    # throwaway session id. These must NOT mint a context window.
    check("max_tokens=1 -> probe", _is_probe({"max_tokens": 1}) is True)
    check("max_tokens=0 -> probe", _is_probe({"max_tokens": 0}) is True)
    check("real turn (max_tokens=1000) -> not a probe",
          _is_probe({"max_tokens": 1000}) is False)
    check("missing max_tokens -> not a probe", _is_probe({}) is False)

    print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
