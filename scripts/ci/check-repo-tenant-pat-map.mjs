#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
const { parse, ParseErrorCode } = createRequire(import.meta.url)("../../deploy/cloudflare/node_modules/jsonc-parser/lib/umd/main.js");

const repoPattern = /^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?\/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$/;
const fail = (message) => { throw new Error(message); };

function parseJsonc(text) {
  const errors = [];
  const value = parse(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.some((error) => error.error === ParseErrorCode.DuplicateKey || error.error !== ParseErrorCode.None)) {
    fail("malformed or ambiguous JSONC");
  }
  return value;
}

function mapCount(value) {
  const map = parseJsonc(value);
  if (!map || Array.isArray(map) || typeof map !== "object") fail("invalid map shape");
  const seen = new Set();
  for (const [key, secret] of Object.entries(map)) {
    if (typeof key !== "string" || !repoPattern.test(key.trim()) || typeof secret !== "string" || !secret) fail("invalid map entry");
    const canonical = key.trim().toLowerCase();
    if (seen.has(canonical)) fail("ambiguous map entry");
    seen.add(canonical);
  }
  return Object.keys(map).length;
}

async function getJson(base, account, script, suffix, token) {
  const path = `/accounts/${encodeURIComponent(account)}/workers/scripts/${encodeURIComponent(script)}/${suffix}`;
  let response;
  try {
    response = await fetch(base.replace(/\/$/, "") + path, {
      method: "GET", redirect: "error",
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
    });
    if (!response.ok) fail("Cloudflare API request failed");
    return await response.json();
  } catch {
    fail("Cloudflare API request failed");
  }
}

async function main() {
  const token = process.env.CLOUDFLARE_API_TOKEN;
  if (!token) { console.error("::error::Cloudflare API token is missing"); return 1; }
  try {
    const config = parseJsonc(await readFile(process.argv[2] || "deploy/cloudflare/wrangler.jsonc", "utf8"));
    const account = config?.account_id;
    const script = config?.name;
    const candidateText = config?.vars?.REPO_TENANT_PAT_MAP;
    if (typeof account !== "string" || typeof script !== "string" || typeof candidateText !== "string") fail("missing config field");
    const candidate = mapCount(candidateText);
    const base = process.env.CLOUDFLARE_API_BASE || "https://api.cloudflare.com/client/v4";
    const deployments = await getJson(base, account, script, "deployments", token);
    const activeDeployments = deployments?.result?.deployments;
    if (deployments?.success !== true || !Array.isArray(activeDeployments) || !activeDeployments.length) fail("no active deployment");
    const active = activeDeployments[0];
    if (!active || !Array.isArray(active.versions) || active.versions.length !== 1) fail("ambiguous active deployment");
    const activeVersion = active.versions[0];
    if (typeof activeVersion?.version_id !== "string" || activeVersion.percentage !== 100) fail("active deployment is not fully serving one version");
    const version = await getJson(base, account, script, `versions/${encodeURIComponent(activeVersion.version_id)}`, token);
    if (version?.success !== true || !version.result || !version.result.resources || !Array.isArray(version.result.resources.bindings)) fail("malformed active version");
    const matches = version.result.resources.bindings.filter((binding) => binding?.name === "REPO_TENANT_PAT_MAP");
    if (matches.length > 1) fail("duplicate live map binding");
    const live = matches.length === 0 ? 0 : (matches[0].type === "plain_text" && typeof matches[0].text === "string" ? mapCount(matches[0].text) : fail("malformed live map binding"));
    if (live > 0 && candidate === 0) { console.error("::error::REPO_TENANT_PAT_MAP depletion guard failed (live=nonempty candidate=empty)"); return 1; }
    console.log(`REPO_TENANT_PAT_MAP guard passed: candidate_count=${candidate} live_count=${live}`);
    return 0;
  } catch { console.error("::error::REPO_TENANT_PAT_MAP deployment guard failed closed"); return 1; }
}
process.exitCode = await main();
