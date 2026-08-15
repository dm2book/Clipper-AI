#!/usr/bin/env node
/**
 * Generate `src/api/types.ts` from the API's own OpenAPI document.
 *
 * The point is that these types cannot drift. A field renamed in
 * `schemas.py` changes the schema, regenerating changes the TypeScript, and
 * every page that read the old name fails `tsc` — instead of rendering
 * `undefined` into a table cell where nobody notices for a week.
 *
 *   node scripts/generate-types.mjs [openapi.json | http://host/api/v1/openapi.json]
 *
 * Deliberately small and dependency-free rather than reaching for a codegen
 * package: this API's schema uses a narrow slice of OpenAPI, and a hundred
 * lines that handle exactly that slice is easier to reason about than a tool
 * whose output nobody reads.
 */
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../src/api/types.ts");
const source = process.argv[2] ?? resolve(here, "../../openapi.json");

async function load(where) {
  if (where.startsWith("http")) {
    const response = await fetch(where);
    if (!response.ok) throw new Error(`${where} → ${response.status}`);
    return response.json();
  }
  return JSON.parse(readFileSync(where, "utf8"));
}

const RESERVED = new Set(["Page", "ErrorBody", "ErrorResponse"]);

function tsName(name) {
  // FastAPI expands a generic into one concrete schema and names it
  // `Page_ChannelOut_`. Normalised to `PageChannelOut`, because the raw form
  // reads like a private symbol and would be quoted in every import.
  return name
    .replace(/[[\]]/g, "_")
    .replace(/[^A-Za-z0-9_]/g, "")
    .replace(/_+/g, "_")
    .replace(/_$/, "")
    .replace(/_(.)/g, (_, c) => c.toUpperCase());
}

function tsType(schema, required) {
  if (!schema) return "unknown";
  if (schema.$ref) return tsName(schema.$ref.split("/").pop());
  if (schema.anyOf) {
    const parts = schema.anyOf.map((s) => tsType(s, true));
    // FastAPI expresses `X | None` as anyOf[X, null]; collapse to `X | null`.
    return [...new Set(parts)].join(" | ");
  }
  if (schema.allOf) return tsType(schema.allOf[0], required);
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  switch (schema.type) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array":
      return `${tsType(schema.items, true)}[]`;
    case "object":
      if (schema.additionalProperties)
        return `Record<string, ${tsType(schema.additionalProperties, true)}>`;
      return "Record<string, unknown>";
    default:
      return "unknown";
  }
}

function renderInterface(name, schema) {
  const required = new Set(schema.required ?? []);
  const lines = [];
  if (schema.description) {
    lines.push(`/** ${schema.description.split("\n")[0]} */`);
  }
  lines.push(`export interface ${tsName(name)} {`);
  for (const [field, spec] of Object.entries(schema.properties ?? {})) {
    const optional = required.has(field) ? "" : "?";
    const doc = spec.description ? `  /** ${spec.description.split("\n")[0]} */\n` : "";
    lines.push(`${doc}  ${field}${optional}: ${tsType(spec, required.has(field))};`);
  }
  lines.push("}");
  return lines.join("\n");
}

const document = await load(source);
const schemas = document.components?.schemas ?? {};

const header = `/**
 * GENERATED FILE — do not edit.
 *
 * Produced from the API's OpenAPI document by \`npm run generate:api\`.
 * Edit \`src/clipforge/api/schemas.py\` and regenerate instead; editing here
 * makes the types agree with nothing.
 *
 * Source: ${source}
 * Schemas: ${Object.keys(schemas).length}
 */
`;

const body = Object.entries(schemas)
  .filter(([name]) => !name.startsWith("HTTPValidation") && !name.startsWith("ValidationError"))
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([name, schema]) => renderInterface(name, schema))
  .join("\n\n");

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, `${header}\n${body}\n`);
console.log(`wrote ${out} — ${Object.keys(schemas).length} schemas`);
void RESERVED;
