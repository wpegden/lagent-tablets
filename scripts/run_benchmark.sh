#!/bin/bash
# Benchmark models using oneshot (exact supervisor code path).
# Usage: bash scripts/run_benchmark.sh [model] [node]
#
# Runs one test at a time. No external timeouts — oneshot handles everything.

set -u
cd /home/leanagent/src/lagent-tablets

REPO=/home/leanagent/math/connectivity_gnp_tablets
MODELS="${1:-gemini-auto gemini-pro claude-opus-max claude-sonnet}"
NODES="${2:-expected_isolated_limit prob_no_isolated_limit}"
RESULTS=/tmp/model_tests/results
mkdir -p "$RESULTS"

cleanup() {
    pkill -9 -f agentapi 2>/dev/null
    sleep 3
}

restore_sorry() {
    local node=$1
    cleanup
    sudo -n -u lagentworker rm -f "$REPO/Tablet/${node}.lean" 2>/dev/null
    rm -f "$REPO/Tablet/${node}.lean" 2>/dev/null
    cp "/tmp/model_tests/${node}_sorry.lean" "$REPO/Tablet/${node}.lean"
    chmod 664 "$REPO/Tablet/${node}.lean"
    rm -f "$REPO/worker_handoff.json" "$REPO/reviewer_decision.json"
    python3 -c "
import json
s = json.load(open('$REPO/.agent-supervisor/state.json'))
s['active_node'] = '$node'
s['phase'] = 'proof_formalization'
s['last_review'] = None
json.dump(s, open('$REPO/.agent-supervisor/state.json', 'w'), indent=2)
"
}

restore_proven() {
    local node=$1
    cleanup
    sudo -n -u lagentworker rm -f "$REPO/Tablet/${node}.lean" 2>/dev/null
    rm -f "$REPO/Tablet/${node}.lean" 2>/dev/null
    cp "/tmp/model_tests/proven/${node}.lean" "$REPO/Tablet/${node}.lean"
    chmod 664 "$REPO/Tablet/${node}.lean"
}

run_test() {
    local model=$1
    local node=$2
    local config="configs/test_${model}.json"

    echo ""
    echo "============================================================"
    echo "  $model on $node"
    echo "============================================================"

    restore_sorry "$node"

    local start=$(date +%s)

    # Run oneshot — identical code path to the real supervisor
    python3 -u -m lagent_tablets.oneshot \
        --config "$config" \
        --action worker \
        --node "$node" \
        --timeout 1800 \
        > "$RESULTS/${model}_${node}.log" 2>&1
    local exit_code=$?

    local end=$(date +%s)
    local duration=$((end - start))

    # Check results AFTER oneshot exits
    local lines=$(wc -l < "$REPO/Tablet/${node}.lean" 2>/dev/null || echo 0)
    local sorry=$(grep -c "sorry" "$REPO/Tablet/${node}.lean" 2>/dev/null || echo 999)
    local handoff="no"
    test -f "$REPO/worker_handoff.json" && handoff="yes"

    local status="FAIL"
    [ "$sorry" = "0" ] && [ "$lines" -gt 50 ] && status="PROVED"

    echo "  $status | ${duration}s | ${lines} lines | sorry=$sorry | handoff=$handoff | exit=$exit_code"
    echo "$model,$node,$status,$duration,$lines,$sorry,$handoff,$exit_code" >> "$RESULTS/benchmark.csv"

    restore_proven "$node"
}

echo "model,node,status,duration_s,lines,sorry,handoff,exit_code" > "$RESULTS/benchmark.csv"

for node in $NODES; do
    for model in $MODELS; do
        run_test "$model" "$node"
    done
done

echo ""
echo "============================================================"
echo "  RESULTS"
echo "============================================================"
column -t -s, "$RESULTS/benchmark.csv"
