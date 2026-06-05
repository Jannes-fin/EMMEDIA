# AGENTS.md

## Project Overview
Dieses Repository enthaelt einen Python-basierten Research-Agenten in `agent.py`.
Der Agent sucht nach potenziellen Partner-Leads ueber Whop, Reddit, Websuche und Twitter/Nitter, filtert die Ergebnisse mit Claude und sendet passende Leads ueber Discord.

## Commands
- Install dependencies: `pip install -r requirements.txt`
- Run scheduled mode: `python agent.py`
- Run immediate mode: `python agent.py --now`

## Architecture
- Main entry point: `agent.py`
- Prompts: `agents/prompts.py`
- Keywords and exclusions: `keywords.json`
- Existing partners: `existing_partners.json`
- Logs and result files: `logs/`
- Environment variables: `.env`

Der Agent nutzt:
- `ANTHROPIC_API_KEY`
- `DISCORD_WEBHOOK_URL`
- `RUN_HOUR`

Diese Werte duerfen niemals angezeigt, committed oder gepusht werden.

## Safety Rules
- Vor jeder Aenderung `git status` ausfuehren.
- Niemals `.env` lesen, anzeigen, committen oder pushen.
- Niemals API-Keys, Tokens, Webhook-URLs oder Secrets ausgeben.
- Niemals `logs/`, `results_*.json`, `__pycache__` oder `*.pyc` committen.
- Keine produktiven Discord-Webhooks ausloesen ohne ausdrueckliche Freigabe.
- Keine produktiven API-Calls ausloesen ohne ausdrueckliche Freigabe.
- Keine Commits ohne meine Freigabe.
- Keine Pushes ohne meine Freigabe.
- Python heisst auf diesem System `python`, nicht `python3`.
- Wenn unklar ist, ob ein Befehl produktive Aktionen ausloest, vorher stoppen und fragen.

## Development Rules
- Kleine, nachvollziehbare Aenderungen.
- Bestehende Bot-Logik nicht unnoetig umbauen.
- Vor Aenderungen kurz Plan erklaeren.
- Nach Aenderungen Tests nennen.
- Moeglichst Dry-Run/Testmodus verwenden.
- Bei Fehlern zuerst Ursache erklaeren, nicht automatisch reparieren.
- Keine Secrets in `README`, `AGENTS.md`, Logs oder Testausgaben schreiben.

## Testing
Vor Commits nach Moeglichkeit ausfuehren:
- `python -m compileall .`
- `python agent.py --help`, falls verfuegbar
- Import-/Syntaxpruefung ohne echte Sends

Wenn ein Test echte Webhooks, Discord-Sends oder Claude-API-Kosten ausloesen koennte, vorher fragen.

## Git Rules
- Vor Aenderungen: `git status`
- Nach Aenderungen: `git diff` pruefen
- Vor Commit: `git status` und `git diff --cached` pruefen
- Vor Push: explizite Freigabe einholen
- `.env` muss untracked bleiben
