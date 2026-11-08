const json = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...headers,
    },
  });

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function bytesToBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function base64UrlToBytes(value) {
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/");
  const binary = atob(base64 + "=".repeat((4 - base64.length % 4) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function hmacKey(secret) {
  if (!secret) throw new HttpError(503, "Dashboard session secret is not configured");
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function issueSession(secret) {
  const expires = Math.floor(Date.now() / 1000) + 12 * 60 * 60;
  const payload = `lv1:${expires}`;
  const signature = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    new TextEncoder().encode(payload),
  );
  return `${expires}.${bytesToBase64Url(new Uint8Array(signature))}`;
}

async function validSession(request, secret) {
  const cookie = request.headers.get("Cookie") || "";
  const match = cookie.match(/(?:^|;\s*)lv_session=(\d+)\.([A-Za-z0-9_-]+)/);
  if (!match || Number(match[1]) <= Math.floor(Date.now() / 1000)) return false;
  try {
    return await crypto.subtle.verify(
      "HMAC",
      await hmacKey(secret),
      base64UrlToBytes(match[2]),
      new TextEncoder().encode(`lv1:${match[1]}`),
    );
  } catch {
    return false;
  }
}

function constantTimeEqual(left, right) {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let mismatch = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    mismatch |= (a[i] || 0) ^ (b[i] || 0);
  }
  return mismatch === 0;
}

async function readJson(request) {
  const text = await request.text();
  if (new TextEncoder().encode(text).length > 8192) {
    throw new HttpError(413, "Request body is too large");
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "Request body must be valid JSON");
  }
}

function requireSameOrigin(request) {
  const origin = request.headers.get("Origin");
  if (!origin || origin !== new URL(request.url).origin) {
    throw new HttpError(403, "Request origin was rejected");
  }
}

function trackedUsername(value) {
  if (typeof value !== "string") throw new HttpError(400, "Username is required");
  const username = value.trim().replace(/^@/, "").toLowerCase();
  if (!/^[a-z0-9._]{1,24}$/.test(username)) {
    throw new HttpError(400, "Enter a valid TikTok username");
  }
  return username;
}

async function hmacHex(value, secret) {
  const signature = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function checkLoginRate(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const ipHash = await hmacHex(ip, env.DASHBOARD_SESSION_SECRET);
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("DELETE FROM login_attempts WHERE window_start < ?")
    .bind(now - 86400).run();
  const row = await env.DB.prepare(
    "SELECT window_start, failures FROM login_attempts WHERE ip_hash = ?",
  ).bind(ipHash).first();
  if (row && now - row.window_start < 900 && row.failures >= 10) {
    throw new HttpError(429, "Too many login attempts; try again in 15 minutes");
  }
  return { ipHash, row, now };
}

async function login(request, env) {
  requireSameOrigin(request);
  if (!env.DASHBOARD_PASSWORD || !env.DASHBOARD_SESSION_SECRET) {
    throw new HttpError(503, "Dashboard login is not configured");
  }
  const rate = await checkLoginRate(request, env);
  const body = await readJson(request);
  const submitted = typeof body.password === "string" ? body.password : "";
  const expectedDigest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(env.DASHBOARD_PASSWORD),
  );
  const submittedDigest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(submitted),
  );
  const valid = constantTimeEqual(
    bytesToBase64Url(new Uint8Array(expectedDigest)),
    bytesToBase64Url(new Uint8Array(submittedDigest)),
  );
  if (!valid) {
    const first = !rate.row || rate.now - rate.row.window_start >= 900;
    await env.DB.prepare(
      `INSERT INTO login_attempts (ip_hash, window_start, failures) VALUES (?, ?, 1)
       ON CONFLICT(ip_hash) DO UPDATE SET
         window_start = CASE WHEN ? THEN excluded.window_start ELSE window_start END,
         failures = CASE WHEN ? THEN 1 ELSE failures + 1 END`,
    ).bind(rate.ipHash, rate.now, first ? 1 : 0, first ? 1 : 0).run();
    throw new HttpError(401, "Incorrect password");
  }
  await env.DB.prepare("DELETE FROM login_attempts WHERE ip_hash = ?").bind(rate.ipHash).run();
  const session = await issueSession(env.DASHBOARD_SESSION_SECRET);
  return json({ ok: true }, 200, {
    "Set-Cookie": `lv_session=${session}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200`,
  });
}

async function githubRequest(env, path, method = "GET", body, allowNotFound = false) {
  if (!env.GITHUB_PAT || !env.GITHUB_REPOSITORY) {
    throw new HttpError(503, "GitHub integration is not configured");
  }
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY)) {
    throw new HttpError(500, "GitHub repository configuration is invalid");
  }
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${env.GITHUB_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(body ? { "Content-Type": "application/json" } : {}),
      "User-Agent": "LIVEVAULT-dashboard",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok && !(allowNotFound && response.status === 404)) {
    throw new HttpError(502, `GitHub integration failed (HTTP ${response.status})`);
  }
  return response;
}

async function syncTracked(env) {
  const rows = await env.DB.prepare(
    "SELECT username FROM accounts WHERE active = 1 ORDER BY username",
  ).all();
  const value = (rows.results || []).map((row) => row.username).join(",");
  let response = await githubRequest(env, "/actions/variables/TRACKED", "GET", undefined, true);
  if (response.status === 404) {
    response = await githubRequest(env, "/actions/variables", "POST", {
      name: "TRACKED",
      value,
    });
  } else {
    response = await githubRequest(env, "/actions/variables/TRACKED", "PATCH", {
      name: "TRACKED",
      value,
    });
  }
  if (!response.ok) throw new HttpError(502, `GitHub variable update failed (HTTP ${response.status})`);
}

async function dispatchMonitor(env, username = null) {
  const response = await githubRequest(
    env,
    "/actions/workflows/monitor.yml/dispatches",
    "POST",
    {
      ref: "main",
      ...(username ? { inputs: { target_username: username } } : {}),
    },
  );
  if (response.status !== 204) {
    throw new HttpError(502, `Monitor dispatch failed (HTTP ${response.status})`);
  }
}

async function queueManualMonitor(env, username = null) {
  const now = Math.floor(Date.now() / 1000);
  const result = await env.DB.prepare(
    `INSERT INTO monitor_manual_dispatches (scope, dispatched_at) VALUES ('manual', ?)
     ON CONFLICT(scope) DO UPDATE SET dispatched_at = excluded.dispatched_at
     WHERE monitor_manual_dispatches.dispatched_at <= ?`,
  ).bind(now, now - 60).run();
  if (!result.meta.changes) {
    const previous = await env.DB.prepare(
      "SELECT dispatched_at FROM monitor_manual_dispatches WHERE scope = 'manual'",
    ).first();
    const retrySeconds = Math.max(1, (previous?.dispatched_at || now) + 60 - now);
    throw new HttpError(429, `Please wait ${retrySeconds} seconds before another manual status check`);
  }
  await dispatchMonitor(env, username);
}

async function dispatchDriveMaintenance(env, action, driveId = null) {
  const response = await githubRequest(
    env,
    "/actions/workflows/drive-admin.yml/dispatches",
    "POST",
    {
      ref: "main",
      inputs: {
        action,
        ...(driveId ? { drive_id: driveId } : {}),
      },
    },
  );
  if (response.status !== 204) {
    throw new HttpError(502, `Drive action dispatch failed (HTTP ${response.status})`);
  }
}

async function initializeAccounts(env) {
  const initialized = await env.DB.prepare(
    "SELECT value FROM dashboard_settings WHERE key = 'accounts_initialized'",
  ).first();
  if (initialized) return;

  const response = await githubRequest(
    env,
    "/actions/variables/TRACKED",
    "GET",
    undefined,
    true,
  );
  const value = response.status === 404 ? "" : (await response.json()).value || "";
  const usernames = [...new Set(
    value.split(",").map((item) => item.trim()).filter(Boolean).map(trackedUsername),
  )];
  const statements = usernames.map((username) =>
    env.DB.prepare("INSERT OR IGNORE INTO accounts (username, active) VALUES (?, 1)")
      .bind(username),
  );
  statements.push(
    env.DB.prepare(
      "INSERT OR IGNORE INTO dashboard_settings (key, value) VALUES ('accounts_initialized', '1')",
    ),
  );
  await env.DB.batch(statements);
}

async function accountList(env) {
  const rows = await env.DB.prepare(
    `SELECT accounts.username, accounts.active, accounts.created_at,
            account_status.online, account_status.recorder_status, account_status.checked_at
     FROM accounts LEFT JOIN account_status USING (username)
     ORDER BY accounts.username`,
  ).all();
  return (rows.results || []).map((row) => ({
    username: row.username,
    active: row.active === 1,
    created_at: row.created_at,
    online: row.online === null ? null : row.online === 1,
    recorder_status: row.recorder_status || "idle",
    checked_at: row.checked_at,
  }));
}

async function workflowDiagnostics(env, workflow) {
  try {
    const response = await githubRequest(
      env,
      `/actions/workflows/${workflow}.yml/runs?per_page=20`,
    );
    const result = await response.json();
    const remaining = response.headers.get("X-RateLimit-Remaining");
    const limit = response.headers.get("X-RateLimit-Limit");
    const reset = response.headers.get("X-RateLimit-Reset");
    return {
      status: "ok",
      rate_limit: remaining && limit && reset
        ? { remaining: Number(remaining), limit: Number(limit), reset_at: Number(reset) }
        : null,
      runs: (result.workflow_runs || []).map((run) => ({
        status: run.status,
        conclusion: run.conclusion,
        created_at: run.created_at,
        updated_at: run.updated_at,
        url: run.html_url,
      })),
    };
  } catch (error) {
    return {
      status: "error",
      message: error instanceof Error ? error.message : "Workflow status request failed",
      runs: [],
    };
  }
}

async function diagnostics(env) {
  const result = {
    generated_at: new Date().toISOString(),
    cloudflare: { status: "ok" },
    d1: { status: "unknown" },
    integrations: {
      github_pat: Boolean(env.GITHUB_PAT),
      github_repository: Boolean(env.GITHUB_REPOSITORY),
      dashboard_ingest_token: Boolean(env.DASHBOARD_API_TOKEN),
      host_key_secret: Boolean(env.HOST_KEY_SECRET),
    },
    tracking_sync: { status: "not_checked" },
    workflows: {
      monitor: { status: "not_checked", runs: [] },
      recorder: { status: "not_checked", runs: [] },
    },
  };

  try {
    const counts = await env.DB.prepare(
      `SELECT
         (SELECT COUNT(*) FROM accounts) AS accounts,
         (SELECT COUNT(*) FROM recordings) AS recordings,
         (SELECT MAX(checked_at) FROM account_status) AS last_account_check`,
    ).first();
    result.d1 = {
      status: "ok",
      accounts: counts.accounts,
      recordings: counts.recordings,
      last_account_check: counts.last_account_check,
    };
  } catch (error) {
    result.d1 = {
      status: "error",
      message: error instanceof Error ? error.message : "D1 query failed",
    };
  }

  if (env.GITHUB_PAT && env.GITHUB_REPOSITORY) {
    const [monitor, recorder] = await Promise.all([
      workflowDiagnostics(env, "monitor"),
      workflowDiagnostics(env, "record"),
    ]);
    result.workflows.monitor = monitor;
    result.workflows.recorder = recorder;
    try {
      const [databaseAccounts, trackedResponse] = await Promise.all([
        env.DB.prepare("SELECT username FROM accounts WHERE active = 1 ORDER BY username").all(),
        githubRequest(env, "/actions/variables/TRACKED", "GET", undefined, true),
      ]);
      const databaseTracked = (databaseAccounts.results || [])
        .map((row) => row.username.toLowerCase())
        .sort();
      const githubTracked = trackedResponse.status === 404
        ? []
        : ((await trackedResponse.json()).value || "")
          .split(",")
          .map((username) => username.trim().replace(/^@/, "").toLowerCase())
          .filter(Boolean)
          .sort();
      result.tracking_sync = {
        status: JSON.stringify(databaseTracked) === JSON.stringify(githubTracked)
          ? "synced"
          : "mismatch",
        dashboard_accounts: databaseTracked.length,
        github_accounts: githubTracked.length,
      };
    } catch (error) {
      result.tracking_sync = {
        status: "error",
        message: error instanceof Error ? error.message : "Tracking sync check failed",
      };
    }
  }
  return json({ diagnostics: result });
}

async function internalAuthorized(request, env) {
  if (!env.DASHBOARD_API_TOKEN) throw new HttpError(503, "Dashboard ingest is not configured");
  const supplied = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/, "");
  if (!constantTimeEqual(supplied, env.DASHBOARD_API_TOKEN)) {
    throw new HttpError(401, "Dashboard ingest authorization failed");
  }
}

async function internalRecording(request, env) {
  await internalAuthorized(request, env);
  const body = await readJson(request);
  const username = trackedUsername(body.username);
  const fields = ["drive_id", "room_id", "started_at", "filename", "drive_url"];
  if (fields.some((key) => typeof body[key] !== "string" || !body[key])) {
    throw new HttpError(400, "Recording metadata is incomplete");
  }
  if (!/^\d{8}_\d{6}$/.test(body.started_at) || !Number.isInteger(body.part_number) ||
      body.part_number < 1 || !Number.isInteger(body.file_size) || body.file_size < 1 ||
      typeof body.duration_seconds !== "number" ||
      !Number.isFinite(body.duration_seconds) || body.duration_seconds <= 0) {
    throw new HttpError(400, "Recording metadata is invalid");
  }
  let driveUrl;
  try {
    driveUrl = new URL(body.drive_url);
  } catch {
    throw new HttpError(400, "Drive link is invalid");
  }
  if (driveUrl.protocol !== "https:" || driveUrl.hostname !== "drive.google.com") {
    throw new HttpError(400, "Drive link is invalid");
  }
  await env.DB.prepare(
    `INSERT OR IGNORE INTO recordings
     (drive_id, username, room_id, started_at, part_number, filename, drive_url, file_size,
      duration_seconds)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    body.drive_id,
    username,
    body.room_id,
    body.started_at,
    body.part_number,
    body.filename,
    driveUrl.toString(),
    body.file_size,
    body.duration_seconds,
  ).run();
  await addActivity(env, username, `Uploaded ${body.filename}`);
  return json({ ok: true }, 201);
}

async function addActivity(env, username, message) {
  const safeMessage = message.replace(/[\r\n]/g, " ").slice(0, 200);
  await env.DB.prepare(
    "INSERT INTO activity_logs (username, message) VALUES (?, ?)",
  ).bind(username || null, safeMessage).run();
  await env.DB.prepare(
    "DELETE FROM activity_logs WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 500)",
  ).run();
}

async function internalStatus(request, env) {
  await internalAuthorized(request, env);
  const body = await readJson(request);
  const username = trackedUsername(body.username);
  const validStatuses = new Set(["queued", "recording", "idle", "error"]);
  if (
    (typeof body.online !== "boolean" && body.online !== null) ||
    !validStatuses.has(body.recorder_status) ||
    typeof body.message !== "string" ||
    body.message.length > 200
  ) {
    throw new HttpError(400, "Account status is invalid");
  }
  const account = await env.DB.prepare(
    "SELECT active FROM accounts WHERE username = ?",
  ).bind(username).first();
  if (!account) return json({ ok: true, skipped: true }, 202);
  const recorderStatus = account.active ? body.recorder_status : "idle";
  await env.DB.prepare(
    `INSERT INTO account_status (username, online, recorder_status, checked_at)
     VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
     ON CONFLICT(username) DO UPDATE SET
       online = CASE WHEN ? IS NULL THEN account_status.online ELSE excluded.online END,
       recorder_status = excluded.recorder_status,
       checked_at = CASE WHEN ? IS NULL THEN account_status.checked_at ELSE excluded.checked_at END`,
  ).bind(
    username,
    body.online === null ? null : body.online ? 1 : 0,
    recorderStatus,
    body.online === null ? null : 1,
    body.online === null ? null : 1,
  ).run();
  await addActivity(env, username, body.message);
  return json({ ok: true });
}

async function handle(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;
  if (path === "/api/login" && request.method === "POST") return login(request, env);
  if (path === "/api/internal/recordings" && request.method === "POST") {
    return internalRecording(request, env);
  }
  if (path === "/api/internal/recordings/missing-duration" && request.method === "GET") {
    await internalAuthorized(request, env);
    const rows = await env.DB.prepare(
      `SELECT drive_id, username, filename
       FROM recordings WHERE duration_seconds <= 0
       ORDER BY created_at ASC LIMIT 200`,
    ).all();
    return json({ recordings: rows.results || [] });
  }
  if (path.startsWith("/api/internal/recordings/") && request.method === "PATCH") {
    await internalAuthorized(request, env);
    const driveId = decodeURIComponent(path.slice("/api/internal/recordings/".length));
    const body = await readJson(request);
    if (!driveId || typeof body.duration_seconds !== "number" ||
        !Number.isFinite(body.duration_seconds) || body.duration_seconds <= 0) {
      throw new HttpError(400, "Recording duration update is invalid");
    }
    const result = await env.DB.prepare(
      "UPDATE recordings SET duration_seconds = ? WHERE drive_id = ? AND duration_seconds <= 0",
    ).bind(body.duration_seconds, driveId).run();
    if (!result.meta.changes) return json({ ok: true, skipped: true }, 202);
    return json({ ok: true });
  }
  if (path.startsWith("/api/internal/recordings/") && request.method === "GET") {
    await internalAuthorized(request, env);
    const driveId = decodeURIComponent(path.slice("/api/internal/recordings/".length));
    const recording = await env.DB.prepare(
      "SELECT drive_id, filename FROM recordings WHERE drive_id = ?",
    ).bind(driveId).first();
    if (!recording) throw new HttpError(404, "Recording was not found");
    return json(recording);
  }
  if (path.startsWith("/api/internal/recordings/") && request.method === "DELETE") {
    await internalAuthorized(request, env);
    const driveId = decodeURIComponent(path.slice("/api/internal/recordings/".length));
    const recording = await env.DB.prepare(
      "SELECT username, filename FROM recordings WHERE drive_id = ?",
    ).bind(driveId).first();
    if (!recording) return json({ ok: true, skipped: true }, 202);
    await env.DB.prepare("DELETE FROM recordings WHERE drive_id = ?").bind(driveId).run();
    await addActivity(env, recording.username, `Deleted recording ${recording.filename}`);
    return json({ ok: true });
  }
  if (path === "/api/internal/status" && request.method === "POST") {
    return internalStatus(request, env);
  }
  if (path === "/api/logout" && request.method === "POST") {
    requireSameOrigin(request);
    return json({ ok: true }, 200, {
      "Set-Cookie": "lv_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0",
    });
  }
  if (!path.startsWith("/api/")) throw new HttpError(404, "Not found");
  if (!(await validSession(request, env.DASHBOARD_SESSION_SECRET))) {
    throw new HttpError(401, "Please log in");
  }

  if (path === "/api/diagnostics" && request.method === "GET") {
    return diagnostics(env);
  }
  if (path === "/api/accounts" && request.method === "GET") {
    await initializeAccounts(env);
    return json({ accounts: await accountList(env) });
  }
  if (path === "/api/accounts" && request.method === "POST") {
    requireSameOrigin(request);
    const body = await readJson(request);
    const username = trackedUsername(body.username);
    const existing = await env.DB.prepare(
      `SELECT accounts.active, account_status.checked_at
       FROM accounts LEFT JOIN account_status USING (username)
       WHERE accounts.username = ?`,
    ).bind(username).first();
    await env.DB.prepare(
      `INSERT INTO accounts (username, active) VALUES (?, 1)
       ON CONFLICT(username) DO UPDATE SET active = 1`,
    )
      .bind(username).run();
    await syncTracked(env);
    const checkedAt = existing?.checked_at ? Date.parse(existing.checked_at) : NaN;
    const statusIsStale = !Number.isFinite(checkedAt) || Date.now() - checkedAt >= 5 * 60_000;
    const shouldCheckNow = !existing || existing.active !== 1 || statusIsStale;
    if (shouldCheckNow) {
      try {
        await queueManualMonitor(env, username);
      } catch (error) {
        const detail = error instanceof Error ? error.message : "GitHub API request failed";
        throw new HttpError(
          error instanceof HttpError ? error.status : 502,
          `Account was saved, but its immediate status check could not be queued: ${detail}`,
        );
      }
    }
    return json({ ok: true, check_queued: shouldCheckNow }, 201);
  }
  if (path === "/api/monitor" && request.method === "POST") {
    requireSameOrigin(request);
    const body = await readJson(request);
    const username = body.username === undefined || body.username === null || body.username === ""
      ? null
      : trackedUsername(body.username);
    if (username) {
      const account = await env.DB.prepare(
        "SELECT active FROM accounts WHERE username = ?",
      ).bind(username).first();
      if (!account || account.active !== 1) {
        throw new HttpError(404, "Active tracked account was not found");
      }
    }
    await queueManualMonitor(env, username);
    return json({ ok: true, queued: true, username }, 202);
  }
  if (path.startsWith("/api/accounts/") && request.method === "PATCH") {
    requireSameOrigin(request);
    const username = trackedUsername(decodeURIComponent(path.slice("/api/accounts/".length)));
    const body = await readJson(request);
    if (typeof body.active !== "boolean") throw new HttpError(400, "Active must be true or false");
    const result = await env.DB.prepare("UPDATE accounts SET active = ? WHERE username = ?")
      .bind(body.active ? 1 : 0, username).run();
    if (!result.meta.changes) throw new HttpError(404, "Account was not found");
    await syncTracked(env);
    return json({ ok: true });
  }
  if (path.startsWith("/api/accounts/") && request.method === "DELETE") {
    requireSameOrigin(request);
    const username = trackedUsername(decodeURIComponent(path.slice("/api/accounts/".length)));
    const result = await env.DB.prepare("DELETE FROM accounts WHERE username = ?")
      .bind(username).run();
    if (!result.meta.changes) throw new HttpError(404, "Account was not found");
    await syncTracked(env);
    return json({ ok: true });
  }
  if (path === "/api/recordings" && request.method === "GET") {
    const rows = await env.DB.prepare(
      `SELECT drive_id, username, room_id, started_at, part_number, filename, drive_url, file_size,
              duration_seconds, created_at
       FROM recordings
       ORDER BY started_at DESC, username ASC, part_number ASC, created_at ASC
       LIMIT 200`,
    ).all();
    const recordings = rows.results || [];
    return json({
      recordings,
      missing_durations: recordings.filter((recording) => recording.duration_seconds <= 0).length,
    });
  }
  if (path === "/api/recordings/backfill-duration" && request.method === "POST") {
    requireSameOrigin(request);
    await dispatchDriveMaintenance(env, "backfill-durations");
    return json({ ok: true, queued: true }, 202);
  }
  if (path.startsWith("/api/recordings/") && request.method === "DELETE") {
    requireSameOrigin(request);
    const driveId = decodeURIComponent(path.slice("/api/recordings/".length));
    const recording = await env.DB.prepare(
      "SELECT drive_id FROM recordings WHERE drive_id = ?",
    ).bind(driveId).first();
    if (!recording) throw new HttpError(404, "Recording was not found");
    await dispatchDriveMaintenance(env, "delete", driveId);
    return json({ ok: true, queued: true }, 202);
  }
  if (path === "/api/activity" && request.method === "GET") {
    const rows = await env.DB.prepare(
      `SELECT username, message, created_at FROM activity_logs
       ORDER BY id DESC LIMIT 50`,
    ).all();
    return json({ events: rows.results || [] });
  }
  throw new HttpError(404, "Not found");
}

export async function onRequest({ request, env }) {
  try {
    return await handle(request, env);
  } catch (error) {
    if (error instanceof HttpError) return json({ error: error.message }, error.status);
    console.error("LIVEVAULT dashboard request failed", error);
    return json({ error: "Request failed; check the Cloudflare Worker logs" }, 500);
  }
}
