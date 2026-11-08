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

function createDatabase(initialAccounts = []) {
  const accounts = new Map(initialAccounts.map((account) => [account.username, account]));
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
            accounts.set(values[0], { active: 1, checked_at: null });
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
    assert.deepEqual(JSON.parse(dispatch.body), { ref: "main" });
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
