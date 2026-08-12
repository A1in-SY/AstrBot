import type {
  CronJobRequest,
  ScriptDiagnostic,
  ScriptLanguageRegistry,
  ScriptValidationResult,
} from "@/api/generated/openapi-v1";

export type {
  ScriptDiagnostic,
  ScriptLanguageRegistry,
  ScriptValidationResult,
};

export const DEFAULT_SCRIPT_LANGUAGE_VERSION = "astrbot-python-subset/v1";

export interface ScriptExecutionAuthorization {
  allowed: boolean;
  reason?: string | null;
}

export interface ScriptJobSummary {
  language_version?: string;
  bound_umo?: string;
  execution_authorization?: ScriptExecutionAuthorization;
}

export interface ScriptJobDetail {
  source: string;
  language_version: string;
  bound_umo: string;
  state: Record<string, unknown>;
  creator_sender_id?: string | null;
}

export interface CronJob {
  job_id: string;
  job_type: "active_agent" | "script" | string;
  name?: string;
  description?: string | null;
  note?: string;
  cron_expression?: string | null;
  timezone?: string | null;
  enabled?: boolean;
  run_once?: boolean;
  run_at?: string | null;
  status?: string;
  last_run_at?: string | null;
  next_run_time?: string | null;
  last_error?: string | null;
  session?: string;
  payload?: {
    session?: string;
    run_at?: string;
    [key: string]: unknown;
  };
  script_summary?: ScriptJobSummary | null;
  script?: ScriptJobDetail;
}

export interface ScriptCronJobDetail extends CronJob {
  job_type: "script";
  script: ScriptJobDetail;
}

export interface ScriptSourceDraft {
  source: string;
  language_version: string;
}

export interface ScriptTaskUpdateRequest {
  name: string;
  note: string;
  bound_umo: string;
  cron_expression: string;
  run_once: boolean;
  run_at: string;
}

export interface ScriptTaskCreateRequest extends ScriptTaskUpdateRequest {
  job_type: "script";
  source: string;
  language_version: string;
}

export interface CronRunAccepted {
  job_id: string;
  accepted: boolean;
}

export interface CronApiErrorData {
  code?: string;
  [key: string]: unknown;
}

export interface CronApiErrorEnvelope {
  status?: "error";
  message?: string | null;
  data?: CronApiErrorData;
}

export function createEmptyScriptSourceDraft(): ScriptSourceDraft {
  return {
    source: "",
    language_version: DEFAULT_SCRIPT_LANGUAGE_VERSION,
  };
}
