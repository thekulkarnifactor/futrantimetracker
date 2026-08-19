# Shift Tracker — Multi-Tenant Setup

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
3. Run `run_sync.bat` (Windows) — it loops every 15 minutes, pulling the list of opted-in users and syncing each of them.

## Notes / things worth knowing
- "Deactivate" in the admin panel stops someone from punching in/out or having auto-sync run for them, but doesn't block them from logging in — Supabase Auth doesn't allow a full account ban from client-side code without extra server infrastructure (an Edge Function with the service key). Happy to add that if you want a harder lock later.
- The anon key embedded in the HTML files is meant to be public — it's safe as long as Row Level Security (set up in the SQL) stays enabled. The service role key is the one that must stay private.
- `greyhr_scraper.py` (the older one-off script) isn't part of this pipeline and wasn't changed — `greyt_sync.py` is the one wired up to Supabase.
