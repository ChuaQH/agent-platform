"use client";

import { createContext, useContext } from "react";

export type DurableApprovalStatus =
  | "pending"
  | "approved"
  | "denied"
  | "expired"
  | "executing"
  | "resolved";

export type ApprovalUiStatus =
  | "idle"
  | "submitting_decision"
  | "resuming"
  | "error";

export interface PendingApprovalResponse {
  run_id: string;
  action_id: string;
  tool_name: string;
  fingerprint: string;
  required_capabilities: string[];
  arguments_preview: unknown;
  requested_at: string;
  expires_at: string;
  decision?: "approved" | "denied" | "expired" | null;
  status?: string;
}

export interface ApprovalState extends PendingApprovalResponse {
  durableStatus: DurableApprovalStatus;
  uiStatus: ApprovalUiStatus;
  error?: string;
  executionStatus?: string;
  assistantMessageId?: string;
}

export interface ApprovalContextValue {
  approvals: readonly ApprovalState[];
  hasWaitingApproval: boolean;
  decide: (actionId: string, decision: "approved" | "denied", reason?: string) => Promise<void>;
  refreshForThread: (threadId: string) => Promise<void>;
}

const ApprovalContext = createContext<ApprovalContextValue | null>(null);

export const ApprovalProvider = ApprovalContext.Provider;

export function useApprovals(): ApprovalContextValue {
  const value = useContext(ApprovalContext);
  if (!value) throw new Error("useApprovals must be used inside MyRuntimeProvider");
  return value;
}

export function approvalStatusFromBackend(
  status: string,
  decision: PendingApprovalResponse["decision"],
): DurableApprovalStatus {
  if (decision === "approved" || status === "approved") return "approved";
  if (decision === "denied" || status === "denied") return "denied";
  if (decision === "expired" || status === "expired") return "expired";
  if (status === "executing") return "executing";
  if (status === "resolved" || status === "succeeded" || status === "failed") return "resolved";
  return "pending";
}
