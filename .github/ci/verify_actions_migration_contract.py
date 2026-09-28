#!/usr/bin/env python3
"""Fail closed on hosted CI routing and provider-workflow trigger boundaries."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOSTED = (
    "ci.yml",
    "conformance.yml",
    "dco.yml",
    "pg-suite.yml",
    "plan-integrity.yml",
    "secret-scan.yml",
    "spawn-worker-ci.yml",
)
MANUAL_PROVIDER = (
    "build-cf-container-images.yml",
    "build-fabricd-image.yml",
    "deploy-spawn-worker.yml",
)
ACTIONLINT_VERSION = "1.7.12"
ACTIONLINT_SHA256 = "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"


def workflow_path(name: str) -> Path:
    return ROOT / ".github" / "workflows" / name


def top_level_events(text: str) -> tuple[set[str], dict[str, str]]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if re.fullmatch(r'(?:"on"|on):', line))
    except StopIteration:
        return set(), {}
    event_lines: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        match = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$", line)
        if match:
            current = match.group(1)
            event_lines[current] = [match.group(2) or ""]
        elif current is not None:
            event_lines[current].append(line)
    blocks = {key: "\n".join(value) for key, value in event_lines.items()}
    return set(blocks), blocks


def job_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line == "jobs:")
    except StopIteration:
        return {}
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    starts = [
        (i, match.group(1))
        for i in range(start + 1, end)
        if (match := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", lines[i]))
    ]
    blocks: dict[str, str] = {}
    for index, (line_no, name) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else end
        blocks[name] = "\n".join(lines[line_no:stop])
    return blocks


def runner_labels(text: str) -> list[str]:
    return re.findall(r"(?m)^    runs-on:\s*(.*?)\s*(?:#.*)?$", text)


def has_exact_read_only_permissions(text: str) -> bool:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line == "permissions:")
    except StopIteration:
        return False
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    grants = [
        (match.group(1), match.group(2))
        for line in lines[start + 1:end]
        if (match := re.match(r"^  ([a-z_]+):\s*([a-z]+)\s*$", line))
    ]
    has_job_override = bool(re.search(r"(?m)^    permissions:", "\n".join(lines)))
    return grants == [("contents", "read")] and not has_job_override


def has_no_secret_path(text: str) -> bool:
    non_comment_lines = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    forbidden = (
        r"\$\{\{\s*secrets\.",
        r"(?mi)^\s*secrets:\s*inherit\s*$",
        r"(?m)^ {4,6}environment:\s*[^#\s]*",
        r"(?m)^    uses:.*\.github/workflows/[^\s@]+@",
    )
    return not any(re.search(pattern, non_comment_lines) for pattern in forbidden)


def has_pinned_actionlint_bootstrap(text: str) -> bool:
    required = (
        f"- name: Install pinned actionlint {ACTIONLINT_VERSION}",
        f"version='{ACTIONLINT_VERSION}'",
        f"sha256='{ACTIONLINT_SHA256}'",
        '[[ "$(uname -s)" == \'Linux\' && "$(uname -m)" == \'x86_64\' ]]',
        "https://github.com/rhysd/actionlint/releases/download/v${version}/actionlint_${version}_linux_amd64.tar.gz",
        "printf '%s  %s\\n' \"$sha256\" \"$archive\" | sha256sum --check --status",
        'tar -xzf "$archive" -C "$install_dir" actionlint',
        '"$install_dir/actionlint" -version | grep -Fx "$version" >/dev/null',
        'echo "$install_dir" >> "$GITHUB_PATH"',
    )
    positions = [text.find(fragment) for fragment in required]
    lint_position = text.find("- name: Lint workflow syntax")
    return all(position >= 0 for position in positions) and (
        positions[0] < lint_position and all(
            left < right for left, right in zip(positions, positions[1:])
        )
    ) and lint_position >= 0


def validate(documents: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for name in HOSTED:
        jobs = job_blocks(documents[name])
        if not jobs or any(
            [value.strip("'\"") for value in runner_labels(block)] != ["ubuntu-latest"]
            for block in jobs.values()
        ):
            errors.append(f"{name}: every job must use ubuntu-latest")
        if not has_exact_read_only_permissions(documents[name]):
            errors.append(f"{name}: permissions must be only workflow-level contents: read")
        if not has_no_secret_path(documents[name]):
            errors.append(f"{name}: hosted validation must not read repository/environment secrets")
        for block in jobs.values():
            if not re.search(r"(?m)^    timeout-minutes:\s*[1-9][0-9]*\s*$", block):
                errors.append(f"{name}: each job must declare a finite timeout")

    if not has_pinned_actionlint_bootstrap(documents["plan-integrity.yml"]):
        errors.append(
            "plan-integrity.yml: actionlint must be installed before use from the "
            "pinned v1.7.12 Linux x86_64 release and verified by SHA-256"
        )

    for name in MANUAL_PROVIDER:
        events, _ = top_level_events(documents[name])
        if events != {"workflow_dispatch"}:
            errors.append(f"{name}: only workflow_dispatch is allowed (found {sorted(events)})")
        jobs = job_blocks(documents[name])
        if name == "deploy-spawn-worker.yml":
            expected_runners = {
                "test-corelink": "corelink",
                "test-hosted": "ubuntu-latest",
                "deploy-corelink": "corelink",
                "deploy-hosted": "ubuntu-latest",
            }
            if set(jobs) != set(expected_runners):
                errors.append(
                    f"{name}: jobs must remain limited to the mutually exclusive "
                    "CoreLink and opt-in hosted bootstrap lanes"
                )
            for job, runner in expected_runners.items():
                if job in jobs and [
                    value.strip("'\"") for value in runner_labels(jobs[job])
                ] != [runner]:
                    errors.append(f"{name}: {job} must run on {runner}")

            for input_name in ("hosted_bootstrap", "force"):
                input_match = re.search(
                    rf"(?ms)^      {input_name}:\n(?P<body>.*?)(?=^      [a-z_]+:|^permissions:)",
                    documents[name],
                )
                if input_match is None:
                    errors.append(f"{name}: {input_name} input is missing")
                    continue
                input_body = input_match.group("body")
                for required in ("type: boolean", "default: false"):
                    if required not in input_body:
                        errors.append(
                            f"{name}: {input_name} must declare {required}"
                        )

            for job, predicate in (
                ("test-corelink", "if: ${{ !inputs.hosted_bootstrap }}"),
                ("test-hosted", "if: ${{ inputs.hosted_bootstrap }}"),
                (
                    "deploy-corelink",
                    "if: ${{ github.ref == 'refs/heads/main' && !inputs.hosted_bootstrap }}",
                ),
                (
                    "deploy-hosted",
                    "if: ${{ github.ref == 'refs/heads/main' && inputs.hosted_bootstrap }}",
                ),
            ):
                if job in jobs and predicate not in jobs[job]:
                    errors.append(f"{name}: {job} is missing its exact branch/mode guard")

            for deploy_job, test_job in (
                ("deploy-corelink", "test-corelink"),
                ("deploy-hosted", "test-hosted"),
            ):
                block = jobs.get(deploy_job, "")
                if f"needs: {test_job}" not in block:
                    errors.append(f"{name}: {deploy_job} must depend on {test_job}")
                if "environment: corelink-runners" not in block:
                    errors.append(f"{name}: {deploy_job} must use corelink-runners environment")
                for required in (
                    "bash scripts/ci/wait-for-idle-fleet.sh",
                    "node scripts/ci/check-repo-tenant-pat-map.mjs deploy/cloudflare/wrangler.jsonc",
                    "npx wrangler deploy",
                ):
                    if required not in block:
                        errors.append(f"{name}: {deploy_job} is missing {required}")
            hosted_deploy = jobs.get("deploy-hosted", "")
            for required in (
                "if: ${{ inputs.force }}",
                "FORCE: 'false'",
                "hosted_bootstrap requires force=false",
            ):
                if required not in hosted_deploy:
                    errors.append(f"{name}: hosted deploy is missing force safety guard {required}")

            if not has_exact_read_only_permissions(documents[name]):
                errors.append(f"{name}: permissions must be only workflow-level contents: read")
            if not has_no_secret_path(jobs.get("test-hosted", "")):
                errors.append(f"{name}: hosted test must not read secrets or environments")
            if "FORCE: ${{ inputs.force }}" not in jobs.get("deploy-corelink", ""):
                errors.append(f"{name}: CoreLink deploy must preserve the force input")
            continue
        if name == "build-cf-container-images.yml":
            expected_runners = {
                "validate-dispatch": "ubuntu-latest",
                "devenv-build-only": "ubuntu-latest",
                "devenv-publish": "ubuntu-24.04",
                "build-and-push": "corelink",
            }
            if set(jobs) != set(expected_runners):
                errors.append(
                    f"{name}: jobs must remain limited to guarded DevEnv proof/publication "
                    "and the corelink production publisher"
                )
            for job, runner in expected_runners.items():
                if job in jobs and [
                    value.strip("'\"") for value in runner_labels(jobs[job])
                ] != [runner]:
                    errors.append(f"{name}: {job} must run on {runner}")
            for hosted_job in ("validate-dispatch", "devenv-build-only"):
                if hosted_job in jobs and not has_no_secret_path(jobs[hosted_job]):
                    errors.append(
                        f"{name}: hosted job {hosted_job} must not read secrets or environments"
                    )
            publisher = jobs.get("devenv-publish", "")
            required_publisher_contract = (
                "inputs.operation == 'devenv-publish'",
                "github.ref == 'refs/heads/main'",
                "inputs.expected_source_sha == github.sha",
                "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}",
                "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}",
                "wrangler containers push",
                "> \"${OUTPUT}\" 2>&1",
                "scripts/ci/resolve-pushed-ref.sh",
                "runner-devenv-publication-receipt.json",
                "<redacted>",
                "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
                "retention-days: 90",
            )
            for required in required_publisher_contract:
                if required not in publisher:
                    errors.append(f"{name}: hosted DevEnv publisher is missing {required}")
            publication_step = re.search(
                r"(?ms)^      - name: Build, publish, and capture immutable receipt\n"
                r"(.*?)(?=^      - name: |\Z)",
                publisher,
            )
            if publication_step is None:
                errors.append(f"{name}: hosted DevEnv publication step is missing")
                publication_step_body = ""
            else:
                publication_step_body = publication_step.group(1)
            publisher_job_header = publisher.split("    steps:\n", 1)[0]
            if "${{ secrets." in publisher_job_header:
                errors.append(f"{name}: hosted DevEnv publisher secrets must not be job-scoped")
            publisher_secrets = {
                line.strip()
                for line in publication_step_body.splitlines()
                if "${{ secrets." in line
            }
            if publisher_secrets != {
                "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}",
                "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}",
            }:
                errors.append(f"{name}: hosted DevEnv publisher secret access is broader than required")
            for forbidden in ("wrangler deploy", "wrangler.jsonc", "pull_request:", "packages: write"):
                if forbidden in publisher:
                    errors.append(f"{name}: hosted DevEnv publisher must not contain {forbidden}")
            for forbidden in ('echo "${CLOUDFLARE_API_TOKEN}', 'echo "${CLOUDFLARE_ACCOUNT_ID}'):
                if forbidden in publisher:
                    errors.append(f"{name}: hosted DevEnv publisher must not log secret/account values")
        elif not jobs or any(
            [value.strip("'\"") for value in runner_labels(block)] != ["corelink"]
            for block in jobs.values()
        ):
            errors.append(f"{name}: every job must retain the corelink runner boundary")

    release_events, release_blocks = top_level_events(documents["release.yml"])
    if release_events != {"push"}:
        errors.append(f"release.yml: only tag push is allowed (found {sorted(release_events)})")
    push_block = release_blocks.get("push", "")
    if not re.search(r"(?m)^\s+tags:\s*\n\s+-\s*['\"]?v\*['\"]?\s*$", push_block):
        errors.append("release.yml: push must remain limited to v* tags")
    if re.search(r"(?m)^\s+branches:", push_block):
        errors.append("release.yml: branch pushes are not allowed")
    return errors


def negative_controls(documents: dict[str, str]) -> int:
    mutations = (
        ("provider pull_request", "deploy-spawn-worker.yml",
         lambda s: s.replace("\non:\n", "\non:\n  pull_request:\n", 1)),
        ("provider branch push", "build-cf-container-images.yml",
         lambda s: s.replace("\non:\n", "\non:\n  push:\n    branches: [main]\n", 1)),
        ("provider schedule", "deploy-spawn-worker.yml",
         lambda s: s.replace("\non:\n", "\non:\n  schedule:\n    - cron: '0 0 * * *'\n", 1)),
        ("release schedule", "release.yml",
         lambda s: s.replace("\non:\n", "\non:\n  schedule:\n    - cron: '0 0 * * *'\n", 1)),
        ("release branch push", "release.yml",
         lambda s: s.replace("\n  push:\n", "\n  push:\n    branches: [main]\n", 1)),
        ("hosted runner removed", "ci.yml",
         lambda s: s.replace("runs-on: ubuntu-latest", "runs-on: corelink", 1)),
        ("extra permission", "ci.yml",
         lambda s: s.replace("  contents: read\n", "  contents: read\n  packages: write\n", 1)),
        ("job permission override", "ci.yml",
         lambda s: s.replace("  gates:\n", "  gates:\n    permissions:\n      id-token: write\n", 1)),
        ("inherited secrets", "ci.yml",
         lambda s: s.replace("  gates:\n", "  gates:\n    secrets: inherit\n", 1)),
        ("environment secrets", "ci.yml",
         lambda s: s.replace("  gates:\n", "  gates:\n    environment: production\n", 1)),
        ("partial provider runner migration", "deploy-spawn-worker.yml",
         lambda s: re.sub(r"(?m)^    runs-on: corelink$", "    runs-on: ubuntu-latest", s, count=1)),
        ("corelink bootstrap lane moved to hosted runner", "deploy-spawn-worker.yml",
         lambda s: re.sub(
             r"(?s)(  test-corelink:\n.*?^    runs-on:) corelink$",
             r"\1 ubuntu-latest", s, count=1, flags=re.MULTILINE
         )),
        ("hosted bootstrap default enabled", "deploy-spawn-worker.yml",
         lambda s: re.sub(
             r"(?ms)(^      hosted_bootstrap:\n.*?^        default:) false$",
             r"\1 true", s, count=1
         )),
        ("hosted bootstrap deploy branch guard removed", "deploy-spawn-worker.yml",
         lambda s: s.replace(
             "if: ${{ github.ref == 'refs/heads/main' && inputs.hosted_bootstrap }}",
             "if: ${{ inputs.hosted_bootstrap }}", 1
         )),
        ("hosted bootstrap environment protection removed", "deploy-spawn-worker.yml",
         lambda s: re.sub(
             r"(?s)(  deploy-hosted:\n.*?^    )environment: corelink-runners\n",
             r"\1steps:\n", s, count=1, flags=re.MULTILINE
         )),
        ("hosted bootstrap force bypass enabled", "deploy-spawn-worker.yml",
         lambda s: s.replace("          FORCE: 'false'", "          FORCE: ${{ inputs.force }}", 1)),
        ("hosted bootstrap force guard removed", "deploy-spawn-worker.yml",
         lambda s: s.replace("        if: ${{ inputs.force }}\n", "        if: false\n", 1)),
        ("hosted bootstrap input changed from boolean", "deploy-spawn-worker.yml",
         lambda s: s.replace("        type: boolean\n        default: false\n\npermissions:",
                             "        type: string\n        default: false\n\npermissions:", 1)),
        ("non-hosted lane moved to arbitrary runner", "deploy-spawn-worker.yml",
         lambda s: re.sub(
             r"(?s)(  test-corelink:\n.*?^    runs-on:) corelink$",
             r"\1 macos-latest", s, count=1, flags=re.MULTILINE
         )),
        ("unapproved provider job added", "deploy-spawn-worker.yml",
         lambda s: s + "\n  rogue-hosted:\n    runs-on: ubuntu-latest\n"),
        ("DevEnv proof moved to self-hosted runner", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-build-only:\n.*?^    runs-on:) ubuntu-latest$",
             r"\1 corelink",
             s,
             count=1,
             flags=re.MULTILINE,
         )),
        ("DevEnv publisher moved to self-hosted runner", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-publish:\n.*?^    runs-on:) ubuntu-24\.04$",
             r"\1 corelink",
             s,
             count=1,
             flags=re.MULTILINE,
         )),
        ("dispatch guard moved to self-hosted runner", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  validate-dispatch:\n.*?^    runs-on:) ubuntu-latest$",
             r"\1 corelink",
             s,
             count=1,
             flags=re.MULTILINE,
         )),
        ("hosted dispatch guard reads secrets", "build-cf-container-images.yml",
         lambda s: s.replace(
             "  validate-dispatch:\n",
             "  validate-dispatch:\n    env:\n      PROBE: ${{ secrets.UNSAFE }}\n",
             1,
         )),
        ("production publisher moved to hosted runner", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?m)^    runs-on: corelink$", "    runs-on: ubuntu-latest", s, count=1
         )),
        ("DevEnv publication main guard removed", "build-cf-container-images.yml",
         lambda s: s.replace(
             "&& github.ref == 'refs/heads/main' && inputs.expected_source_sha == github.sha",
             "&& github.ref == 'refs/heads/other' && inputs.expected_source_sha == github.sha",
             1,
         )),
        ("DevEnv publisher deploys", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-publish:\n.*?)(wrangler containers push)",
             r"\1wrangler deploy",
             s,
             count=1,
         )),
        ("DevEnv publisher accesses extra secret", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-publish:\n.*?CLOUDFLARE_API_TOKEN: )\$\{\{ secrets\.CLOUDFLARE_API_TOKEN \}\}",
             r"\1${{ secrets.OTHER_TOKEN }}",
             s,
             count=1,
         )),
        ("DevEnv publisher secrets moved to job scope", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-publish:\n.*?    permissions:\n      contents: read\n)    steps:\n",
             r"\1    env:\n      CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}\n"
             r"      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n    steps:\n",
             s,
             count=1,
         )),
        ("DevEnv publisher logs its token", "build-cf-container-images.yml",
         lambda s: re.sub(
             r"(?s)(  devenv-publish:\n.*?)(      - name: Checkout exact protected main commit)",
             r'\1      - name: Unsafe diagnostic\n        run: echo "${CLOUDFLARE_API_TOKEN}"\n\2',
             s,
             count=1,
         )),
        ("actionlint checksum changed", "plan-integrity.yml",
         lambda s: s.replace(ACTIONLINT_SHA256, "0" + ACTIONLINT_SHA256[1:], 1)),
    )
    for label, path, mutate in mutations:
        case = dict(documents)
        before = case[path]
        case[path] = mutate(before)
        if case[path] == before or not validate(case):
            raise SystemExit(f"negative control failed to reject {label}")
    return len(mutations)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    names = (*HOSTED, *MANUAL_PROVIDER, "release.yml")
    documents = {name: workflow_path(name).read_text() for name in names}
    errors = validate(documents)
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    if args.self_test:
        control_count = negative_controls(documents)
        print(f"workflow migration contract and {control_count} negative controls passed")
    else:
        print("workflow migration contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
