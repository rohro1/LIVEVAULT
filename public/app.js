const loginPanel = document.querySelector("#login-panel");
const dashboard = document.querySelector("#dashboard");
const notice = document.querySelector("#notice");
const accountsList = document.querySelector("#accounts");
const recordingsBody = document.querySelector("#recordings");
const activityList = document.querySelector("#activity");
const logoutButton = document.querySelector("#logout");
const diagnosticLog = document.querySelector("#diagnostic-log");
const diagnosticConnection = document.querySelector("#diagnostic-connection");
const checkAllButton = document.querySelector("#check-all");
const backfillDurationsButton = document.querySelector("#backfill-durations");
const organizeRecordingsButton = document.querySelector("#organize-recordings");
const recordingsRefreshButton = document.querySelector("#refresh");
const recordingFilters = [...document.querySelectorAll("[data-recording-filter]")];
const recordingCount = document.querySelector("#recording-count");
const recordingsPageLabel = document.querySelector("#recordings-page");
const recordingsPagination = document.querySelector("#recordings-pagination");
const recordingFilterAccount = document.querySelector("#recording-filter-account");
let cachedDiagnostics = null;
let diagnosticsFetchedAt = 0;
let recordingsRequestId = 0;
let recordingFilterTimer = null;
let recordingPageState = { page: 1, pages: 0, total: 0 };
const DIAGNOSTICS_REFRESH_MS = 120_000;
const ACCOUNT_STATUS_STALE_MS = 7 * 60_000;

function showNotice(message, error = false) {
  notice.textContent = message;
  notice.classList.toggle("error", error);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(result.error || `Request failed (${response.status})`);
  }
  return result;
}

function showLogin() {
  loginPanel.classList.remove("hidden");
  dashboard.classList.add("hidden");
  logoutButton.classList.add("hidden");
}

function showDashboard() {
  loginPanel.classList.add("hidden");
  dashboard.classList.remove("hidden");
  logoutButton.classList.remove("hidden");
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "Unavailable";
  const totalSeconds = Math.round(seconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;
  return hours
    ? `${hours}h ${minutes}m ${remainder}s`
    : minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function formatElapsedTime(startedAt, now = Date.now()) {
  const start = Date.parse(startedAt);
  if (!Number.isFinite(start)) return "00:00:00";
  const elapsedSeconds = Math.max(0, Math.floor((now - start) / 1000));
  const hours = Math.floor(elapsedSeconds / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  const seconds = elapsedSeconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function updateRecordingElapsed() {
  for (const timer of document.querySelectorAll("[data-recording-start]")) {
    const checkedAt = Date.parse(timer.dataset.recordingCheckedAt);
    if (!Number.isFinite(checkedAt) || Date.now() - checkedAt > ACCOUNT_STATUS_STALE_MS) {
      timer.textContent = "status stale";
      timer.setAttribute("aria-label", "Recording status is stale");
      continue;
    }
    timer.textContent = formatElapsedTime(timer.dataset.recordingStart);
    timer.setAttribute("aria-label", `Recording for ${timer.textContent}`);
  }
}

function renderAccounts(accounts) {
  accountsList.replaceChildren();
  for (const account of accounts) {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const name = document.createElement("a");
    name.href = `https://www.tiktok.com/@${encodeURIComponent(account.username)}`;
    name.target = "_blank";
    name.rel = "noopener noreferrer";
    name.textContent = account.username;
    const status = document.createElement("span");
    status.className = "account-status";
    const online = document.createElement("span");
    const hasChecked = Boolean(account.checked_at);
    const checkedAt = hasChecked ? Date.parse(account.checked_at) : NaN;
    const stale = hasChecked && (!Number.isFinite(checkedAt) ||
      Date.now() - checkedAt > ACCOUNT_STATUS_STALE_MS);
    const statusClass = !account.active || !hasChecked || stale || account.online === null
      ? "unknown"
      : account.online ? "online" : "offline";
    online.className = `status-pill ${statusClass}`;
    online.textContent = !account.active
      ? "Paused"
      : !hasChecked
        ? "Not checked"
        : stale
          ? "Not verified (stale)"
          : account.online === true
            ? "Online"
            : account.online === false
              ? "Offline"
              : "Not verified";
    online.title = !account.active
      ? "Monitoring is paused for this account."
      : !hasChecked
        ? "No status check has completed yet."
        : stale
          ? "The last status check is older than seven minutes; wait for or request a fresh check."
          : account.online === null
            ? "TikTok did not provide enough reliable data to confirm Online or Offline."
            : account.online
              ? "TikTok returned a usable live stream, confirming this account is Online."
              : "TikTok returned an explicit offline status.";
    const recording = document.createElement("span");
    recording.className = `status-pill ${account.recorder_status === "recording" ? "recording" : ""}`;
    recording.textContent = account.recorder_status === "recording"
      ? "Recording"
      : account.recorder_status === "queued"
        ? "Starting"
        : account.recorder_status === "error"
          ? "Error"
          : account.active ? "Watching" : "Paused";
    if (account.active && account.online === true && !stale &&
        account.recorder_status === "recording" && account.recording_started_at) {
      const elapsed = document.createElement("time");
      elapsed.className = "recording-elapsed";
      elapsed.dateTime = account.recording_started_at;
      elapsed.dataset.recordingStart = account.recording_started_at;
      elapsed.dataset.recordingCheckedAt = account.checked_at;
      elapsed.setAttribute("aria-label", "Recording duration");
      recording.append(" ", elapsed);
    }
    status.append(online, recording);
    if (account.checked_at) {
      const checked = document.createElement("small");
      checked.className = "checked-at";
      checked.textContent = `Checked ${new Date(account.checked_at).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/New_York",
      })} ET${stale ? " (stale)" : ""}`;
      status.append(checked);
    }
    details.append(name, status);

    const lastActive = document.createElement("div");
    lastActive.className = "account-last-active";
    if (account.last_online_at) {
      const lastActiveTime = document.createElement("time");
      lastActiveTime.dateTime = account.last_online_at;
      lastActiveTime.textContent = new Date(account.last_online_at).toLocaleString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/New_York",
        timeZoneName: "short",
      });
      lastActive.append(lastActiveTime);
    } else {
      lastActive.textContent = "Not confirmed online yet";
    }

    const actions = document.createElement("div");
    actions.className = "row-actions";
    const toggle = document.createElement("button");
    toggle.className = "secondary";
    toggle.type = "button";
    toggle.textContent = account.active ? "Pause" : "Resume";
    toggle.dataset.action = "toggle";
    toggle.dataset.username = account.username;
    toggle.dataset.active = String(!account.active);
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.type = "button";
    remove.textContent = "Remove";
    remove.dataset.action = "remove";
    remove.dataset.username = account.username;
    const check = document.createElement("button");
    check.className = "secondary";
    check.type = "button";
    check.textContent = "Check now";
    check.dataset.action = "check";
    check.dataset.username = account.username;
    check.disabled = !account.active;
    actions.append(check, toggle, remove);
    row.append(details, lastActive, actions);
    accountsList.append(row);
  }
}

function renderActivity(events) {
  activityList.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "No activity yet. Status updates will appear after the monitor runs.";
    activityList.append(empty);
    return;
  }
  for (const event of events) {
    const row = document.createElement("li");
    const message = document.createElement("span");
    message.textContent = event.username
      ? `${event.username}: ${event.message}`
      : event.message;
    const time = document.createElement("time");
    time.dateTime = event.created_at;
    time.textContent = new Date(event.created_at).toLocaleString([], {
      hour: "numeric",
      minute: "2-digit",
      month: "short",
      day: "numeric",
      timeZone: "America/New_York",
    });
    row.append(message, time);
    activityList.append(row);
  }
}

function formatUploadedTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "America/New_York",
  });
}

function recordingUrl() {
  const params = new URLSearchParams({
    page: String(recordingPageState.page),
    sort: recordingFilters.find((filter) => filter.name === "sort").value,
  });
  for (const filter of recordingFilters) {
    if (filter.name === "sort") continue;
    const value = filter.value.trim();
    if (value) params.set(filter.name, value);
  }
  return `/api/recordings?${params}`;
}

function renderRecordings(data) {
  recordingsBody.replaceChildren();
  recordingPageState = {
    page: data.page,
    pages: data.pages,
    total: data.total,
  };
  const selectedAccount = recordingFilterAccount.value;
  recordingFilterAccount.replaceChildren(new Option("All accounts", ""));
  for (const username of data.usernames) {
    recordingFilterAccount.add(new Option(username, username));
  }
  recordingFilterAccount.value = data.usernames.includes(selectedAccount) ? selectedAccount : "";

  if (!data.recordings.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "empty-state";
    cell.textContent = data.total
      ? "No recordings match these filters."
      : "No verified recordings yet.";
    row.append(cell);
    recordingsBody.append(row);
  }
  for (const recording of data.recordings) {
    const row = document.createElement("tr");
    const started = recording.started_at;
    const formatted = started.length === 15
      ? `${started.slice(0, 4)}-${started.slice(4, 6)}-${started.slice(6, 8)} ${started.slice(9, 11)}:${started.slice(11, 13)}:${started.slice(13, 15)}`
      : started;
    for (const value of [recording.username, formatted, formatUploadedTime(recording.created_at)]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const partCell = document.createElement("td");
    const partLink = document.createElement("a");
    partLink.href = recording.drive_url;
    partLink.target = "_blank";
    partLink.rel = "noopener noreferrer";
    partLink.textContent = `part${String(recording.part_number).padStart(3, "0")}`;
    partCell.append(partLink);
    row.append(partCell);
    const sizeCell = document.createElement("td");
    sizeCell.textContent = formatBytes(recording.file_size);
    row.append(sizeCell);
    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = recording.drive_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open";
    linkCell.append(link);
    row.append(linkCell);
    const durationCell = document.createElement("td");
    durationCell.textContent = formatDuration(recording.duration_seconds);
    if (durationCell.textContent === "Unavailable") {
      durationCell.title = "Use Fetch missing durations to recover it from Google Drive.";
    }
    row.append(durationCell);
    const actionsCell = document.createElement("td");
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.dataset.action = "delete-recording";
    remove.dataset.driveId = recording.drive_id;
    remove.dataset.filename = recording.filename;
    actionsCell.append(remove);
    row.append(actionsCell);
    recordingsBody.append(row);
  }
  const firstItem = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const lastItem = Math.min(data.page * data.page_size, data.total);
  recordingCount.textContent = data.total
    ? `Showing ${firstItem}–${lastItem} of ${data.total}`
    : "0 recordings";
  recordingsPageLabel.textContent = `Page ${data.page} of ${Math.max(1, data.pages)}`;
  for (const button of recordingsPagination.querySelectorAll("button[data-page-action]")) {
    button.disabled = button.dataset.pageAction === "first" ||
      button.dataset.pageAction === "previous"
      ? data.page <= 1
      : data.page >= data.pages;
  }
  backfillDurationsButton.classList.toggle("hidden", data.missing_durations === 0);
  backfillDurationsButton.disabled = data.missing_durations === 0;
  backfillDurationsButton.textContent =
    `Fetch ${data.missing_durations} missing duration(s)`;
}

function diagnosticLine(container, message, state = "ok", url = "") {
  const line = document.createElement("div");
  line.className = state;
  if (url) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = message;
    line.append(link);
  } else {
    line.textContent = message;
  }
  container.append(line);
}

function renderDiagnostics(data, events) {
  diagnosticLog.replaceChildren();
  const timestamp = new Date(data.generated_at).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
  diagnosticLine(diagnosticLog, `[${timestamp}] Diagnostics refreshed`);
  diagnosticLine(diagnosticLog, `[${timestamp}] Cloudflare Pages Function: responding`, "ok");
  let monitorOverdue = false;
  let recentWorkflowFailure = false;

  if (data.d1.status === "ok") {
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] D1: connected; ${data.d1.accounts} accounts; ${data.d1.recordings} verified recordings`,
    );
    if (data.d1.last_account_check) {
      const ageMinutes = Math.max(
        0,
        Math.floor((Date.now() - new Date(data.d1.last_account_check).getTime()) / 60_000),
      );
      diagnosticLine(
        diagnosticLog,
        `[${timestamp}] Last account status update: ${ageMinutes} minute(s) ago`,
        ageMinutes > 10 ? "warn" : "ok",
      );
    } else {
      diagnosticLine(diagnosticLog, `[${timestamp}] No account status check has reached D1 yet`, "warn");
    }
  } else {
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] D1 error: ${data.d1.message || "database unavailable"}`,
      "error",
    );
  }

  const integrations = [
    ["GitHub PAT", data.integrations.github_pat],
    ["GitHub repository", data.integrations.github_repository],
    ["Dashboard ingest token", data.integrations.dashboard_ingest_token],
    ["Host key secret", data.integrations.host_key_secret],
  ];
  for (const [name, configured] of integrations) {
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] ${name}: ${configured ? "configured" : "MISSING"}`,
      configured ? "ok" : "error",
    );
  }
  if (data.tracking_sync.status === "synced") {
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] Tracked accounts: dashboard and GitHub agree (${data.tracking_sync.dashboard_accounts})`,
    );
  } else {
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] Tracked account sync: ${data.tracking_sync.message ||
        `mismatch (dashboard ${data.tracking_sync.dashboard_accounts}; GitHub ${data.tracking_sync.github_accounts})`}`,
      data.tracking_sync.status === "not_checked" ? "warn" : "error",
    );
  }
  const verifiedUploads = data.d1.status === "ok" ? data.d1.recordings : 0;
  diagnosticLine(
    diagnosticLog,
    `[${timestamp}] Google Drive OAuth: stored in GitHub Actions and not inspectable here; ${verifiedUploads} verified upload(s) recorded`,
    verifiedUploads ? "ok" : "warn",
  );

  for (const [name, workflow] of Object.entries(data.workflows)) {
    if (workflow.status !== "ok") {
      diagnosticLine(
        diagnosticLog,
        `[${timestamp}] ${name} workflow status unavailable: ${workflow.message || "GitHub credentials missing"}; grant Actions: read to GITHUB_PAT to inspect runs`,
        "warn",
      );
      continue;
    }
    if (!workflow.runs.length) {
      diagnosticLine(diagnosticLog, `[${timestamp}] ${name} workflow: no runs found`, "warn");
      continue;
    }
    for (const run of workflow.runs) {
      const outcome = run.status === "completed" ? run.conclusion || "unknown" : run.status;
      const state = outcome === "success" || outcome === "in_progress" || outcome === "queued"
        ? "ok"
        : "error";
      const runTime = Date.parse(run.updated_at || run.created_at);
      if (state === "error" && Date.now() - runTime <= 24 * 60 * 60_000) {
        recentWorkflowFailure = true;
      }
      diagnosticLine(
        diagnosticLog,
        `[${new Date(run.updated_at || run.created_at).toLocaleTimeString()}] GitHub ${name}: ${outcome}`,
        state,
        run.url,
      );
    }
    if (name === "monitor") {
      const recentRuns = workflow.runs
        .map((run) => Date.parse(run.created_at))
        .filter((runTime) => Number.isFinite(runTime) && Date.now() - runTime <= 24 * 60 * 60_000)
        .sort((left, right) => left - right);
      const lastRun = recentRuns.at(-1);
      const ageMinutes = lastRun === undefined
        ? null
        : Math.max(0, Math.floor((Date.now() - lastRun) / 60_000));
      if (ageMinutes === null) {
        monitorOverdue = true;
        diagnosticLine(
          diagnosticLog,
          `[${timestamp}] WARNING: no monitor workflow run was found in the last 24 hours`,
          "error",
        );
      } else if (ageMinutes > 7) {
        monitorOverdue = ageMinutes > 12;
        diagnosticLine(
          diagnosticLog,
          `[${timestamp}] WARNING: latest monitor run was ${ageMinutes} minute(s) ago; expected about every 5 minutes`,
          ageMinutes > 12 ? "error" : "warn",
        );
      }
      const largestGapMinutes = recentRuns.slice(1).reduce(
        (largest, runTime, index) =>
          Math.max(largest, (runTime - recentRuns[index]) / 60_000),
        0,
      );
      if (largestGapMinutes > 10) {
        diagnosticLine(
          diagnosticLog,
          `[${timestamp}] WARNING: a recent gap between monitor runs was ${Math.round(largestGapMinutes)} minutes`,
          largestGapMinutes > 20 ? "error" : "warn",
        );
        if (largestGapMinutes > 20) monitorOverdue = true;
      }
    }
  }

  const rateLimits = Object.values(data.workflows)
    .map((workflow) => workflow.rate_limit)
    .filter((rate) => rate && Number.isFinite(rate.remaining) && Number.isFinite(rate.limit));
  if (rateLimits.length) {
    const rate = rateLimits.reduce((lowest, current) =>
      current.remaining < lowest.remaining ? current : lowest
    );
    const percent = Math.floor((rate.remaining / rate.limit) * 100);
    diagnosticLine(
      diagnosticLog,
      `[${timestamp}] GitHub API core rate limit: ${rate.remaining}/${rate.limit} remaining (${percent}%)`,
      percent <= 10 ? "error" : percent <= 20 ? "warn" : "ok",
    );
    if (Number.isFinite(rate.reset_at)) {
      const resetTime = new Date(rate.reset_at * 1000).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
      diagnosticLine(diagnosticLog, `[${timestamp}] GitHub API rate limit resets at ${resetTime}`);
    }
    if (percent <= 20) {
      diagnosticLine(
        diagnosticLog,
        `[${timestamp}] Reduce manual checks while the GitHub API limit recovers.`,
        "warn",
      );
    }
  }

  for (const event of events.slice(0, 20)) {
    const time = new Date(event.created_at).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
    diagnosticLine(
      diagnosticLog,
      `[${time}] ${event.username ? `${event.username}: ` : ""}${event.message}`,
      /failed|error|offline/i.test(event.message) ? "warn" : "ok",
    );
  }
  diagnosticLog.scrollTop = 0;
  const failed = data.d1.status !== "ok" ||
    Object.values(data.integrations).some((configured) => configured === false) ||
    data.tracking_sync.status === "mismatch" ||
    data.tracking_sync.status === "error" ||
    Object.values(data.workflows).some((workflow) => workflow.status !== "ok") ||
    recentWorkflowFailure ||
    monitorOverdue ||
    (data.d1.last_account_check &&
      Date.now() - new Date(data.d1.last_account_check).getTime() > 12 * 60_000);
  diagnosticConnection.textContent = failed ? "Issues detected" : "Connected";
  diagnosticConnection.className = `diagnostic-state ${failed ? "error" : "online"}`;
}

async function fetchRecordingPage() {
  const requestId = ++recordingsRequestId;
  const data = await api(recordingUrl());
  if (requestId === recordingsRequestId) renderRecordings(data);
}

async function refreshRecordingPage() {
  try {
    await fetchRecordingPage();
    if (notice.classList.contains("error")) showNotice("");
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function loadDashboard(quiet = false) {
  const shouldRefreshDiagnostics = !cachedDiagnostics ||
    Date.now() - diagnosticsFetchedAt >= DIAGNOSTICS_REFRESH_MS;
  const [accounts, , activity, diagnostics] = await Promise.all([
    api("/api/accounts"),
    fetchRecordingPage(),
    api("/api/activity"),
    shouldRefreshDiagnostics
      ? api("/api/diagnostics")
      : Promise.resolve(cachedDiagnostics),
  ]);
  renderAccounts(accounts.accounts);
  renderActivity(activity.events);
  cachedDiagnostics = diagnostics;
  if (shouldRefreshDiagnostics) diagnosticsFetchedAt = Date.now();
  renderDiagnostics(cachedDiagnostics.diagnostics, activity.events);
  showDashboard();
  if (!quiet) showNotice("");
}

for (const filter of recordingFilters) {
  const eventName = filter.type === "search" || filter.type === "number" ? "input" : "change";
  filter.addEventListener(eventName, () => {
    recordingPageState.page = 1;
    if (eventName === "input") {
      clearTimeout(recordingFilterTimer);
      recordingFilterTimer = setTimeout(refreshRecordingPage, 250);
    } else {
      refreshRecordingPage();
    }
  });
}

document.querySelector("#clear-recording-filters").addEventListener("click", () => {
  for (const filter of recordingFilters) {
    filter.value = filter.name === "sort" ? "uploaded_desc" : "";
  }
  recordingPageState.page = 1;
  refreshRecordingPage();
});

recordingsPagination.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-page-action]");
  if (!button || button.disabled) return;
  const lastPage = Math.max(1, recordingPageState.pages);
  const pageByAction = {
    first: 1,
    previous: recordingPageState.page - 1,
    next: recordingPageState.page + 1,
    last: lastPage,
  };
  recordingPageState.page = pageByAction[button.dataset.pageAction];
  refreshRecordingPage();
});

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const password = new FormData(form).get("password");
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    form.reset();
    await loadDashboard();
  } catch (error) {
    showNotice(error.message, true);
  }
});

document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const username = new FormData(form).get("username");
  try {
    const result = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ username }),
    });
    form.reset();
    await loadDashboard();
    showNotice(result.check_queued
      ? "Account added; an immediate status check is queued."
      : "Account is already being watched.");
  } catch (error) {
    try {
      await loadDashboard(true);
    } catch (refreshError) {
      showNotice(`${error.message}; dashboard refresh failed: ${refreshError.message}`, true);
      return;
    }
    showNotice(error.message, true);
  }
});

accountsList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  try {
    if (button.dataset.action === "check") {
      await api("/api/monitor", {
        method: "POST",
        body: JSON.stringify({ username: button.dataset.username }),
      });
      await loadDashboard(true);
      showNotice(`Status check queued for ${button.dataset.username}.`);
    } else if (button.dataset.action === "toggle") {
      await api(`/api/accounts/${encodeURIComponent(button.dataset.username)}`, {
        method: "PATCH",
        body: JSON.stringify({ active: button.dataset.active === "true" }),
      });
    } else {
      if (!window.confirm(`Remove @${button.dataset.username} from LIVEVAULT? This stops monitoring. Existing Drive recordings are kept.`)) {
        return;
      }
      await api(`/api/accounts/${encodeURIComponent(button.dataset.username)}`, {
        method: "DELETE",
      });
    }
    await loadDashboard();
    showNotice(button.dataset.action === "remove"
      ? "Account removed; any active recorder will stop shortly and upload its captured part. Existing Drive recordings are kept."
      : "Account list updated.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

checkAllButton.addEventListener("click", async () => {
  checkAllButton.disabled = true;
  try {
    await api("/api/monitor", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadDashboard(true);
    showNotice("Status check queued for all active accounts. Manual checks are limited to one per minute.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    checkAllButton.disabled = false;
  }
});

recordingsRefreshButton.addEventListener("click", async () => {
  try {
    await loadDashboard();
  } catch (error) {
    showNotice(error.message, true);
  }
});

backfillDurationsButton.addEventListener("click", async () => {
  backfillDurationsButton.disabled = true;
  try {
    await api("/api/recordings/backfill-duration", { method: "POST", body: "{}" });
    showNotice(
      "Duration lookup queued. Drive metadata and, if needed, the video itself will be checked; the table refreshes automatically.",
    );
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    backfillDurationsButton.disabled = false;
  }
});

organizeRecordingsButton.addEventListener("click", async () => {
  organizeRecordingsButton.disabled = true;
  try {
    await api("/api/recordings/organize", { method: "POST", body: "{}" });
    showNotice(
      "Drive organization queued. Known recordings will be moved into LIVEVAULT / username / date folders; new uploads use that layout automatically.",
    );
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    organizeRecordingsButton.disabled = false;
  }
});

recordingsBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action='delete-recording']");
  if (!button) return;
  if (!window.confirm(`Permanently delete ${button.dataset.filename} from Google Drive and LIVEVAULT?`)) {
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/recordings/${encodeURIComponent(button.dataset.driveId)}`, {
      method: "DELETE",
    });
    showNotice("Recording deletion queued. The row will disappear after Google Drive confirms deletion.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showLogin();
    showNotice("Signed out.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

loadDashboard().catch((error) => {
  if (error.message !== "Please log in") showNotice(error.message, true);
});

window.setInterval(() => {
  if (!dashboard.classList.contains("hidden")) {
    loadDashboard(true).catch((error) => showNotice(error.message, true));
  }
}, 30_000);

window.setInterval(updateRecordingElapsed, 1_000);
