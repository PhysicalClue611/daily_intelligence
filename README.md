# Daily Intelligence

A personal finance intelligence system that generates twice-daily briefings on your stock watchlist. It aggregates prices (yfinance + Finnhub), pulls from 14 RSS feeds + Guardian API, runs Tavily deep-search on notable moves, and synthesizes everything through a two-pass LLM pipeline (DeepSeek via OpenRouter). Reports arrive by email and Telegram, and the Telegram bot accepts natural-language follow-up questions.

Designed to run on a macOS host machine with an Obsidian vault. Intended to be deployed with the help of an AI coding assistant (Claude Code or similar).

---

## Architecture overview

```
NYSE calendar check
  ↓
fetch_prices.py      yfinance bulk download → per-ticker Finnhub fallback
fetch_news.py        14 RSS feeds + Guardian API (48h window)
  ↓
Skip check           no anomalies AND no geo triggers → silent exit
  ↓
memory_context_finance.py   MemPalace vector search + Obsidian notes (fail-open)
Finnhub news         latest headlines for watchlist tickers
Sonar macro brief    Perplexity via OpenRouter (fail-open)
  ↓
LLM Pass 1           DeepSeek V4 Flash → report draft + Tavily search queries
Tavily search        web search → extract full-text chunks
  ↓
LLM Pass 2           DeepSeek V4 Pro (thinking) → final report
  ↓
Obsidian append      monthly file: Hermes/Daily Intelligence/Daily Reports/
Email                Resend API
Telegram             @your_bot
```

Scheduled via macOS launchd: AM at 8:30 ET (pre-market), PM at 20:10 ET (post-close).

---

## Prerequisites

- macOS (launchd scheduling)
- Python 3.12
- [Obsidian](https://obsidian.md) — vault used as both config store and report output

---

## Quick start

```bash
git clone https://github.com/PhysicalClue611/daily_intelligence.git
cd daily_intelligence
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then work through the setup sections below. An AI assistant (Claude Code recommended) can do most of the wiring given this README.

---

## Required: Obsidian vault structure

The system reads config from and writes reports to an Obsidian vault. Set `OBSIDIAN_PATH` to your vault root.

### watchlist.md

Create this file at `<vault>/Hermes/Daily Intelligence/watchlist.md`. This is the only strictly required config file — the system exits if it is missing.

```markdown
## 个股与基金
NVDA
INTC
QQQM
SPCX

## 商品期货
GC=F
CL=F

## 汇率
DX-Y.NYB
^TNX

## 地缘政治关键词
Middle East: Iran, Israel, Strait of Hormuz, OPEC
Trade: tariff, semiconductor export control, TSMC

## 异动阈值
stock_pct: 3.0
commodity_pct: 2.0
fx_pct: 1.0
tnx_bps: 10

## 收件人
you@example.com
```

**Sections explained:**
- `个股与基金` — US stock/ETF tickers (yfinance format)
- `商品期货` — commodity futures (`GC=F` gold, `CL=F` crude oil)
- `汇率` — FX and rates (`DX-Y.NYB` DXY, `^TNX` 10Y Treasury)
- `地缘政治关键词` — geopolitical keyword groups; any RSS match triggers LLM even without price anomalies
- `异动阈值` — price move thresholds (%) to flag as anomalies
- `收件人` — email recipients (one address per line)

### Output directory

Create the directory (script will append to monthly files here):
```
<vault>/Hermes/Daily Intelligence/Daily Reports/
```

### Optional Obsidian files

| File | Purpose |
|---|---|
| `Hermes/Daily Intelligence/Layer_A_Prompt.md` | System prompt for Pass 2. If missing, a built-in default is used. Edit in Obsidian to tune report style. Starter template: [`templates/layer_a_prompt.example.md`](templates/layer_a_prompt.example.md). |
| `Finance/portfolio_report_latest.md` | Your current holdings (cost basis, position size). Injected into Pass 2 for personalised analysis. Safe to omit. |
| `Finance/Investment Operating Manual v1.0.md` | Your investment philosophy and decision rules (capability boundaries, position-sizing triggers, Strategic Alpha Score rubric). Extracted verbatim into Pass 2 prompts and read by `sas_review.py` for the quarterly deep-dive. Safe to omit — Pass 2 just runs without the personalized framework section. Starter template: [`templates/investment_operating_manual.example.md`](templates/investment_operating_manual.example.md). |

---

## Required: API keys

Create `~/Daily_Intelligence/.env` (gitignored):

```bash
# LLM — primary provider (required)
OPENROUTER_API_KEY=sk-or-...

# Web search (required for full reports)
TAVILY_API_KEY=tvly-...

# Stock price fallback + news (free tier sufficient, required)
FINNHUB_API_KEY=...

# Email delivery via Resend (required for email reports)
RESEND_API_KEY=re_...
FINANCE_FROM_ADDRESS=intel@yourdomain.com

# Telegram bot (required for Telegram delivery and interactive Q&A)
FINANCE_TELEGRAM_BOT_TOKEN=...
FINANCE_TELEGRAM_CHAT_ID=...

# Optional
GUARDIAN_API_KEY=...          # Guardian Open Platform, free 500/day
SERPAPI_API_KEY=...           # Google search fallback when Tavily quota exhausted
EXA_API_KEY=...               # Exa search, Sonar fallback
PARALLEL_API_KEY=...          # Parallel.ai full-text research for Telegram Q&A
PARALLEL_MONTHLY_BUDGET_USD=0  # Optional disaster hard cap (0=off, observe-only). Not used to prefer Sonar.
PARALLEL_P1_ENABLED=1          # Optional: set 0 to disable the adaptive 3rd Parallel query
```

### Where to get each key

| Key | Where | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) | Routes to DeepSeek V4 Flash/Pro via Novita. ~$0.01/report. |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) | Free tier: 1000 credits/month. System uses ~5–6 per report. |
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) | Free tier sufficient. Used as yfinance fallback and for news. |
| `RESEND_API_KEY` | [resend.com](https://resend.com) | Free tier: 3000 emails/month. Requires a verified sender domain. |
| `FINANCE_FROM_ADDRESS` | Your verified Resend sender | e.g. `intel@yourdomain.com` |
| `FINANCE_TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram | Create a new bot, copy the token. |
| `FINANCE_TELEGRAM_CHAT_ID` | Your Telegram user or group chat ID | Send a message to your bot, then call `getUpdates` to find your chat ID. |
| `GUARDIAN_API_KEY` | [open-platform.theguardian.com](https://open-platform.theguardian.com) | Free, 500 req/day. |
| `SERPAPI_API_KEY` | [serpapi.com](https://serpapi.com) | Free tier: 100 searches/month. Only used when Tavily daily quota is exhausted. |
| `PARALLEL_API_KEY` | [parallel.ai](https://parallel.ai) | One-time $20 credit, then paid usage. Primary TG follow-up research (higher info density than Sonar). |
| `PARALLEL_MONTHLY_BUDGET_USD` | Local setting | Optional disaster brake only. Default `0` = no hard cap; estimated spend is still logged for TG status. Do not use this to prefer Sonar over Parallel. |
| `PARALLEL_P1_ENABLED` | Local setting | First cost lever for issue #7. Default `1`; set `0` when Parallel dashboard credit drops below `$5` to keep main Parallel research but skip the adaptive 3rd query. |

### Environment variable for Obsidian path

Set `OBSIDIAN_PATH` to your vault root. Example for macOS iCloud sync:

```bash
export OBSIDIAN_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/YourVault"
```

Pass it on the command line or add to your shell profile. The launchd plists also need this set (see Scheduling below).

---

## Running manually

```bash
OBSIDIAN_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/YourVault" \
  ~/Daily_Intelligence/.venv/bin/python ~/Daily_Intelligence/scripts/run_finance.py
```

Force flags:
```bash
FINANCE_FORCE_DATE=2026-06-01  # rerun a specific date
FINANCE_FORCE_SLOT=am          # force am or pm slot
FINANCE_FORCE_RUN=1            # bypass duplicate-check and NYSE calendar guard
```

Logs: `/tmp/daily_intelligence.log`

---

## Telegram bot

The bot handles watchlist management and natural-language Q&A about the market:

```bash
OBSIDIAN_PATH="..." \
  ~/Daily_Intelligence/.venv/bin/python ~/Daily_Intelligence/scripts/telegram_commands.py
```

Example commands (send to your bot):
```
加 MSFT          # add ticker
删 INTC          # remove ticker
加收件人 x@x.com # add email recipient
加关键词 OPEC tariff  # add geo keyword
状态             # show watchlist + Tavily usage + recent reports
强制运行         # trigger report immediately
Why did NVDA drop today?    # natural-language follow-up (runs 3-step research pipeline)
```

---

## Scheduling on macOS (launchd)

Create two plist files in `~/Library/LaunchAgents/`.

**`com.daily-intel.finance.am.plist`** — 8:30 AM ET (5:30 AM PT):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.daily-intel.finance.am</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/Daily_Intelligence/.venv/bin/python</string>
        <string>/path/to/Daily_Intelligence/scripts/run_finance.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>5</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OBSIDIAN_PATH</key>
        <string>/path/to/your/obsidian/vault</string>
    </dict>
    <key>StandardOutPath</key><string>/tmp/daily_intelligence.log</string>
    <key>StandardErrorPath</key><string>/tmp/daily_intelligence.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
```

Create an identical `com.daily-intel.finance.pm.plist` with `Hour=17, Minute=10` (5:10 PM PT = 8:10 PM ET).

**Important:** Use two separate plist files — macOS launchd silently ignores all but the first entry when `StartCalendarInterval` contains an array.

Load:
```bash
launchctl load ~/Library/LaunchAgents/com.daily-intel.finance.am.plist
launchctl load ~/Library/LaunchAgents/com.daily-intel.finance.pm.plist
```

For the Telegram bot (keep-alive):
```xml
<key>Label</key><string>com.daily-intel.finance.telegram</string>
<key>KeepAlive</key><true/>
```

---

## Optional: MemPalace vector memory

The system optionally connects to a MemPalace bridge at `http://localhost:8765` for semantic search over past reports and your personal investment notes. All bridge calls are fail-open — the system works without it, just without the vector-context injection.

MemPalace is a separate project. If you have it running, the bridge endpoint is auto-detected.

---

## Cost estimates

| Component | Cost |
|---|---|
| LLM (Pass 1 + Pass 2 via OR/Novita) | ~$0.008–0.015/report |
| Sonar macro brief | ~$0.005/report |
| Tavily search + extract | ~$0.005–0.010/report (5–6 credits at ~$0.002/credit) |
| Telegram follow-up Q&A | ~$0.010–0.025/question |
| **Total per report** | ~$0.02–0.03 |
| **Per month (2× daily, 21 trading days)** | ~$1–2 |

---

## Project structure

```
Daily_Intelligence/
├── scripts/
│   ├── run_finance.py          main report pipeline
│   ├── llm_config.py           per-stage LLM selection + runtime override loader
│   ├── fetch_prices.py         yfinance + Finnhub price fetching
│   ├── fetch_news.py           RSS aggregation (14 feeds) + Guardian API
│   ├── finance_email.py        Resend email client
│   ├── memory_context_finance.py   MemPalace + Obsidian context injection
│   ├── telegram_commands.py    Telegram bot (long polling)
│   ├── sas_review.py           quarterly Strategic Alpha Score deep-dive (manual/earnings-triggered)
│   ├── sec_edgar_utils.py       SEC EDGAR helpers (Form 4 insider buys, 10-K risk factors) via edgartools
│   ├── backfill_drawers.py     one-shot: backfill past reports into MemPalace
│   └── migrate_reports.py      one-shot: migrate old single-file reports to monthly
├── docs/
│   └── design.md                full architecture reference (manually synced snapshot of the
│                                 author's private Obsidian design doc — see note at top of the file)
├── templates/
│   ├── investment_operating_manual.example.md   starting point for Finance/Investment Operating Manual v1.0.md
│   └── layer_a_prompt.example.md                starting point for Hermes/Daily Intelligence/Layer_A_Prompt.md
├── .env                        API keys (gitignored)
├── llm_config.example.json     LLM selection schema + defaults (copy to llm_config.json to override)
├── llm_config.json             runtime LLM selection overrides (optional, gitignored)
├── finance_tavily_budget.json  Tavily daily usage counter (auto-reset, gitignored)
├── finance_serpapi_budget.json SerpApi monthly counter (auto-reset, gitignored)
├── finance_parallel_budget.json Parallel estimated monthly spend counter (auto-reset, gitignored)
├── sas_tracked_tickers.json    SAS tracked-ticker list (auto-maintained, gitignored)
├── tg_offset.json              Telegram polling offset (gitignored)
└── .venv/                      Python virtual environment (gitignored)
```

---

## Dependencies

Install with:
```bash
.venv/bin/pip install -r requirements.txt
```

Key packages: `yfinance`, `httpx`, `feedparser`, `exchange_calendars`, `openai` (OpenRouter-compatible), `tavily-python`, `parallel-web`, `python-dotenv`, `google-api-python-client` (Gmail OAuth), `exa-py`.

A `requirements.txt` can be generated from the venv:
```bash
.venv/bin/pip freeze > requirements.txt
```

---

## Deploying with Claude Code

This project is designed to be deployed interactively with Claude Code (or any capable coding assistant):

1. Clone the repo and open it: `claude` in the project directory
2. Tell Claude: _"Read the README and help me set up Daily Intelligence on this machine"_
3. Claude will walk through: creating `.env`, setting up `watchlist.md`, writing the launchd plists with your correct paths, and running the first manual test

The CLAUDE.md in this repo contains extended architecture notes and a full history of design decisions and bugs encountered during development.
