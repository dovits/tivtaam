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

## Activate (fill a cart)

`doctor` is an offline pre-flight — it checks every prerequisite and names the
one next step, so run it whenever you're unsure:

```powershell
python -m tivtaam doctor
```

Order of operations on a fresh machine:

```powershell
python scripts/explore.py         # once — record selectors
python -m tivtaam history-sync    # headed; clear the captcha/OTP by hand once
python -m tivtaam doctor          # should now say "Ready to fill a cart"
```

Then fill a cart either way:

```powershell
python -m tivtaam plan -f list.txt --live   # terminal
python -m tivtaam bot                       # telegram: /shop -> picks -> /approve <job> live
```

Both park the browser at `/cart` — **payment stays manual.**

To start the bot automatically at logon, register it as a scheduled task
(`scripts\install_service.ps1`). It must run in your own interactive session,
**not** as a Windows service: a service lives in session 0 with no desktop, so
the headed login and the parked `/cart` page would be invisible to you.

## Layout

- `src/tivtaam/` — package
- `data/` — SQLite + recorded selectors + IPC job files
- `scripts/explore.py` — manual selector-recording session
- `.pw-userdata/` — Playwright persistent context (cookies, gitignored)
- `tests/` — offline unit tests

## Hard rules (do not change)

- **Never** click "תשלום" / "שלם" / "שלח הזמנה" / "place order" — the runner stops at `/cart`.
- Credentials live only in `.env` (gitignored). Don't commit them.
- Telegram bot only responds to `TELEGRAM_ALLOWED_USER_ID`.
