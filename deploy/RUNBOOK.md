# Personal deployment runbook — garmin-mcp on Google Cloud Run

> **For Claude (or a human) picking this up cold:** this fork runs the upstream
> `taxuspt/garmin_mcp` server on Cloud Run as a **claude.ai custom connector**
> for one person's Garmin account. Read this whole file before touching
> anything — especially **Ground rules**. Everything deployment-specific lives
> in `deploy/`; the rest of the repo is upstream code.
>
> This repo is a public fork: never commit tokens, the bearer secret, the
> service URL, or personal details. Look those up live (commands below).

---

## 1. What is deployed

```
claude.ai custom connector (web / desktop / mobile)
   │  HTTPS, header  authorization: Bearer <secret>      ("No sign-in" auth)
   ▼
Cloud Run service  garmin-mcp   (region europe-west2, min 0 / max 1 instance)
   ├─ image: Artifact Registry  garmin-mcp/garmin-mcp:<git short SHA>
   ├─ transport: streamable-http, stateless, port 8080, path /mcp
   ├─ secret env GARMIN_MCP_HTTP_TOKEN  ← Secret Manager  garmin-mcp-http-token
   ├─ tools: GARMIN_ENABLED_TOOLS = all READ_ONLY tools + create-only workout
   │         tools (WRITE_TOOLS in deploy.sh). No delete/unschedule/modify tools.
   └─ /tokens ← Cloud Storage FUSE mount of bucket  <project>-tokens
                (garmin_tokens.json, read/write so token refreshes persist;
                 object versioning ON for rollback)
Runtime service account  garmin-mcp-run@<project>  (bucket objectUser + secret accessor only)
```

* Project / region / names: top of [`deploy.sh`](deploy.sh).
* Branch with all our changes: **`gcp-deploy`** (upstream `a2ca5fc` + upstream
  PR #479 + `garminconnect` bumped to **0.3.20** + `deploy/`).
* No Garmin email/password is configured on the server, ever. It only uses
  saved OAuth tokens, so it can never trigger a credential login.

Look up live values:

```bash
gcloud config set project garmin-connect-515416        # if not already
gcloud run services describe garmin-mcp --region europe-west2 --format='value(status.url)'   # + /mcp
gcloud storage ls -l gs://garmin-connect-515416-tokens/
```

## 2. Ground rules (learned the hard way)

1. **Never run `garmin-mcp-auth` or `Garmin().login(email, password)`.** One run
   fires a chain of ~5 strategies × several TLS fingerprints at Garmin SSO and
   gets the account/fingerprint rate-limited (HTTP 429) for 24h+.
2. **Re-login = `deploy/one_shot_login.py` in browser mode only** (section 5).
   It sends no credentials from a script. If anything fails, **stop and
   diagnose — never loop retries.** On any `RATE LIMITED`, wait 24h.
3. Garmin's 429 is **fingerprint-based, not IP-based** — hotspots/VPNs don't help.
   `garminconnect` < 0.3.20 sends a cookie-less login POST that is always 429'd.
   Do not downgrade the pin below 0.3.20.
4. The owner's work Mac runs **Zscaler**; Claude Code there goes through it.
   Browser-mode login works fine with Zscaler ON. (Credential modes needed it
   off, which also cuts off Claude — another reason to avoid them.)
5. Only **one** process may use a given token set: Garmin may rotate the refresh
   token on refresh. The bucket copy is live; `~/.garminconnect` locally is a
   stale backup — don't point a second server at it.
6. Claude Code's auto-mode safety classifier blocks some actions here (public
   Cloud Run deploy, fetching Garmin pages). Ask the owner rather than working
   around it.

## 3. Triage: "the connector broke"

Run these first (needs `gcloud auth login` as the owner's personal Google account):

```bash
U=$(gcloud run services describe garmin-mcp --region europe-west2 --format='value(status.url)')
curl -s $U/readyz                                     # {"status":"ready"} = signed in to Garmin
gcloud run services logs read garmin-mcp --region europe-west2 --limit 50
```

(`/healthz` returns 404 externally — Cloud Run reserves paths ending in `z`. Use `/readyz`.)

| Symptom | Likely cause | Fix |
|---|---|---|
| `gcloud` says `invalid_grant` / reauth | gcloud login expired | owner runs `gcloud auth login` |
| `readyz` → `failed`; logs: "Failed to retrieve social profile", "Token is not active", DI refresh failed | Garmin tokens expired/revoked (e.g. password change, ~months old) | Section 5 (re-login), then `deploy.sh tokens && deploy.sh deploy` |
| `readyz` → `ready` but claude.ai shows 401 / auth errors | Connector header wrong or secret rotated | Re-add header in claude.ai (section 6) |
| claude.ai: "couldn't connect", 5xx, timeouts | Service failing to start, billing off, image missing | Check logs; `gcloud billing projects describe <project>`; redeploy `deploy.sh all` |
| Tools missing / old tool list in Claude | Connector cached tool list | Disconnect + reconnect connector in claude.ai, new chat |
| Tool returns "Garmin rate limit hit" | Too many API calls in a burst (not login) | Wait a few minutes |
| Errors after an upstream merge | Library/API change | Run tests (section 7), check `garminconnect` changelog |

Quick authenticated smoke test (prints nothing secret):

```bash
T=$(gcloud secrets versions access latest --secret=garmin-mcp-http-token)
curl -s -X POST $U/mcp -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_stats","arguments":{"date":"'$(date -v-1d +%F)'"}}}' \
  | sed -n 's/^data: //p' | head -c 400
```

## 4. Deploy commands

```bash
deploy/deploy.sh build    # Cloud Build -> image tagged with git short SHA (commit first!)
deploy/deploy.sh tokens   # upload ~/.garminconnect/garmin_tokens.json -> bucket
deploy/deploy.sh deploy   # (re)deploy Cloud Run; server logs in from tokens at startup
deploy/deploy.sh all      # build + deploy
READ_ONLY=1 deploy/deploy.sh deploy   # deploy without any write tools
```

* `deploy` needs an image for the **current HEAD SHA** — after any commit, use `all`.
* The server reads tokens only at startup, so redeploy after uploading tokens.
* Roll back a bad token file: `gcloud storage ls -a gs://<project>-tokens/` and
  copy an older generation back.

## 5. Re-login procedure (browser-ticket mode) — the method that worked

Owner runs this in **Terminal.app** (needs interactive input):

```bash
cd ~/Documents/Code_Projects/garmin-mcp && uv run python deploy/one_shot_login.py
```

The script prints these steps too:

1. In **Chrome**: open DevTools (Cmd+Opt+I) → Network → tick **Preserve log**.
2. Cmd+Shift+P → "request conditions" → tick **Enable blocking and throttling** →
   `+` → `*://mobile.integration.garmin.com/*` → **Block**. It must have **no red
   outline** (red = invalid URLPattern = not blocking; `*ticket=ST-*` is invalid).
3. Open the sign-in URL the script prints (Garmin iOS-app sign-in page), sign in,
   enter the emailed MFA code.
4. **Within seconds**, copy the ticket: either the address bar / blocked request
   URL (`...gcm/ios?ticket=ST-...`, the page shows "site can't be reached" —
   expected), or Network → `verifyCode` → Response → `serviceTicketId`.
5. Paste into the Terminal prompt. Success looks like
   `OK - tokens saved ...` + `Verified: signed in as '<uuid>'`.

Then: `deploy/deploy.sh tokens && deploy/deploy.sh deploy` and check `readyz`.

Failure meanings (exit codes in the script docstring):
* `AUTH FAILED: DI token exchange failed for all client IDs` → ticket expired or
  already consumed (first attempt failed this way after a slow paste). The new
  `diauth ... HTTP 4xx` lines show Garmin's reason. Safe to redo browser mode
  (it's a normal browser login); try `--service embed` if the iOS page misbehaves.
* `RATE LIMITED` → stop for 24h.
* Last resort only: `--mode mobile` (one credential POST; needs Zscaler off).

## 6. claude.ai connector settings

Customize → Connectors → Add custom connector:

* URL: `<service URL>/mcp`
* Authentication: **No sign-in**
* Request headers: `authorization` = `Bearer <secret>` (Required)
* Transport: Streamable HTTP

Copy the header value to the clipboard without printing it:

```bash
printf "Bearer %s" "$(gcloud secrets versions access latest --secret=garmin-mcp-http-token)" | pbcopy
```

Rotate the secret: `openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add garmin-mcp-http-token --data-file=-`,
then `deploy/deploy.sh deploy` and update the header in claude.ai (auth settings
can't be edited — remove and re-add the connector).

## 7. Updating from upstream

```bash
git fetch https://github.com/taxuspt/garmin_mcp.git main:upstream-main
git merge upstream-main          # on gcp-deploy; keep garminconnect >= 0.3.20
uv sync && uv run pytest tests/integration tests/unit -q
deploy/deploy.sh all
```

If upstream adds/renames tools, the allowlist adapts automatically (built from
`src/garmin_mcp/tool_annotations.py`); `WRITE_TOOLS` names must stay in
`ADDITIVE_TOOLS` or the deploy aborts.

## 8. History / decisions (2026-11)

* Heroku was considered first; moved to GCP (owner's paid personal account).
* Read-only at first; 2026-11-23 added 11 create-only workout tools so Claude can
  design/schedule/push workouts. Deletes deliberately excluded.
* Auth: owner's claude.ai has the (beta) **Request headers** connector option, so
  the server's built-in shared-secret bearer gate is used. If that option ever
  disappears, the fallback is OAuth in front of the server (e.g. FastMCP
  `GoogleProvider` restricted to the owner's Google account).
* Login: 0.3.2 → instant 429 everywhere; upgraded to 0.3.20; browser-ticket mode
  succeeded on the second try (first ticket expired/consumed while hunting in DevTools).
* Cost: scales to zero; pennies/month (image storage + occasional requests).
* Teardown: `gcloud run services delete garmin-mcp --region europe-west2`, delete
  the bucket, secret, Artifact Registry repo, service account.
