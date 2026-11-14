# LIVEVAULT

LIVEVAULT contains a GitHub Actions recording engine and a Cloudflare Pages
dashboard. The engine checks tracked TikTok accounts every 5 minutes, records
host-based live streams, saves numbered 30- or 60-minute MP4 parts to Google
Drive, and hands long recordings to a new Actions job before the six-hour job
limit.

The dashboard code must be deployed and configured before it is reachable.
A guest-only appearance is not detected or recorded unless the live's host is
tracked. TikTok endpoints are unofficial and may change; neither detection nor
uninterrupted recording is guaranteed.
Scheduled-workflow keep-alive is not implemented. Recorder failures are reported
to the dashboard when its Actions-to-dashboard integration is configured.
Recordings and stream URLs are never uploaded to Actions artifacts or releases.
Review GitHub's current Actions terms before using the repository for continuous
recording.

## Configure GitHub

In the repository's **Settings → Secrets and variables → Actions**, add:

**Optional repository variable**

- `SEGMENT_MINUTES`: `30` or `60` (optional; defaults to `30`).

Do not maintain `TRACKED` manually. The dashboard adds and updates it when you
manage usernames on the website. Existing `TRACKED` names are imported the
first time the dashboard opens.

**Repository secrets**

- `HOST_KEY_SECRET`: generate a random secret in the Codespace terminal:

  ```sh
  python3 -c 'import secrets; print(secrets.token_hex(32))'
  ```

  Copy the output directly to the GitHub secret; do not commit or paste it into
  chat. It lets workflows refer to tracked accounts using opaque keys instead
  of exposing usernames in workflow-dispatch inputs.
- `DRIVE_CLIENT_ID`
- `DRIVE_CLIENT_SECRET`
- `DRIVE_REFRESH_TOKEN`

**Simplest Google credential location:** on GitHub, open
**rohro1/LIVEVAULT → Settings → Secrets and variables → Actions → New
repository secret**. Add the three names above there, one at a time, and paste
each matching value from Google Cloud/OAuth Playground. Do not put these
credentials in the website or Cloudflare.

`GITHUB_TOKEN` is supplied by GitHub Actions. The workflows require Actions
read/write permission so the monitor can start recorder jobs and recorder jobs
can hand off to their successors.

## Configure Google Drive OAuth

1. In Google Cloud Console, create/select a project and enable the Google Drive
   API.
2. Configure Google Auth Platform consent and add the Google account that owns
   the destination Drive as a test user.
3. Use the `https://www.googleapis.com/auth/drive.file` scope. Google lists it
   as non-sensitive and it allows the app to create files it owns; LIVEVAULT
   verifies each newly-uploaded file by its returned ID.
4. In **Credentials → Create credentials → OAuth client ID**, create a **Web
   application** client and add
   `https://developers.google.com/oauthplayground` as an authorized redirect
   URI.
5. Open [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/),
   open its settings, enable **Use your own OAuth credentials**, and enter that
   client ID and secret. In Step 1, request only
   `https://www.googleapis.com/auth/drive.file`, authorize using the Google
   account that will own the recordings, and exchange the authorization code
   in Step 2. Copy the refresh token shown there. Using your own client
   credentials avoids OAuth Playground's separate 24-hour refresh-token
   revocation.
6. Store the client ID, client secret, and refresh token as the three GitHub
   Actions secrets above. Never commit OAuth credentials.
7. If the OAuth app remains in **Testing**, Google says authorizations for
   Drive scopes expire after seven days, including refresh tokens. That is not
   suitable for uninterrupted operation. For ongoing use, move the app to
   **Production** and complete any Google branding/verification requirements
   shown for your project.

Google's current guidance:
[OAuth app audience and publishing status](https://support.google.com/cloud/answer/15549945),
[Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[OAuth refresh tokens](https://developers.google.com/identity/protocols/oauth2/web-server).

## Optional quiet hours

In the same Actions variables page, set:

- `QUIET_HOURS_ENABLED` to `true`
- `QUIET_START` and `QUIET_END` to local 24-hour times such as `04:00` and
  `08:00`

Times are interpreted in `America/New_York` and follow EST/EDT. Quiet hours are
off unless explicitly enabled. Active recordings stop at quiet-hour start;
recording jobs recheck the tracked list and quiet-hours setting while running.

## Start and verify

After the repository variables and secrets are set, enable GitHub Actions. The
**Live monitor** workflow is scheduled every 5 minutes, GitHub Actions'
minimum scheduled interval (GitHub may delay scheduled starts); it can also be
started from **Actions → Live monitor → Run workflow**. Each detected live host
gets its own recorder job; jobs for different hosts can run concurrently, while
duplicate jobs for the same host are serialized. GitHub may queue jobs when its
available runner concurrency is exhausted. If TikTok reports a live before
stream options are available, the resolver retries for up to 50 seconds.
Each running recorder checks the current dashboard account list every five
seconds. Removing an account logs the stop request; its active capture is
interrupted, any captured part is finalized and uploaded, and the recorder logs
that it stopped. A queued recorder for a removed account exits before capture.
Repeated recorder failures use a per-account retry backoff that never exceeds
the 5-minute monitor interval, without delaying recordings for other accounts.
Successful
parts are uploaded to the Drive account used for OAuth and deleted from runner
disk only after Drive confirms the uploaded file ID, name, and size.
New uploads are placed in `LIVEVAULT/<username>/<YYYY-MM-DD>` folders, with
dates based on the recording's America/New_York start date. Use **Organize Drive
folders** in the dashboard to move previously uploaded, dashboard-tracked files
into the same layout. The job verifies each Drive file against its dashboard
record before moving it; it does not scan or move unrelated Drive files.
Recording parts target 30 minutes by default. A part can be shorter if the live
ends or the stream disconnects; the dashboard displays each uploaded video's
measured duration so interruptions are visible. Parts are grouped by newest
recording session with their part numbers in ascending order.
The dashboard reports Online only when TikTok returns a usable live stream,
and Offline only when TikTok returns an explicit offline status. A possible-live
signal without stream data remains **Not verified** while the recorder checks
for a stream; failed checks do not preserve a stale Online/Offline result.
Results older than seven minutes are marked stale. Other tracked accounts are
still checked.
The recorder is shown as active only
after FFmpeg produces stream data; startup failures are reported with sanitized
diagnostics. Failed recorder jobs are marked as errors in the dashboard when
its API URL and token are configured; the activity message includes a
URL-sanitized failure reason when the recorder can identify one.
The dashboard's System diagnostics panel checks Cloudflare/D1 health, required
integration configuration, tracked-account synchronization, and recent GitHub
Actions runs. The dashboard must be deployed once after code changes for these
diagnostics and UI updates to appear; ongoing checks and status refreshes then
run automatically.
Adding a new account syncs the tracked list and queues an immediate monitor run;
the account-only refresh button checks just that account, while **Check all now**
checks all active accounts. Manual checks share a one-per-minute throttle to
limit Actions dispatches; scheduled checks remain on the normal five-minute
cadence. The dashboard warns when recent monitor intervals are over 10 minutes
or the GitHub API core budget falls below 20%. TikTok does not publish a stable
public API quota for these status endpoints, so use the manual refresh throttle
and avoid repeated refreshes if TikTok starts returning HTTP 429.
The Cloudflare
Pages `GITHUB_PAT` therefore needs repository **Variables: read and write** and
**Actions: read and write** permissions. Replace an expired or unauthorized
token there if dashboard diagnostics report HTTP 401.

The recordings table can queue permanent Drive deletion after confirmation.
It shows 15 recordings per page across the full history, with combined
per-column filters, global search, and sorting by either New York start time or
Drive upload time. The Uploaded column is the time the verified upload was
recorded by the dashboard.
**Fetch missing durations** uses Google Drive video metadata to backfill older
rows and probes the video with FFmpeg when Drive has not published a duration.
The recordings refresh updates the table as durations are saved; videos that
cannot be downloaded or probed remain marked unavailable.
These operations run through the `Manage Drive recordings` GitHub Actions
workflow so Google OAuth credentials stay in Actions, not Cloudflare Pages.

Cloudflare Cron dispatches the monitor every five minutes. The monitor workflow
does not also use GitHub's schedule, avoiding duplicate scans and runner usage.
Create a fine-grained GitHub token limited to this repository with
**Actions: read and write** permission. Store it as the separate Worker secret
`MONITOR_GITHUB_PAT` and deploy the scheduler once:

```sh
npx wrangler secret put MONITOR_GITHUB_PAT --config wrangler.monitor-scheduler.toml
npx wrangler deploy --config wrangler.monitor-scheduler.toml
```

The Worker checks for an active monitor run before dispatching another one.
Keep this token separate from the dashboard's `GITHUB_PAT`; it only needs
workflow read/dispatch access, not repository-variable access.

If the repository is public, workflow metadata and logs are public too. Monitor
and recorder diagnostic events include tracked usernames and UTC timestamps;
stream URLs and room IDs are omitted, and recorder job names use secret-keyed
opaque account identifiers. The destination Drive files remain private unless
their owner changes sharing settings.

## Deploy the dashboard

The dashboard uses Cloudflare Pages Functions and D1. It stores account
configuration and verified Drive links in D1. The first signed-in dashboard
load imports the existing `TRACKED` variable into D1; subsequent account
changes sync the active list back to GitHub.

1. **Codespace terminal:** create the D1 database and copy its `database_id`
   from the output:

   ```sh
   npx wrangler d1 create livevault
   ```

   Verify:

   ```sh
   npx wrangler d1 list
   ```

2. **Codespace editor:** replace `REPLACE_WITH_D1_DATABASE_ID` in
   `wrangler.toml` with that ID, then push the repository.
3. **Cloudflare website:** create a Pages project named `livevault-dashboard`
   connected to this repository. Leave the build command empty and set the
   output directory to `public`. Keep the D1 binding named `DB` and linked to
   the database ID from step 1.
4. **Codespace terminal:** apply the D1 schema:

   ```sh
   npx wrangler d1 execute livevault --remote --file=dashboard/schema.sql
   ```

   For an existing installation, apply the one-time D1 migrations before
   deploying the updated dashboard:

   ```sh
   npx wrangler d1 execute livevault --remote --file=dashboard/add-recording-duration.sql
   npx wrangler d1 execute livevault --remote --file=dashboard/add-monitor-manual-dispatches.sql
   npx wrangler d1 execute livevault --remote --file=dashboard/add-recording-started-at.sql
   ```

   Verify:

   ```sh
   npx wrangler d1 execute livevault --remote --command="SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
   ```

5. **Cloudflare website → Pages → livevault-dashboard → Settings → Variables
   and Secrets:** add these values. Keep all entries marked **Secret** private.
   - Secrets: `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`,
     `DASHBOARD_API_TOKEN`, `GITHUB_PAT`, `HOST_KEY_SECRET`
   - Variable: `GITHUB_REPOSITORY` = `rohro1/LIVEVAULT`
   - `GITHUB_PAT` must be a fine-grained token limited to this repository with
     repository **Variables: read and write** and **Actions: read and write**
     permissions. Actions write queues monitor checks and Drive operations;
     Actions read lets the dashboard display recent workflow runs.
   - Use a different strong random value for each dashboard password/session
     secret/ingest token. `HOST_KEY_SECRET` must match the GitHub Actions
     secret of the same name.
6. **GitHub website → repository Settings → Secrets and variables → Actions:**
   add the same `DASHBOARD_API_TOKEN` as a repository secret. Add a repository
   variable `DASHBOARD_API_URL` with the deployed Pages URL, for example
   `https://livevault-dashboard.pages.dev`. Push once after adding the workflow
   environment change so Actions receives it.
7. **Cloudflare website:** redeploy the Pages project after setting its secrets
   and D1 binding, then open its URL and sign in with `DASHBOARD_PASSWORD`.

The dashboard's first load creates or synchronizes `TRACKED`. Add, pause,
resume, or remove accounts there. Verified Drive uploads appear in the
recordings list after the Actions workflow receives the internal ingest token.
