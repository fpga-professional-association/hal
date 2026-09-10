#!/usr/bin/env bash
#
# Run the examples/agilex3_walkthroughs check.py suites in CI.
#
# Every walkthrough ships a check.py that re-asserts the claims its guide.html
# makes.  They split into two tiers by what they need to run:
#
#   nohal  no HAL build, no Quartus, no Graphviz -- a plain Python 3
#          interpreter plus tools/hal_agilex.  Cheap enough to run on every
#          push, before the C++ build, so a broken walkthrough fails in
#          seconds instead of an hour.  This tier is run with HAL_BASE_PATH,
#          HAL_PY_PATH and PYTHONPATH scrubbed from the environment, so it is
#          structurally incapable of picking up a hal_py and quietly turning
#          into the expensive tier.
#
#   hal    needs a built HAL on HAL_BASE_PATH / HAL_PY_PATH / PYTHONPATH.
#          Run after ctest, so a red unit test still fails first.
#
# Usage:  tests/headless_smoke/run_walkthrough_checks.sh {nohal|hal}
#
# Each walkthrough is announced with "== <name>" and the first failing one
# aborts the run, so the log names the design that broke.
#
# Nothing here may write into the working tree: 03 and 08 default their probe
# and analysis output to <walkthrough>/artifacts, so both are redirected to a
# scratch directory, and `git status` is compared before and after the run (a
# warning, not a failure -- the checks are the test, this is a canary).  The
# comparison is against the state on entry, not against "clean", so a dirty
# checkout does not produce a false alarm.

set -euo pipefail

TIER="${1:-}"
if [[ "${TIER}" != "nohal" && "${TIER}" != "hal" ]]; then
    echo "usage: $0 {nohal|hal}" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
WALKTHROUGHS="${REPO}/examples/agilex3_walkthroughs"

PYTHON="${PYTHON:-python3}"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/walkthrough_checks.XXXXXX")"
cleanup() { rm -rf "${SCRATCH}"; }
trap cleanup EXIT

echo "tier:         ${TIER}"
echo "repository:   ${REPO}"
echo "scratch:      ${SCRATCH}"
echo

# Snapshot of everything the checks touch, as git sees it.  --no-optional-locks
# keeps this from rewriting .git/index; a checkout where git is unusable (a
# container bind mount, a source tarball) yields the empty string both times
# and the comparison silently degrades to a no-op.
tree_state() {
    if command -v git >/dev/null 2>&1 &&
       git --no-optional-locks -C "${REPO}" rev-parse --git-dir >/dev/null 2>&1; then
        git --no-optional-locks -C "${REPO}" status --porcelain -- \
            examples/agilex3_walkthroughs tools 2>/dev/null || true
    fi
}

TREE_BEFORE="$(tree_state)"

# run <walkthrough-dir-name> [extra check.py arguments...]
run() {
    local name="$1"; shift
    echo "== ${name}"
    if [[ ! -d "${WALKTHROUGHS}/${name}" ]]; then
        echo "   missing walkthrough directory ${WALKTHROUGHS}/${name}" >&2
        exit 1
    fi
    if [[ "${TIER}" == "nohal" ]]; then
        # -u, not just an empty value: hal_agilex and the check scripts probe
        # for these, and "set but empty" is not the same as "absent".
        env -u HAL_BASE_PATH -u HAL_PY_PATH -u PYTHONPATH \
            "${PYTHON}" "${WALKTHROUGHS}/${name}/check.py" "$@"
    else
        "${PYTHON}" "${WALKTHROUGHS}/${name}/check.py" "$@"
    fi
    echo "== ${name}: ok"
    echo
}

if [[ "${TIER}" == "nohal" ]]; then
    # 02 is pure standard library; 04 needs only tools/hal_agilex.
    run 02_traffic_fsm
    run 04_pwm_generator
    # 03/06/08 ship a second tier that needs hal_py.  Invoked without
    # --with-hal / --require-hal they run tier 1 only; 06 additionally
    # announces the skip rather than silently passing.
    run 03_uart_tx -o "${SCRATCH}/03_uart_tx"
    run 06_accumulator_alu
    run 08_shift_debouncer -o "${SCRATCH}/08_shift_debouncer"
else
    for var in HAL_BASE_PATH HAL_PY_PATH PYTHONPATH; do
        if [[ -z "${!var:-}" ]]; then
            echo "${var} is not set; the 'hal' tier needs a built HAL" >&2
            exit 2
        fi
        echo "${var}=${!var}"
    done
    echo

    # These three have no HAL-free tier at all: they load the netlist through
    # hal_py and the AGILEX_TENNM gate library.
    run 01_blinky_counter
    run 05_lfsr_prng
    run 10_crc8_checker
    # ... and these re-run tier 1 plus the structural analysis.  Pointing -o at
    # the scratch directory also means analyze.py is really re-run instead of
    # reading the committed artifacts/analysis.json.
    run 03_uart_tx --with-hal -o "${SCRATCH}/03_uart_tx"
    run 06_accumulator_alu --require-hal
    run 08_shift_debouncer --with-hal -o "${SCRATCH}/08_shift_debouncer"
fi

# Canary: the checks are supposed to read the committed artifacts, not
# regenerate them.
TREE_AFTER="$(tree_state)"
if [[ -z "${TREE_BEFORE}" && -z "${TREE_AFTER}" ]]; then
    echo "working tree unchanged by the checks"
elif [[ "${TREE_BEFORE}" == "${TREE_AFTER}" ]]; then
    echo "working tree unchanged by the checks (it was already dirty on entry)"
else
    echo "WARNING: the walkthrough checks changed the working tree." >&2
    echo "WARNING: before:" >&2
    printf '%s\n' "${TREE_BEFORE}" >&2
    echo "WARNING: after:" >&2
    printf '%s\n' "${TREE_AFTER}" >&2
fi

echo
echo "all ${TIER} walkthrough checks passed"
