import assert from "node:assert/strict";
import test from "node:test";

import { dispatchMonitor, runScheduled } from "../workers/monitor-scheduler.mjs";

const env = {
  MONITOR_GITHUB_PAT: "test-token",
  GITHUB_REPOSITORY: "owner/repo",
  GITHUB_REF: "main",
};

function okResponse(body = {}) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  };
}

test("dispatches the monitor when there is no active run", async () => {
  const requests = [];
  const request = async (url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? okResponse({ workflow_runs: [] })
      : { ok: true, status: 204 };
  };

  const result = await dispatchMonitor(env, request);

  assert.deepEqual(result, { dispatched: true });
  assert.equal(requests.length, 2);
  assert.match(requests[0].url, /actions\/workflows\/monitor\.yml\/runs/);
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), { ref: "main" });
  assert.equal(
    requests[1].options.headers.Authorization,
    "Bearer test-token",
  );
});

test("skips dispatch while a monitor run is queued", async () => {
  let requestCount = 0;
  const result = await dispatchMonitor(env, async () => {
    requestCount += 1;
    return okResponse({
      workflow_runs: [{ status: "queued" }],
    });
  });

  assert.deepEqual(result, { dispatched: false, reason: "active_run" });
  assert.equal(requestCount, 1);
});

test("fails explicitly when the GitHub token lacks workflow access", async () => {
  await assert.rejects(
    dispatchMonitor(env, async () => ({ ok: false, status: 403 })),
    /HTTP 403/,
  );
});

test("explains how to restore an expired scheduler token", async () => {
  await assert.rejects(
    dispatchMonitor(env, async () => ({ ok: false, status: 401 })),
    /replace the scheduler Worker secret with an Actions read\/write token/,
  );
});

test("validates the repository before making requests", async () => {
  let called = false;
  await assert.rejects(
    dispatchMonitor(
      { ...env, GITHUB_REPOSITORY: "owner/repo?bad" },
      async () => {
        called = true;
      },
    ),
    /GITHUB_REPOSITORY is not configured correctly/,
  );
  assert.equal(called, false);
});

test("scheduled runner triggers dispatch using its injected request client", async () => {
  let requestCount = 0;
  await runScheduled(env, async () => {
    requestCount += 1;
    return requestCount === 1
      ? okResponse({ workflow_runs: [] })
      : { ok: true, status: 204 };
  });
  assert.equal(requestCount, 2);
});
