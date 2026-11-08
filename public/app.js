const loginPanel = document.querySelector("#login-panel");
const dashboard = document.querySelector("#dashboard");
const notice = document.querySelector("#notice");
const accountsList = document.querySelector("#accounts");
const recordingsBody = document.querySelector("#recordings");
const activityList = document.querySelector("#activity");
const logoutButton = document.querySelector("#logout");

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

function renderAccounts(accounts) {
  accountsList.replaceChildren();
  for (const account of accounts) {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = account.username;
    const status = document.createElement("span");
    status.className = "account-status";
    const online = document.createElement("span");
    online.className = `status-pill ${!account.active ? "unknown" : account.online === true ? "online" : account.online === false ? "offline" : "unknown"}`;
    online.textContent = !account.active
      ? "Paused"
      : account.online === true
        ? "Online"
        : account.online === false
          ? "Offline"
          : "Not checked";
    const recording = document.createElement("span");
    recording.className = `status-pill ${account.recorder_status === "recording" ? "recording" : ""}`;
    recording.textContent = account.recorder_status === "recording"
      ? "Recording"
      : account.recorder_status === "queued"
        ? "Starting"
        : account.recorder_status === "error"
          ? "Error"
          : account.active ? "Watching" : "Paused";
    status.append(online, recording);
    if (account.checked_at) {
      const checked = document.createElement("small");
      checked.className = "checked-at";
      checked.textContent = `Checked ${new Date(account.checked_at).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/New_York",
      })} ET`;
      status.append(checked);
    }
    details.append(name, status);

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
    actions.append(toggle, remove);
    row.append(details, actions);
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

function renderRecordings(recordings) {
  recordingsBody.replaceChildren();
  for (const recording of recordings) {
    const row = document.createElement("tr");
    const started = recording.started_at;
    const formatted = started.length === 15
      ? `${started.slice(0, 4)}-${started.slice(4, 6)}-${started.slice(6, 8)} ${started.slice(9, 11)}:${started.slice(11, 13)}:${started.slice(13, 15)}`
      : started;
    for (const value of [
      recording.username,
      formatted,
      `part${String(recording.part_number).padStart(3, "0")}`,
      formatBytes(recording.file_size),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = recording.drive_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open";
    linkCell.append(link);
    row.append(linkCell);
    recordingsBody.append(row);
  }
}

async function loadDashboard(quiet = false) {
  const [accounts, recordings, activity] = await Promise.all([
    api("/api/accounts"),
    api("/api/recordings"),
    api("/api/activity"),
  ]);
  renderAccounts(accounts.accounts);
  renderRecordings(recordings.recordings);
  renderActivity(activity.events);
  showDashboard();
  if (!quiet) showNotice("");
}

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
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ username }) });
    form.reset();
    await loadDashboard();
    showNotice("Account added.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

accountsList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  try {
    if (button.dataset.action === "toggle") {
      await api(`/api/accounts/${encodeURIComponent(button.dataset.username)}`, {
        method: "PATCH",
        body: JSON.stringify({ active: button.dataset.active === "true" }),
      });
    } else {
      await api(`/api/accounts/${encodeURIComponent(button.dataset.username)}`, {
        method: "DELETE",
      });
    }
    await loadDashboard();
    showNotice("Account list updated.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

document.querySelector("#refresh").addEventListener("click", async () => {
  try {
    await loadDashboard();
  } catch (error) {
    showNotice(error.message, true);
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
