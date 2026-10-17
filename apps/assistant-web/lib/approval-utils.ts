export type ApprovalPresentation = {
  durableStatus: "pending" | "approved" | "denied" | "expired" | "executing" | "resolved";
  uiStatus: "idle" | "submitting_decision" | "resuming" | "error";
  expires_at: string;
  error?: string;
  executionStatus?: string;
};

export function approvalPreviewText(value: unknown): string {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

export function approvalStatusCopy(approval: ApprovalPresentation): string {
  if (approval.uiStatus === "submitting_decision") return "Recording your decision…";
  if (approval.uiStatus === "resuming") return approval.durableStatus === "denied" ? "Denied; continuing the assistant…" : "Approved; resuming…";
  if (approval.uiStatus === "error") return approval.error || "The decision could not be completed. Refresh and try again.";
  if (approval.durableStatus === "expired") return "This approval has expired.";
  if (approval.durableStatus === "denied") return approval.executionStatus ? "Denied; the assistant received the result." : "Denied; awaiting continuation.";
  if (approval.durableStatus === "approved") return approval.executionStatus ? "Approved; action result received." : "Approved; awaiting execution.";
  if (approval.durableStatus === "executing") return "Approved; action is executing…";
  if (approval.durableStatus === "resolved") return `Action resolved${approval.executionStatus ? ` (${approval.executionStatus})` : ""}.`;
  return "The assistant is requesting permission before it continues.";
}

/** Show only approval requests for which no durable decision exists yet. */
export function isApprovalVisible(
  approval: Pick<ApprovalPresentation, "durableStatus">,
): boolean {
  return approval.durableStatus === "pending";
}

export function isApprovalExpired(approval: Pick<ApprovalPresentation, "durableStatus" | "expires_at">, now = Date.now()): boolean {
  return approval.durableStatus === "expired" || Date.parse(approval.expires_at) <= now;
}
