# Solana Sniper Bot

Detecteert nieuwe token-lanceringen op Solana realtime, analyseert elk token
automatisch, stuurt Telegram-meldingen en koopt alleen wanneer alle filters
slagen, binnen strikte risicolimieten.

> ⚠️ **Risico.** Het overgrote deel van nieuwe Solana-tokens gaat naar nul.
> De filters verkleinen het risico op rug pulls en honeypots, maar sluiten het
> niet uit. Een stop-loss garandeert geen verkoopprijs: bij een rug pull kan
> de prijs in één blok onder je stop vallen. Gebruik een aparte wallet met
> alleen geld dat je kwijt kunt.

Ontwerp, database en event flow: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Wat de bot doet

| Onderdeel | Standaardinstelling (`config.yaml`) |
|---|---|
| Detectie | Raydium AMM v4, Raydium CPMM, PumpSwap (pump.fun optioneel) via `logsSubscribe` |
| Filters | liquiditeit ≥ $25.000 · ≥ 100 holders · top-10 < 40% · mint/freeze authority ingetrokken · geen gevaarlijke Token-2022-extensies · RugCheck zonder `danger` · pool actief en verhandelbaar (round-trip ≤ 10% verlies) |
| Watchlist | tokens die nog niet slagen worden elke 20 s opnieuw gecheckt, max. 30 min |
| Risico | $20 per trade · max. 3 open posities · max. 6 aankopen/uur · max. $60 dagverlies · 0,05 SOL reserve |
| Exits | stop-loss −20% · take-profit +50% (verkoopt 50%) · trailing stop 15% op de rest |
| Modus | `paper` (echte data, gesimuleerde trades) of `live` |

Telegram-commando's: `/status`, `/positions`, `/pause`, `/resume`,
`/sell <id> CONFIRM`, `/sellall CONFIRM`.

## Installatie op een Ubuntu VPS

```bash
# 1. Docker
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER   # opnieuw inloggen

# 2. Code en configuratie
git clone <deze repo> && cd <repo>/solana-sniper
cp .env.example .env && chmod 600 .env        # RPC, Telegram, database invullen
cp config.example.yaml config.yaml            # limieten naar wens

# 3. Configuratie controleren en starten (paper-modus)
docker compose run --rm bot python -m sniper --check-config
docker compose up -d
docker compose logs -f bot
```

### Vereisten

- **RPC**: een betaalde Solana-RPC met WebSocket (Helius, QuickNode, Triton).
  Publieke endpoints throttlen `logsSubscribe` en weigeren `getProgramAccounts`.
  Gebruik je Helius, zet dan `analysis.holders_method: helius_das`.
- **Telegram**: maak een bot via [@BotFather](https://t.me/BotFather), stuur
  hem een bericht en haal je chat-ID op via
  `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### Naar live

1. Laat de bot minstens een paar dagen in `paper` draaien en bekijk wat hij
   koopt en verkoopt (`/positions`, tabel `positions`, `events`).
2. Maak een **nieuwe** wallet en zet daar alleen op wat de bot mag gebruiken,
   bijvoorbeeld 3 × $20 + fees + 0,05 SOL reserve.
3. Zet `SOLANA_PRIVATE_KEY` in `.env` (of mount een keypair-bestand via
   `SOLANA_KEYPAIR_PATH`), zet `trading.mode: live` en herstart:
   `docker compose up -d --build`.

## Beheer

```bash
docker compose ps                                  # status + healthcheck
curl -s localhost:8080/health                      # {"db": true, "websocket": true, ...}
docker compose exec postgres psql -U sniper sniper # database
docker compose exec postgres pg_dump -U sniper sniper | gzip > backup-$(date +%F).sql.gz
```

Handige queries:

```sql
-- Laatste analyses en waarom tokens niet slaagden
SELECT mint, symbol, status, last_reason FROM tokens ORDER BY detected_at DESC LIMIT 20;
-- Resultaat per gesloten positie (lamports; 1 SOL = 1e9)
SELECT id, symbol, close_reason, realized_lamports - entry_lamports AS pnl_lamports
FROM positions JOIN tokens USING (mint) WHERE status = 'CLOSED' ORDER BY closed_at DESC;
```

## Ontwikkeling

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # unit-tests
TEST_DATABASE_URL=postgresql://postgres@localhost/sniper_test pytest   # + integratie (wist die database!)
```

Opbouw: `src/sniper/` — `detector.py` (lanceringen), `analysis/` (data en
filters), `executor.py` (koop/verkoop), `positions.py` + `exits.py`
(bewaking), `recovery.py` (herstel na crash), `telegram.py`, `app.py`
(wiring en health), `db.py`. De beslislogica (`analysis/filters.py`,
`exits.py`, `risk.py`) is puur en zonder I/O getest.
