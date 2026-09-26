# `corelink-memoize` — memoized CI steps on CoreLink runners

Wrap any command in **CoreLink cache memoization**: on a cache HIT the memoized
result is returned **without re-running** (re-runs ≈ 0 — the cache-moat earning
its keep). Measured on the dogfood runner: a ~10.8 s `cargo build` re-run drops to
~3.7 s on a hit (**2.7×**, hit/miss confirmed by clw; see
`docs/handoff/2026-06-21-moat-benchmark-result-first-warm-signal.md`).

**Optional mode:** if the moat is absent (`CLW_*` not injected), the command runs
**COLD**. If `clw run` fails with a fresh receipt proving `NOT_STARTED`, the
command gets one cold fallback. A missing, malformed, `DISPATCHING`, or
`EXECUTED` receipt never authorizes a retry; a child's exit 125 is preserved.

**Required-hit mode:** this is a separate fail-closed policy. It requires a
centrally signed candidate manifest and detached SSH signature, verifies the
manifest's capability, provenance, supported target, and SHA-256 of the exact
resolved `clw` executable before dispatch, then calls `clw run --require-hit`.
Missing or invalid proof, an unsupported capability/target, a version mismatch,
or an unavailable moat returns 78 without launching the wrapped command. The
package version alone is never capability proof.

## Usage

Runs on a **CoreLink ephemeral runner** (`runs-on: corelink-dogfood` / your
CoreLink label); the autoscaler injects `CLW_*` so memoization just works.

```yaml
jobs:
  test:
    runs-on: corelink-dogfood
    steps:
      - uses: actions/checkout@v4
      - uses: HuGR-dev/corelink-runners/actions/corelink-memoize@main
        with:
          run: cargo test --all
          # paths that determine the result (the cache key). Exclude build dirs
          # via .gitignore/.clwignore so re-runs hit. Default: "."
          inputs: |
            Cargo.toml
            Cargo.lock
            src
          # SAFETY-BY-DEFAULT: auto-fold the toolchain version into the key so a
          # compiler upgrade correctly busts the cache (no stale build).
          tool: rust
```

A HIT returns the prior verdict+output instantly; a MISS runs the command and
memoizes it for next time.

For optional fallback, the action gives `clw run` a private
`CLW_RUN_STATE_FILE`. `clw` atomically records `NOT_STARTED`, `DISPATCHING`, or
`EXECUTED` there and removes the variable before launching the child. `EXECUTED`
means the direct child's exit status was observed and bounded output capture
completed; failures after dispatch leave `DISPATCHING`. If the action cannot
create its private receipt directory, it fails closed without dispatching the
command. The action never parses stderr, cache HIT text, or an exit number to
infer whether execution happened. Older CLIs that do not implement this receipt
cannot authorize a cold retry after a failed memoized invocation.

## Inputs

| input    | required | default | meaning |
|----------|----------|---------|---------|
| `run`    | yes      | —       | command to execute (via `bash -c`) |
| `inputs` | no       | `.`     | space/newline-separated paths forming the cache key (repeated `--input`) |
| `env`    | no       | `""`    | space-separated env var NAMES folded into the key (repeated `--env`) |
| `tool`   | no       | `""`    | toolchains whose VERSION is auto-folded into the key (`rust`·`node`·`python`·`go`) so an upgrade busts the cache |
| `cache-policy` | no | `optional` | `optional` preserves the #567 fallback protocol; `required-hit` requires signed candidate proof before dispatch |
| `required-hit-manifest` | no | `""` | Local path to the signed candidate JSON; required for `required-hit`, ignored for `optional` |
| `required-hit-signature` | no | `""` | Local path to the detached SSH signature over the exact manifest bytes; required for `required-hit`, ignored for `optional` |

The required-hit manifest schema is version 1 and binds the canonical
`HuGR-dev/corelink-workspaces` repository identity, source SHA, package version,
`required-hit/v1` capability, binary name and target, binary SHA-256, and
candidate workflow run ID/attempt. The detached signature is checked with the
pinned `corelink-release-candidate` principal in the
`corelink-clw-candidate` namespace. This candidate trust is not a release or
production activation mechanism.

The verifier uses the Python 3 standard library and OpenSSH `ssh-keygen` for
signature validation. The current pinned CoreLink runner image installs
`python3` but does not explicitly install `openssh-client`; required-hit will
fail closed there until that runtime dependency is separately approved and
provided. Do not treat this candidate Action change as production activation.

## Notes

- The cache key = `hash(inputs ‖ named env ‖ command)`. Keep `inputs` to the
  SOURCE; exclude `target/`, `node_modules/`, etc. via `.clwignore`/`.gitignore`
  or a build artifact in the inputs will bust the key on the next run.
- WARM is not literally zero: a HIT restores the memoized output (e.g. build
  artifacts) from the CAS — you skip the compute, pay the CAS restore. The bigger
  the cold cost, the larger the absolute saving.
- Off a CoreLink runner (no `CLW_*`), this is a transparent passthrough — safe to
  leave in a workflow that also runs on stock runners.
