#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
python3 - <<'PY'
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

guard = Path("scripts/ci/check-repo-tenant-pat-map.mjs").resolve()

class Handler(BaseHTTPRequestHandler):
    scenario = {}
    redirect_hits = 0
    requests = []
    def do_GET(self):
        Handler.requests.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/redirect-destination"):
            Handler.redirect_hits += 1
            self.send_response(200)
            self.end_headers()
            return
        key = "deployments" if self.path.endswith("/deployments") else "version" if "/versions/" in self.path else "other"
        value = self.scenario.get(key, (200, {}))
        status, body = value
        encoded = json.dumps(body).encode()
        self.send_response(status)
        if status in (301, 302, 303, 307, 308) and isinstance(body, dict) and body.get("location"):
            self.send_header("Location", body["location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % server.server_port

def run(map_value, deployments, settings):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonc") as config:
        config.write('{"name":"corelink-spawn-worker","account_id":"acct","vars":{"REPO_TENANT_PAT_MAP":%s}}' % json.dumps(map_value))
        config.flush()
        # Keep this assertion scoped to the invocation under test.  A prior
        # subprocess can finish its socket handling just after it exits, so
        # retaining process-wide request history makes the wiring check flaky.
        Handler.requests.clear()
        Handler.scenario = {"deployments": deployments, "version": settings}
        env = dict(os.environ, CLOUDFLARE_API_TOKEN="fixture", CLOUDFLARE_API_BASE=base)
        return subprocess.run(["node", str(guard), config.name], env=env, text=True, capture_output=True)

def run_raw(raw):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonc") as config:
        config.write(raw)
        config.flush()
        Handler.scenario = {"deployments": ok_deploy, "version": live_empty}
        env = dict(os.environ, CLOUDFLARE_API_TOKEN="fixture", CLOUDFLARE_API_BASE=base)
        return subprocess.run(["node", str(guard), config.name], env=env, text=True, capture_output=True)

ok_deploy = (200, {"success": True, "result": {"deployments": [{"id": "deployment", "versions": [{"version_id": "version", "percentage": 100}]}]}})
live_nonempty = (200, {"success": True, "result": {"resources": {"bindings": [{"name": "REPO_TENANT_PAT_MAP", "type": "plain_text", "text": '{"a/b":"SECRET"}'}]}}})
live_empty = (200, {"success": True, "result": {"resources": {"bindings": []}}})

assert run("{}", ok_deploy, live_nonempty).returncode != 0
assert run('{"a/b":"SECRET"}', ok_deploy, live_nonempty).returncode == 0
assert Handler.requests[-2:] == [("/accounts/acct/workers/scripts/corelink-spawn-worker/deployments", "Bearer fixture"), ("/accounts/acct/workers/scripts/corelink-spawn-worker/versions/version", "Bearer fixture")]
assert run("{}", ok_deploy, live_empty).returncode == 0
assert run("{", ok_deploy, live_empty).returncode != 0
assert run("{}", ok_deploy, (200, {"success": True, "result": {"resources": {"bindings": [{"name": "REPO_TENANT_PAT_MAP", "type": "secret_text", "text": "x"}]}}})).returncode != 0
assert run("{}", (200, {"success": True, "result": {"deployments": []}}), live_empty).returncode != 0
assert run("{}", (200, {"success": True, "result": {"unexpected": []}}), live_empty).returncode != 0
assert run("{}", (200, {"success": False, "result": {"deployments": [{"versions": [{"version_id": "inactive", "percentage": 100}]}]}}), live_empty).returncode != 0
assert run("{}", (200, {"success": True, "result": {"deployments": [{"versions": [{"version_id": "inactive", "percentage": 50}]}]}}), live_empty).returncode != 0
assert run("{}", (200, {"success": True, "result": {"deployments": [{"versions": [{"version_id": "v1", "percentage": 50}, {"version_id": "v2", "percentage": 50}]}]}}), live_empty).returncode != 0
assert run("{}", (403, {"success": False, "result": None}), live_empty).returncode != 0
assert run("{}", ok_deploy, (401, {"success": False, "result": None})).returncode != 0
assert run_raw('// "name":"decoy"\n{"name":"corelink-spawn-worker","account_id":"acct","vars":{"REPO_TENANT_PAT_MAP":"{}"}}').returncode == 0
bad = run_raw('{"name":"corelink-spawn-worker",')
assert bad.returncode != 0 and "decoy" not in bad.stdout + bad.stderr
Handler.redirect_hits = 0
Handler.scenario = {"deployments": (302, {"location": base + "/redirect-destination"}), "version": live_empty}
redirect = run("{}", Handler.scenario["deployments"], live_empty)
assert redirect.returncode != 0 and Handler.redirect_hits == 0

workflow = Path(".github/workflows/deploy-spawn-worker.yml").read_text()
guard_at = workflow.index("scripts/ci/check-repo-tenant-pat-map.mjs")
deploy_at = workflow.index("npx wrangler deploy")
assert guard_at < deploy_at

selftests_workflow = Path(".github/workflows/selftests.yml").read_text()
dependency_setup_at = selftests_workflow.index(
    "npm ci --ignore-scripts --no-audit --no-fund --prefix deploy/cloudflare"
)
discovery_at = selftests_workflow.index("- name: Discover and run every tracked selftest")
assert dependency_setup_at < discovery_at
print("check-repo-tenant-pat-map selftest: scenarios and workflow ordering passed")
server.shutdown()
PY
