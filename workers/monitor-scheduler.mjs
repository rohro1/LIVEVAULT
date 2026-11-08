const API = "https://api.github.com";
const WORKFLOW = "monitor.yml";

function validateEnvironment(env) {
  if (!env.MONITOR_GITHUB_PAT) {
    throw new Error("MONITOR_GITHUB_PAT is not configured");
  }
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY || "")) {
    throw new Error("GITHUB_REPOSITORY is not configured correctly");
  }
  if (!/^[A-Za-z0-9._/-]+$/.test(env.GITHUB_REF || "")) {
    throw new Error("GITHUB_REF is not configured correctly");
  }
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.MONITOR_GITHUB_PAT}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "LIVEVAULT-monitor-scheduler",
  };
}

export async function dispatchMonitor(env, request = fetch) {
  validateEnvironment(env);

  const workflowUrl =
    `${API}/repos/${env.GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}`;
  const runsResponse = await request(`${workflowUrl}/runs?per_page=100`, {
    headers: githubHeaders(env),
  });
  if (!runsResponse.ok) {
    throw new Error(
      `GitHub could not check monitor runs (HTTP ${runsResponse.status})`,
    );
  }

  const { workflow_runs: runs = [] } = await runsResponse.json();
  const activeRun = runs.find((run) =>
    ["queued", "in_progress", "waiting", "requested", "pending"].includes(run.status)
  );
  if (activeRun) {
    console.log(`Monitor run already ${activeRun.status}; skipping this tick`);
    return { dispatched: false, reason: "active_run" };
  }

  const dispatchResponse = await request(`${workflowUrl}/dispatches`, {
    method: "POST",
    headers: {
      ...githubHeaders(env),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF }),
  });
  if (!dispatchResponse.ok) {
    throw new Error(
      `GitHub monitor dispatch failed (HTTP ${dispatchResponse.status})`,
    );
  }

  console.log("Monitor workflow dispatched");
  return { dispatched: true };
}

export async function runScheduled(env, request = fetch) {
  await dispatchMonitor(env, request);
}

export default {
  async scheduled(_event, env) {
    await runScheduled(env);
  },
};
