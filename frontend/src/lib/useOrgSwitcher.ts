import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { api, tokenStore } from "./api";
import type { ScopeSwitcherReadState, ScopeSwitcherWriteState, SwitcherOption } from "../components/shell/EntitySwitcher";

interface OrgRow {
  org_id: string;
  name: string;
  current: boolean;
}

class InvalidOrganizationChoices extends Error {}

function parseOrganizations(value: unknown): OrgRow[] {
  if (!Array.isArray(value)) throw new InvalidOrganizationChoices();
  const ids = new Set<string>();
  return value.map((row: unknown) => {
    if (!row || typeof row !== "object") throw new InvalidOrganizationChoices();
    const { org_id, name, current } = row as Record<string, unknown>;
    if (typeof org_id !== "string" || !org_id.trim() || ids.has(org_id)
      || typeof name !== "string" || !name.trim() || typeof current !== "boolean") {
      throw new InvalidOrganizationChoices();
    }
    ids.add(org_id);
    return { org_id, name, current };
  });
}

// As in the portal read flow, honor seconds/date headers without overflowing
// timers. A missing/invalid header permits an explicit retry, never a loop.
function retryAfterDeadline(error: unknown, receivedAt: number): number {
  if (!isAxiosError(error) || error.response?.status !== 429) return 0;
  const header = error.response.headers["retry-after"];
  if (typeof header !== "string") return 0;
  const value = header.trim();
  const deadline = /^\d+$/.test(value) ? receivedAt + Number(value) * 1_000 : Date.parse(value);
  return Number.isSafeInteger(deadline) && deadline > 0 ? deadline : 0;
}

/**
 * Drives the shell's org `ScopeSwitcher` from `GET /auth/organizations` and
 * `POST /auth/switch-org/{id}`. The identity read owns the current organization;
 * the membership list only supplies choices. Read recovery cannot switch scope.
 */
export function useOrgSwitcher(): {
  options: SwitcherOption[];
  currentId: string;
  onSwitch: (id: string) => void;
  readState: ScopeSwitcherReadState;
  writeState: ScopeSwitcherWriteState;
} {
  const { user, org, cacheIdentity } = useAuth();
  const [switchState, setSwitchState] = useState<{ phase: ScopeSwitcherWriteState["phase"]; message: string }>({ phase: "idle", message: "" });
  const switchPhase = useRef(switchState.phase);
  const currentIdentity = useRef(cacheIdentity);
  currentIdentity.current = cacheIdentity;
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  function updateSwitch(phase: ScopeSwitcherWriteState["phase"], message: string) {
    switchPhase.current = phase;
    setSwitchState({ phase, message });
  }
  function ownership() {
    const identity = cacheIdentity, generation = tokenStore.generation(), token = tokenStore.get();
    return () => alive.current && currentIdentity.current === identity && tokenStore.generation() === generation && tokenStore.get() === token;
  }
  const enabled = Boolean(user?.id && org?.id);
  const query = useQuery<OrgRow[]>({
    queryKey: ["my-orgs", user?.id, org?.id],
    queryFn: async ({ signal }) => parseOrganizations((await api.get("/auth/organizations", { signal, timeout: 15_000 })).data),
    enabled,
    retry: false,
    retryOnMount: false,
    refetchOnMount: (cached) => cached.state.status !== "error",
    networkMode: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    meta: { errorHandled: true },
  });
  const orgs = query.data ?? [];
  const markedCurrent = orgs.filter((row) => row.current);
  const matchesIdentity = markedCurrent.length === 1 && markedCurrent[0].org_id === org?.id;
  const phase = query.isFetching || query.isPending ? "loading"
    : query.isError ? "error"
    : orgs.length === 0 ? "empty"
    : !matchesIdentity ? "mismatch" : "ready";

  const retryAt = useMemo(
    () => retryAfterDeadline(query.error, query.errorUpdatedAt),
    [query.error, query.errorUpdatedAt],
  );
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const remaining = retryAt - Date.now();
    if (remaining <= 0) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(remaining, 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [retryAt, now]);

  const status = isAxiosError(query.error) ? query.error.response?.status : undefined;
  const message = phase === "loading" ? "Loading organization choices…"
    : phase === "empty" ? "No organization choices were returned. Retry to check your access."
    : phase === "mismatch" ? "Organization choices do not match this session. Reload the workspace before switching."
    : query.error instanceof InvalidOrganizationChoices ? "Organization choices could not be verified. Retry to load them again."
    : status === 429 ? "Too many organization requests. Wait before retrying."
    : status === 403 ? "Organization choices are not available with your current access."
    : "Could not load organization choices. Check your connection and retry.";

  function retry() {
    if (!enabled || query.isFetching || retryAt > Date.now()) return;
    void query.refetch({ cancelRefetch: false });
  }

  async function onSwitch(id: string) {
    if (!id || id === org?.id || phase !== "ready" || !orgs.some((row) => row.org_id === id)
      || !["idle", "failed"].includes(switchPhase.current)) return;
    const current = ownership();
    updateSwitch("pending", "Switching organization… Wait for the workspace to reload.");
    try {
      await api.post(`/auth/switch-org/${id}`, null, { timeout: 15_000 });
      if (current()) location.assign("/");
    } catch (error) {
      if (!current()) return;
      const status = isAxiosError(error) ? error.response?.status : undefined;
      if (status !== undefined && status >= 400 && status < 500) {
        updateSwitch("failed", "The organization could not be switched. Check your access and choose an organization again.");
      } else {
        updateSwitch("uncertain", "The switch outcome is unconfirmed. Check the current organization before choosing again.");
      }
    }
  }

  async function checkCurrentOrganization() {
    if (switchPhase.current !== "uncertain") return;
    const current = ownership();
    updateSwitch("checking", "Checking the current organization…");
    try {
      // Read only: never replay the switch POST after a missing response.
      // A full document then restores AuthContext and its identity-owned cache.
      const result = await api.get("/auth/me", { timeout: 15_000 });
      if (!current()) return;
      if (result.data?.user?.id !== user?.id || typeof result.data?.organization?.id !== "string") throw new Error("Unverified identity");
      location.assign("/");
    } catch {
      if (current()) updateSwitch("uncertain", "The current organization could not be verified. Check your connection and try checking again.");
    }
  }

  return {
    options: phase === "ready" ? orgs.map((o) => ({ id: o.org_id, name: o.name })) : [],
    currentId: org?.id ?? "",
    onSwitch,
    writeState: { ...switchState, onCheck: () => { void checkCurrentOrganization(); } },
    readState: {
      phase,
      currentName: org?.name?.trim() || "Organization name unavailable",
      message,
      onRetry: query.isFetched && phase !== "mismatch" ? retry : undefined,
      isFetching: query.isFetching,
      retryDisabled: query.isFetching || retryAt > Date.now(),
    },
  };
}
