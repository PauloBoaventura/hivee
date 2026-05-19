import { api } from "./client";

export interface SubscriptionInfo {
  id: string;
  name: string;
  description: string;
  provider: string;
  flag: string;
  default_model: string;
  api_base?: string;
}

export interface LLMConfig {
  provider: string;
  model: string;
  has_api_key: boolean;
  max_tokens: number | null;
  max_context_tokens: number | null;
  connected_providers: string[];
  active_subscription: string | null;
  detected_subscriptions: string[];
  subscriptions: SubscriptionInfo[];
}

export interface LLMConfigUpdateOptions {
  max_tokens?: number;
  max_context_tokens?: number;
}

export interface LLMConfigUpdateResponse {
  provider: string;
  model: string;
  has_api_key: boolean;
  max_tokens: number;
  max_context_tokens: number;
  sessions_swapped: number;
  active_subscription: string | null;
}

export interface ModelOption {
  id: string;
  label: string;
  recommended: boolean;
  max_tokens: number;
  max_context_tokens: number;
}


export interface TokenSettings {
  token_budget_total: number;
  max_output_tokens: number;
  reserved_response_tokens: number;
  max_input_tokens: number;
  token_estimation_enabled: boolean;
  auto_prune_context: boolean;
  auto_reduce_output_tokens: boolean;
  block_oversized_requests: boolean;
  include_tools_in_budget: boolean;
  include_history_in_budget: boolean;
  include_system_prompt_in_budget: boolean;
  rate_limit_max_retries: number;
}

export interface AppConfigResponse {
  token_settings: TokenSettings;
}

export interface ModelsCatalogue {
  models: Record<string, ModelOption[]>;
}

export const configApi = {
  getLLMConfig: () => api.get<LLMConfig>("/config/llm"),

  setLLMConfig: (provider: string, model: string, options?: LLMConfigUpdateOptions) =>
    api.put<LLMConfigUpdateResponse>("/config/llm", { provider, model, ...(options || {}) }),

  activateSubscription: (subscriptionId: string) =>
    api.put<LLMConfigUpdateResponse>("/config/llm", { subscription: subscriptionId }),

  getModels: () => api.get<ModelsCatalogue>("/config/models"),

  getAppConfig: () => api.get<AppConfigResponse>("/config"),

  patchAppConfig: (token_settings: Partial<TokenSettings>) =>
    api.patch<AppConfigResponse>("/config", { token_settings }),

  getProfile: () =>
    api.get<{ displayName: string; about: string; theme: string }>("/config/profile"),

  setProfile: (displayName: string, about: string, theme?: string) =>
    api.put<{ displayName: string; about: string; theme: string }>("/config/profile", {
      displayName,
      about,
      ...(theme ? { theme } : {}),
    }),

  uploadAvatar: (file: File) => {
    const fd = new FormData();
    fd.append("avatar", file);
    return api.upload<{ avatar_url: string }>("/config/profile/avatar", fd);
  },
};
