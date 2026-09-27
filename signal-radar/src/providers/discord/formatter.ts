/**
 * AlertPayload -> Discord webhook body (embeds). Pure and fully tested.
 *
 * Token names, symbols and provider reason strings are attacker-controlled:
 *  - mentions are disabled at the API level (`allowed_mentions: { parse: [] }`)
 *    AND neutralised in text (a zero-width space after every @)
 *  - markdown is escaped, control and invisible characters are stripped
 *  - lengths are capped to Discord's embed limits
 *  - no link is built from token metadata; only fixed explorer URLs with the
 *    (base58-validated) address
 */
import type { AlertPayload } from '../../core/alerts.js';

export const DISCLAIMER = 'Meetbare signalen, geen financieel advies en geen koersvoorspelling.';

// https://discord.com/developers/docs/resources/message#embed-object-embed-limits
const LIMITS = { title: 256, description: 4096, fieldName: 256, fieldValue: 1024, fields: 25, footer: 2048, total: 6000 };

const INVISIBLE = /[\u0000-\u001f\u007f-\u009f­؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁯ㅤ︀-️﻿ﾠ]/g;
const BASE58 = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

/** Neutralise untrusted text for display inside Discord markdown. */
export function sanitize(text: string | null | undefined, max = 64): string {
  if (!text) return '—';
  const cleaned = text
    .normalize('NFKC')
    .replace(INVISIBLE, '')
    .replace(/[\\`*_~|>#[\]()<:-]/g, (c) => `\\${c}`)
    .replace(/@/g, '@​')
    .replace(/\s+/g, ' ')
    .trim();
  if (!cleaned) return '—';
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned;
}

/** Characters from outside the Latin/common ranges in a ticker: a lookalike warning sign. */
export function looksSpoofed(text: string | null | undefined): boolean {
  if (!text) return false;
  return /[^\u0000-ɏ -⁯₠-⃏℀-⅏←-⇿☀-➿\u{1f300}-\u{1faff}]/u.test(
    text.normalize('NFKC'),
  );
}

function usd(v: number | null): string {
  if (v === null) return 'onbekend';
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 10_000) return `$${Math.round(v / 1_000)}k`;
  if (Math.abs(v) >= 1) return `$${v.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
  return `$${v.toPrecision(3)}`;
}

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}u ${Math.floor((s % 3600) / 60)}m`;
}

function cap(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

const COLORS = { NEW_TOKEN: 0x3b82f6, MOMENTUM: 0xf59e0b } as const;

export interface DiscordEmbed {
  title: string;
  description?: string;
  url?: string;
  color: number;
  fields: { name: string; value: string; inline?: boolean }[];
  footer: { text: string };
  timestamp: string;
}

export interface DiscordWebhookBody {
  username: string;
  allowed_mentions: { parse: [] };
  embeds: [DiscordEmbed];
}

export function renderAlert(p: AlertPayload, username: string): DiscordWebhookBody {
  const symbol = sanitize(p.symbol, 24);
  const address = BASE58.test(p.tokenAddress) ? p.tokenAddress : null;
  const title =
    p.type === 'NEW_TOKEN'
      ? `🆕 Nieuwe token: ${symbol}`
      : `📈 Uitzonderlijke activiteit: ${symbol} — Momentum Score ${p.score?.value.toFixed(0) ?? '?'}/100`;

  const fields: DiscordEmbed['fields'] = [];
  const add = (name: string, value: string, inline = true) =>
    fields.push({ name: cap(name, LIMITS.fieldName), value: cap(value || '—', LIMITS.fieldValue), inline });

  add('Token', `${sanitize(p.name, 48)} (${symbol})${looksSpoofed(p.symbol) ? '\n⚠️ ongebruikelijke tekens in ticker' : ''}`);
  add('Leeftijd', p.age ? `${duration(p.age.seconds)} (${p.age.source === 'onchain' ? 'on-chain' : 'volgens marktdata'})` : 'onbekend');
  add('Liquiditeit', usd(p.market.liquidityUsd));
  add(p.market.marketCapUsd !== null ? 'Market cap' : 'FDV', usd(p.market.marketCapUsd ?? p.market.fdvUsd));
  add('Volume 5m / 1u', `${usd(p.market.volumeM5Usd)} / ${usd(p.market.volumeH1Usd)}`);
  add(
    'Transacties 5m',
    p.market.buysM5 === null || p.market.sellsM5 === null ? 'onbekend' : `${p.market.buysM5} kopen / ${p.market.sellsM5} verkopen`,
  );
  if (p.holders) {
    add(
      'Holders',
      `${p.holders.count}${p.holders.capped ? '+' : ''}${p.holders.top10Pct === null ? '' : ` · top-10 ${p.holders.top10Pct.toFixed(1)}%`}`,
    );
  }
  const providers = p.safety.providers.map((s) => `${sanitize(s.provider, 24)} ${s.verdict}`).join(' · ');
  const reasons = p.safety.reasons.slice(0, 5).map((r) => `• ${sanitize(r, 120)}`).join('\n');
  add(`Veiligheid: ${p.safety.verdict}`, [providers, reasons].filter(Boolean).join('\n'), false);

  if (p.score) {
    add('Redenen', p.score.reasons.map((r) => `• ${sanitize(r, 120)}`).join('\n') || '—', false);
    const lines = p.score.components
      .filter((c) => c.points > 0)
      .map((c) => `${c.label}: ${c.points.toFixed(1)}/${c.weight}`);
    const missing = p.score.components.filter((c) => !c.available).map((c) => c.label);
    if (missing.length) lines.push(`Geen data: ${missing.join(', ')}`);
    for (const pen of p.score.penalties) lines.push(`− ${sanitize(pen.reason, 90)}: −${pen.points}`);
    lines.push(
      `Betrouwbaarheid ${(p.score.confidence * 100).toFixed(0)}% (datadekking, geen kans) · venster ${p.score.window} · ${p.score.version}`,
    );
    add('Score-opbouw', lines.join('\n'), false);
    if (p.score.warnings.length) {
      add('Let op', p.score.warnings.slice(0, 5).map((w) => `• ${sanitize(w, 140)}`).join('\n'), false);
    }
  }

  add(
    'Data',
    `${sanitize(p.market.source, 24)} · ${sanitize(p.market.dexId, 24)} · waargenomen ${new Date(p.market.observedAt).toISOString().slice(11, 19)} UTC`,
    false,
  );
  if (address) {
    add('Adres', `\`${address}\`\n[DexScreener](https://dexscreener.com/solana/${address}) · [Solscan](https://solscan.io/token/${address})`, false);
  }

  const embed: DiscordEmbed = {
    title: cap(title, LIMITS.title),
    ...(address ? { url: `https://dexscreener.com/solana/${address}` } : {}),
    color: COLORS[p.type],
    fields: fields.slice(0, LIMITS.fields),
    footer: { text: DISCLAIMER },
    timestamp: p.decidedAt,
  };
  return { username: cap(username, 80), allowed_mentions: { parse: [] }, embeds: [enforceTotal(embed)] };
}

/** Discord rejects embeds over 6000 characters in total; trim the longest fields first. */
function enforceTotal(embed: DiscordEmbed): DiscordEmbed {
  const size = () =>
    embed.title.length + embed.footer.text.length + embed.fields.reduce((a, f) => a + f.name.length + f.value.length, 0);
  while (size() > LIMITS.total) {
    const longest = embed.fields.reduce((a, f) => (f.value.length > a.value.length ? f : a));
    longest.value = cap(longest.value, Math.max(16, Math.floor(longest.value.length / 2)));
  }
  return embed;
}
