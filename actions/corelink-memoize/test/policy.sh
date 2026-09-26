#!/usr/bin/env bash
set -euo pipefail

# Optional-policy fixtures preserve #567's receipt/fallback behavior. Required-
# hit's real Action+CLI acceptance is the separate signed Workspaces matrix.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/corelink-memoize-test.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
bin="$tmp/bin"; empty_bin="$tmp/empty-bin"; mkdir -p "$bin" "$empty_bin"; system_path="${PATH}"

cat >"$bin/clw" <<'FAKE_CLW'
#!/bin/bash
set -e
if [ "${1:-}" = "--version" ]; then printf '%s\n' "${FAKE_CLW_VERSION:-clw 0.1.12}"; exit "${FAKE_CLW_VERSION_RC:-0}"; fi
printf '%q ' "$@" > "$FAKE_CLW_ARGS"; printf '\n' >> "$FAKE_CLW_ARGS"
printf '%s\n' "${__CL_TOOLVERS:-}" > "$FAKE_CLW_TOOLVERS"
case "${FAKE_CLW_MODE:-config78}" in
  cached0) printf 'cached stdout\n'; printf 'cached stderr\n' >&2; exit 0 ;;
  cached125) printf 'cached stderr\n' >&2; exit 125 ;;
  preexec125) printf 'NOT_STARTED\n' > "$CLW_RUN_STATE_FILE"; exit 125 ;;
  dispatching125) printf 'DISPATCHING\n' > "$CLW_RUN_STATE_FILE"; exit 125 ;;
  malformed125) printf 'NOT_STARTED\nextra\n' > "$CLW_RUN_STATE_FILE"; exit 125 ;;
  executed-child)
    printf 'EXECUTED\n' > "$CLW_RUN_STATE_FILE"
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
    [ "$#" -gt 0 ] && shift
    unset CLW_RUN_STATE_FILE
    "$@"
    exit "$?"
    ;;
  miss78|config78) exit 78 ;;
  unsupported) exit 2 ;;
  *) exit 125 ;;
esac
FAKE_CLW
chmod +x "$bin/clw"

cat >"$bin/mktemp" <<'FAKE_MKTEMP'
#!/bin/bash
if [ "${FAIL_MKTEMP:-}" = 1 ]; then exit 1; fi
exec /usr/bin/mktemp "$@"
FAKE_MKTEMP
chmod +x "$bin/mktemp"

run_case() {
  local name="$1" policy="$2" expected_rc="$3" moat="$4" version="$5" mode="$6"
  local marker="$tmp/$name.marker" out="$tmp/$name.out" err="$tmp/$name.err"
  rm -f "$marker"; set +e
  (
    export CL_RUN="printf x >> '$marker'"
    if [ "$mode" = executed-child ]; then export CL_RUN="printf x >> '$marker'; exit 125"; fi
    export CL_INPUTS="fixture second" CL_ENVNAMES="CACHE_KEY" CACHE_KEY="cache-value"
    export CL_TOOLS="node" CL_CACHE_POLICY="$policy"
    export GITHUB_ACTION_PATH="$root"
    unset CL_REQUIRED_HIT_MANIFEST CL_REQUIRED_HIT_SIGNATURE
    export FAKE_CLW_VERSION="$version" FAKE_CLW_MODE="$mode"
    if [ "$mode" = version-fails ]; then export FAKE_CLW_VERSION_RC=1; fi
    if [ "$mode" = receipt-dir-failure ]; then export FAIL_MKTEMP=1; fi
    export FAKE_CLW_ARGS="$tmp/$name.args" FAKE_CLW_TOOLVERS="$tmp/$name.toolvers"
    case "$moat" in
      present) export CLW_ENDPOINT=fake CLW_TOKEN=fake PATH="$bin:$system_path" ;;
      endpoint-missing) unset CLW_ENDPOINT CLW_TOKEN CLW_CRED_TICKET; export PATH="$bin:$system_path" ;;
      creds-missing) export CLW_ENDPOINT=fake; unset CLW_TOKEN CLW_CRED_TICKET; export PATH="$bin:$system_path" ;;
      binary-missing) export CLW_ENDPOINT=fake CLW_TOKEN=fake PATH="$empty_bin" ;;
      absent) unset CLW_ENDPOINT CLW_TOKEN CLW_CRED_TICKET; export PATH="$system_path" ;;
    esac
    /bin/bash "$root/memoize.sh"
  ) >"$out" 2>"$err"
  local rc=$?; set -e
  if [ "$rc" -ne "$expected_rc" ]; then echo "FAIL $name: expected rc=$expected_rc got rc=$rc" >&2; cat "$out" "$err" >&2 || true; exit 1; fi
  printf '%s: rc=%s\n' "$name" "$rc"
}

assert_marker() {
  local name="$1" expected="$2" marker="$tmp/$1.marker"
  if [ "$expected" = yes ]; then [ -s "$marker" ] || { echo "FAIL $name: wrapped command did not run" >&2; exit 1; }; else [ ! -e "$marker" ] || { echo "FAIL $name: wrapped command ran unexpectedly" >&2; exit 1; }; fi
}

assert_count() {
  local name="$1" expected="$2" marker="$tmp/$1.marker" actual=0
  if [ -f "$marker" ]; then actual="$(wc -c < "$marker" | tr -d ' ')"; fi
  if [ "$actual" -ne "$expected" ]; then echo "FAIL $name: expected executions=$expected got=$actual" >&2; exit 1; fi
}

run_case optional-cold optional 0 absent "clw 0.1.12" config78; assert_count optional-cold 1
run_case optional-receipt-dir-failure optional 125 present "clw 0.1.12" receipt-dir-failure; assert_count optional-receipt-dir-failure 0
test ! -e "$tmp/optional-receipt-dir-failure.args" || { echo "FAIL receipt-dir-failure: clw must not be dispatched without a receipt" >&2; exit 1; }
run_case optional-internal-fallback optional 0 present "clw 0.1.12" preexec125; assert_count optional-internal-fallback 1
run_case optional-child-125-once optional 125 present "clw 0.1.12" executed-child; assert_count optional-child-125-once 1
run_case optional-ambiguous-no-retry optional 125 present "clw 0.1.12" dispatching125; assert_count optional-ambiguous-no-retry 0
run_case optional-malformed-no-retry optional 125 present "clw 0.1.12" malformed125; assert_count optional-malformed-no-retry 0
run_case optional-old-cli-no-proof optional 125 present "clw 0.1.5" cached125; assert_count optional-old-cli-no-proof 0
run_case required-missing-proof required-hit 78 present "clw 0.1.12" cached0; assert_marker required-missing-proof no
run_case required-proof-before-child required-hit 78 absent "clw 0.1.12" cached0; assert_marker required-proof-before-child no
run_case invalid-policy invalid 78 absent "clw 0.1.12" config78; assert_marker invalid-policy no

grep -F -- 'run --input fixture --input second --env CACHE_KEY --env __CL_TOOLVERS -- bash -c' "$tmp/optional-internal-fallback.args" >/dev/null

# A test-only ephemeral signer exercises exact-byte signature verification and
# the semantic unsupported-capability branch without exposing a trust override
# in the production Action interface.
ssh-keygen -q -t ed25519 -N '' -f "$tmp/test_signer"
read -r key_type key_data _ < "$tmp/test_signer.pub"
printf 'corelink-release-candidate %s %s\n' "$key_type" "$key_data" > "$tmp/test_allowed_signers"
python3 - "$tmp/manifest.json" "$tmp/unsupported.json" <<'PY'
import json, sys
base = {
    "schema_version": 1,
    "repository": "HuGR-dev/corelink-workspaces",
    "repository_id": 1259579816,
    "source_sha": "0ebcc1803df30cb61e69173fbca2e8c04cca4a6d",
    "package_version": "0.1.12",
    "capability": "required-hit/v1",
    "binary": "clw",
    "target": "x86_64-unknown-linux-gnu",
    "binary_sha256": "0" * 64,
    "workflow_run_id": "36269530635",
    "workflow_run_attempt": "1",
}
with open(sys.argv[1], "wb") as stream:
    stream.write(json.dumps(base, sort_keys=True, separators=(",", ":")).encode() + b"\n")
base["capability"] = "required-hit/v999"
with open(sys.argv[2], "wb") as stream:
    stream.write(json.dumps(base, sort_keys=True, separators=(",", ":")).encode() + b"\n")
PY
ssh-keygen -q -Y sign -n corelink-clw-candidate -f "$tmp/test_signer" "$tmp/unsupported.json"
ssh-keygen -q -Y sign -n corelink-clw-candidate -f "$tmp/test_signer" "$tmp/manifest.json"
python3 - "$root/verify-required-hit.py" "$tmp" <<'PY'
import importlib.util, pathlib, sys
module_path, tmp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("required_hit_verifier", module_path)
verifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(verifier)
manifest = (tmp / "unsupported.json").read_bytes()
signature = (tmp / "unsupported.json.sig").read_bytes()
verifier.verify_signature(manifest, signature, tmp / "test_allowed_signers")
parsed = verifier.decode_manifest(manifest)
try:
    verifier.validate_manifest(parsed)
except verifier.Rejected as error:
    assert str(error) == "unsupported signed clw capability", str(error)
else:
    raise AssertionError("validly signed unsupported capability was accepted")
valid = (tmp / "manifest.json").read_bytes()
verifier.verify_signature(valid, (tmp / "manifest.json.sig").read_bytes(), tmp / "test_allowed_signers")
verifier.validate_manifest(verifier.decode_manifest(valid))
duplicate = manifest.replace(b'"schema_version":1,', b'"schema_version":1,"schema_version":1,', 1)
try:
    verifier.decode_manifest(duplicate)
except verifier.Rejected:
    pass
else:
    raise AssertionError("duplicate manifest key was accepted")
print("test-key signed unsupported capability and strict JSON checks: PASS")
PY

echo "T6-W2 policy fixtures: all green"
