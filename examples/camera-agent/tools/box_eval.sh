#!/usr/bin/env bash
# Run the camera agent's live eval ON THE BOX, against the agent as deployed.
#
#   bash examples/camera-agent/tools/box_eval.sh <label> [repeat] [--cpu]
#
# Prompts for an OpenNVR operator login here (nothing typed in chat), gets
# an access + refresh token from core, copies the harness and the cases into
# the agent container (the only place with PyYAML and the agent's network),
# and runs them against the agent over loopback TLS. The harness refreshes
# the token when it expires, so a long run survives the 30-minute access
# token. Output: /tmp/eval-<label>.json (the full run) and /tmp/eval-<label>.log.
#
# --cpu: run against the CPU brain instead of whatever .env points the agent
# at (on a Qualcomm board that is the NPU adapter): the LLM variables are
# switched to Ollama on the host for the run and put back afterwards, the
# agent container recreated both times — the config-init container that
# renders the agent's config.yml from .env, then the agent, and ONLY
# those two (--no-deps): a bare `compose up camera-agent` also recreated
# core as its dependency, and Compose 2.24 then left core off the
# database network, so every login and every segment index 500'd until
# ./start.sh re-attached it (Ventuno Q, 2026-11-10 01:33); recreating
# the agent alone kept the OLD rendered config, and the "CPU" run was
# the NPU again (01:43). The script checks the model the agent reports
# before it runs. Nothing else on the box changes.
set -euo pipefail
label=${1:?label, e.g. npu or cpu}; repeat=${2:-3}; mode=${3:-}
cd "$(dirname "$0")/../../.."       # the checkout
cam=${EVAL_CAMERA:-cam23}

recreate_agent() {
    # The init container renders /app/config.yml from .env; it must run
    # again or the agent comes back with the previous settings.
    docker compose up --no-deps --force-recreate camera-agent-config-init >/dev/null 2>&1 || true
    docker compose up -d --no-deps --force-recreate camera-agent >/dev/null
}
agent_model() {
    docker exec opennvr_camera_agent python -c 'import urllib.request,ssl,json;ssl._create_default_https_context=ssl._create_unverified_context;print(json.load(urllib.request.urlopen("https://127.0.0.1:9100/health",timeout=3)).get("llm_model") or "")' 2>/dev/null
}
restore() {
    if [[ -f /tmp/env.before-cpu-eval ]]; then
        echo "restoring the LLM settings and recreating the agent…"
        cp /tmp/env.before-cpu-eval .env && rm -f /tmp/env.before-cpu-eval
        recreate_agent
    fi
}
if [[ "$mode" == "--cpu" ]]; then
    cp .env /tmp/env.before-cpu-eval
    trap restore EXIT
    sed -i -E 's/^LLM_PROVIDER=.*/LLM_PROVIDER=ollama/; s/^LLM_BASE_URL=.*/LLM_BASE_URL=/; s/^LLM_MODEL=.*/LLM_MODEL=/; s/^LLM_PREWARM=.*/LLM_PREWARM=false/' .env
    cpu_model=$(grep -E '^OLLAMA_MODEL=' .env | cut -d= -f2)
    echo "agent on the CPU brain (Ollama on the host, ${cpu_model}) for this run…"
    recreate_agent
fi
# Wait for the agent to answer, and for core to answer a DATABASE-backed
# request (the login is one): a core that is up but off its database
# network answers /health and 500s the login, and a used MFA code cannot
# be retried.
echo "waiting for the agent and core…"
for _ in $(seq 1 60); do
    if docker exec opennvr_camera_agent python -c 'import urllib.request,ssl;ssl._create_default_https_context=ssl._create_unverified_context;urllib.request.urlopen("https://127.0.0.1:9100/health",timeout=3)' 2>/dev/null; then break; fi
    sleep 2
done
core_ok=""
for _ in $(seq 1 30); do
    if docker exec opennvr_core python3 -c 'import os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8000/api/v1/internal/camera-agent/status",headers={"X-Internal-Api-Key":os.environ["INTERNAL_API_KEY"]})
urllib.request.urlopen(r,timeout=5)' 2>/dev/null; then core_ok=1; break; fi
    sleep 2
done
if [[ -z "$core_ok" ]]; then
    echo "core is not answering database-backed requests (off its database network? run ./start.sh, which re-attaches it) — not asking for a login." >&2
    exit 1
fi
model=$(agent_model)
echo "agent reports LLM model: ${model:-?}"
if [[ "$mode" == "--cpu" && "$model" != "$cpu_model" ]]; then
    echo "the agent is not on the CPU brain (${model:-?} instead of ${cpu_model}) — not running." >&2
    exit 1
fi

read -rp "OpenNVR username: " U
read -rsp "OpenNVR password: " P; echo
read -rp "MFA code (6 digits, blank if MFA is off): " CODE
toks=$(docker exec -i opennvr_core python - "$U" "$P" "$CODE" <<'PY'
import sys, json, urllib.request
u, p, code = sys.argv[1], sys.argv[2], sys.argv[3]
body = {"username": u, "password": p}
if code.strip():
    body["code"] = code.strip()
req = urllib.request.Request("http://127.0.0.1:8000/api/v1/auth/login-json",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
body = json.loads(urllib.request.urlopen(req, timeout=15).read())
tok = body.get("access_token")
if not tok:
    sys.exit("no access_token in the login response: keys " + ",".join(body.keys()))
print(tok, body.get("refresh_token") or "-", body.get("device_token") or "-")
PY
)
unset P CODE
[ -n "$toks" ] || { echo "login failed" >&2; exit 1; }
read -r tok rtok devtok <<<"$toks"
[[ "$rtok" == "-" ]] && rtok=""
[[ "$devtok" == "-" ]] && devtok=""

docker exec opennvr_camera_agent mkdir -p /tmp/eval
docker cp examples/camera-agent/tools/eval_harness.py opennvr_camera_agent:/tmp/eval/eval_harness.py
docker cp examples/camera-agent/tools/eval_cases.yml opennvr_camera_agent:/tmp/eval/eval_cases.yml
# Two patches on the copy: the agent serves its own self-signed TLS on 9100
# (Python 3 has no PYTHONHTTPSVERIFY), and the device firewall wants the
# device token the login minted (X-Device-Token), which the harness does
# not know about.
docker exec opennvr_camera_agent sh -c '
  sed -i "s/^import urllib.request$/import urllib.request\nimport ssl as _ssl\n_ssl._create_default_https_context = _ssl._create_unverified_context/" /tmp/eval/eval_harness.py
  sed -i "s/^            req.add_header(\"Authorization\", f\"Bearer {state\[.token.\]}\")$/&\n        import os\n        if os.environ.get(\"DEVTOK\"):\n            req.add_header(\"X-Device-Token\", os.environ[\"DEVTOK\"])/" /tmp/eval/eval_harness.py
  grep -q _create_unverified_context /tmp/eval/eval_harness.py && grep -q X-Device-Token /tmp/eval/eval_harness.py'
echo "running $label x$repeat against https://127.0.0.1:9100 (camera $cam, model ${model:-?})…"
docker exec -e TOK="$tok" -e RTOK="$rtok" -e DEVTOK="$devtok" opennvr_camera_agent sh -c \
  'python /tmp/eval/eval_harness.py --url https://127.0.0.1:9100 --cases /tmp/eval/eval_cases.yml \
     --camera '"$cam"' --repeat '"$repeat"' --throwaway --token "$TOK" ${RTOK:+--refresh-token "$RTOK"} \
     --auth-base http://opennvr-core:8000 --out /tmp/eval/run.json' \
  2>&1 | tee /tmp/eval-"$label".log | tail -60
docker cp opennvr_camera_agent:/tmp/eval/run.json /tmp/eval-"$label".json
echo "saved /tmp/eval-$label.json and /tmp/eval-$label.log"
