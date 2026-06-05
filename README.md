# EM Media — Whop Research Agent

Daily research agent for EM Media that scans Whop, Reddit, Twitter/X, and the web for potential ticket-space partners.

## What it does
- Searches across 4 sources: Whop, Reddit, DuckDuckGo (web), Twitter/X
- Uses 10 random keywords per day from your keyword list (rotates daily)
- Claude filters results — only keeps groups with free Discord / free trial / public forum
- Sends a daily Discord embed with all valid leads and contact info

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure .env
```bash
cp .env.example .env
```
Then edit `.env`:
```
ANTHROPIC_API_KEY=your_key_here
DISCORD_WEBHOOK_URL=your_webhook_here
RUN_HOUR=9
```

### 3. Adjust keywords (optional)
Edit `keywords.json` to add/remove search terms.

## Running

**Scheduled (daily at configured hour):**
```bash
python agent.py
```

**Manual / immediate run:**
```bash
python agent.py --now
```

## Modifying keywords via Claude Code
Just open Claude Code in this directory and say:
- "Add keyword: ticket group Netherlands"
- "Remove keyword: ticket scalping discord"
- "Show me today's results"
- "Run the agent now"

## Output
- Discord embed sent daily with all valid leads
- JSON results saved in `logs/results_YYYY-MM-DD.json`
- Run logs saved in `logs/YYYY-MM-DD.log`
