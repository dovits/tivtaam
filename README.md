# tivtaam — Personal shopping automation for Tiv Taam (טיב טעם)

Converts a generic weekly shopping list (e.g. "גבינה בולגרית") into a fully populated cart on `tivtaam.co.il`, using your purchase history to pick the specific product you usually buy. Suggests forgotten items, surfaces relevant coupons, and exposes a Telegram bot for triggering the run from your phone. **Payment and delivery selection always remain manual.**

## Setup

```powershell
# 1) Install Python 3.11+ then create a venv
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2) Install package + dev deps
pip install -e ".[dev]"
playwright install chromium

# 3) Copy env template and fill credentials
copy .env.example .env
# edit .env — TIVTAAM_USERNAME, TIVTAAM_PASSWORD, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
```

## Phase 1: history-only flow (no cart writes, no Claude, no Telegram)

```powershell
# Manual selector recording — required once before any other command
python scripts/explore.py

# Login + scrape your past orders into SQLite
python -m tivtaam history-sync

# Resolve a single generic to a SKU using your history
python -m tivtaam resolve "גבינה בולגרית"
```

See `C:\Users\dovit\.claude\plans\shimmying-cooking-sunbeam.md` for the full plan and roadmap.

## Running always-on

The bot uses Telegram **long-polling**, so the host needs no public IP, no port
forwarding and no inbound firewall rule — only outbound HTTPS. That makes an
ordinary PC on your home network a fine always-on host, and it keeps the
residential Israeli IP the tivtaam account always logs in from, which matters
for a site with bot detection.

```powershell
# Run as Administrator. Installs the NSSM service and disables sleep on AC.
.\scripts\install_service.ps1 -RunAsUser "$env:USERDOMAIN\$env:USERNAME"
```

What the installer hardens:

| Concern | Handling |
| --- | --- |
| Crash | NSSM restarts after 5s; `AppThrottle` backs off if the process dies within 10s, so a bad `.env` can't cause a restart storm |
| NSSM itself dying | `sc.exe failure` recovery actions restart the service (5s / 30s / 60s), counter resets daily |
| Boot ordering | Depends on `Tcpip`/`Dnscache` + delayed auto-start — long-polling fails if it races the network stack |
| Log growth | stdout/stderr rotate at 10 MB, online, appending across restarts |
| PC sleeping | `standby-timeout-ac` / `hibernate-timeout-ac` set to 0 |
| Power loss | **Manual step:** enable "Restore on AC Power Loss" in BIOS |

**Service account.** LocalSystem runs in session 0, which has no visible
desktop, and the runner launches Chrome *headful* by default. Either pass
`-RunAsUser` (above) or set `RUN_HEADLESS=true` in `.env` and accept the weaker
browser fingerprint.

### Checking on it from your phone

`/health` reports uptime, DB counts, history-sync age, the last job (flagging one
stuck in `running` past `run.timeout_minutes` — a runner that died without
writing a terminal state), open questions, cookie-jar age, whether Chrome is
still findable, and free disk. It also tails today's last `ERROR` line.

Low uptime on a service installed weeks ago means it's crash-looping — check
`logs\bot.stderr.log`.

### Why not the cloud or the iPhone

iOS can't run a background Python process or a Chromium automation stack at all,
so the phone stays the *client* (the Telegram app). A VPS works — the cart lives
server-side on your account, so a cart built remotely still shows up on your
phone — but logging in from a datacenter IP in another country is the most
likely way to trip bot detection or an OTP challenge that an unattended headless
box has no way to answer.

## Layout

- `src/tivtaam/` — package
- `data/` — SQLite + recorded selectors + IPC job files
- `scripts/explore.py` — manual selector-recording session
- `.pw-userdata/` — Playwright persistent context (cookies, gitignored)
- `tests/` — offline unit tests
- `logs/` — service stdout/stderr + daily runner logs (gitignored)

## Hard rules (do not change)

- **Never** click "תשלום" / "שלם" / "שלח הזמנה" / "place order" — the runner stops at `/cart`.
- Credentials live only in `.env` (gitignored). Don't commit them.
- Telegram bot only responds to `TELEGRAM_ALLOWED_USER_ID`.
