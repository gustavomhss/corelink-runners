#!/usr/bin/env bash
set +e

required_miss=78
policy="${CL_CACHE_POLICY:-optional}"
verified_clw=""
case "$policy" in
  optional) ;;
  required-hit)
    verifier="${GITHUB_ACTION_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/verify-required-hit.py"
    if ! verified_clw="$(python3 "$verifier" \
      "${CL_REQUIRED_HIT_MANIFEST:-}" "${CL_REQUIRED_HIT_SIGNATURE:-}" 2>&1)"; then
      echo "::error title=corelink-memoize::required-hit candidate proof rejected: ${verified_clw:-verification failed}" >&2
      exit "$required_miss"
    fi
    ;;
  *)
    echo "::error title=corelink-memoize::invalid cache-policy '$policy' (expected optional or required-hit)" >&2
    exit "$required_miss"
    ;;
esac

# Build repeated --input / --env argument arrays (simple word-split; paths
# with spaces are out of scope for v0).
input_args=(); for p in $CL_INPUTS; do input_args+=(--input "$p"); done
env_args=();   for e in $CL_ENVNAMES; do env_args+=(--env "$e"); done

# Auto-fold toolchain versions into the key (safety-by-default): capture each
# tool's version and add it via a folded env var, so a toolchain upgrade busts
# the cache. A tool that's absent contributes empty (still safe).
if [ -n "${CL_TOOLS:-}" ]; then
  tv=""
  for t in $CL_TOOLS; do
    case "$t" in
      rust)   tv="${tv}|rust=$(rustc --version 2>/dev/null)";;
      node)   tv="${tv}|node=$(node --version 2>/dev/null)";;
      python) tv="${tv}|python=$(python3 --version 2>/dev/null)";;
      go)     tv="${tv}|go=$(go version 2>/dev/null)";;
      *)      echo "::warning title=corelink-memoize::unknown tool '$t' (ignored)";;
    esac
  done
  export __CL_TOOLVERS="$tv"
  env_args+=(--env __CL_TOOLVERS)
  echo "corelink-memoize: toolchain folded into key →$tv"
fi

run_cold() { bash -c "$CL_RUN"; }
state_dir=""
cleanup_run_state() {
  if [ -n "$state_dir" ]; then rm -rf -- "$state_dir"; fi
}
trap cleanup_run_state EXIT

# Moat present? (CLW_* injected by the CoreLink autoscaler + clw on PATH).
# Two credential shapes are accepted:
#   • CLW_TOKEN — the raw CAS PAT (legacy direct injection), OR
#   • CLW_CRED_TICKET — env-0 ticket redeemed by clw at run time.
if [ -n "${CLW_ENDPOINT:-}" ] && { [ -n "${CLW_TOKEN:-}" ] || [ -n "${CLW_CRED_TICKET:-}" ]; } && command -v clw >/dev/null 2>&1; then
  export CLW_REF_DOMAIN="${CLW_REF_DOMAIN:-runner}"
  echo "corelink-memoize: moat present — memoizing via clw run"
  echo "corelink-memoize[env-0-check]: CLW_REF_DOMAIN=[${CLW_REF_DOMAIN:-}] CLW_CRED_TICKET_len=[${#CLW_CRED_TICKET}] CLW_LEASE_ID=[${CLW_LEASE_ID:-}] CLW_FABRIC_ENDPOINT_set=[$([ -n "${CLW_FABRIC_ENDPOINT:-}" ] && echo yes || echo no)] CLW_TOKEN_set=[$([ -n "${CLW_TOKEN:-}" ] && echo yes || echo no)]"
  if [ "$policy" = required-hit ]; then
    "$verified_clw" run --require-hit "${input_args[@]}" "${env_args[@]}" -- bash -c "$CL_RUN"
    exit "$?"
  else
    state_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/corelink-memoize-state.XXXXXX")" || {
      echo "::error title=corelink-memoize::cannot create private execution-state receipt; refusing to dispatch without it" >&2
      exit 125
    }
    state_file="$state_dir/state"
    CLW_RUN_STATE_FILE="$state_file" clw run "${input_args[@]}" "${env_args[@]}" -- bash -c "$CL_RUN"
    rc=$?
    execution_state="UNKNOWN"
    if [ -f "$state_file" ]; then
      state_bytes="$(wc -c < "$state_file" | tr -d ' ')"
      state_line="$(cat "$state_file" 2>/dev/null)"
      if { [ "$state_bytes" = 12 ] && [ "$state_line" = "NOT_STARTED" ]; } || \
         { [ "$state_bytes" = 12 ] && [ "$state_line" = "DISPATCHING" ]; } || \
         { [ "$state_bytes" = 9 ] && [ "$state_line" = "EXECUTED" ]; }; then
        execution_state="$state_line"
      fi
    fi
    if [ "$rc" -ne 0 ] && [ "$execution_state" = "NOT_STARTED" ]; then
      echo "::warning title=corelink-memoize::clw proved the child was not started; falling back to a COLD run once"
      unset CLW_RUN_STATE_FILE
      run_cold; rc=$?
    elif [ "$rc" -ne 0 ] && [ "$execution_state" != "EXECUTED" ]; then
      echo "::warning title=corelink-memoize::execution state is unknown; not retrying the command"
    fi
    exit "$rc"
  fi
else
  if [ "$policy" = required-hit ]; then
    echo "::error title=corelink-memoize::required-hit requires the CLW moat and a verified required-hit/v1 candidate" >&2
    exit "$required_miss"
  fi
  echo "corelink-memoize: moat absent (no CLW_*/clw) — COLD run"
  run_cold; exit "$?"
fi
