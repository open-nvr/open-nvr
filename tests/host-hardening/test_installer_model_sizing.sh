#!/usr/bin/env bash
# Regression tests for the installer's hardware-aware model sizing.
#
# The field bug: the installer suggested `qwen3:1.7b` + `gemma3:4b` on every
# machine an operator tried, including low-end Windows and Linux boxes, and
# the camera agent then failed to run. The vision model was gated on
# `RAM >= 8` ALONE, so an 8 GB machine was handed 5 GB of models on top of a
# ~3 GB stack — the whole machine — and thrashed.
#
# The rule these tests defend: both Ollama models are RESIDENT AT ONCE
# (OLLAMA_KEEP_ALIVE=-1 in the shipped compose) and share the box with the
# OpenNVR stack, so they must be budgeted TOGETHER, never independently.
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running installer model-sizing tests"
echo ""

# Pull just the sizing block out of install.sh and run it in this shell, so
# the tests exercise the REAL logic rather than a copy that can drift.
SIZING=$(awk '/^OPENNVR_STACK_GB=/,/^# ── Catalog-driven model menu/' scripts/install.sh | sed '$d')
if [[ -z "$SIZING" ]]; then
    echo "✗ could not extract the sizing block from scripts/install.sh" >&2
    exit 1
fi
eval "$SIZING"

# size <ram_gb> <cores> <accel> → "LLM|VLM|adapter|whisper"
size() {
    HW_RAM_GB="$1"; HW_CORES="$2"; HW_ACCEL="$3"
    suggest_models
    printf '%s|%s|%s|%s' "$SUGGEST_LLM" "$SUGGEST_VLM" \
        "$SUGGEST_CAPTION_ADAPTER" "$(suggest_whisper_model)"
}

# ── 1. the reported failure ──
start_test "8 GB / 4 cores does not get a 4 GB vision model"
got=$(size 8 4 cpu)
vlm=$(cut -d'|' -f2 <<<"$got")
if [[ -z "$vlm" ]]; then
    pass
else
    fail "8 GB box was handed Ollama vision model '${vlm}' (got: ${got})"
fi

start_test "8 GB / 4 cores falls back to the small in-container adapter"
adapter=$(cut -d'|' -f3 <<<"$(size 8 4 cpu)")
[[ "$adapter" == "moondream" ]] && pass \
    || fail "expected the moondream adapter, got '${adapter}'"

# ── 2. the models are budgeted together, not independently ──
start_test "LLM + vision never exceed the machine's model budget"
catalog="examples/camera-agent/model_catalog.txt"
ram_of() {  # model name → its catalog min_ram_gb (0 if absent/empty)
    [[ -n "$1" ]] || { echo 0; return; }
    awk -F'|' -v m="$1" '$2 == m { print $3; found=1 } END { if (!found) print 0 }' \
        <(grep -v '^#' "$catalog")
}
# Smallest tested LLM in the catalog — the floor the installer may fall back
# to on a machine too small for anything, because suggesting NOTHING is not a
# usable install. That fallback is the one permitted overshoot.
FLOOR_RAM=$(awk -F'|' '$1 == "llm" && $4 == "yes" { if (min == "" || $3 < min) min = $3 }
                       END { print min + 0 }' <(grep -v '^#' "$catalog"))
over=""
for spec in "4 2 cpu" "8 4 cpu" "8 8 cpu" "16 8 cpu" "16 4 cpu" \
            "32 16 cpu" "8 8 cuda" "16 8 cuda" "32 12 metal"; do
    read -r ram cores accel <<<"$spec"
    got=$(size "$ram" "$cores" "$accel")
    HW_RAM_GB="$ram"; compute_model_budget
    total=$(( $(ram_of "$(cut -d'|' -f1 <<<"$got")") \
            + $(ram_of "$(cut -d'|' -f2 <<<"$got")") ))
    if (( HW_MODEL_BUDGET_GB < FLOOR_RAM )); then
        # Too small for even the floor: the only acceptable answer is that
        # floor alone, with vision pushed to the small container adapter.
        (( total > FLOOR_RAM )) && over="${over} [${spec}: ${total}GB where only the ${FLOOR_RAM}GB floor is allowed]"
    elif (( total > HW_MODEL_BUDGET_GB )); then
        over="${over} [${spec}: ${total}GB of models vs ${HW_MODEL_BUDGET_GB}GB budget]"
    fi
done
[[ -z "$over" ]] && pass || fail "over budget:${over}"

# ── 3. capability actually changes the answer ──
start_test "a weak machine and a strong one get different models"
weak=$(size 4 2 cpu)
strong=$(size 32 16 cpu)
[[ "$weak" != "$strong" ]] && pass \
    || fail "same suggestion for a 4 GB mini PC and a 32 GB server: ${weak}"

start_test "cores matter on CPU-only, not just RAM"
a=$(cut -d'|' -f1 <<<"$(size 16 4 cpu)")
b=$(cut -d'|' -f1 <<<"$(size 16 8 cpu)")
[[ "$a" != "$b" ]] && pass \
    || fail "4-core and 8-core 16 GB boxes both got '${a}'"

start_test "a GPU lifts the tier above the CPU-only answer"
cpu=$(cut -d'|' -f1 <<<"$(size 16 8 cpu)")
gpu=$(cut -d'|' -f1 <<<"$(size 16 8 cuda)")
[[ "$cpu" != "$gpu" ]] && pass \
    || fail "GPU and CPU-only 16 GB boxes both got '${cpu}'"

# ── 4. failing to detect must size DOWN, never up ──
start_test "undetectable hardware is treated as a small machine"
got=$(size 0 0 cpu)
llm=$(cut -d'|' -f1 <<<"$got"); vlm=$(cut -d'|' -f2 <<<"$got")
if [[ "$llm" == "qwen2.5:0.5b" && -z "$vlm" ]]; then
    pass
else
    fail "detection failure should size down, got: ${got}"
fi

# A machine smaller than the stack itself drives the budget NEGATIVE. It must
# clamp to zero and pick the floor — not wrap, and not be read as "unlimited".
# Plenty of threads here on purpose, so the core tier cannot mask the bug.
start_test "a machine smaller than the stack clamps to the floor"
HW_RAM_GB=2; compute_model_budget
if (( HW_MODEL_BUDGET_GB != 0 )); then
    fail "2 GB machine reported a ${HW_MODEL_BUDGET_GB} GB model budget"
else
    got=$(size 2 8 cpu)
    llm=$(cut -d'|' -f1 <<<"$got"); vlm=$(cut -d'|' -f2 <<<"$got")
    if [[ "$llm" == "qwen2.5:0.5b" && -z "$vlm" ]]; then
        pass
    else
        fail "2 GB / 8 threads should get the floor and no Ollama vision, got: ${got}"
    fi
fi

# ── 5. never suggest past the tested envelope ──
start_test "suggestions stay inside the tested set"
bad=""
for spec in "4 2 cpu" "8 4 cpu" "16 8 cpu" "32 16 cpu" "32 12 metal" "64 32 cuda"; do
    read -r ram cores accel <<<"$spec"
    got=$(size "$ram" "$cores" "$accel")
    for m in "$(cut -d'|' -f1 <<<"$got")" "$(cut -d'|' -f2 <<<"$got")"; do
        [[ -n "$m" ]] || continue
        tested=$(awk -F'|' -v m="$m" '$2 == m { print $4 }' <(grep -v '^#' "$catalog"))
        [[ "$tested" == "yes" ]] || bad="${bad} [${spec} → ${m} is '${tested:-not in catalog}']"
    done
done
[[ -z "$bad" ]] && pass || fail "untested model suggested:${bad}"

# ── 6. the two installers must not drift apart ──
start_test "install.ps1 carries the same budget arithmetic"
if grep -q 'stackGb = 3; \$osHeadroomGb = 2' scripts/install.ps1 \
   && grep -q 'budgetGb = \[math\]::Max(0, \$ramGb - \$stackGb - \$osHeadroomGb)' scripts/install.ps1; then
    pass
else
    fail "install.ps1's budget no longer matches install.sh's"
fi

start_test "install.ps1 gates the Ollama vision path on the budget"
grep -q 'captionSuggest -eq .ollamavlm.' scripts/install.ps1 && pass \
    || fail "install.ps1 no longer lets the budget veto CAPTION_ADAPTER=ollamavlm"

echo ""
if (( TESTS_FAILED > 0 )); then
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} tests failed"
    exit 1
fi
echo "✓ all ${TESTS_RUN} tests passed"
