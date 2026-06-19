import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.3";

const PPT_BASE = "https://www.pokemonpricetracker.com/api/v2";
const DEFAULT_JOB_NAME = "pokemon_price_tracker_edge_dev";
const DEFAULT_BATCH_LIMIT = 50;
const DEFAULT_MAX_CARDS_PER_RUN = 300;
const DEFAULT_DAILY_RESERVE = 0;
const DEFAULT_REQUEST_DELAY_MS = 1100;
const DEFAULT_NOTIFY_EMAIL = "underground182@yahoo.com";
const DEFAULT_EMAIL_FROM = "PokeVault <onboarding@resend.dev>";

type Language = "english" | "japanese";
type KeyStatus = "active" | "rate_limited" | "disabled";

type ApiKeyConfig = {
  name: string;
  key: string;
  secretName: string;
};

type ActiveApiKey = ApiKeyConfig & {
  dailyRemaining: number | null;
};

type ApiUsage = {
  credits: number;
  dailyRemaining: number | null;
  minuteRemaining: number | null;
  breakdown: Record<string, unknown>;
};

type RunConfig = {
  language: Language;
  jobName: string;
  batchLimit: number;
  maxCardsPerRun: number;
  dailyReserve: number;
  requestDelayMs: number;
  dryRun: boolean;
  notifyEmail: string | null;
};

type FetchResult = {
  ok: boolean;
  body: Record<string, unknown> | null;
  usage: ApiUsage;
  retryable: boolean;
  rateLimited: boolean;
  errorMessage?: string;
};

function jsonResponse(body: Record<string, unknown>, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function intEnv(name: string, fallback: number) {
  const raw = Deno.env.get(name);
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function boolEnv(name: string, fallback = false) {
  const raw = Deno.env.get(name);
  if (!raw) return fallback;
  return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

async function readRequestJson(req: Request): Promise<Record<string, unknown>> {
  if (req.method === "GET") return {};
  const text = await req.text();
  if (!text.trim()) return {};
  return JSON.parse(text);
}

function parseLanguage(value: unknown): Language {
  return value === "english" ? "english" : "japanese";
}

function numberFrom(value: unknown, fallback: number) {
  const parsed = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function loadConfig(overrides: Record<string, unknown>): RunConfig {
  const batchLimit = Math.min(
    200,
    Math.max(1, numberFrom(overrides.batchLimit, intEnv("PPT_BATCH_LIMIT", DEFAULT_BATCH_LIMIT))),
  );
  return {
    language: parseLanguage(overrides.language ?? Deno.env.get("PPT_LANGUAGE")),
    jobName: String(overrides.jobName ?? Deno.env.get("PPT_JOB_NAME") ?? DEFAULT_JOB_NAME),
    batchLimit,
    maxCardsPerRun: Math.max(
      1,
      numberFrom(overrides.maxCardsPerRun, intEnv("PPT_MAX_CARDS_PER_RUN", DEFAULT_MAX_CARDS_PER_RUN)),
    ),
    dailyReserve: Math.max(
      0,
      numberFrom(overrides.dailyReserve, intEnv("PPT_DAILY_RESERVE", DEFAULT_DAILY_RESERVE)),
    ),
    requestDelayMs: Math.max(
      0,
      numberFrom(overrides.requestDelayMs, intEnv("PPT_REQUEST_DELAY_MS", DEFAULT_REQUEST_DELAY_MS)),
    ),
    dryRun: Boolean(overrides.dryRun ?? boolEnv("PPT_DRY_RUN", false)),
    notifyEmail: String(
      overrides.notifyEmail ??
        Deno.env.get("PPT_NOTIFY_EMAIL") ??
        DEFAULT_NOTIFY_EMAIL,
    ),
  };
}

function loadApiKeys(): ApiKeyConfig[] {
  const keyNamesRaw = Deno.env.get("PPT_KEY_NAMES");
  if (keyNamesRaw) {
    const names = JSON.parse(keyNamesRaw);
    if (!Array.isArray(names)) throw new Error("PPT_KEY_NAMES must be a JSON array");
    return names.map((secretName) => {
      const name = String(secretName);
      const key = Deno.env.get(name);
      if (!key) throw new Error(`Missing Supabase secret value for ${name}`);
      return { name, key, secretName: name };
    });
  }

  const keysRaw = Deno.env.get("POKEMON_PRICE_TRACKER_API_KEYS");
  if (keysRaw) {
    const entries = JSON.parse(keysRaw);
    if (!Array.isArray(entries)) {
      throw new Error("POKEMON_PRICE_TRACKER_API_KEYS must be a JSON array");
    }
    return entries.map((entry, index) => ({
      name: String(entry.name ?? `dev_key_${index + 1}`),
      key: String(entry.key ?? ""),
      secretName: String(entry.secretName ?? entry.name ?? `POKEMON_PRICE_TRACKER_API_KEYS[${index}]`),
    })).filter((entry) => entry.key);
  }

  const single = Deno.env.get("POKEMON_PRICE_TRACKER_API_KEY");
  if (single) {
    return [{ name: "default", key: single, secretName: "POKEMON_PRICE_TRACKER_API_KEY" }];
  }

  throw new Error("No Pokemon Price Tracker API keys configured");
}

function parseHeaderInt(headers: Headers, name: string): number | null {
  const value = headers.get(name);
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function usageFromResponse(resp: Response, body: Record<string, unknown> | null): ApiUsage {
  const metadata = (body?.metadata ?? {}) as Record<string, unknown>;
  const consumed = metadata.apiCallsConsumed as Record<string, unknown> | undefined;
  const headerCredits = parseHeaderInt(resp.headers, "X-API-Calls-Consumed");
  const credits = typeof consumed?.total === "number" ? consumed.total : headerCredits ?? 0;
  return {
    credits,
    dailyRemaining: parseHeaderInt(resp.headers, "X-RateLimit-Daily-Remaining"),
    minuteRemaining: parseHeaderInt(resp.headers, "X-RateLimit-Minute-Remaining"),
    breakdown: (consumed?.breakdown as Record<string, unknown> | undefined) ?? {},
  };
}

function toArrayData(body: Record<string, unknown> | null): Record<string, unknown>[] {
  const data = body?.data;
  if (Array.isArray(data)) return data as Record<string, unknown>[];
  if (data && typeof data === "object") return [data as Record<string, unknown>];
  return [];
}

async function pptGet(path: string, params: Record<string, string | number | boolean>, key: string): Promise<FetchResult> {
  const url = new URL(`${PPT_BASE}${path}`);
  for (const [name, value] of Object.entries(params)) {
    url.searchParams.set(name, String(value));
  }

  const resp = await fetch(url, {
    headers: {
      "Accept": "application/json",
      "Authorization": `Bearer ${key}`,
    },
  });

  let body: Record<string, unknown> | null = null;
  const text = await resp.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: text };
    }
  }

  const usage = usageFromResponse(resp, body);
  if (resp.ok) return { ok: true, body, usage, retryable: false, rateLimited: false };

  const errorMessage = String(body?.error ?? body?.message ?? resp.statusText);
  const is429 = resp.status === 429;
  const dailyLimited = is429 && /daily|credit|limit/i.test(errorMessage);
  return {
    ok: false,
    body,
    usage,
    retryable: is429 && !dailyLimited,
    rateLimited: is429,
    errorMessage,
  };
}

function normalizeTextArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.map((item) => String(item));
}

function toInt(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function mapSet(raw: Record<string, unknown>, language: Language) {
  return {
    language,
    ppt_id: raw.id ?? null,
    tcg_player_id: raw.tcgPlayerId ?? null,
    tcg_player_numeric_id: raw.tcgPlayerNumericId ?? null,
    name: raw.name ?? "",
    series: raw.series ?? null,
    release_date: raw.releaseDate ?? null,
    card_count: raw.cardCount ?? null,
    image_cdn_url: raw.imageCdnUrl ?? null,
    image_cdn_url_200: raw.imageCdnUrl200 ?? null,
    image_cdn_url_400: raw.imageCdnUrl400 ?? null,
    image_cdn_url_800: raw.imageCdnUrl800 ?? null,
    image_url: raw.imageUrl ?? null,
    price_guide_url: raw.priceGuideUrl ?? null,
    has_price_guide: raw.hasPriceGuide ?? null,
    no_price_guide_reason: raw.noPriceGuideReason ?? null,
    api_created_at: raw.createdAt ?? null,
    api_updated_at: raw.updatedAt ?? null,
    raw,
    synced_at: new Date().toISOString(),
  };
}

function mapCard(raw: Record<string, unknown>, language: Language) {
  const prices = (raw.prices ?? {}) as Record<string, unknown>;
  return {
    language,
    ppt_id: raw.id ?? null,
    tcg_player_id: String(raw.tcgPlayerId ?? ""),
    set_numeric_id: raw.setId ?? null,
    set_name: raw.setName ?? null,
    name: raw.name ?? "",
    card_number: raw.cardNumber ?? null,
    rarity: raw.rarity ?? null,
    card_type: raw.cardType ?? null,
    pokemon_type: raw.pokemonType ?? null,
    energy_type: normalizeTextArray(raw.energyType),
    flavor_text: raw.flavorText ?? null,
    hp: toInt(raw.hp),
    stage: raw.stage ?? null,
    attacks: normalizeTextArray(raw.attacks),
    weakness: raw.weakness ?? null,
    resistance: raw.resistance ?? null,
    retreat_cost: toInt(raw.retreatCost),
    artist: raw.artist ?? null,
    tcg_player_url: raw.tcgPlayerUrl ?? null,
    price_market: prices.market ?? null,
    price_low: prices.low ?? null,
    listings: prices.listings ?? null,
    sellers: prices.sellers ?? null,
    primary_printing: prices.primaryPrinting ?? null,
    price_last_updated: prices.lastUpdated ?? null,
    price_was_corrected: prices.priceWasCorrected ?? null,
    variants: raw.variants ?? null,
    printings_available: normalizeTextArray(raw.printingsAvailable),
    image_cdn_url: raw.imageCdnUrl ?? null,
    image_cdn_url_200: raw.imageCdnUrl200 ?? null,
    image_cdn_url_400: raw.imageCdnUrl400 ?? null,
    image_cdn_url_800: raw.imageCdnUrl800 ?? null,
    image_url: raw.imageUrl ?? null,
    external_catalog_id: raw.externalCatalogId ?? null,
    needs_detailed_scrape: raw.needsDetailedScrape ?? null,
    data_completeness: raw.dataCompleteness ?? null,
    last_scraped_at: raw.lastScrapedAt ?? null,
    api_created_at: raw.createdAt ?? null,
    api_updated_at: raw.updatedAt ?? null,
    raw,
    synced_at: new Date().toISOString(),
  };
}

function mapPriceVariants(raw: Record<string, unknown>, language: Language) {
  const cardId = String(raw.tcgPlayerId ?? "");
  const variants = raw.variants;
  if (!cardId || !variants || typeof variants !== "object" || Array.isArray(variants)) return [];

  return Object.entries(variants as Record<string, unknown>).flatMap(([printing, value]) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const item = value as Record<string, unknown>;
    return [{
      language,
      card_tcg_player_id: cardId,
      printing,
      market_price: item.marketPrice ?? null,
      low_price: item.lowPrice ?? null,
      condition_used: item.conditionUsed ?? null,
      raw: item,
      synced_at: new Date().toISOString(),
    }];
  });
}

async function upsertRows(
  sb: any,
  table: string,
  rows: unknown[],
  onConflict: string,
  ignoreDuplicates = false,
) {
  if (!rows.length) return;
  const { error } = await sb.from(table).upsert(rows, { onConflict, ignoreDuplicates });
  if (error) throw new Error(`Supabase upsert error [${table}]: ${error.message}`);
}

function stripUndefined(input: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(input).filter(([, value]) => value !== undefined));
}

async function updateKeyStatus(sb: any, key: ApiKeyConfig, patch: Record<string, unknown>) {
  const cleanPatch = { ...patch };
  const creditDelta = typeof cleanPatch.credits_used_delta === "number"
    ? cleanPatch.credits_used_delta
    : null;
  delete cleanPatch.credits_used_delta;

  if (creditDelta !== null) {
    const { data, error } = await sb
      .from("ppt_api_keys")
      .select("credits_used_today,credit_window_date")
      .eq("key_name", key.name)
      .maybeSingle();
    if (error) throw new Error(`ppt_api_keys usage read failed: ${error.message}`);

    const currentCredits = data?.credit_window_date === todayIso()
      ? Number(data.credits_used_today ?? 0)
      : 0;
    cleanPatch.credits_used_today = currentCredits + creditDelta;
    cleanPatch.credit_window_date = todayIso();
  }

  const { error } = await sb.from("ppt_api_keys").upsert({
    key_name: key.name,
    key_secret_name: key.secretName,
    updated_at: new Date().toISOString(),
    ...stripUndefined(cleanPatch),
  }, { onConflict: "key_name" });
  if (error) throw new Error(`ppt_api_keys update failed: ${error.message}`);
}

function escapeHtml(value: unknown) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function buildEmailHtml(summary: Record<string, unknown>) {
  const rows = Object.entries(summary).map(([key, value]) => `
    <tr>
      <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#555;">${escapeHtml(key)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eee;font-weight:600;">${escapeHtml(value)}</td>
    </tr>
  `).join("");

  return `
    <div style="font-family:Arial,sans-serif;line-height:1.45;color:#111;">
      <h2 style="margin:0 0 12px;">PokeVault PPT Sync Result</h2>
      <table style="border-collapse:collapse;min-width:360px;">${rows}</table>
      <p style="margin-top:16px;color:#666;font-size:12px;">
        Generated by Supabase Edge Function <code>ppt-sync-cards</code>.
      </p>
    </div>
  `;
}

async function sendResultEmail(summary: Record<string, unknown>) {
  const apiKey = Deno.env.get("RESEND_API_KEY");
  const to = String(summary.notifyEmail ?? "");
  if (!apiKey || !to) {
    return { sent: false, error: !apiKey ? "RESEND_API_KEY is not configured" : "notifyEmail is empty" };
  }

  const from = Deno.env.get("PPT_EMAIL_FROM") ?? DEFAULT_EMAIL_FROM;
  const subject = `PokeVault PPT sync ${summary.status}: ${summary.language} (${summary.cardsUpserted} cards)`;
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from,
      to,
      subject,
      html: buildEmailHtml(summary),
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    return { sent: false, error: `Resend ${resp.status}: ${text}` };
  }

  return { sent: true, error: null };
}

async function ensureApiKeyRows(sb: any, keys: ApiKeyConfig[]) {
  const today = todayIso();
  for (const key of keys) {
    const { data, error } = await sb
      .from("ppt_api_keys")
      .select("key_name,status,credit_window_date")
      .eq("key_name", key.name)
      .maybeSingle();
    if (error) throw new Error(`ppt_api_keys read failed: ${error.message}`);

    const shouldReset = data && data.credit_window_date !== today && data.status === "rate_limited";
    await updateKeyStatus(sb, key, {
      status: shouldReset || !data ? "active" : data.status,
      credit_window_date: today,
      credits_used_today: shouldReset || !data ? 0 : undefined,
      daily_remaining: shouldReset ? null : undefined,
      rate_limited_at: shouldReset ? null : undefined,
      last_error: shouldReset ? null : undefined,
    });
  }
}

async function getActiveKeys(sb: any, keys: ApiKeyConfig[]): Promise<ActiveApiKey[]> {
  const today = todayIso();
  const { data, error } = await sb
    .from("ppt_api_keys")
    .select("key_name,status,daily_remaining,credit_window_date")
    .in("key_name", keys.map((key) => key.name));
  if (error) throw new Error(`ppt_api_keys list failed: ${error.message}`);

  const stateByName = new Map((data ?? []).map((row: any) => [row.key_name, row]));
  return keys.flatMap((key) => {
    const state = stateByName.get(key.name);
    if (!state) return [{ ...key, dailyRemaining: null }];
    if (state.status === "disabled") return [];
    if (state.credit_window_date !== today) return [{ ...key, dailyRemaining: null }];
    if (state.status !== "active") return [];
    return [{
      ...key,
      dailyRemaining: typeof state.daily_remaining === "number" ? state.daily_remaining : null,
    }];
  });
}

async function ensureSets(sb: any, config: RunConfig, key: ApiKeyConfig) {
  const { count, error: countError } = await sb
    .from("ppt_sets")
    .select("id", { count: "exact", head: true })
    .eq("language", config.language);
  if (countError) throw new Error(`ppt_sets count failed: ${countError.message}`);
  if ((count ?? 0) > 0) return 0;

  let offset = 0;
  let total = 0;
  while (true) {
    const result = await pptGet("/sets", {
      language: config.language,
      limit: 500,
      offset,
      sortBy: "releaseDate",
      sortOrder: "asc",
    }, key.key);
    if (!result.ok) throw new Error(`Failed to sync sets: ${result.errorMessage}`);

    const sets = toArrayData(result.body);
    if (!sets.length) break;
    if (!config.dryRun) {
      await upsertRows(sb, "ppt_sets", sets.map((set) => mapSet(set, config.language)), "language,tcg_player_id");
    }
    total += sets.length;

    const metadata = (result.body?.metadata ?? {}) as Record<string, unknown>;
    if (!metadata.hasMore || sets.length < 500) break;
    offset += sets.length;
    await sleep(config.requestDelayMs);
  }
  return total;
}

async function getCheckpoint(sb: any, config: RunConfig) {
  const { data, error } = await sb
    .from("ppt_sync_checkpoints")
    .select("*")
    .eq("job_name", config.jobName)
    .eq("language", config.language)
    .maybeSingle();
  if (error) throw new Error(`checkpoint read failed: ${error.message}`);
  return data;
}

async function updateCheckpoint(sb: any, config: RunConfig, patch: Record<string, unknown>) {
  const { error } = await sb.from("ppt_sync_checkpoints").upsert({
    job_name: config.jobName,
    language: config.language,
    credit_window_date: todayIso(),
    updated_at: new Date().toISOString(),
    ...patch,
  }, { onConflict: "job_name,language" });
  if (error) throw new Error(`checkpoint update failed: ${error.message}`);
}

function normalizeCompletedSetIds(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => Number(item)).filter((item) => Number.isFinite(item));
}

function withCompletedSetId(completedSetIds: Set<number>, setId: number) {
  completedSetIds.add(setId);
  return Array.from(completedSetIds).sort((a, b) => a - b);
}

function isInvalidApiKeyError(message: string | undefined) {
  return /invalid api key|unauthorized|forbidden|authentication|api key/i.test(message ?? "");
}

async function getSets(sb: any, config: RunConfig) {
  const { data, error } = await sb
    .from("ppt_sets")
    .select("tcg_player_numeric_id,name,card_count,release_date")
    .eq("language", config.language)
    .not("tcg_player_numeric_id", "is", null)
    .order("release_date", { ascending: true, nullsFirst: false })
    .order("name", { ascending: true })
    .limit(10000);
  if (error) throw new Error(`ppt_sets fetch failed: ${error.message}`);
  return data ?? [];
}

async function countCards(sb: any, language: Language, setId: number) {
  const { count, error } = await sb
    .from("ppt_cards")
    .select("id", { count: "exact", head: true })
    .eq("language", language)
    .eq("set_numeric_id", setId);
  if (error) throw new Error(`ppt_cards count failed: ${error.message}`);
  return count ?? 0;
}

serve(async (req: Request) => {
  const startedAt = new Date().toISOString();
  let runId: string | null = null;
  let cardsUpserted = 0;
  let cardsSkipped = 0;
  let cardsFailed = 0;
  let creditsUsed = 0;
  let setsUpserted = 0;
  let lastKeyName: string | null = null;
  let keysRateLimited = 0;
  let stoppedReason: string | null = null;
  const invalidKeys: string[] = [];

  try {
    const overrides = await readRequestJson(req);
    const config = loadConfig(overrides);
    const keys = loadApiKeys();
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
    if (!supabaseUrl || !serviceRoleKey) {
      throw new Error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
    }

    const sb = createClient(supabaseUrl, serviceRoleKey);
    const { data: run, error: runError } = await sb.from("ppt_edge_runs").insert({
      job_name: config.jobName,
      language: config.language,
      status: "running",
      params: config,
      started_at: startedAt,
    }).select("id").single();
    if (runError) throw new Error(`edge run insert failed: ${runError.message}`);
    runId = run.id;

    await ensureApiKeyRows(sb, keys);
    let activeKeys = await getActiveKeys(sb, keys);
    if (!activeKeys.length) throw new Error("No active Pokemon Price Tracker API keys available");

    setsUpserted = await ensureSets(sb, config, activeKeys[0]);
    const sets = await getSets(sb, config);
    const checkpoint = await getCheckpoint(sb, config);
    const completedSetIds = new Set<number>(
      normalizeCompletedSetIds(checkpoint?.completed_set_numeric_ids),
    );
    let checkpointOffset = checkpoint?.credit_window_date === todayIso()
      ? Number(checkpoint.current_offset ?? 0)
      : 0;
    let checkpointSetId = checkpoint?.credit_window_date === todayIso()
      ? checkpoint.current_set_numeric_id
      : null;

    await updateCheckpoint(sb, config, {
      status: "running",
      credits_used_today: 0,
      daily_remaining: null,
    });

    outer:
    for (const set of sets) {
      const setId = set.tcg_player_numeric_id;
      if (!setId) continue;

      const cardCount = Number(set.card_count ?? 0);
      const existing = await countCards(sb, config.language, setId);
      if (completedSetIds.has(setId) || (cardCount && existing >= cardCount)) {
        cardsSkipped += existing;
        await updateCheckpoint(sb, config, {
          status: "running",
          current_set_numeric_id: setId,
          current_set_name: set.name,
          current_offset: existing,
          last_completed_set_numeric_id: setId,
          completed_set_numeric_ids: withCompletedSetId(completedSetIds, setId),
        });
        continue;
      }

      let offset = checkpointSetId === setId ? Math.max(checkpointOffset, existing) : existing;
      if (offset > 0 && offset < (cardCount || Number.MAX_SAFE_INTEGER)) {
        cardsSkipped += offset;
      }
      checkpointSetId = null;
      checkpointOffset = 0;
      let setDone = false;

      while (!setDone && (!cardCount || offset < cardCount)) {
        if (cardsUpserted >= config.maxCardsPerRun) break outer;

        activeKeys = await getActiveKeys(sb, keys);
        if (!activeKeys.length) {
          stoppedReason = "No active API keys available";
          break outer;
        }

        const remainingInSet = cardCount ? Math.max(cardCount - offset, 0) : config.batchLimit;
        let requestLimit = Math.min(config.batchLimit, remainingInSet, config.maxCardsPerRun - cardsUpserted);
        if (requestLimit <= 0) break;

        let savedBatch = false;
        for (const key of activeKeys) {
          lastKeyName = key.name;
          const keyCreditsAvailable = key.dailyRemaining === null
            ? requestLimit
            : Math.max(key.dailyRemaining - config.dailyReserve, 0);
          const keyRequestLimit = Math.min(requestLimit, keyCreditsAvailable);
          if (keyRequestLimit <= 0) {
            await updateKeyStatus(sb, key, {
              status: "rate_limited",
              daily_remaining: key.dailyRemaining,
              rate_limited_at: new Date().toISOString(),
              last_error: "No daily credits left above reserve",
            });
            continue;
          }

          const result = await pptGet("/cards", {
            language: config.language,
            setId,
            limit: keyRequestLimit,
            offset,
            sortBy: "cardNumber",
            sortOrder: "asc",
            includeHistory: false,
            includeEbay: false,
            includeCardmarket: false,
          }, key.key);

          if (!result.ok) {
            if (isInvalidApiKeyError(result.errorMessage)) {
              invalidKeys.push(key.name);
              await updateKeyStatus(sb, key, {
                status: "disabled",
                daily_remaining: result.usage.dailyRemaining,
                last_error: result.errorMessage ?? "Invalid API key",
                rate_limited_at: null,
              });
              continue;
            }
            if (result.rateLimited) {
              keysRateLimited += 1;
              await updateKeyStatus(sb, key, {
                status: "rate_limited",
                daily_remaining: result.usage.dailyRemaining,
                last_error: result.errorMessage,
                rate_limited_at: new Date().toISOString(),
              });
              stoppedReason = `API key ${key.name} rate limited`;
              continue;
            }
            cardsFailed += keyRequestLimit;
            throw new Error(`Pokemon Price Tracker request failed: ${result.errorMessage}`);
          }

          const cards = toArrayData(result.body);
          creditsUsed += result.usage.credits || keyRequestLimit;

          if (!config.dryRun && cards.length) {
            await upsertRows(
              sb,
              "ppt_cards",
              cards.map((card) => mapCard(card, config.language)),
              "language,tcg_player_id",
              true,
            );
            const variantRows = cards.flatMap((card) => mapPriceVariants(card, config.language));
            await upsertRows(
              sb,
              "ppt_card_price_variants",
              variantRows,
              "language,card_tcg_player_id,printing",
              true,
            );
          }

          cardsUpserted += cards.length;
          offset += cards.length;
          savedBatch = true;

          await updateKeyStatus(sb, key, {
            status: result.usage.dailyRemaining !== null && result.usage.dailyRemaining <= config.dailyReserve
              ? "rate_limited"
              : "active",
            daily_remaining: result.usage.dailyRemaining,
            credits_used_delta: result.usage.credits || keyRequestLimit,
            credit_window_date: todayIso(),
            last_used_at: new Date().toISOString(),
            rate_limited_at: result.usage.dailyRemaining !== null && result.usage.dailyRemaining <= config.dailyReserve
              ? new Date().toISOString()
              : null,
            last_error: null,
          });

          await updateCheckpoint(sb, config, {
            status: "running",
            current_set_numeric_id: setId,
            current_set_name: set.name,
            current_offset: offset,
            credits_used_today: creditsUsed,
            daily_remaining: result.usage.dailyRemaining,
          });

          const metadata = (result.body?.metadata ?? {}) as Record<string, unknown>;
          if (!metadata.hasMore || cards.length < keyRequestLimit) {
            setDone = true;
            await updateCheckpoint(sb, config, {
              status: "running",
              current_set_numeric_id: setId,
              current_set_name: set.name,
              current_offset: offset,
              last_completed_set_numeric_id: setId,
              completed_set_numeric_ids: withCompletedSetId(completedSetIds, setId),
              credits_used_today: creditsUsed,
              daily_remaining: result.usage.dailyRemaining,
            });
          }
          if (!setDone) await sleep(config.requestDelayMs);
          break;
        }

        if (!savedBatch) {
          if (!stoppedReason && invalidKeys.length) {
            stoppedReason = `No usable API keys available; invalid keys: ${invalidKeys.join(", ")}`;
          }
          break outer;
        }
      }
    }

    const status = cardsUpserted >= config.maxCardsPerRun || stoppedReason ? "stopped" : "completed";
    await updateCheckpoint(sb, config, {
      status,
      credits_used_today: creditsUsed,
    });

    const summary = {
      status,
      language: config.language,
      jobName: config.jobName,
      cardsUpserted,
      cardsSkipped,
      cardsFailed,
      creditsUsed,
      setsUpserted,
      keysRateLimited,
      invalidKeys: invalidKeys.join(", "),
      lastKeyName: lastKeyName ?? "",
      stoppedReason: stoppedReason ?? "",
      dryRun: config.dryRun,
      notifyEmail: config.notifyEmail ?? "",
      startedAt,
      finishedAt: new Date().toISOString(),
    };

    const emailResult = await sendResultEmail(summary);

    await sb.from("ppt_edge_runs").update({
      status,
      key_name: lastKeyName,
      cards_upserted: cardsUpserted,
      cards_skipped: cardsSkipped,
      cards_failed: cardsFailed,
      credits_used: creditsUsed,
      sets_upserted: setsUpserted,
      keys_rate_limited: keysRateLimited,
      error_message: invalidKeys.length
        ? `${stoppedReason ?? "Invalid API keys skipped"}: ${invalidKeys.join(", ")}`
        : stoppedReason,
      email_sent_at: emailResult.sent ? new Date().toISOString() : null,
      email_error: emailResult.error,
      finished_at: new Date().toISOString(),
    }).eq("id", runId);

    return jsonResponse({
      success: true,
      status,
      language: config.language,
      cardsUpserted,
      cardsSkipped,
      cardsFailed,
      creditsUsed,
      setsUpserted,
      keysRateLimited,
      invalidKeys,
      emailSent: emailResult.sent,
      emailError: emailResult.error,
      dryRun: config.dryRun,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(message);
    try {
      const supabaseUrl = Deno.env.get("SUPABASE_URL");
      const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
      if (runId && supabaseUrl && serviceRoleKey) {
        const sb = createClient(supabaseUrl, serviceRoleKey);
        await sb.from("ppt_edge_runs").update({
          status: "failed",
          key_name: lastKeyName,
          cards_upserted: cardsUpserted,
          cards_skipped: cardsSkipped,
          cards_failed: cardsFailed,
          credits_used: creditsUsed,
          sets_upserted: setsUpserted,
          keys_rate_limited: keysRateLimited,
          error_message: message,
          finished_at: new Date().toISOString(),
        }).eq("id", runId);
      }
    } catch (updateError) {
      console.error(updateError);
    }
    return jsonResponse({ success: false, error: message }, 500);
  }
});
