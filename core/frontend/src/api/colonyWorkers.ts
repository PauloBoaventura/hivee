import { api } from "./client";

export interface WorkerResult {
  status: string;
  summary: string;
  error: string | null;
  tokens_used: number;
  duration_seconds: number;
}

export interface WorkerSummary {
  worker_id: string;
  task: string;
  status: string;
  started_at: number;
  result: WorkerResult | null;
  /** Name of the colony's worker profile this worker was spawned from.
   *  Empty for legacy / single-template colonies. Surfaced as a small
   *  badge in the Sessions tab so the user can see which authorized
   *  account the worker is calling MCP tools as. */
  profile_name?: string;
}

export interface ColonySkill {
  name: string;
  description: string;
  location: string;
  base_dir: string;
  source_scope: string;
}

export interface ColonyTool {
  name: string;
  description: string;
  /** Canonical credential/provider key (e.g. "hubspot", "gmail") for
   *  tools bound to an Aden credential. ``null`` for framework/core
   *  tools that don't require a provider credential. */
  provider: string | null;
}

export const colonyWorkersApi = {
  /** List spawned workers (live + completed) for a colony session. */
  list: (sessionId: string) =>
    api.get<{ workers: WorkerSummary[] }>(`/sessions/${sessionId}/workers`),

  /** List the colony's shared skills catalog. */
  listSkills: (sessionId: string) =>
    api.get<{ skills: ColonySkill[] }>(`/sessions/${sessionId}/colony/skills`),

  /** List the colony's default tools. */
  listTools: (sessionId: string) =>
    api.get<{ tools: ColonyTool[] }>(`/sessions/${sessionId}/colony/tools`),
};
