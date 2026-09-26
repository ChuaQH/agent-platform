export type ThreadRunStatus = "running" | "waiting_for_approval" | "completed" | "failed" | "cancelled";

export type ThreadRunSummary = {
  run_id: string;
  strategy: string;
  status: ThreadRunStatus;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  terminal_reason: string | null;
  action_count: number;
  executed_count: number;
  succeeded_count: number;
  rejected_count: number;
  waiting_for_approval_count: number;
  uncertain_count: number;
  has_pending_approval: boolean;
};

const SAFE_STATUSES = new Set<ThreadRunStatus>(["running", "waiting_for_approval", "completed", "failed", "cancelled"]);

export function previewText(value: unknown): string {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return "Preview unavailable"; }
}

export function readableReason(code: string | null): string | null {
  if (!code) return null;
  const messages: Record<string, string> = {
    invalid_json: "The proposed arguments were not valid JSON.", arguments_not_object: "The proposed arguments were not an object.",
    schema_validation_failed: "The proposed arguments did not match this tool's contract.", tool_unavailable: "The tool was not available in this run.",
    tool_not_allowed: "This tool was outside the effective allowlist.", capability_not_allowed: "The action required a capability outside the effective policy.",
    approval_required: "The gateway paused before dispatch for exact-action approval.", approval_denied: "The exact action was denied and was not dispatched.",
    approval_expired: "The approval expired before dispatch.", tool_call_limit_exhausted: "The run's total tool-call limit was reached.",
    per_tool_call_limit_exhausted: "This tool's call limit was reached.", cumulative_execution_time_exhausted: "The cumulative tool-execution-time limit was reached.",
    tool_execution_failed: "The tool reported an execution failure.", execution_interrupted: "Dispatch began, but the outcome is uncertain after interruption.",
  };
  return messages[code] || `Gateway reason: ${code.replaceAll("_", " ")}.`;
}

export function runStatusLabel(status: ThreadRunStatus): string {
  return {
    running: "Running", waiting_for_approval: "Waiting for approval", completed: "Completed",
    failed: "Failed", cancelled: "Cancelled",
  }[status];
}

function numberField(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function parseRunSummaries(value: unknown): ThreadRunSummary[] {
  if (!Array.isArray(value)) return [];
  const summaries: ThreadRunSummary[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const counts = ["action_count", "executed_count", "succeeded_count", "rejected_count", "waiting_for_approval_count", "uncertain_count"]
      .map((key) => numberField(row[key]));
    if (typeof row.run_id !== "string" || typeof row.strategy !== "string" || typeof row.status !== "string" || !SAFE_STATUSES.has(row.status as ThreadRunStatus)
      || typeof row.created_at !== "string" || typeof row.updated_at !== "string" || typeof row.has_pending_approval !== "boolean" || counts.some((count) => count === null)
      || (row.completed_at !== null && typeof row.completed_at !== "string") || (row.terminal_reason !== null && typeof row.terminal_reason !== "string")) continue;
    summaries.push({
      run_id: row.run_id, strategy: row.strategy, status: row.status as ThreadRunStatus,
      created_at: row.created_at, updated_at: row.updated_at,
      completed_at: row.completed_at as string | null, terminal_reason: row.terminal_reason as string | null,
      action_count: counts[0]!, executed_count: counts[1]!, succeeded_count: counts[2]!, rejected_count: counts[3]!,
      waiting_for_approval_count: counts[4]!, uncertain_count: counts[5]!, has_pending_approval: row.has_pending_approval,
    });
  }
  return summaries;
}

export function mergeRunSummaries(current: readonly ThreadRunSummary[], incoming: readonly ThreadRunSummary[]): ThreadRunSummary[] {
  const byId = new Map(current.map((item) => [item.run_id, item]));
  for (const item of incoming) byId.set(item.run_id, item);
  return [...byId.values()].sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.run_id.localeCompare(left.run_id));
}
