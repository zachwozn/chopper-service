# Chopper AI service

Gives Chopper a brain. Runs a local LLM + your notes on your **Docker server**;
your bot (on the VPS) calls it over HTTP when someone does `@Chopper <question>`.

```
  Discord user: "@Chopper how does nerve work?"
        │
        ▼
  FactionDiscord bot (VPS)  ──HTTP POST /ask──▶  chopper-ai service (Docker server)
                                                      │  RAG over notes + persona
                                                      ▼
                                                  Ollama + llama3.2:3b (CPU)
        ◀──────────────── "Nerve? 1 per 5 min, ..." ─┘
```

## What's in here

| File | What it is |
|---|---|
| `chopper_service.py` | The web API: `POST /ask` → Chopper's answer (RAG + persona + Ollama) |
| `Dockerfile` / `docker-compose.yml` | Runs the service **and** Ollama together |
| `data/notes/` | Chopper's notebook (`chopper.md`, and drop in `torn.md` too) |
| `data/personas.json` | Chopper's snarky voice |
| `.env.sample` | Copy to `.env`, set a secret |

---

## Part 1 — Deploy on the Docker server

1. Copy this whole `chopper-service/` folder to the server.

2. **Add your Torn notes** so Chopper can answer Torn questions, not just
   questions about itself. Copy `torn.md` from your PC
   (`Documents\ai-assistant\notes\torn.md`) into `data/notes/` here, next to
   `chopper.md`.

3. Make your secret:
   ```bash
   cp .env.sample .env
   python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into CHOPPER_API_SECRET
   ```

4. Build and start:
   ```bash
   docker compose up -d --build
   ```

5. Pull the model (one time — downloads ~2 GB):
   ```bash
   docker compose exec ollama ollama pull llama3.2:3b
   ```

6. Test it locally on the server:
   ```bash
   curl -s -X POST http://localhost:8000/ask \
     -H "Authorization: Bearer $CHOPPER_API_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"question":"how do I start a lotto?"}'
   ```
   You should get back a short, snarky Chopper answer. First call is slow
   (model loads into RAM); after that it's a few seconds on CPU.

---

## Part 2 — Let the VPS reach the service

The bot on your VPS needs to reach `http://<docker-server>:8000/ask`. Two cases:

**A) Docker server has a public IP / is another cloud box.**
Point the bot at `http://<server-ip>:8000/ask`. **Lock down port 8000** so only
your VPS can hit it — either a firewall rule allowing just the VPS IP, or put it
behind a reverse proxy (Caddy/nginx) with HTTPS. The `CHOPPER_API_SECRET` is a
second layer, but don't rely on it alone.

**B) Docker server is at home / behind NAT.**
Easiest is [Tailscale](https://tailscale.com): install it on both the Docker
server and the VPS (free), and the bot hits the server's Tailscale IP
(`http://100.x.y.z:8000/ask`). No port forwarding, encrypted, private.

---

## Part 3 — Wire up the bot

The cog is already in your repo at `bot/cogs/chopper_ai.py` and registered in
`bot/core/bot.py`. It just needs two environment variables on the **VPS** where
the bot runs (add them to the bot's `.env`):

```
CHOPPER_API_URL=http://<docker-server-or-tailscale-ip>:8000/ask
CHOPPER_API_SECRET=<the same secret from the service .env>
```

Restart the bot. Now in Discord: `@Chopper how does nerve work?` → Chopper
replies in character. It's rate-limited to one question per user every 8 seconds.

---

## Tuning

- **Snappier but dumber:** `CHOPPER_MODEL=llama3.2:1b` in the service `.env`.
- **Sharper but slower:** `qwen2.5:3b`, or `qwen3:8b` if the server has ~8 GB
  RAM to spare (expect 20-60s replies on CPU).
- **Change Chopper's personality:** edit `data/personas.json` → the `voice`
  string. No rebuild needed; it's re-read every request.
- **Update Chopper's knowledge:** edit / add `.md` files in `data/notes/`.
  Also re-read live.

## Live Torn data (auto-refresh)

`torn_live.py` keeps Chopper current by pulling game data straight from the
official Torn API v2: items (with shop/abroad prices), weapon mods, ammo, gyms,
properties, company types + specials, crimes + subcrimes, education, the faction
tree, honors, medals, merits, museum sets, OC scenarios, stocks, bank rates, city
shop prices, Halloween upgrades, Elimination standings, and the event calendar.

* Set `TORN_API_KEY` in the Portainer stack environment (a **Public Only** key is
  enough). Without it the service just runs on the static notes.
* It refreshes on startup and every `TORN_REFRESH_HOURS` (default 6). No image
  rebuild or redeploy is needed for data to update.
* Live data replaces the static API-dump notes it supersedes (honors, medals,
  merits, stocks, OC scenarios, education, faction tree, company details,
  calendar). Wiki-written notes are kept as they are.
* `GET /health` shows what loaded and any errors; `POST /refresh` (same
  `Authorization: Bearer <secret>` as `/ask`) forces a refresh now.
* Test locally: `pip install httpx` then
  `TORN_API_KEY=... python torn_live.py --dump live-dump` writes the rendered
  notes to `live-dump/` so you can read exactly what Chopper will know.

### Live player lookups

With `TORN_API_KEY` set, questions like "who is flirt", "is flirt online", "what
level is EMH", "look up flirt [2384632]" or "who is @someone" pull that player's
**public** Torn profile live (level, rank, account age, faction + position,
status, last action, property, spouse, awards) and hand it to Chopper as a fact.
Names resolve via the API's player search (exact name match only); pings resolve
via the bot's `verification` table (Torn ID), then Torn's own Discord link, then
the display name. Lookups are cached for 5 minutes. "Tell me about X" only looks
up a player when X isn't already something in the notes (an item, faction...).
The bot sends pinged users in the `/ask` payload as
`"mentions": [{"discord_id": "...", "name": "TornName [id]"}]`.

The Torn **wiki is not fetched**: it sits behind an anti-bot challenge, so
wiki-derived notes in `data/notes/` are still maintained by hand.

**This repo is public - never commit a real key.** `.env` is git-ignored.
