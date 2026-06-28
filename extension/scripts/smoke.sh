#!/usr/bin/env bash
# End-to-end smoke test for the acm-gateway layer.
#
#   acm-gateway &                       # in another terminal
#   ./scripts/smoke.sh
#
# No-key checks (status, profiles, providers, memory) always run. Live-model
# checks (trimming, visual method, manual removal, sub-agent, compact) run only
# when ACM_UPSTREAM_API_KEY is set. Set ACM_MODEL to your model id.
set -uo pipefail

B="${ACM_GATEWAY_URL:-http://127.0.0.1:8807}"
MODEL="${ACM_MODEL:-meta-llama/llama-3.1-8b-instruct}"
PASS=0; FAIL=0
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
ok() { echo "  ✅ $1"; PASS=$((PASS+1)); }
no() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "== gateway @ $B =="
curl -sf "$B/status" >/dev/null || { echo "Gateway not reachable — start 'acm-gateway' first."; exit 1; }
ok "gateway reachable"

echo "== profiles & techniques =="
curl -sf -X POST "$B/profile" -H 'content-type: application/json' -d '{"name":"long_chat"}' >/dev/null \
  && [ "$(curl -sf "$B/status" | j "d['techniques']['tool_result_trimming']")" = "True" ] \
  && ok "apply preset long_chat (trimming on)" || no "apply preset"
# enable visual_method via body merge, preserve across preset change
PROF=$(curl -sf "$B/profile" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['active']))")
curl -sf -X POST "$B/profile" -H 'content-type: application/json' \
  -d "{\"body\":$PROF,\"visual_method\":{\"enabled\":true,\"trigger_tokens\":500,\"only_tools\":[],\"exclude_tools\":[]}}" >/dev/null
[ "$(curl -sf "$B/status" | j "d['techniques']['visual_method']")" = "True" ] && ok "enable visual_method" || no "enable visual_method"
curl -sf -X POST "$B/profile" -H 'content-type: application/json' -d '{"name":"minimal"}' >/dev/null
[ "$(curl -sf "$B/profile" | j "d['visual_method']['enabled']")" = "True" ] && ok "preset preserves visual_method" || no "visual_method wiped by preset"

echo "== providers =="
curl -sf -X POST "$B/providers" -H 'content-type: application/json' -d '{"slug":"smoke","type":"openrouter","api_key":"sk-or-demo","default":true}' >/dev/null
[ "$(curl -sf "$B/providers" | j "d['default']")" = "smoke" ] && ok "add provider + default" || no "add provider"
[ "$(curl -sf "$B/providers" | j "'sk-or' not in json.dumps(d)")" = "True" ] && ok "api key masked" || no "api key masked"
curl -sf -X DELETE "$B/providers/smoke" >/dev/null && ok "delete provider" || no "delete provider"

echo "== memory =="
curl -sf -X POST "$B/memory/remember" -H 'content-type: application/json' -d '{"text":"auth in login.ts","scope":"user"}' >/dev/null
[ "$(curl -sf "$B/memory/recall?query=auth" | j "len(d['items'])")" -ge 1 ] && ok "remember + recall" || no "remember/recall"
curl -sf -X POST "$B/memory/clear" -H 'content-type: application/json' -d '{"scope":"user"}' >/dev/null
[ "$(curl -sf "$B/memory/recall" | j "len(d['items'])")" = "0" ] && ok "memory clear" || no "memory clear"

if [ -z "${ACM_UPSTREAM_API_KEY:-}" ]; then
  echo "== live-model checks SKIPPED (set ACM_UPSTREAM_API_KEY + ACM_MODEL) =="
  echo "== $PASS passed, $FAIL failed =="
  [ "$FAIL" -eq 0 ]; exit $?
fi

echo "== live: a turn + trimming/visual + manual removal =="
curl -sf -X POST "$B/profile" -H 'content-type: application/json' \
  -d "{\"body\":$(curl -sf "$B/profile" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['active']))"),\"visual_method\":{\"enabled\":true,\"trigger_tokens\":300,\"only_tools\":[],\"exclude_tools\":[]}}" >/dev/null
BIG=$(python3 -c "print('search result with https://example.com/x  '*200)")
REQ=$(python3 -c "import json,sys;print(json.dumps({'model':'$MODEL','messages':[{'role':'user','content':'what did the tool find?'},{'role':'assistant','content':'','tool_calls':[{'id':'c1','type':'function','function':{'name':'grep','arguments':'{}'}}]},{'role':'tool','tool_call_id':'c1','name':'grep','content':sys.argv[1]},{'role':'user','content':'summarise'}]}))" "$BIG")
curl -sf -X POST "$B/v1/chat/completions" -H 'content-type: application/json' -d "$REQ" >/dev/null \
  && ok "chat turn forwarded" || no "chat turn (check key/model/upstream)"
[ "$(curl -sf "$B/status" | j "any(e['type']=='visual_method' for e in d['last_events'])")" = "True" ] \
  && ok "visual_method fired" || echo "  ⚠️  no visual_method event (model/threshold?)"
FP=$(curl -sf "$B/messages" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next((m['fp'] for m in d['messages'] if m['role']=='tool'),''))")
if [ -n "$FP" ]; then
  curl -sf -X POST "$B/messages/drop" -H 'content-type: application/json' -d "{\"fp\":\"$FP\"}" >/dev/null
  curl -sf -X POST "$B/v1/chat/completions" -H 'content-type: application/json' -d "$REQ" >/dev/null
  [ "$(curl -sf "$B/status" | j "any(e['type']=='manual_removal' for e in d['last_events'])")" = "True" ] \
    && ok "manual removal fired" || no "manual removal"
else no "could not find tool message fp"; fi

echo "== live: sub-agent + compact =="
curl -sf -X POST "$B/subagent" -H 'content-type: application/json' -d '{"task":"name one auth risk in a login flow"}' | j "'summary' in d" >/dev/null && ok "sub-agent" || no "sub-agent"
curl -sf -X POST "$B/compact" -H 'content-type: application/json' -d '{"text":"user asked X. agent did Y. result Z."}' | j "'summary' in d" >/dev/null && ok "compact" || no "compact"

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
