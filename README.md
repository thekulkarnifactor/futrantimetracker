# Shift Tracker — Multi-Tenant Setup

## v2 upgrade (November 2026) — do these three things

1. **Database** — run `supabase/migration_v2.sql` in Supabase → SQL Editor (safe to re-run).
2. **Web** — push `tracker.html` and `admin.html` to GitHub Pages as usual.
3. **Sync agent** — on the always-on laptop, replace the old `greyt_sync.py` / `run_sync.bat` with the `sync/` folder:
   - copy `sync/.env.example` to `sync/.env` and paste the **service_role** key
   - double-click `sync/run_sync.bat` (first run creates `.venv`, installs packages and Chromium)
   - move any existing `sync_sessions/` folder into `sync/` to keep saved logins

### What v2 fixes
- **Last out showing `--:--`**: the agent now reads swipes from the JSON the greytHR attendance page downloads, not from the page's visible text, so OUT swipes are seen even when the portal draws them as icons or keeps them in a collapsed list. Page text is still used as a fallback and merged in.
- **Active time growing while you're out**: the agent always marked the day `ACTIVE`, so the dashboard kept adding active minutes after an OUT swipe. The state now follows your last swipe (`ON_BREAK` after an OUT).
- **Missing OUT swipes are visible**: the dashboard shows every swipe as a timeline and warns when two INs follow each other.
- **Quick Correction** now understands `01:47`, `1h 47m`, `107m` and `1.78` (it used to read `01:47` as 1 hour).
- **Stale data warning** on the dashboard when greytHR hasn't updated for 5+ minutes.

### Production behaviour of the agent
- India time (`Asia/Kolkata`) everywhere, independent of the laptop's clock setting.
- Retries Supabase calls with back-off; one bad user never stops the cycle.
- After 3 failed logins in a row a user is paused for 30 min so greytHR doesn't lock the account.
- Concurrency capped at 4 browsers by default; only scrapes inside `SYNC_WINDOW`; close-out of earlier days still runs every cycle.
- Rotating log file in `sync/logs/sync.log`; failure screenshots auto-deleted after 3 days.
- Heartbeat now includes the host name, agent version and the reason each failing user failed (shown on the admin page).
- Clean shutdown on Ctrl+C; `run_sync.bat` restarts it on crash with a growing delay.

### Diagnosing one person
```
cd sync
.venv\Scripts\python greyt_sync.py --probe FTRN-PUN-XXXX
```
Prints the swipes it found and where they came from, and saves the raw greytHR responses to `sync/sync_debug/probe_*`. Nothing is written to the database. `--once` runs a single normal cycle.

---

## Original setup (v1)

## What changed

- **Auth**: Supabase Auth (email + password). `login.html` handles sign up, log in, and "forgot password"; `reset-password.html` is where the reset email link lands.
- **Per-user data**: `tracker_logs` is now keyed by `(user_id, date_tracked)` instead of just `date_tracked`, so everyone has their own row per day. Row Level Security means people can only see/edit their own logs.
- **Admin role**: `admin.html` — live status board for the whole team, ability to correct anyone's punch times, activate/deactivate accounts, promote/revoke admins, and export a CSV timesheet for a date range.
- **Per-user greytHR auto-sync**: each person can paste their own greytHR login into their Settings panel in `tracker.html`. It's encrypted in the database (via `pgcrypto` + Supabase Vault) and only decrypted server-side, by the background sync script, using the service-role key — never sent back to any browser.
- **Date format fix**: dates are now stored as `YYYY-MM-DD` instead of `"Wed Aug 19 2026"`. This was a real bug in the original — text-sorting the old format doesn't sort chronologically across months, which would've broken date-range exports and history views.
- **Other additions**: adjustable per-user target hours (replacing the hardcoded "7.0 hours"), a 7-day personal history view, and CSV export for admins.

## Setup steps

### 1. Run the SQL migration
Open Supabase Dashboard → SQL Editor → paste in `sql/schema.sql` and run it top to bottom. Two places need a decision from you as you go (marked `ACTION` in the file):
- **Old test data**: your `tracker_logs` table already has rows with no `user_id`. Either delete them or backfill them to your account once you've signed up — instructions are inline.
- **Encryption key**: generate one yourself (`openssl rand -base64 32` in any terminal) and paste it into the `vault.create_secret(...)` line before running it.

### 2. Deploy the web files
Host `login.html`, `reset-password.html`, `tracker.html`, and `admin.html` together on any static host (GitHub Pages, Netlify, Vercel, or even just your company intranet). They already point at your Supabase project — no other config needed.

### 3. Create your account and make yourself admin
Open `login.html`, sign up normally, then back in the SQL Editor run:
```sql
update public.profiles set role = 'admin' where email = 'you@example.com';
```
Now `admin.html` is available to you (there's a link for it on the tracker page's header).

### 4. Invite your team
Share the `login.html` link. Everyone signs up on their own and gets a normal (non-admin) account automatically. Promote anyone to admin later from the User Management panel.

### 5. Set up the sync agent (optional, per opted-in user)
Each person who wants automated greytHR syncing pastes their portal domain/username/password into **Settings → greytHR Auto-Sync** on their tracker page — that's stored encrypted.

On the one machine that will run the background agent:
1. Get your **service role key** from Supabase Dashboard → Settings → API (different from the anon key — keep this one secret, never put it in a web page).
2. Set it as an environment variable: `setx SUPABASE_SERVICE_KEY "your-service-role-key-here"`, then open a new terminal.
3. Run `sync/run_sync.bat` (Windows) — see the v2 section above.

## Notes / things worth knowing
- "Deactivate" in the admin panel stops someone from punching in/out or having auto-sync run for them, but doesn't block them from logging in — Supabase Auth doesn't allow a full account ban from client-side code without extra server infrastructure (an Edge Function with the service key). Happy to add that if you want a harder lock later.
- The anon key embedded in the HTML files is meant to be public — it's safe as long as Row Level Security (set up in the SQL) stays enabled. The service role key is the one that must stay private.
- `greyhr_scraper.py` (the older one-off script) isn't part of this pipeline and wasn't changed — `greyt_sync.py` is the one wired up to Supabase.

## Update: reliability, admin visibility, notifications, reports

- **Run `sql/sync_status.sql`** (new file) once — it adds a `sync_status` heartbeat table that `greyt_sync.py` now writes to after every cycle (who synced, who was skipped, who failed, with their `user_id` so it can be matched to a real name).
- **`admin.html`** now has a **Sync Agent Health** card at the top (green/yellow/red by how stale the last cycle is, plus which users are currently failing), a **⚠ sync failing** flag next to any affected user in the Live Team Status table, and a **Team Summary Report** card — pick a range (or use the This Week / This Month buttons) to see total active/break hours per person vs their target, with its own CSV export separate from the raw per-day export.
- **`tracker.html`** now has a **This Week** card (days logged, total active hours, vs target) above Quick Correction, and a **Leave-Time Alerts** toggle in Settings — a browser notification 15 minutes before your target and when you hit it. This uses the standard Notification API, so it only fires while the tracker tab is open (even in the background) — it's not a true push notification that works with the tab fully closed, which would need a service worker and a push backend. Worth doing later if this turns out to matter in practice.
- `greyt_sync.py` was also updated to report `user_id` (not just the greytHR username) for each failed sync, so the admin health card can show real names.
