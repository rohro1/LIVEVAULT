import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

globalThis.crypto ??= webcrypto;

const source = await readFile(new URL("../functions/api/[[path]].js", import.meta.url), "utf8");
const { onRequest } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

const sessionSecret = "test-session-secret";

async function sessionCookie() {
  const expires = Math.floor(Date.now() / 1000) + 3600;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(sessionSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`lv1:${expires}`),
  );
  return `lv_session=${expires}.${Buffer.from(signature).toString("base64url")}`;
}

function createDatabase(initialAccounts = [], initialRecordings = []) {
  const accounts = new Map(initialAccounts.map((account) => [account.username, account]));
  const recordings = new Map(initialRecordings.map((recording) => [recording.drive_id, recording]));
  let manualDispatchedAt = null;
  return {
    accounts,
    prepare(sql) {
      let values = [];
      return {
        bind(...parameters) {
          values = parameters;
          return this;
        },
        async first() {
          if (sql.includes("SELECT accounts.active, account_status.checked_at")) {
            return accounts.get(values[0]) || null;
          }
          if (sql.includes("SELECT active FROM accounts WHERE username")) {
            return accounts.get(values[0]) || null;
          }
          if (sql.includes("SELECT dispatched_at FROM monitor_manual_dispatches")) {
            return manualDispatchedAt === null ? null : { dispatched_at: manualDispatchedAt };
          }
          if (sql.includes("SELECT drive_id FROM recordings WHERE drive_id")) {
            return recordings.get(values[0]) || null;
          }
          throw new Error(`Unexpected first() query: ${sql}`);
        },
        async all() {
          if (sql.includes("SELECT username FROM accounts WHERE active = 1")) {
            return {
              results: [...accounts]
                .filter(([, account]) => account.active === 1)
                .map(([username]) => ({ username })),
            };
          }
          throw new Error(`Unexpected all() query: ${sql}`);
        },
        async run() {
          if (sql.includes("INSERT INTO accounts")) {
            accounts.set(values[0], {
              ...(accounts.get(values[0]) || {}),
              active: 1,
              checked_at: null,
            });
            return { meta: { changes: 1 } };
          }
          if (sql.includes("INSERT INTO monitor_manual_dispatches")) {
            if (manualDispatchedAt !== null && manualDispatchedAt > values[1]) {
              return { meta: { changes: 0 } };
            }
            manualDispatchedAt = values[0];
            return { meta: { changes: 1 } };
          }
          throw new Error(`Unexpected run() query: ${sql}`);
        },
      };
    },
  };
}

function createEnvironment(database) {
  return {
    DB: database,
    DASHBOARD_SESSION_SECRET: sessionSecret,
    GITHUB_PAT: "test-token",
    GITHUB_REPOSITORY: "owner/repo",
  };
}

async function addAccount(env, username = "alice") {
  return onRequest({
    request: new Request("https://livevault.example/api/accounts", {
      method: "POST",
      headers: {
        Cookie: await sessionCookie(),
        Origin: "https://livevault.example",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ username }),
    }),
    env,
  });
}

async function checkAccounts(env, body = {}) {
  return onRequest({
    request: new Request("https://livevault.example/api/monitor", {
      method: "POST",
      headers: {
        Cookie: await sessionCookie(),
        Origin: "https://livevault.example",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    }),
    env,
  });
}

async function deleteRecording(env, driveId) {
  return onRequest({
    request: new Request(
      `https://livevault.example/api/recordings/${encodeURIComponent(driveId)}`,
      {
        method: "DELETE",
        headers: {
          Cookie: await sessionCookie(),
          Origin: "https://livevault.example",
        },
      },
    ),
    env,
  });
}

async function backfillDurations(env) {
  return onRequest({
    request: new Request("https://livevault.example/api/recordings/backfill-duration", {
      method: "POST",
      headers: {
        Cookie: await sessionCookie(),
        Origin: "https://livevault.example",
        "Content-Type": "application/json",
      },
      body: "{}",
    }),
    env,
  });
}

test("adding an unchecked account queues an immediate monitor run", async () => {
  const database = createDatabase();
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method, body: options.body });
    if (String(url).endsWith("/actions/variables/TRACKED")) {
      return new Response(JSON.stringify({ value: "" }), { status: 200 });
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await addAccount(createEnvironment(database));
    assert.equal(response.status, 201);
    assert.deepEqual(await response.json(), { ok: true, check_queued: true });
    assert.equal(database.accounts.get("alice").active, 1);
    const dispatch = calls.find(({ url }) => url.endsWith("/actions/workflows/monitor.yml/dispatches"));
    assert.ok(dispatch);
    assert.deepEqual(JSON.parse(dispatch.body), {
      ref: "main",
      inputs: { target_username: "alice" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("adding an already checked active account does not queue a redundant scan", async () => {
  const checkedAt = new Date(Date.now() - 60_000).toISOString();
  const database = createDatabase([{ username: "alice", active: 1, checked_at: checkedAt }]);
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method });
    if (String(url).endsWith("/actions/variables/TRACKED")) {
      return new Response(JSON.stringify({ value: "alice" }), { status: 200 });
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await addAccount(createEnvironment(database));
    assert.equal(response.status, 201);
    assert.deepEqual(await response.json(), { ok: true, check_queued: false });
    assert.equal(
      calls.some(({ url }) => url.endsWith("/actions/workflows/monitor.yml/dispatches")),
      false,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("manual check-all dispatches one scan for every tracked account", async () => {
  const database = createDatabase([
    { username: "alice", active: 1 },
    { username: "bob", active: 1 },
  ]);
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), body: options.body });
    return new Response(null, { status: 204 });
  };

  try {
    const response = await checkAccounts(createEnvironment(database));
    assert.equal(response.status, 202);
    assert.deepEqual(await response.json(), { ok: true, queued: true, username: null });
    const dispatch = calls.find(({ url }) => url.endsWith("/actions/workflows/monitor.yml/dispatches"));
    assert.deepEqual(JSON.parse(dispatch.body), { ref: "main" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("per-account refresh only dispatches the requested active account", async () => {
  const database = createDatabase([{ username: "alice", active: 1 }]);
  const originalFetch = globalThis.fetch;
  let dispatchBody;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/workflows/monitor.yml/dispatches")) {
      dispatchBody = JSON.parse(options.body);
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await checkAccounts(createEnvironment(database), { username: "alice" });
    assert.equal(response.status, 202);
    assert.deepEqual(dispatchBody, {
      ref: "main",
      inputs: { target_username: "alice" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("manual checks are rate limited to one dispatched scan per minute", async () => {
  const database = createDatabase([{ username: "alice", active: 1 }]);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });

  try {
    const first = await checkAccounts(createEnvironment(database), { username: "alice" });
    assert.equal(first.status, 202);
    const second = await checkAccounts(createEnvironment(database), { username: "alice" });
    assert.equal(second.status, 429);
    assert.match((await second.json()).error, /wait .* seconds/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("recording deletion queues a Drive workflow for only a known dashboard recording", async () => {
  const recording = { drive_id: "drive_file_12345" };
  const database = createDatabase([], [recording]);
  const originalFetch = globalThis.fetch;
  let dispatchBody;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/workflows/drive-admin.yml/dispatches")) {
      dispatchBody = JSON.parse(options.body);
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await deleteRecording(createEnvironment(database), recording.drive_id);
    assert.equal(response.status, 202);
    assert.deepEqual(await response.json(), { ok: true, queued: true });
    assert.deepEqual(dispatchBody, {
      ref: "main",
      inputs: { action: "delete", drive_id: recording.drive_id },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("duration backfill queues a Drive maintenance workflow", async () => {
  const originalFetch = globalThis.fetch;
  let dispatchBody;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/workflows/drive-admin.yml/dispatches")) {
      dispatchBody = JSON.parse(options.body);
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await backfillDurations(createEnvironment(createDatabase()));
    assert.equal(response.status, 202);
    assert.deepEqual(dispatchBody, {
      ref: "main",
      inputs: { action: "backfill-durations" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("recording deletion rejects file IDs not present in the dashboard", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("GitHub should not be called for an unknown file");
  };

  try {
    const response = await deleteRecording(createEnvironment(createDatabase()), "unknown-file-id");
    assert.equal(response.status, 404);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a missing Actions permission is reported instead of claiming an immediate check", async () => {
  const database = createDatabase();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/variables/TRACKED")) {
      return new Response(JSON.stringify({ value: "" }), { status: 200 });
    }
    if (options.method === "PATCH") return new Response(null, { status: 204 });
    return new Response("Forbidden", { status: 403 });
  };

  try {
    const response = await addAccount(createEnvironment(database));
    assert.equal(response.status, 502);
    const result = await response.json();
    assert.match(result.error, /Account was saved, but its immediate status check could not be queued/);
    assert.equal(database.accounts.get("alice").active, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
