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
  const accountStatuses = new Map(initialAccounts.map((account) => [
    account.username,
    {
      online: account.online ?? null,
      recorder_status: account.recorder_status || "idle",
      recording_started_at: account.recording_started_at || null,
      checked_at: account.checked_at || null,
    },
  ]));
  const recordings = new Map(initialRecordings.map((recording) => [recording.drive_id, recording]));
  const activity = [];
  const queries = [];
  let manualDispatchedAt = null;
  return {
    accounts,
    accountStatuses,
    activity,
    queries,
    prepare(sql) {
      let values = [];
      const query = { sql, bindings: [] };
      queries.push(query);
      return {
        bind(...parameters) {
          values = parameters;
          query.bindings = parameters;
          return this;
        },
        async first() {
          if (sql.includes("SELECT value FROM dashboard_settings WHERE key = 'accounts_initialized'")) {
            return { value: "1" };
          }
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
          if (sql.includes(
            "SELECT COUNT(*) AS count FROM recordings WHERE duration_seconds IS NULL",
          )) {
            return {
              count: [...recordings.values()].filter(
                (recording) => recording.duration_seconds == null ||
                  recording.duration_seconds <= 0,
              ).length,
            };
          }
          if (sql.includes("SELECT COUNT(*) AS count FROM recordings")) {
            return { count: recordings.size };
          }
          throw new Error(`Unexpected first() query: ${sql}`);
        },
        async all() {
          if (sql.includes("SELECT DISTINCT username FROM recordings")) {
            return {
              results: [...new Set([...recordings.values()].map(({ username }) => username))]
                .sort((a, b) => a.localeCompare(b))
                .map((username) => ({ username })),
            };
          }
          if (sql.includes("FROM accounts LEFT JOIN account_status")) {
            return {
              results: [...accounts].map(([username, account]) => ({
                username,
                active: account.active,
                created_at: account.created_at,
                ...accountStatuses.get(username),
              })),
            };
          }
          if (sql.includes("SELECT username FROM accounts WHERE active = 1")) {
            return {
              results: [...accounts]
                .filter(([, account]) => account.active === 1)
                .map(([username]) => ({ username })),
            };
          }
          if (sql.includes("FROM recordings") && sql.includes("LIMIT ? OFFSET ?")) {
            let rows = [...recordings.values()];
            if (sql.includes("ORDER BY started_at ASC")) {
              rows.sort((a, b) => a.started_at.localeCompare(b.started_at));
            } else if (sql.includes("ORDER BY started_at DESC")) {
              rows.sort((a, b) => b.started_at.localeCompare(a.started_at));
            } else if (sql.includes("ORDER BY created_at ASC")) {
              rows.sort((a, b) => a.created_at.localeCompare(b.created_at));
            } else {
              rows.sort((a, b) => b.created_at.localeCompare(a.created_at));
            }
            const limit = values.at(-2);
            const offset = values.at(-1);
            return { results: rows.slice(offset, offset + limit) };
          }
          if (sql.includes("FROM recordings") && sql.includes("ORDER BY started_at")) {
            return {
              results: [...recordings.values()]
                .sort((a, b) => b.started_at.localeCompare(a.started_at))
                .slice(0, 200),
            };
          }
          if (sql.includes("SELECT drive_id, username, started_at, filename")) {
            let rows = [...recordings.values()]
              .sort((a, b) => a.drive_id.localeCompare(b.drive_id));
            if (sql.includes("drive_id > ?")) {
              rows = rows.filter((recording) => recording.drive_id > values[0]);
            }
            return {
              results: rows.slice(0, 200).map(({
                drive_id,
                username,
                started_at,
                filename,
              }) => ({ drive_id, username, started_at, filename })),
            };
          }
          if (sql.includes("duration_seconds IS NULL")) {
            let missing = [...recordings.values()]
              .filter((recording) => recording.duration_seconds == null ||
                recording.duration_seconds <= 0)
              .sort((a, b) => a.drive_id.localeCompare(b.drive_id));
            if (sql.includes("drive_id > ?")) {
              missing = missing.filter((recording) => recording.drive_id > values[0]);
            }
            return {
              results: missing.slice(0, 200)
                .map(({ drive_id, username, filename }) => ({
                  drive_id,
                  username,
                  filename,
                })),
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
          if (sql.includes("DELETE FROM accounts WHERE username")) {
            return { meta: { changes: Number(accounts.delete(values[0])) } };
          }
          if (sql.includes("DELETE FROM account_status WHERE username")) {
            accountStatuses.delete(values[0]);
            return { meta: { changes: 1 } };
          }
          if (sql.includes("INSERT INTO account_status")) {
            const previous = accountStatuses.get(values[0]);
            const recording = values[2] === "recording" && values[1] === 1;
            accountStatuses.set(values[0], {
              online: values[1],
              recorder_status: values[2],
              recording_started_at: recording
                ? previous?.recorder_status === "recording" && previous.online === 1
                  ? previous.recording_started_at
                  : new Date().toISOString()
                : null,
              checked_at: new Date().toISOString(),
            });
            return { meta: { changes: 1 } };
          }
          if (sql.includes("UPDATE account_status SET recorder_status = 'idle'")) {
            const previous = accountStatuses.get(values[0]);
            if (previous) {
              accountStatuses.set(values[0], {
                ...previous,
                recorder_status: "idle",
                recording_started_at: null,
              });
            }
            return { meta: { changes: Number(Boolean(previous)) } };
          }
          if (sql.includes("INSERT INTO activity_logs")) {
            activity.unshift({ username: values[0], message: values[1] });
            return { meta: { changes: 1 } };
          }
          if (sql.includes("DELETE FROM activity_logs")) {
            return { meta: { changes: 0 } };
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
    DASHBOARD_API_TOKEN: "dashboard-token",
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

async function getAccounts(env) {
  return onRequest({
    request: new Request("https://livevault.example/api/accounts", {
      headers: { Cookie: await sessionCookie() },
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

async function organizeRecordings(env) {
  return onRequest({
    request: new Request("https://livevault.example/api/recordings/organize", {
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

async function listRecordings(env, query = "") {
  return onRequest({
    request: new Request(`https://livevault.example/api/recordings${query}`, {
      headers: { Cookie: await sessionCookie() },
    }),
    env,
  });
}

async function listRecordingsToOrganize(env, after) {
  const query = after ? `?after=${encodeURIComponent(after)}` : "";
  return onRequest({
    request: new Request(
      `https://livevault.example/api/internal/recordings/organize${query}`,
      { headers: { Authorization: "Bearer dashboard-token" } },
    ),
    env,
  });
}

async function listMissingDurations(env, after) {
  const query = after ? `?after=${encodeURIComponent(after)}` : "";
  return onRequest({
    request: new Request(
      `https://livevault.example/api/internal/recordings/missing-duration${query}`,
      { headers: { Authorization: "Bearer dashboard-token" } },
    ),
    env,
  });
}

async function removeAccount(env, username) {
  return onRequest({
    request: new Request(`https://livevault.example/api/accounts/${encodeURIComponent(username)}`, {
      method: "DELETE",
      headers: {
        Cookie: await sessionCookie(),
        Origin: "https://livevault.example",
      },
    }),
    env,
  });
}

async function checkInternalAccount(env, username) {
  return onRequest({
    request: new Request(
      `https://livevault.example/api/internal/accounts/${encodeURIComponent(username)}`,
      { headers: { Authorization: "Bearer dashboard-token" } },
    ),
    env,
  });
}

async function reportInternalStatus(env, status) {
  return onRequest({
    request: new Request("https://livevault.example/api/internal/status", {
      method: "POST",
      headers: {
        Authorization: "Bearer dashboard-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(status),
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
    assert.deepEqual(database.activity.map((event) => event.message), [
      "Immediate account status check queued",
      "Account added; monitoring enabled",
    ]);
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

test("removing an account immediately makes it inactive to recorder workers and logs the stop request", async () => {
  const database = createDatabase([{ username: "alice", active: 1 }]);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/variables/TRACKED")) {
      return options.method === "GET"
        ? new Response(JSON.stringify({ value: "alice" }), { status: 200 })
        : new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected GitHub request: ${url}`);
  };

  try {
    const response = await removeAccount(createEnvironment(database), "alice");
    assert.equal(response.status, 200);
    assert.deepEqual(database.activity[0], {
      username: "alice",
      message: "Account removed; recorder stop requested",
    });

    const status = await checkInternalAccount(createEnvironment(database), "alice");
    assert.equal(status.status, 200);
    assert.deepEqual(await status.json(), { active: false });

    const stopped = await reportInternalStatus(createEnvironment(database), {
      username: "alice",
      online: false,
      recorder_status: "idle",
      message: "Account was removed; recording stopped",
    });
    assert.equal(stopped.status, 202);
    assert.deepEqual(database.activity[0], {
      username: "alice",
      message: "Account was removed; recording stopped",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a failed recorder check clears an earlier online result", async () => {
  const database = createDatabase([{
    username: "alice",
    active: 1,
    online: 1,
    recorder_status: "recording",
    checked_at: "2026-11-08T22:00:00.000Z",
  }]);

  const response = await reportInternalStatus(createEnvironment(database), {
    username: "alice",
    online: null,
    recorder_status: "error",
    message: "Recorder could not obtain a stream URL",
  });

  assert.equal(response.status, 200);
  assert.equal(database.accountStatuses.get("alice").online, null);
  assert.equal(database.accountStatuses.get("alice").recorder_status, "error");
  assert.deepEqual(database.activity[0], {
    username: "alice",
    message: "Recorder could not obtain a stream URL",
  });
});

test("recording start time persists across heartbeats and clears when recording stops", async () => {
  const database = createDatabase([{ username: "alice", active: 1 }]);
  const env = createEnvironment(database);
  const report = (online, recorder_status, message) => reportInternalStatus(env, {
    username: "alice",
    online,
    recorder_status,
    message,
  });

  assert.equal((await report(true, "recording", "Recording stream")).status, 200);
  const startedAt = database.accountStatuses.get("alice").recording_started_at;
  assert.ok(startedAt);

  assert.equal((await report(true, "recording", "Recording stream")).status, 200);
  assert.equal(database.accountStatuses.get("alice").recording_started_at, startedAt);

  assert.equal((await report(false, "idle", "Account is offline")).status, 200);
  assert.equal(database.accountStatuses.get("alice").recording_started_at, null);

  assert.equal((await report(true, "recording", "Recording stream")).status, 200);
  const restartedAt = database.accountStatuses.get("alice").recording_started_at;
  assert.ok(restartedAt);
  const response = await getAccounts(env);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).accounts[0].recording_started_at, restartedAt);
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

test("Drive organization queues a Drive maintenance workflow", async () => {
  const originalFetch = globalThis.fetch;
  let dispatchBody;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith("/actions/workflows/drive-admin.yml/dispatches")) {
      dispatchBody = JSON.parse(options.body);
    }
    return new Response(null, { status: 204 });
  };

  try {
    const response = await organizeRecordings(createEnvironment(createDatabase()));
    assert.equal(response.status, 202);
    assert.deepEqual(dispatchBody, {
      ref: "main",
      inputs: { action: "organize" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("recordings return 15 rows while retaining counts for the full Drive history", async () => {
  const recordings = Array.from({ length: 201 }, (_, index) => ({
    drive_id: `drive_file_${String(index).padStart(5, "0")}`,
    username: "alice",
    started_at: `20261108_${String(index).padStart(6, "0")}`,
    part_number: 1,
    filename: `alice_part${index}.mp4`,
    drive_url: "https://drive.google.com/",
    file_size: 100,
    duration_seconds: index === 0 ? 0 : 120,
    created_at: "2026-11-08T00:00:00.000Z",
  }));
  const response = await listRecordings(
    createEnvironment(createDatabase([], recordings)),
  );

  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.recordings.length, 15);
  assert.equal(result.total, 201);
  assert.equal(result.page, 1);
  assert.equal(result.page_size, 15);
  assert.equal(result.pages, 14);
  assert.deepEqual(result.usernames, ["alice"]);
  assert.equal(result.missing_durations, 1);
});

test("recordings paginate and combine column filters with New York date bounds", async () => {
  const recordings = Array.from({ length: 40 }, (_, index) => ({
    drive_id: `drive_file_${String(index).padStart(5, "0")}`,
    username: index % 2 ? "alice" : "bob",
    room_id: "room",
    started_at: `20260308_${String(index).padStart(6, "0")}`,
    part_number: index + 1,
    filename: `recording_part${index}.mp4`,
    drive_url: "https://drive.google.com/",
    file_size: 20 * 1024 * 1024,
    duration_seconds: 1800,
    created_at: "2026-03-08T12:00:00.000Z",
  }));
  const database = createDatabase([], recordings);
  const env = createEnvironment(database);
  const response = await listRecordings(
    env,
    "?account=alice&started_from=2026-03-08&started_to=2026-03-08" +
      "&uploaded_from=2026-03-08&uploaded_to=2026-03-08&part=2" +
      "&size_min=10&size_max=20&drive=part002&duration_min=15" +
      "&duration_max=30&search=alice&page=2&sort=started_asc",
  );
  const result = await response.json();
  const listQuery = database.queries.find(({ sql }) => sql.includes("LIMIT ? OFFSET ?"));
  const countQuery = database.queries.find(({ sql }) =>
    sql.includes("SELECT COUNT(*) AS count FROM recordings WHERE username = ?")
  );

  assert.equal(response.status, 200);
  assert.equal(result.page, 2);
  assert.equal(result.recordings.length, 15);
  assert.equal(listQuery.bindings.at(-2), 15);
  assert.equal(listQuery.bindings.at(-1), 15);
  assert.match(listQuery.sql, /username = \?/);
  assert.match(listQuery.sql, /substr\(started_at, 1, 8\) >= \?/);
  assert.match(listQuery.sql, /created_at >= \?/);
  assert.match(listQuery.sql, /part_number = \?/);
  assert.match(listQuery.sql, /file_size >= \?/);
  assert.match(listQuery.sql, /instr\(lower\(filename\), lower\(\?\)\)/);
  assert.match(listQuery.sql, /duration_seconds >= \?/);
  assert.match(listQuery.sql, /ORDER BY started_at ASC/);
  assert.equal(listQuery.bindings[1], "20260308");
  assert.equal(listQuery.bindings[3], "2026-03-08T05:00:00.000Z");
  assert.equal(listQuery.bindings[4], "2026-03-09T04:00:00.000Z");
  assert.ok(countQuery);
  assert.ok(countQuery.sql.includes("duration_seconds <= ?"));
});

test("recording filters reject reversed date ranges", async () => {
  const response = await listRecordings(
    createEnvironment(createDatabase()),
    "?started_from=2026-11-09&started_to=2026-11-08",
  );
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /range is reversed/);
});

test("missing-duration endpoint supports paging beyond 200 Drive files", async () => {
  const recordings = Array.from({ length: 205 }, (_, index) => ({
    drive_id: `drive_file_${String(index).padStart(5, "0")}`,
    username: "alice",
    filename: `alice_part${index}.mp4`,
    duration_seconds: 0,
  }));
  const env = createEnvironment(createDatabase([], recordings));
  const firstResponse = await listMissingDurations(env);
  const firstPage = await firstResponse.json();
  const secondResponse = await listMissingDurations(
    env,
    firstPage.recordings.at(-1).drive_id,
  );
  const secondPage = await secondResponse.json();

  assert.equal(firstResponse.status, 200);
  assert.equal(firstPage.recordings.length, 200);
  assert.equal(secondResponse.status, 200);
  assert.equal(secondPage.recordings.length, 5);
});

test("recording organization endpoint pages through all known Drive files", async () => {
  const recordings = Array.from({ length: 205 }, (_, index) => ({
    drive_id: `drive_file_${String(index).padStart(5, "0")}`,
    username: "alice",
    started_at: `20261108_${String(index).padStart(6, "0")}`,
    filename: `alice_part${index}.mp4`,
  }));
  const env = createEnvironment(createDatabase([], recordings));
  const firstResponse = await listRecordingsToOrganize(env);
  const firstPage = await firstResponse.json();
  const secondResponse = await listRecordingsToOrganize(
    env,
    firstPage.recordings.at(-1).drive_id,
  );
  const secondPage = await secondResponse.json();

  assert.equal(firstResponse.status, 200);
  assert.equal(firstPage.recordings.length, 200);
  assert.equal(secondResponse.status, 200);
  assert.equal(secondPage.recordings.length, 5);
  assert.deepEqual(Object.keys(firstPage.recordings[0]).sort(), [
    "drive_id",
    "filename",
    "started_at",
    "username",
  ]);
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
