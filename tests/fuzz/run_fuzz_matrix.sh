#!/usr/bin/env bash
#
# Seed-matrix driver for the nightly fuzz tier (issue #51).
#
# The harnesses in this directory are a *search*, and the fixed 25 seeds that
# .github/workflows/ubuntu24.04.yml runs on every push are one sample of the
# space they search. This widens that sample along three axes and reports every
# case it ran, so a red nightly says which seed found what:
#
#   fixed-seeds   ctest -L fuzz in $FUZZ_CTEST_BUILD_DIR -- the same 25 seeds the
#                 per-push job runs, through the same entry point, so a nightly
#                 that goes red on them means the regression is not seed-specific
#                 (skipped when FUZZ_CTEST_BUILD_DIR is unset)
#   deep-sweep    the first $FUZZ_MATRIX_ITERS seeds of each harness'
#                 deterministic seed_sequence(), i.e. the 25 fixed ones plus the
#                 derived ones after them, walked in chunks of
#                 $FUZZ_MATRIX_CHUNK seeds per process (FUZZ_ITERS_OFFSET +
#                 FUZZ_ITERS). The chunking is a memory bound, not a scheduling
#                 trick: neither harness releases the hal_py objects it builds,
#                 so a single 500-seed BooleanFunction process peaks around
#                 12 GB and is killed on a runner, while a 25-seed process peaks
#                 at 0.7 GB and is also measurably faster. Deterministic across
#                 nights: a finding here is reproducible from the seed printed
#                 with it, whatever chunk it was found in
#   extra-seeds   $FUZZ_MATRIX_SEEDS, one harness run per seed with
#                 FUZZ_SAMPLES=$FUZZ_MATRIX_SAMPLES -- deeper per seed rather
#                 than more seeds, which is the axis that grows the trees and the
#                 netlists instead of the seed count
#   random-seed   $NIGHTLY_SEED, the one seed that differs every night. Derived
#                 from the run id and date by the caller, printed here with a
#                 full repro command, and if it ever fails it should be added to
#                 DEFAULT_SEEDS as a permanent regression seed (issue #51)
#
# Every case runs even after an earlier one fails -- a nightly that stops at the
# first finding wastes the night -- and the script exits 1 if any of them did.
#
# Inputs (all optional except the paths hal_py needs):
#   FUZZ_CTEST_BUILD_DIR   build tree to run `ctest -L fuzz` in; unset = skip
#   FUZZ_LOG_DIR           where the per-case logs go (default: ./fuzz-logs)
#   FUZZ_MATRIX_ITERS      seeds for the deep sweep (default: 500)
#   FUZZ_MATRIX_CHUNK      seeds per deep-sweep process (default: 25)
#   FUZZ_MATRIX_SEEDS      extra fixed seeds, space separated
#   FUZZ_MATRIX_SAMPLES    FUZZ_SAMPLES for the extra seeds (default: 250)
#   FUZZ_RANDOM_SAMPLES    FUZZ_SAMPLES for the random seed (default: 500)
#   NIGHTLY_SEED           the random seed (default: derived from today's date)
#   GITHUB_STEP_SUMMARY    appended to with a Markdown report when set
#
# Usage (from the repository root):
#   HAL_BASE_PATH=build PYTHONPATH=build/lib NIGHTLY_SEED=123 \
#       bash tests/fuzz/run_fuzz_matrix.sh
#
set -u

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO_ROOT" || exit 1

BOOLEAN=tests/fuzz/fuzz_boolean_function.py
VERILOG=tests/fuzz/fuzz_verilog_roundtrip.py

# The harnesses read these three themselves; an inherited value would silently
# override a tier below (a FUZZ_SEED in the environment turns every sweep into a
# single-seed run), so the matrix owns them and nothing else does.
unset FUZZ_SEED FUZZ_ITERS FUZZ_ITERS_OFFSET FUZZ_SAMPLES

LOG_DIR=${FUZZ_LOG_DIR:-$REPO_ROOT/fuzz-logs}
MATRIX_ITERS=${FUZZ_MATRIX_ITERS:-500}
MATRIX_CHUNK=${FUZZ_MATRIX_CHUNK:-25}
MATRIX_SAMPLES=${FUZZ_MATRIX_SAMPLES:-250}
RANDOM_SAMPLES=${FUZZ_RANDOM_SAMPLES:-500}
# Twelve seeds outside DEFAULT_SEEDS and outside the range seed_sequence()
# derives from, so the extra-seed tier really is new ground.
MATRIX_SEEDS=${FUZZ_MATRIX_SEEDS:-"1000003 2000029 4000037 8000009 16000057 32000011 64000031 128000003 271828183 314159265 1618033988 2147483629"}
NIGHTLY_SEED=${NIGHTLY_SEED:-$(date -u +%Y%m%d)}

mkdir -p "$LOG_DIR" || exit 1

FAILED_CASES=""
RESULT_ROWS=""

# The lines worth reading out of a failing log: the repro commands, the seed and
# stage of each finding, and the whole "expected failure that no longer fails"
# verdict, which is prose rather than a repro line. The rest of the log -- the
# generated Verilog, the minimized repro, HAL's own [info] chatter -- is in the
# uploaded artifact.
EXCERPT_PATTERN='repro:|FUZZ_SEED=|NO LONGER|no longer|UNEXPECTED|^FAIL |^ERROR |^Errors|Failed |remove the named entry|assert the now-fixed|Re-running will not'

excerpt() {
    grep -E "$EXCERPT_PATTERN" "$1" | head -n 40
}

# run_case <name> <script> <VAR=VALUE>...
run_case() {
    name=$1
    script=$2
    shift 2
    log="$LOG_DIR/$name.log"
    printf '==> %s (%s)\n' "$name" "$*"
    start=$(date +%s)
    if env "$@" python3 "$script" > "$log" 2>&1; then
        status=ok
    else
        status=FAILED
        FAILED_CASES="$FAILED_CASES $name"
    fi
    elapsed=$(( $(date +%s) - start ))
    printf '    %s in %ds -> %s\n' "$status" "$elapsed" "$log"
    if [ "$status" = FAILED ]; then
        excerpt "$log" | sed 's/^/    | /'
    fi
    RESULT_ROWS="${RESULT_ROWS}${name}	${status}	${elapsed}	$*
"
}

# ---- fixed-seed sweep, through ctest ------------------------------------- #

if [ -n "${FUZZ_CTEST_BUILD_DIR:-}" ]; then
    log="$LOG_DIR/fixed-seeds-ctest.log"
    printf '==> fixed-seeds-ctest (ctest -L fuzz in %s)\n' "$FUZZ_CTEST_BUILD_DIR"
    start=$(date +%s)
    if (cd "$FUZZ_CTEST_BUILD_DIR" && ctest -L fuzz --output-on-failure) > "$log" 2>&1; then
        status=ok
    else
        status=FAILED
        FAILED_CASES="$FAILED_CASES fixed-seeds-ctest"
    fi
    elapsed=$(( $(date +%s) - start ))
    printf '    %s in %ds -> %s\n' "$status" "$elapsed" "$log"
    if [ "$status" = FAILED ]; then
        excerpt "$log" | sed 's/^/    | /'
    fi
    RESULT_ROWS="${RESULT_ROWS}fixed-seeds-ctest	${status}	${elapsed}	ctest -L fuzz
"
fi

# ---- deep deterministic sweep, one process per chunk of seeds ------------- #

offset=0
while [ "$offset" -lt "$MATRIX_ITERS" ]; do
    chunk=$MATRIX_CHUNK
    if [ $(( offset + chunk )) -gt "$MATRIX_ITERS" ]; then
        chunk=$(( MATRIX_ITERS - offset ))
    fi
    label=$(printf 'deep-sweep-%04d-%04d' "$offset" "$(( offset + chunk - 1 ))")
    run_case "${label}-boolean" "$BOOLEAN" \
        "FUZZ_ITERS_OFFSET=$offset" "FUZZ_ITERS=$chunk"
    run_case "${label}-verilog" "$VERILOG" \
        "FUZZ_ITERS_OFFSET=$offset" "FUZZ_ITERS=$chunk"
    offset=$(( offset + chunk ))
done

# ---- extra fixed seeds, deeper per seed ----------------------------------- #

for seed in $MATRIX_SEEDS; do
    run_case "seed-${seed}-boolean" "$BOOLEAN" \
        "FUZZ_SEED=$seed" "FUZZ_SAMPLES=$MATRIX_SAMPLES"
    run_case "seed-${seed}-verilog" "$VERILOG" "FUZZ_SEED=$seed"
done

# ---- the one seed that is new tonight ------------------------------------- #

printf '==> nightly random seed: %s\n' "$NIGHTLY_SEED"
run_case "random-seed-boolean" "$BOOLEAN" \
    "FUZZ_SEED=$NIGHTLY_SEED" "FUZZ_SAMPLES=$RANDOM_SAMPLES"
run_case "random-seed-verilog" "$VERILOG" "FUZZ_SEED=$NIGHTLY_SEED"

# ---- report --------------------------------------------------------------- #

total_cases=$(printf '%s' "$RESULT_ROWS" | grep -c .)
failed_count=0
for _name in $FAILED_CASES; do
    failed_count=$(( failed_count + 1 ))
done

summary=${GITHUB_STEP_SUMMARY:-/dev/null}
{
    printf '## Nightly fuzz tier\n\n'
    printf '%d cases, %d failed. Deep sweep: %d seeds per harness in chunks of %d.\n\n' \
        "$total_cases" "$failed_count" "$MATRIX_ITERS" "$MATRIX_CHUNK"
    printf "tonight's random seed: \`%s\` -- reproduce with\n\n" "$NIGHTLY_SEED"
    printf '```\n'
    printf 'FUZZ_SEED=%s FUZZ_SAMPLES=%s python3 %s\n' "$NIGHTLY_SEED" "$RANDOM_SAMPLES" "$BOOLEAN"
    printf 'FUZZ_SEED=%s python3 %s\n' "$NIGHTLY_SEED" "$VERILOG"
    printf '```\n\n'
    # Folded: the deep sweep alone is 40 rows, and on a green night nobody reads
    # them. The findings below are never folded.
    printf '<details><summary>All %d cases</summary>\n\n' "$total_cases"
    printf '| case | result | seconds | parameters |\n'
    printf '| --- | --- | --- | --- |\n'
    printf '%s' "$RESULT_ROWS" | while IFS=$'\t' read -r name status elapsed params; do
        [ -n "$name" ] || continue
        [ "$status" = ok ] && mark='ok' || mark='**FAILED**'
        printf '| `%s` | %s | %s | `%s` |\n' "$name" "$mark" "$elapsed" "$params"
    done
    printf '\n</details>\n\n'

    if [ -n "$FAILED_CASES" ]; then
        printf '### Findings\n\n'
        printf 'A red nightly is a bug report, not a flake: these harnesses never read the\n'
        printf 'clock, so every line below reproduces on any checkout of this commit. Triage per\n'
        printf 'issue #51 -- a failing random seed becomes a fixed regression seed in\n'
        printf '`DEFAULT_SEEDS`, a real defect becomes an issue plus a `KNOWN_BUGS` entry.\n\n'
        for name in $FAILED_CASES; do
            printf '#### `%s`\n\n' "$name"
            printf '```\n'
            excerpt "$LOG_DIR/$name.log"
            printf '```\n\n'
        done
        printf 'Full logs: the `nightly-fuzz-logs` artifact on this run.\n'
    else
        printf 'All cases green.\n'
    fi
} >> "$summary"

printf '\n'
if [ -n "$FAILED_CASES" ]; then
    printf 'nightly fuzz matrix: FAILED cases:%s\n' "$FAILED_CASES"
    printf 'logs in %s\n' "$LOG_DIR"
    exit 1
fi
printf 'nightly fuzz matrix: OK (logs in %s)\n' "$LOG_DIR"
exit 0
