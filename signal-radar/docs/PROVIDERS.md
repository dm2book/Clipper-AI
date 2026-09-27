# Providers — status, bronnen en wat nog gekoppeld moet worden

Elke externe bron zit achter een interface in `src/providers/interfaces.ts`.
Deze pagina zegt per implementatie **wat bevestigd is, wat niet, en hoe je
het controleert**.

**Legenda**
- ✅ Standaard of officieel gedocumenteerd.
- ⚠️ Gedocumenteerd, maar nog niet getest tegen een opgenomen live-antwoord. Dat moet eerst gebeuren (zie onderaan).
- ❌ Niet gekoppeld.

In de ontwikkelomgeving van deze MVP was uitgaand verkeer naar de providers
geblokkeerd. **Geen enkele mapper is tegen een echt antwoord gedraaid.**
Unit-tests gebruiken synthetische antwoorden, opgebouwd volgens de
documentatie en in de code als `SYNTHETIC` gemarkeerd.

## Overzicht

| Interface | Implementatie | Status | Bron |
|---|---|---|---|
| `TokenDiscoveryProvider` | `SolanaLogsDiscoveryProvider` | ✅ RPC-methoden (`logsSubscribe`, `getTransaction`) · ⚠️ logpatronen per DEX | Solana JSON-RPC-spec |
| `MarketDataProvider` | `DexScreenerMarketDataProvider` | ⚠️ | DexScreener API reference |
| `SafetyProvider` (on-chain) | `SolanaOnchainSafetyProvider` | ✅ | Solana jsonParsed mint-account (SPL Token / Token-2022) |
| `SafetyProvider` (extern) | `RugCheckSafetyProvider` | ⚠️ | RugCheck Swagger |
| `SafetyProvider` (extern) | `GoPlusSafetyProvider` | ⚠️ (API zelf is **beta**) | GoPlus docs |
| `HolderProvider` | `SolanaRpcHolderProvider` | ✅ RPC-methoden · ⚠️ `getProgramAccounts` is bij veel providers beperkt | Solana JSON-RPC-spec |
| `WalletProvider` | `UnconfiguredWalletProvider` | ❌ gooit `NotConfiguredError` | roadmap fase 4 |
| `NotificationProvider` | `DiscordWebhookProvider` | ✅ | Discord API: Execute Webhook |
| `NotificationProvider` | `ConsoleNotificationProvider` | ✅ dry run (zonder webhook-URL) | — |

## Details

### Solana RPC (discovery, on-chain safety, holders)

- **Methoden:** `logsSubscribe` (WebSocket), `getTransaction` (jsonParsed, `maxSupportedTransactionVersion: 0`), `getAccountInfo` (jsonParsed), `getMultipleAccounts`, `getTokenLargestAccounts`, `getProgramAccounts` (met `memcmp` + `dataSlice`). Allemaal standaard.
- **Program-ID's** (publieke mainnet-adressen):
  - Raydium AMM v4 `675kPX9…Mp8`
  - Raydium CPMM `CPMMoo8…qP1C`
  - PumpSwap `pAMMBay…XEA`
  - pump.fun `6EF8rre…F6P`
- ⚠️ **Logpatronen** (`initialize2`, `Instruction: Initialize`, `Instruction: CreatePool`, `Instruction: Create`) zijn de instructienamen die deze programma's bij poolcreatie loggen. Ze zijn **niet** geverifieerd tegen een opgenomen transactie. De bot waarschuwt hierover bij het starten.
  - Controleer ze op een explorer bij een recente lancering.
  - Pas ze indien nodig aan via `DISCOVERY_CUSTOM_SOURCES`.
- ⚠️ `getProgramAccounts` voor het tellen van holders wordt door sommige RPC-providers geweigerd of beperkt. Helius DAS `getTokenAccounts` is een alternatief (nog niet gebouwd).
- **Rate limit:** `SOLANA_RPC_REQUESTS_PER_SECOND` (token bucket). Tijdelijke JSON-RPC-fouten (`-32004/-32005/-32007/-32014/-32016`) worden net als 5xx opnieuw geprobeerd.

### DexScreener

- `GET /tokens/v1/{chainId}/{tokenAddresses}`: maximaal 30 adressen per request, 300 requests/min, geen key.
- **Velden:** `baseToken`, `quoteToken`, `priceUsd`, `liquidity.usd`, `fdv`, `marketCap`, `volume.{m5,h1,h6,h24}`, `txns.{m5,h1,h24}.{buys,sells}`, `priceChange.*`, `pairCreatedAt`.
  - Alles is optioneel gevalideerd: een ontbrekend veld wordt "onbekend", nooit 0.
  - Een pair dat niet valideert, wordt overgeslagen en gelogd.
- Alleen pairs waarin het token het **base token** is, tellen mee: bij een quote-positie gaat `priceUsd` over het andere token.
- ⚠️ Gebruiksvoorwaarden voor (semi-)commercieel gebruik en herpublicatie nog nalopen.

### RugCheck

- `GET /v1/tokens/{mint}/report/summary`, optioneel `X-API-KEY`.
- Het verdict wordt **alleen** gebaseerd op `risks[].level` (`danger`/`critical` → FAIL, `warn` → WARN).
- ⚠️ `score_normalised` wordt opgeslagen maar **niet** gebruikt: openbare beschrijvingen spreken elkaar tegen over de richting van die score (hoger = riskanter of juist veiliger).
- ⚠️ Rate limits zijn niet publiek gedocumenteerd. De standaard is 30/min.

### GoPlus (Solana, beta)

- `GET /api/v1/solana/token_security?contract_addresses={mint}`, optionele `Authorization`-header.
- Envelope `{code, message, result}`, waarbij `code === 1` succes is. Andere codes worden als tijdelijke fout behandeld.
- ⚠️ **Risicovelden:** `mintable`, `freezable`, `balance_mutable_authority`, `non_transferable`, `transfer_hook`, `transfer_fee`, `closable`, `metadata_mutable`, elk met `status` `"0"`/`"1"`. De exacte nesting is niet geverifieerd.
  - Onleesbare velden tellen als onbekend.
  - Is niets leesbaar, dan wordt het een `SchemaError`, vastgelegd in `provider_errors`. Het resultaat is nooit PASS.
- ⚠️ Volgens een externe bron beantwoordt de Solana-route alleen het **eerste** adres. De provider stuurt er daarom één per request.

### Discord

- `POST {webhook}?wait=true` met embeds en `allowed_mentions: {parse: []}`.
- ±30 berichten/min per webhook en 5 requests per 5 s per kanaal. 429's met `Retry-After`/`retry_after` worden gerespecteerd.
- **At-least-once:** na een dubbelzinnige fout (timeout of 5xx) probeert de outbox opnieuw, wat soms een dubbel bericht geeft.

## Nog te koppelen

| Wat | Waarom nodig | Richting |
|---|---|---|
| `WalletProvider` | Wallets met aantoonbare historie volgen (fase 4) | Helius Enhanced Transactions of de eigen `trades`-tabel (fase 3) |
| Trade stream (nieuwe interface) | Unieke kopers, koop- vs. verkoopvolume, whale-trades (fase 3) | Helius Enhanced WebSocket / LaserStream (⚠️ beschikbaarheid per abonnement) |
| Helius DAS `getTokenAccounts` | Holders tellen zonder `getProgramAccounts` | Tweede `HolderProvider` |
| Birdeye | Fallback voor launches en marktdata | `new_listing` + WebSocket (⚠️ afhankelijk van het pakket) |
| Jupiter quote | Verhandelbaarheid (verkoopbaarheid) als extra veiligheidscheck | ⚠️ base-URL/versie verifiëren |
| Ops-kanaal | Systeemmeldingen (circuit open, crashes) in Discord | Tweede webhook naar `NotificationProvider` |

## Verifiëren met echte antwoorden

Op een machine met netwerktoegang:

```bash
npm run record-fixtures -- <mint-adres> [<mint-adres> ...]
npm test   # draait nu ook tests/unit/recordedFixtures.test.ts tegen de opnames
```

Het script slaat de onbewerkte antwoorden op in
`tests/fixtures/recorded/<provider>/<adres>.json`, met de URL (zonder
credentials) en het tijdstip. Gebruik een paar tokens van verschillende
leeftijd: een lancering van een paar minuten oud, een van een paar uur en
een bekend token. Als de contracttests slagen, kun je de ⚠️ in deze tabel
omzetten naar ✅.
