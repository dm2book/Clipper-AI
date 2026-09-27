# Signal Radar

Detecteert nieuwe Solana-tokens vroeg en meldt **objectieve, meetbare**
marktactiviteit via Discord.

> **Wat dit niet is.** Signal Radar voorspelt geen koersen, geeft geen
> koopadvies en handelt niet. Het heeft geen wallet en geen private keys.
> Een alert zegt alleen dát iets meetbaars gebeurde, met de meting erbij.
> Signalen als volume en holders zijn manipuleerbaar (wash trading,
> gebundelde aankopen). Elke alert draagt de disclaimer *"Meetbare signalen,
> geen financieel advies en geen koersvoorspelling."*

Ontwerp: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · Status per databron: [`docs/PROVIDERS.md`](docs/PROVIDERS.md)

## Status van deze MVP

| Onderdeel | Status |
|---|---|
| Tokens detecteren (on-chain, `logsSubscribe` op Raydium AMM v4 / CPMM / PumpSwap) | ✅ gebouwd · ⚠️ logpatronen nog verifiëren |
| Tokenleeftijd (blockTime van de aanmaaktransactie) | ✅ |
| Liquiditeit, volume, transacties, market cap (DexScreener) | ✅ gebouwd · ⚠️ nog niet tegen een live-antwoord getest |
| Veiligheid: mint-/freeze-authority en Token-2022-extensies (on-chain) | ✅ |
| Veiligheid: RugCheck en GoPlus | ✅ gebouwd · ⚠️ nog niet tegen een live-antwoord getest |
| Holders en top-10-concentratie (RPC) | ✅ |
| Volume-spike, transactiegroei, market-cap-verandering, liquiditeitsgroei, aandeel aankopen, holdergroei | ✅ |
| Momentum Detection Engine: 5 vensters, 9 regels, filters tegen false positives, uitlegbare score | ✅ ([docs/MOMENTUM.md](docs/MOMENTUM.md)) |
| NEW TOKEN- en MOMENTUM-alerts naar Discord (outbox, retries, rate limits) | ✅ |
| Realtime monitoring in tiers (15 s → 60 s → 5 min → archief) | ✅ |
| Unieke kopers/verkopers, wash-trading, extreme-trade-filter | ✅ in de engine · ❌ databron (trade stream, fase 3) nog niet gekoppeld |
| Wallets met aantoonbare historie volgen | ❌ fase 4 (`WalletProvider` bestaat als interface, niet gekoppeld) |

## Snel starten (lokaal)

**Nodig:** Node.js ≥ 22.9, Docker (voor PostgreSQL/Redis) en een Solana-RPC
met WebSocket. Een betaalde provider zoals Helius of QuickNode is in de
praktijk nodig.

```bash
cd signal-radar
cp .env.example .env                 # vul minimaal SOLANA_RPC_HTTP_URL en SOLANA_RPC_WS_URL in
docker compose up -d postgres redis  # PostgreSQL 16 + Redis 7 op localhost
npm ci
npm run build
npm start
```

- Migraties draaien automatisch bij het starten (`RUN_MIGRATIONS_ON_START=true`).
- Zonder `DISCORD_WEBHOOK_URL` draait de bot als **dry run**: alerts verschijnen in het log in plaats van in Discord.
- Zet `REDIS_URL=redis://localhost:6379` in `.env` om de Redis-cache te gebruiken. Zonder die variabele gebruikt de bot een cache in het geheugen.
- Controle: `curl localhost:8080/health` en `curl localhost:8080/ready`.
- Stoppen: Ctrl+C. De bot neemt geen nieuw werk meer aan, rondt lopend werk af en sluit de verbindingen.

Ontwikkelen zonder te builden: `npm run dev` (draait TypeScript direct via `tsx`).

### Volledig in Docker

```bash
cp .env.example .env    # invullen; DATABASE_URL/REDIS_URL worden in Compose automatisch goed gezet
docker compose --profile app up -d --build
docker compose logs -f radar
```

## Hoe het werkt

```
logsSubscribe ─▶ nieuwe pool ─▶ getTransaction ─▶ tokens (HOT)
                                                   │
      DexScreener (batches van 30) ◀── tracker ◀───┤  elke 15 s / 60 s / 5 min
                    │                              │
                    ▼                              ▼
          market_snapshots            on-chain + RugCheck + GoPlus, holders
                    └──────────────┬───────────────┘
                                   ▼
                  signalen ─▶ score ─▶ alertregels ─▶ alerts (outbox) ─▶ Discord
```

- **Werkverdeling via PostgreSQL** (`FOR UPDATE SKIP LOCKED` + lease): na een crash of herstart gaat alles verder waar het was.
- **Tokens zonder betekenisvolle liquiditeit** (< `LOW_LIQUIDITY_USD`) worden traag gevolgd, niet verrijkt en na 15 min gearchiveerd. Zo blijft het providerbudget over voor de rest.
- **Eén instance per database**, afgedwongen met een advisory lock.

### Alerts

**NEW TOKEN** (maximaal één keer per token). Alle voorwaarden moeten gelden:
- tokenleeftijd < 10 min (on-chain),
- liquiditeit > $20.000,
- minstens 50 holders,
- veiligheid PASS (of WARN, instelbaar),
- marktdata niet ouder dan 60 s.

**MOMENTUM.** Dit komt uit de **Momentum Detection Engine** ([`docs/MOMENTUM.md`](docs/MOMENTUM.md)). De engine meet over de vensters 1m, 5m, 15m, 30m en 1u de veranderingen in:
- prijs, market cap, liquiditeit en holders;
- volume, transacties, buys/sells en unieke kopers/verkopers;
- de acceleraties van volume en transacties.

Negen regels met instelbare drempels leveren een transparante score op. De alert gaat alleen uit als:
- de engine `MOMENTUM` meldt: score ≥ 60, minstens 3 regels getriggerd, confidence ≥ 0,7, en geen blokkerend filter;
- de veiligheid in orde is;
- de marktdata vers is.

Filters tegen false positives:
- liquiditeitsvloer, minimum holders, minimum volume, minimum transacties en minimum unieke kopers;
- wash-trading-heuristiek;
- uitsluiting van extreme losse trades.

Cooldown van 15 min per token. Daarbinnen komt er alleen een nieuwe alert als de score minstens 15 punten hoger is.

Globaal gaan er maximaal `ALERTS_MAX_PER_HOUR` alerts uit. Alerts boven die limiet worden als `SUPPRESSED` vastgelegd, met reden.

### Momentum Score (voorbeeld, berekend op synthetische testdata)

```
Momentum Score: 66 (MOMENTUM, confidence 1)
Reasons:
* volume +428% (5m: $52.8k vs $10.0k)
* transactions +267% (5m: 165 vs 45)
* holder growth +37% (5m: 100 → 137)
* unique buyers +111% (5m: 74 vs 35)
* liquidity +24% (5m: $50.0k → $62.0k)
…
```

```
punten = getriggerd ? gewicht × (0,5 + 0,5 × sterkte) : 0
score  = clamp(Σ punten − Σ aftrek, 0, 100)
```

Elke regel staat met waarde, drempel, gewicht en punten in de output. De score meet **hoe uitzonderlijk de huidige activiteit is**. Het is geen voorspelling en geen rendementsbelofte.

⚠️ **Unieke kopers/verkopers en wash-trading** vragen per-trade data. Die trade stream is nog niet gekoppeld (fase 3). Tot dan meet de engine volume en transacties uit de 24u-totalen van de provider (exact voor tokens jonger dan 24 u). Dat de rest niet gemeten wordt, staat in de alert.

## Configuratie

Alles via environment variables. [`.env.example`](.env.example) beschrijft elke variabele.

**Verplicht:**
- `DATABASE_URL`
- `SOLANA_RPC_HTTP_URL`
- `SOLANA_RPC_WS_URL`

Een ongeldige configuratie stopt de bot met een melding per variabele. Waarden worden daarbij nooit getoond, omdat het secrets kunnen zijn.

## Monitoring

| Endpoint | Betekenis |
|---|---|
| `GET /health` | Het proces draait. |
| `GET /ready` | Database bereikbaar, discovery verbonden, bot stopt niet. Bevat ook de stand per job en de outbox-achterstand. |
| `GET /metrics` | Prometheus: requests per provider, 429's, circuit breakers, schemafouten, detectielatentie, snapshots, alerts, notificaties, tokens per tier. |

- **Logs:** JSON (pino) naar stdout. API-keys in URL's en webhook-tokens worden gemaskeerd.
- **Schemafouten:** providerantwoorden die niet valideren, komen in de tabel `provider_errors`. Zo zie je een API-wijziging direct.

## Tests

```bash
npm run typecheck
npm test                     # unit-tests; integratietests worden overgeslagen zonder database
```

De integratietests draaien tegen een echte PostgreSQL (en optioneel Redis).
**Gebruik een aparte database: de tests wissen die volledig.**

```bash
docker compose exec postgres createdb -U radar radar_test
TEST_DATABASE_URL=postgres://radar:change-me@localhost:5432/radar_test \
TEST_REDIS_URL=redis://localhost:6379 \
npm test
```

**Testdata:** alle provider-antwoorden in de tests zijn **synthetisch** (in de code gemarkeerd) en opgebouwd volgens de documentatie van de providers.
Om de mappers tegen echte antwoorden te controleren:
1. Draai `npm run record-fixtures -- <mint-adres>` op een machine met internet.
2. Draai daarna `npm test`. De contracttests in `tests/unit/recordedFixtures.test.ts` lopen dan tegen de opnames.

## Projectstructuur

```
src/
  index.ts            startpunt, signalen, graceful shutdown
  app.ts              composition root (bouwt en verbindt alles)
  config/env.ts       validatie van environment variables
  core/               domein, puur (token, snapshot, safety, holders, signals, scoring, alertRules, alerts)
  infra/              db, http (retry/timeout/429), rateLimiter, circuitBreaker, cache, metrics, logger
  providers/          interfaces.ts + solana/, dexscreener/, rugcheck/, goplus/, discord/, console/, wallet/
  repositories/       alle SQL
  services/           discovery, marketData, enrichment, signal, notification, maintenance
  workers/scheduler.ts
  http/healthServer.ts
migrations/           genummerde SQL-migraties
scripts/              record-fixtures
tests/unit, tests/integration, tests/support (mocks en factories, alleen voor tests)
```

## Beveiliging

- **Read-only:** geen wallet, geen keys, geen transacties.
- **Tokennamen en -symbolen zijn vijandige invoer.** Daarom:
  - mentions zijn uitgeschakeld (`allowed_mentions`) en in de tekst geneutraliseerd;
  - markdown en onzichtbare tekens worden verwijderd;
  - lookalike-tickers worden gemarkeerd;
  - links komen alleen van vaste explorer-URL's.
- **Secrets** staan alleen in `.env` (niet in git, `chmod 600`). De Discord-webhook-URL geldt ook als secret.
- **Container:** draait als non-root met een read-only root filesystem. Poorten zijn alleen op localhost gepubliceerd.
