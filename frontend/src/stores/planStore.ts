import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, getAccessToken } from "../lib/api/client";
// FAR-2020: imported from the module that OWNS the registry rather than via the
// api-client re-export. `onAuthChange` is the same function either way, but the
// client module is stubbed wholesale by ~130 component specs (partial mocks with
// no such export), and a store that cannot register its listener would throw on
// creation in every one of them.
import { onAuthChange } from "../lib/api/auth";
import { withTimeout } from "../lib/asyncUtils";
import { formatApiError } from "../lib/api/formatError";
import { registerSyncHandlers, disposeSyncHandlers } from "./syncRegistry";
import { flagCacheKey, parseFlagCache, serializeFlagCache } from "../config/flagCache";
import { decodeJwtPayload } from "../lib/jwt";
import type { EventBusEvent } from "@/types/events";

interface ApiResult<T> {
  data?: T;
  error?: unknown;
}

/**
 * The single `GET /api/v1/plan` bootstrap payload (FAR-1806). Caller-scoped:
 * readable by ANY authenticated org member, carrying the effective tier,
 * the licence view, dev mode and the FULL known flag set (org
 * `feature_overrides` applied; inactive flags present with
 * `currently_active: false`). The vocabulary mirrors what the admin
 * `/admin/feature-flags` + `/admin/license` pair used to serve, so no field
 * renames — FAR-1809 just collapses those two reads into this one.
 */
interface PlanPayload {
  license: {
    tier: string;
    expires_at?: string | null;
    org_id?: string | null;
  };
  dev_mode?: boolean;
  flags: Array<{ name: string; currently_active: boolean }>;
}

interface TiersPayload {
  tiers: Array<{ tier_id: string; label: string; rank: number }>;
}

/**
 * The caller-scoped `GET /api/v1/auth/permissions` payload (FAR-2020): every
 * permission name the caller's LIVE org role resolves to, read straight from
 * the backend registry. The access token deliberately carries no `permissions`
 * claim (200+ names would bloat every request and go stale on a role change),
 * so this read is the nav's only source for permission-gated items.
 */
interface PermissionsPayload {
  permissions: string[];
}

/**
 * Run one plan request to settlement: apply on success, record on failure.
 * Never rejects — the request is built INSIDE a thunk so even a synchronous
 * throw from `api.GET` lands in this function's catch rather than escaping
 * before the chain exists. (Replaces the old allSettled-then-apply pair:
 * applying per source lets a fast plan response settle the nav without
 * waiting on the tier catalogue — FAR-1805.)
 */
async function settlePlanSource<T>(
  label: string,
  makeRequest: () => Promise<unknown>,
  apply: (res: ApiResult<T>, apiErrors: string[]) => void,
  apiErrors: string[],
): Promise<void> {
  try {
    apply((await makeRequest()) as ApiResult<T>, apiErrors);
  } catch (e: unknown) {
    apiErrors.push(`${label}: ${formatApiError(e)}`);
  }
}

/**
 * The org the current access token belongs to, read synchronously from the
 * JWT `org_id` claim. Being synchronous matters: the flag cache must be
 * scoped per org BEFORE first paint, when no server payload has landed yet
 * (FAR-1237 review finding: the cache was per-origin, so signing into org B
 * first-painted org A's resolved chrome). Returns null when the token is
 * absent/opaque — those sessions share the `unknown` bucket.
 */
function currentOrgId(): string | null {
  try {
    const payload = decodeJwtPayload(getAccessToken());
    const orgId = payload?.org_id;
    return typeof orgId === "string" && orgId.length > 0 ? orgId : null;
  } catch {
    // No storage / no token helper available: fall back to the unknown bucket.
    return null;
  }
}

/**
 * The subject (`sub` claim) a token belongs to. `null` = no token / opaque
 * token. Used to tell a ROTATED token for the same person (a refresh — same
 * sub, keep the list) from a different principal (reset), because a same-SPA
 * login can replace the token without ever passing through `null`.
 */
function principalSubject(token: string | null | undefined): string | null {
  // No token at all is its own answer — never decode an absent/opaque token
  // into whoever the decoder last saw.
  if (!token) return null;
  const sub = decodeJwtPayload(token)?.sub;
  return typeof sub === "string" && sub.length > 0 ? sub : null;
}

function initialPrincipalSubject(): string | null {
  try {
    return principalSubject(getAccessToken());
  } catch (err) {
    console.warn("[plan] Could not read the access token", err);
    return null;
  }
}

async function fetchOrgFlagOverride(flagName: string): Promise<boolean | null> {
  try {
    const res = await api.GET(
      "/api/v1/admin/feature-flags/{flag_name}/org-override",
      { params: { path: { flag_name: flagName } } },
    );
    if (res.error) return null;
    const data = res.data as { override: boolean | null } | undefined;
    return data?.override ?? null;
  } catch (err) {
    console.warn("[plan] Failed to fetch org flag override", err);
    return null;
  }
}

/**
 * Synchronous read of the persisted flag map for `orgId` (FAR-1237). Runs at
 * store creation — before first paint — so flag-driven decisions (mobile nav
 * layout, gated surfaces) start from the LAST RESOLVED value for THIS org
 * instead of an empty "everything off" map that later flips when the request
 * lands.
 */
function readFlagCache(orgId: string | null): Record<string, boolean> | null {
  try {
    if (typeof localStorage === "undefined") return null;
    return parseFlagCache(localStorage.getItem(flagCacheKey(orgId)));
  } catch {
    // Storage blocked (private mode, disabled cookies): behave as uncached.
    return null;
  }
}

/** Best-effort persist — quota/private-mode failures must not break the fetch path. */
function writeFlagCache(flags: Record<string, boolean>): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(flagCacheKey(currentOrgId()), serializeFlagCache(flags));
  } catch (err) {
    // Best-effort only: the in-memory map is already correct without it.
    console.warn("[plan] Failed to persist flag cache", err);
  }
}

export const usePlanStore = defineStore("plan", () => {
  const cachedFlags = readFlagCache(currentOrgId());
  const currentTier = ref("community");
  const features = ref<Record<string, boolean>>(cachedFlags ?? {});
  // Where the current flag map came from. 'none' = nothing has ever resolved,
  // the ONLY state in which a flag value is unknown (layout consumers render
  // their 'pending' placeholder then). Write-once: neither a cache hit nor a
  // server payload ever returns it to 'none', so a resolved layout can never
  // fall back to the unresolved state mid-session (FAR-1237 guard). Note
  // `loaded` stays false on a cache hit — it means "fetched from the server",
  // which the router's tier guard relies on as the signal that the TIER is
  // known: it is set together with `currentTier`, so a payload that resolved
  // to an EMPTY flag map is still 'loaded' and tier-checked (FAR-1809).
  //
  // 'unavailable' (FAR-1805) = the flags request SETTLED without a payload
  // (5xx, rejection, the 15s timeout, the all-undefined no-payload branch, or
  // a 4xx such as an expired token). It is terminal "cannot be read", not
  // "has not been read yet": layout consumers treat it as known-and-default
  // (drawer) instead of 'pending', because a permanent placeholder is a
  // silent failure path. Unlike 'none' it IS recoverable — a later successful
  // fetch overwrites it with 'server' (applyPlanPayload), while a
  // cache hit or payload still never returns to 'none'.
  const flagsSource = ref<"none" | "cache" | "server" | "unavailable">(
    cachedFlags ? "cache" : "none",
  );
  const devMode = ref(false);
  const isLoading = ref(false);
  const loaded = ref(false);
  const error = ref<string | null>(null);
  const expiresAt = ref<string | null>(null);
  const orgId = ref<string | null>(null);
  const tierLabels = ref<Record<string, string>>({});
  const tierRanks = ref<Record<string, number>>({
    community: 0,
    team: 1,
  });
  const syncingIds = ref(new Set<string>());
  const unsubHandlers: (() => void)[] = [];

  const isTeam = computed(() => isAtMinimumTier("team"));

  function featureEnabled(name: string): boolean {
    const override = orgOverrides.value[name];
    if (override !== undefined && override !== null) return override;
    return features.value[name] ?? false;
  }

  function getTierLabel(tierId: string): string {
    return (
      tierLabels.value[tierId] ??
      tierId.charAt(0).toUpperCase() + tierId.slice(1)
    );
  }

  function isAtMinimumTier(minTier: string): boolean {
    const currentRank = tierRanks.value[currentTier.value];
    const minRank = tierRanks.value[minTier];
    if (currentRank === undefined || minRank === undefined) return false;
    return currentRank >= minRank;
  }

  /**
   * True when `tierId` is the free community tier — i.e. the lowest tier in
   * the catalog. FAR-1283: `FeatureGate` uses this to keep a disabled
   * community-tier flag's banner tier-accurate (no "team feature" / "upgrade"
   * copy for a capability no plan upgrade can grant). An UNKNOWN tier id
   * returns false, so a newly added tier keeps the (conservative) upgrade
   * wording rather than being mislabelled community.
   */
  function isCommunityTier(tierId: string): boolean {
    const communityRank = tierRanks.value.community;
    const rank = tierRanks.value[tierId];
    if (rank === undefined) return false;
    // Before the tier catalog lands, fall back to the id itself so a gate on
    // `community` is not labelled a paid feature while ranks are still empty.
    if (communityRank === undefined) return tierId === "community";
    return rank === communityRank;
  }

  let fetchPlanPromise: Promise<void> | null = null;

  /**
   * Apply the single caller-scoped `GET /api/v1/plan` payload (FAR-1806,
   * repointed in FAR-1809): tier, dev mode, licence view and the effective
   * flag map arrive together for ANY authenticated member, so this one apply
   * replaces the old instance-admin `/admin/feature-flags` +
   * `/admin/license` pair. `loaded` and `currentTier` are written together
   * here — that pairing is what lets the router's tier guard tell a known
   * tier (payload landed, even with zero flags) from an unknown one.
   */
  function applyPlanPayload(res: ApiResult<PlanPayload>, apiErrors: string[]): void {
    if (res.error) {
      apiErrors.push(`Feature flags: ${formatApiError(res.error)}`);
    } else if (res.data) {
      currentTier.value = res.data.license.tier;
      devMode.value = res.data.dev_mode === true;
      expiresAt.value = res.data.license.expires_at ?? null;
      orgId.value = res.data.license.org_id ?? null;
      const map: Record<string, boolean> = {};
      for (const flag of res.data.flags) {
        map[flag.name] = flag.currently_active;
      }
      features.value = map;
      flagsSource.value = "server";
      loaded.value = true;
      writeFlagCache(map);
    }
  }

  function applyTiersPayload(res: ApiResult<TiersPayload>, apiErrors: string[]): void {
    if (res.error) {
      apiErrors.push(`Tiers: ${formatApiError(res.error)}`);
    } else if (res.data?.tiers?.length) {
      const labels: Record<string, string> = {};
      const ranks: Record<string, number> = {};
      for (const t of res.data.tiers) {
        labels[t.tier_id] = t.label;
        ranks[t.tier_id] = t.rank;
      }
      tierLabels.value = labels;
      tierRanks.value = ranks;
    }
  }

  async function fetchPlan() {
    if (fetchPlanPromise) return fetchPlanPromise;
    fetchPlanPromise = doFetchPlan();
    try {
      await fetchPlanPromise;
    } finally {
      fetchPlanPromise = null;
    }
  }

  async function doFetchPlan() {
    if (isLoading.value) return;
    isLoading.value = true;
    error.value = null;
    const apiErrors: string[] = [];
    try {
      // Two sources, each applying AS IT SETTLES (FAR-1805): the plan
      // bootstrap — ONE caller-scoped `GET /api/v1/plan` (FAR-1806) that
      // carries tier + dev mode + licence view + the effective flag map for
      // ANY authenticated member, replacing the instance-admin
      // `/admin/feature-flags` + `/admin/license` reads (FAR-1809) — and the
      // tier catalogue (`/admin/tiers`, 200 for viewers, unchanged). A slow
      // tiers response must not hold the plan answer the nav is waiting on;
      // vice versa a hung plan request must not delay the tier catalogue.
      // The plan source keeps the label "Feature flags": labels name the
      // SOURCE ROLE (what `flagsSource` and the fallback log below refer to,
      // and the prefix the error filter matches on), not an endpoint path —
      // the path is right here in the thunk.
      const flagsChain = settlePlanSource(
        "Feature flags",
        () => withTimeout(api.GET("/api/v1/plan"), 15000, "Feature flags request"),
        applyPlanPayload,
        apiErrors,
      ).finally(() => {
        // FAR-1805: the FLAGS source settled without producing a payload —
        // terminal, never 'pending' forever (fail open, with a log). The
        // guard is "still 'none' after the flags source settled", deliberately
        // NOT "an error was recorded": the all-undefined no-payload branch
        // ({data: undefined, error: undefined}) records no apiError at all, so
        // an error-only check would miss it. Covers 4xx/5xx, rejection, and the
        // 15s withTimeout alike (settlePlanSource never throws past here) —
        // the 403-for-non-admin case FAR-1805 was born for largely disappears
        // with the caller-scoped endpoint (FAR-1809), but timeout / 5xx must
        // still settle here.
        // 'cache'/'server'/'unavailable' are left untouched (a resolved layout
        // never falls back); a later successful fetch still overwrites with
        // 'server'. The log carries ONLY this source's own errors — flags can
        // settle LAST (the 15s-timeout case), by which time tiers may
        // have recorded theirs, so filter by the label rather than joining
        // whatever has accumulated.
        if (flagsSource.value === "none") {
          flagsSource.value = "unavailable";
          const flagsErrors = apiErrors.filter((entry) =>
            entry.startsWith("Feature flags: "),
          );
          console.warn(
            "[plan] Feature flags request settled without a payload — " +
              "mobile nav falls back to the drawer",
            flagsErrors.length > 0 ? flagsErrors.join("; ") : null,
          );
        }
      });
      const tiersChain = settlePlanSource(
        "Tiers",
        () => withTimeout(api.GET("/api/v1/admin/tiers"), 15000, "Tiers request"),
        applyTiersPayload,
        apiErrors,
      );

      await Promise.all([flagsChain, tiersChain]);
      error.value = apiErrors.length > 0 ? apiErrors.join("; ") : null;
    } catch (e: unknown) {
      error.value = formatApiError(e);
    } finally {
      isLoading.value = false;
    }
  }

  function handleSyncEvent(event: EventBusEvent): void {
    if (
      event.type === "team" ||
      event.type === "license" ||
      event.type === "plan"
    ) {
      if (!syncingIds.value.has(event.id)) {
        syncingIds.value.add(event.id);
        void fetchPlan().finally(() => {
          syncingIds.value.delete(event.id);
        });
      }
    }
  }

  registerSyncHandlers(unsubHandlers, syncingIds, ["team", "license", "plan"], handleSyncEvent);

  function disposeHandlers(): void {
    // FAR-2020: also unhook the principal-change listener, so a disposed store
    // stops reacting to auth changes (and its closures can be collected).
    stopAuthListener?.();
    disposeSyncHandlers(unsubHandlers, syncingIds);
  }

  const orgOverrides = ref<Record<string, boolean | null>>({});

  // ---------------------------------------------------------------------
  // FAR-2020: the caller's effective permission names for nav gating.
  //
  // Resolved ONCE per session from the caller-scoped registry read, because
  // the access token carries no `permissions` claim and duplicating the
  // backend's role -> permission map in the SPA would drift. `permissionsSource`
  // is the settle state: 'none' = not attempted, 'server' = a payload applied,
  // 'unavailable' = settled without one (terminal for the session, so a broken
  // endpoint is not hammered by every component that builds a nav context).
  // Both resolved states are FAIL CLOSED for gating: the list is the source of
  // truth, and an empty list denies every permission-gated item.
  // ---------------------------------------------------------------------
  const userPermissions = ref<string[]>([]);
  const permissionsSource = ref<"none" | "server" | "unavailable">("none");
  let fetchPermissionsPromise: Promise<void> | null = null;
  // FAR-2020: bumped whenever the SPA's principal changes, so a response that
  // was in flight across the switch can never be applied to the new session.
  let permissionsGeneration = 0;

  let permissionsPrincipalSub = initialPrincipalSubject();

  /**
   * Drop the cached permission list (FAR-2020). Called when the SPA's principal
   * changes, so one login can never inherit the previous user's list — the
   * parallel of `clearFlagCache()` in `clearAccessToken`, but scoped to the
   * in-memory permission state, which is per-store-instance rather than
   * persisted.
   */
  function resetCallerPermissions(): void {
    permissionsGeneration += 1;
    userPermissions.value = [];
    permissionsSource.value = "none";
    fetchPermissionsPromise = null;
  }

  // The choke point: EVERY teardown and login path funnels through
  // `setAccessToken` / `clearAccessToken`, and both notify these listeners. A
  // `null` token (logout/expiry) or a different `sub` (another account) resets;
  // a same-sub rotation (token refresh) keeps the resolved list.
  //
  // Registered LAZILY (on the first permissions read) rather than at store
  // creation: `onAuthChange` reads the stored token as it subscribes, so
  // subscribing during store creation would open a storage read for every
  // consumer — including ones that never gate on permissions — and throw where
  // storage is blocked.
  let stopAuthListener: (() => void) | null = null;

  function ensurePrincipalListener(): void {
    if (stopAuthListener) return;
    try {
      stopAuthListener = onAuthChange((token) => {
        const nextSub = principalSubject(token);
        if (nextSub === permissionsPrincipalSub) return;
        permissionsPrincipalSub = nextSub;
        resetCallerPermissions();
      });
    } catch (err) {
      // Best effort: a storage-blocked browser (private mode, disabled cookies)
      // throws here. The list still resolves and still fails closed; the store
      // just cannot observe a later principal switch in this session.
      console.warn("[plan] Could not subscribe to auth changes", err);
      stopAuthListener = () => {};
    }
  }

  async function fetchPermissions(): Promise<void> {
    if (permissionsSource.value !== "none") return;
    if (fetchPermissionsPromise) return fetchPermissionsPromise;
    // Before the request, so a principal switch that lands mid-flight is seen.
    ensurePrincipalListener();
    fetchPermissionsPromise = doFetchPermissions();
    try {
      await fetchPermissionsPromise;
    } finally {
      fetchPermissionsPromise = null;
    }
  }

  async function doFetchPermissions(): Promise<void> {
    const generation = permissionsGeneration;
    try {
      const res = await withTimeout(
        api.GET("/api/v1/auth/permissions"),
        15000,
        "Permissions request",
      );
      // A principal switch while this was in flight makes the answer stale: the
      // reset already cleared the state and the new owner's own read owns it.
      if (generation !== permissionsGeneration) return;
      const permissions = (res.data as PermissionsPayload | undefined)?.permissions;
      if (Array.isArray(permissions)) {
        userPermissions.value = permissions.filter(
          (name): name is string => typeof name === "string",
        );
        permissionsSource.value = "server";
        return;
      }
      // Settled without a payload (4xx / 5xx / timeout / no body): stay fail
      // closed and record it once — the nav degrades to its empty-list
      // behaviour rather than throwing or blocking the render. `console.warn`
      // (not debug) because the lint rule permits only warn/error, and this is
      // the same best-effort-degraded shape the flag-cache persist path warns
      // on; it fires at most once per session (permissionsSource is terminal).
      permissionsSource.value = "unavailable";
      console.warn(
        "[plan] Permissions request settled without a payload - permission-gated nav items stay hidden",
        res.error ?? null,
      );
    } catch (e: unknown) {
      if (generation !== permissionsGeneration) return;
      permissionsSource.value = "unavailable";
      console.warn(
        "[plan] Permissions request failed - permission-gated nav items stay hidden",
        e,
      );
    }
  }

  async function setOrgFlagOverride(flagName: string, enabled: boolean | null): Promise<boolean> {
    try {
      if (enabled === null) {
        const res = await api.DELETE(
          '/api/v1/admin/feature-flags/{flag_name}/org-override',
          { params: { path: { flag_name: flagName } } },
        );
        if (res.error) return false;
      } else {
        const res = await api.PUT(
          '/api/v1/admin/feature-flags/{flag_name}/org-override',
          {
            params: { path: { flag_name: flagName } },
            body: { enabled },
          },
        );
        if (res.error) return false;
      }
      orgOverrides.value[flagName] = enabled;
      return true;
    } catch (err) {
      console.warn('[plan] Failed to set org flag override', err);
      return false;
    }
  }

  return {
    currentTier,
    features,
    flagsSource,
    devMode,
    isLoading,
    loaded,
    error,
    isTeam,
    expiresAt,
    orgId,
    tierLabels,
    tierRanks,
    orgOverrides,
    userPermissions,
    permissionsSource,
    fetchPlan,
    fetchPermissions,
    featureEnabled,
    getTierLabel,
    isAtMinimumTier,
    isCommunityTier,
    fetchOrgFlagOverride,
    setOrgFlagOverride,
    disposeHandlers,
  };
});
