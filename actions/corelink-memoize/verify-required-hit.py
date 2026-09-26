#!/usr/bin/env python3
"""Verify a signed, capability-bearing clw binary before required-hit use."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PRINCIPAL = "corelink-release-candidate"
NAMESPACE = "corelink-clw-candidate"
FINGERPRINT = "SHA256:2qTotApppWZfJL8DCuy4qKud7ItKRTJGdaoqicQl3dg"
WORKSPACES_REPOSITORY = "HuGR-dev/corelink-workspaces"
WORKSPACES_REPOSITORY_ID = 1259579816
CAPABILITY = "required-hit/v1"
PACKAGE_VERSION = "0.1.12"
TARGET = "x86_64-unknown-linux-gnu"
MANIFEST_FIELDS = {
    "schema_version", "repository", "repository_id", "source_sha", "package_version",
    "capability", "binary", "target", "binary_sha256", "workflow_run_id",
    "workflow_run_attempt",
}
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PINNED_SIGNER_LINE = (
    "corelink-release-candidate ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIBf1KSyJEvR257OTrOydiPDkA+bDd3I2z03qOeVBJj4/\n"
)


class Rejected(Exception):
    pass


def reject(message: str) -> None:
    raise Rejected(message)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            reject(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def read_regular_file(path: str, label: str) -> bytes:
    if not path:
        reject(f"{label} path is empty")
    target = Path(path)
    try:
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode):
            reject(f"{label} is not a regular file")
        return target.read_bytes()
    except OSError as error:
        reject(f"cannot read {label}: {error}")
    raise AssertionError("unreachable")


def verify_key_pin(allowed_signers: Path) -> None:
    try:
        allowed_bytes = allowed_signers.read_bytes()
    except OSError as error:
        reject(f"cannot read pinned allowed_signers: {error}")
    if allowed_bytes != PINNED_SIGNER_LINE.encode("ascii"):
        reject("pinned allowed_signers principal/namespace/key does not match policy")

    public_key = " ".join(PINNED_SIGNER_LINE.split()[1:]) + "\n"
    with tempfile.TemporaryDirectory(prefix="clw-required-hit-key-") as temp:
        key_file = Path(temp) / "candidate.pub"
        key_file.write_text(public_key, encoding="ascii")
        try:
            result = subprocess.run(
                ["ssh-keygen", "-lf", str(key_file), "-E", "sha256"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
        except OSError as error:
            reject(f"cannot inspect pinned candidate SSH key: {error}")
    if result.returncode != 0 or FINGERPRINT not in result.stdout:
        reject("pinned candidate SSH key fingerprint mismatch")


def decode_manifest(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, Rejected) as error:
        reject(f"manifest JSON rejected: {error}")
    if not isinstance(parsed, dict) or set(parsed) != MANIFEST_FIELDS:
        reject("manifest has unknown or incomplete schema")
    return parsed


def validate_manifest(manifest: dict[str, Any]) -> None:
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        reject("unsupported manifest schema_version")
    if type(manifest["repository_id"]) is not int or manifest["repository_id"] != WORKSPACES_REPOSITORY_ID:
        reject("manifest Workspaces repository ID mismatch")
    for field in ("repository", "source_sha", "package_version", "capability", "binary",
                  "target", "binary_sha256", "workflow_run_id", "workflow_run_attempt"):
        if not isinstance(manifest[field], str):
            reject(f"manifest field {field} must be a string")
    if manifest["repository"] != WORKSPACES_REPOSITORY:
        reject("manifest repository mismatch")
    if not SOURCE_SHA_RE.fullmatch(manifest["source_sha"]):
        reject("manifest source SHA is malformed")
    if manifest["package_version"] != PACKAGE_VERSION:
        reject("unsupported signed clw package version")
    if manifest["capability"] != CAPABILITY:
        reject("unsupported signed clw capability")
    if manifest["binary"] != "clw":
        reject("unsupported candidate binary name")
    if manifest["target"] != TARGET:
        reject("unsupported candidate target")
    if not DIGEST_RE.fullmatch(manifest["binary_sha256"]):
        reject("manifest binary SHA-256 is malformed")
    for field in ("workflow_run_id", "workflow_run_attempt"):
        if not manifest[field].isdecimal() or int(manifest[field]) < 1:
            reject(f"manifest {field} must be a positive decimal string")


def verify_signature(manifest_bytes: bytes, signature_bytes: bytes, allowed_signers: Path) -> None:
    """Verify exact bytes with caller-supplied trust; production pins this file."""
    if not allowed_signers.is_file():
        reject("allowed_signers is not a regular file")
    with tempfile.TemporaryDirectory(prefix="clw-required-hit-signature-") as temp:
        signature_file = Path(temp) / "candidate.sig"
        signature_file.write_bytes(signature_bytes)
        try:
            result = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", PRINCIPAL,
                 "-n", NAMESPACE, "-s", str(signature_file)],
                input=manifest_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        except OSError as error:
            reject(f"cannot verify candidate SSH signature: {error}")
    if result.returncode != 0:
        reject("detached signature is not valid for the expected principal and namespace")


def verify(manifest_path: str, signature_path: str, allowed_signers: Path) -> str:
    manifest_bytes = read_regular_file(manifest_path, "required-hit manifest")
    signature_bytes = read_regular_file(signature_path, "required-hit signature")
    verify_key_pin(allowed_signers)
    manifest = decode_manifest(manifest_bytes)
    verify_signature(manifest_bytes, signature_bytes, allowed_signers)
    # Check signed claims only after authenticating exact bytes. This leaves the
    # unsupported-capability branch independently testable with an ephemeral key.
    validate_manifest(manifest)

    clw = shutil.which("clw")
    if not clw:
        reject("clw is not available on PATH")
    resolved = os.path.realpath(clw)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        reject("resolved clw is not an executable regular file")
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        reject("candidate target is unsupported on this runner platform")

    digest = hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
    if digest != manifest["binary_sha256"]:
        reject("resolved clw binary SHA-256 does not match signed manifest")
    try:
        version = subprocess.run(
            [resolved, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reject(f"cannot verify signed clw version: {error}")
    if version.returncode != 0 or version.stdout.strip() != f"clw {manifest['package_version']}":
        reject("resolved clw version does not match signed manifest")
    return resolved


def main() -> int:
    try:
        if len(sys.argv) != 3:
            reject("usage: verify-required-hit.py MANIFEST SIGNATURE")
        action_dir = Path(os.environ.get("GITHUB_ACTION_PATH", Path(__file__).parent))
        allowed_signers = action_dir / "trust" / "allowed_signers"
        print(verify(sys.argv[1], sys.argv[2], allowed_signers))
        return 0
    except Rejected as error:
        print(str(error), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
