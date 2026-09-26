"use client";

import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { type ApprovalState, useApprovals } from "@/lib/approval-context";
import { approvalPreviewText, approvalStatusCopy, isApprovalExpired } from "@/lib/approval-utils";
import { useRunInspector } from "@/lib/run-inspector-context";
import { CheckCircle2Icon, ChevronDownIcon, Clock3Icon, ShieldAlertIcon, XCircleIcon } from "lucide-react";
import { useMemo, useState } from "react";

function asPreview(value: unknown): string {
  return approvalPreviewText(value);
}

function shortFingerprint(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

function SpecialPreview({ approval }: { approval: ApprovalState }) {
  const preview = approval.arguments_preview;
  const values = preview && typeof preview === "object" && !Array.isArray(preview)
    ? preview as Record<string, unknown>
    : null;
  if (approval.tool_name === "write_file" && values) {
    const path = typeof values.path === "string" ? values.path : "(path not provided)";
    const mode = values.overwrite === true ? "Overwrite" : "Create or update";
    return <div className="space-y-1 text-xs"><p><strong>{mode}:</strong> <code>{path}</code></p><pre className="max-h-28 overflow-auto rounded bg-muted/70 p-2 whitespace-pre-wrap">{asPreview(values.content ?? "")}</pre></div>;
  }
  if (approval.tool_name === "assistant_remember" && values) {
    return <div className="space-y-1 text-xs"><p><strong>Memory:</strong> {asPreview(values.content ?? values)}</p>{values.category != null && <p><strong>Category:</strong> {asPreview(values.category)}</p>}</div>;
  }
  return <pre className="max-h-36 overflow-auto rounded bg-muted/70 p-2 text-xs whitespace-pre-wrap">{asPreview(preview)}</pre>;
}

function ApprovalCard({ approval }: { approval: ApprovalState }) {
  const { decide } = useApprovals();
  const { openRunInspector } = useRunInspector();
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const expired = isApprovalExpired(approval);
  const disabled = expired || approval.uiStatus === "submitting_decision" || approval.uiStatus === "resuming" || approval.durableStatus !== "pending";
  const expires = useMemo(() => new Date(approval.expires_at).toLocaleString(), [approval.expires_at]);
  const submitDeny = () => decide(approval.action_id, "denied", reason.trim() || undefined);

  return (
    <section className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-3 shadow-sm" aria-labelledby={`approval-${approval.action_id}`}>
      <div className="flex gap-2">
        {expired ? <Clock3Icon className="mt-0.5 size-5 text-muted-foreground" aria-hidden /> : approval.durableStatus === "denied" ? <XCircleIcon className="mt-0.5 size-5 text-destructive" aria-hidden /> : approval.durableStatus === "approved" || approval.durableStatus === "resolved" ? <CheckCircle2Icon className="mt-0.5 size-5 text-green-600 dark:text-green-400" aria-hidden /> : <ShieldAlertIcon className="mt-0.5 size-5 text-amber-600 dark:text-amber-400" aria-hidden />}
        <div className="min-w-0 flex-1 space-y-2">
          <div><h3 id={`approval-${approval.action_id}`} className="font-medium">Allow {approval.tool_name.replaceAll("_", " ")}</h3><p className="text-sm text-muted-foreground" role={approval.uiStatus === "error" ? "alert" : undefined}>{approvalStatusCopy(approval)}</p></div>
          <div><p className="mb-1 text-xs font-medium text-muted-foreground">Requested action</p><SpecialPreview approval={approval} /></div>
          <p className="text-xs text-muted-foreground">Required: {approval.required_capabilities.join(", ") || "No additional capability declared"} · {expired ? "Expired" : `Expires ${expires}`}</p>
          {approval.durableStatus === "pending" && !expired && (
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" onClick={() => decide(approval.action_id, "approved")} disabled={disabled}>Approve</Button>
              <Button type="button" size="sm" variant="outline" onClick={() => setReasonOpen((open) => !open)} disabled={disabled}>Deny</Button>
              {reasonOpen && <div className="flex w-full gap-2"><label className="sr-only" htmlFor={`denial-${approval.action_id}`}>Optional denial reason</label><input id={`denial-${approval.action_id}`} value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} placeholder="Optional reason" className="h-8 min-w-0 flex-1 rounded border bg-background px-2 text-sm" /><Button type="button" size="sm" variant="destructive" onClick={submitDeny} disabled={disabled}>Confirm denial</Button></div>}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2"><Button type="button" size="sm" variant="ghost" onClick={() => openRunInspector(approval.run_id)}>Run details</Button><Collapsible><CollapsibleTrigger className="flex items-center gap-1 text-xs text-muted-foreground underline underline-offset-2">Technical details <ChevronDownIcon className="size-3" /></CollapsibleTrigger><CollapsibleContent className="mt-1 space-y-1 text-xs text-muted-foreground"><p>Action ID: <code>{approval.action_id}</code></p><p>Fingerprint: <code title={approval.fingerprint}>{shortFingerprint(approval.fingerprint)}</code></p></CollapsibleContent></Collapsible></div>
        </div>
      </div>
    </section>
  );
}

export function ApprovalCards() {
  const { approvals } = useApprovals();
  if (!approvals.length) return null;
  return <div className="space-y-3" aria-label="Pending assistant approvals">{approvals.map((approval) => <ApprovalCard key={approval.action_id} approval={approval} />)}</div>;
}
