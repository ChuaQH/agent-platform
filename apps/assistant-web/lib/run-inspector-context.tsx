"use client";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { mergeRunSummaries, parseRunSummaries, previewText as formatPreview, readableReason as reasonMessage, runStatusLabel, type ThreadRunSummary } from "@/lib/governed-run-utils";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type AuditAction = {
  action_id: string; sequence: number; tool_name: string; required_capabilities: string[];
  fingerprint: string | null; arguments_preview: unknown; status: string;
  normalized_result_status: string | null; reason_code: string | null;
  proposed_at: string; execution_started_at: string | null; completed_at: string | null;
  approval_required: boolean; approval: { status: string; requested_at: string; expires_at: string; decided_at: string | null } | null;
  dispatched: boolean; events: { state: string; timestamp: string; reason_code: string | null }[];
};

type Audit = {
  run: {
    run_id: string; thread_id: string; strategy: string; status: string; created_at: string; updated_at: string;
    completed_at: string | null; terminal_reason: string | null;
    policy: { allowed_capabilities: string[]; allowed_tools: string[] | null; approval_required_capabilities: string[]; approval_required_tools: string[] | null; max_tool_calls: number | null; max_calls_per_tool: number | null; max_cumulative_tool_execution_seconds: number | null };
    budget: { tool_call_attempts: number; tool_calls_by_name: Record<string, number>; cumulative_tool_execution_seconds: number; llm_usage: Record<string, number> };
  };
  actions: AuditAction[];
  timeline: { sequence: number; action_id: string | null; action_sequence: number | null; state: string; label: string; timestamp: string; reason_code: string | null }[];
};

export type { ThreadRunSummary } from "@/lib/governed-run-utils";

type InspectorContextValue = {
  openRunInspector: (runId: string) => void;
  openThreadActivity: () => void;
};
const InspectorContext = createContext<InspectorContextValue | null>(null);

function getToken(): string | null {
  return typeof window === "undefined" ? null : localStorage.getItem("aca_token");
}

export function previewText(value: unknown): string {
  return formatPreview(value);
}

export function readableReason(code: string | null): string | null {
  return reasonMessage(code);
}

function shortFingerprint(value: string | null): string {
  return value && value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value || "Not recorded";
}

function stamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function RunInspectorProvider({
  children, threadId, discoveredRuns, activityRevision,
}: {
  children: React.ReactNode;
  threadId: string | null;
  discoveredRuns: readonly ThreadRunSummary[];
  activityRevision: number;
}) {
  const [runId, setRunId] = useState<string | null>(null);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const [runs, setRuns] = useState<ThreadRunSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [activityLoading, setActivityLoading] = useState(false);
  const [activityError, setActivityError] = useState<string | null>(null);
  const activeThreadRef = useRef(threadId);
  const nextCursorRef = useRef<string | null>(null);

  useEffect(() => { activeThreadRef.current = threadId; }, [threadId]);

  const loadActivity = useCallback(async (reset: boolean) => {
    if (!threadId) return;
    const cursor = reset ? null : nextCursorRef.current;
    if (!reset && !cursor) return;
    const expectedThread = threadId;
    setActivityLoading(true); setActivityError(null);
    try {
      const token = getToken();
      const query = new URLSearchParams({ page_size: "20" });
      if (cursor) query.set("cursor", cursor);
      const response = await fetch(`${API_URL}/api/threads/${encodeURIComponent(expectedThread)}/runs?${query}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) throw new Error(response.status === 404 ? "This thread is unavailable." : "Activity could not be loaded.");
      const value = await response.json() as { runs?: unknown; next_cursor?: string | null };
      const incoming = parseRunSummaries(value.runs);
      setRuns((current) => {
        if (activeThreadRef.current !== expectedThread) return current;
        if (reset) return incoming;
        return mergeRunSummaries(current, incoming);
      });
      if (activeThreadRef.current === expectedThread) {
        nextCursorRef.current = value.next_cursor || null;
        setNextCursor(value.next_cursor || null);
      }
    } catch (cause: unknown) {
      if (activeThreadRef.current === expectedThread) setActivityError(cause instanceof Error ? cause.message : "Activity could not be loaded.");
    } finally {
      if (activeThreadRef.current === expectedThread) setActivityLoading(false);
    }
  }, [threadId]);

  useEffect(() => {
    nextCursorRef.current = null;
    setRuns([]); setNextCursor(null); setActivityError(null); setActivityOpen(false);
  }, [threadId]);

  useEffect(() => {
    if (!activityOpen) return;
    void loadActivity(true);
  }, [activityOpen, activityRevision, loadActivity]);

  useEffect(() => {
    if (!activityOpen || !discoveredRuns.length) return;
    setRuns((current) => {
      return mergeRunSummaries(discoveredRuns, current);
    });
  }, [activityOpen, discoveredRuns]);

  useEffect(() => {
    if (!runId) return;
    let active = true;
    setLoading(true); setError(null); setAudit(null);
    const token = getToken();
    fetch(`${API_URL}/api/runs/${encodeURIComponent(runId)}/audit`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 404 ? "This run is unavailable." : "Run details could not be loaded.");
        return response.json() as Promise<Audit>;
      })
      .then((value) => { if (active) setAudit(value); })
      .catch((cause: unknown) => { if (active) setError(cause instanceof Error ? cause.message : "Run details could not be loaded."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [runId]);

  const value = useMemo(() => ({
    openRunInspector: (id: string) => setRunId(id),
    openThreadActivity: () => setActivityOpen(true),
  }), []);
  return <InspectorContext.Provider value={value}>{children}
    <Dialog open={runId !== null} onOpenChange={(open) => { if (!open) setRunId(null); }}>
      <DialogContent className="max-h-[90dvh] max-w-3xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader><DialogTitle>Run details</DialogTitle><DialogDescription>The model proposes actions. The execution gateway independently validates policy, budgets, and approval before dispatch.</DialogDescription></DialogHeader>
        {loading && <p className="text-sm text-muted-foreground" role="status">Loading governed run details…</p>}
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        {!loading && !error && audit && <AuditPanel audit={audit} />}
        {!loading && !error && !audit && <p className="text-sm text-muted-foreground">No run details are available.</p>}
      </DialogContent>
    </Dialog>
    <Dialog open={activityOpen} onOpenChange={setActivityOpen}>
      <DialogContent className="max-h-[90dvh] max-w-xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader><DialogTitle>Thread activity</DialogTitle><DialogDescription>Recent governed runs for this thread. Select one for its detailed activity.</DialogDescription></DialogHeader>
        {activityLoading && !runs.length && <p className="text-sm text-muted-foreground" role="status">Loading activity...</p>}
        {activityError && <div className="space-y-2"><p className="text-sm text-destructive" role="alert">{activityError}</p><Button type="button" size="sm" variant="outline" onClick={() => void loadActivity(true)}>Retry</Button></div>}
        {!activityLoading && !activityError && !runs.length && <p className="text-sm text-muted-foreground">No governed runs are available for this thread.</p>}
        {!!runs.length && <ol className="space-y-2" aria-label="Recent governed runs">{runs.map((run) => <li key={run.run_id}><Button type="button" variant="outline" className="h-auto w-full justify-start p-3 text-left" onClick={() => { setActivityOpen(false); setRunId(run.run_id); }}><RunSummaryCard run={run} /></Button></li>)}</ol>}
        {nextCursor && <Button type="button" variant="outline" disabled={activityLoading} onClick={() => void loadActivity(false)}>{activityLoading ? "Loading..." : "Load more"}</Button>}
      </DialogContent>
    </Dialog>
  </InspectorContext.Provider>;
}

export function useRunInspector(): InspectorContextValue {
  const value = useContext(InspectorContext);
  if (!value) throw new Error("useRunInspector must be used inside RunInspectorProvider");
  return value;
}

function AuditPanel({ audit }: { audit: Audit }) {
  const { run } = audit;
  return <div className="space-y-5 text-sm">
    <section className="rounded-lg border p-3"><h3 className="font-medium">Run overview</h3><dl className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"><div><dt className="text-muted-foreground">Status</dt><dd>{run.status.replaceAll("_", " ")}</dd></div><div><dt className="text-muted-foreground">Strategy</dt><dd>{run.strategy}</dd></div><div><dt className="text-muted-foreground">Tool calls</dt><dd>{run.budget.tool_call_attempts} / {run.policy.max_tool_calls ?? "—"}</dd></div><div><dt className="text-muted-foreground">Updated</dt><dd>{stamp(run.updated_at)}</dd></div></dl>{run.terminal_reason && <p className="mt-2 text-xs text-muted-foreground">Terminal reason: {readableReason(run.terminal_reason)}</p>}</section>
    <section className="rounded-lg border p-3"><h3 className="font-medium">Effective authority</h3><div className="mt-2 grid gap-2 text-xs sm:grid-cols-2"><p><span className="text-muted-foreground">Capabilities:</span> {run.policy.allowed_capabilities.join(", ") || "None"}</p><p><span className="text-muted-foreground">Tools:</span> {run.policy.allowed_tools?.join(", ") || "All registered tools"}</p><p><span className="text-muted-foreground">Approval:</span> {run.policy.approval_required_capabilities.join(", ") || "None"}</p><p><span className="text-muted-foreground">Limits:</span> {run.policy.max_calls_per_tool ?? "—"}/tool · {run.policy.max_cumulative_tool_execution_seconds ?? "—"}s cumulative</p></div></section>
    <section><h3 className="font-medium">Action timeline</h3><ol className="mt-2 space-y-3" aria-label="Governed action timeline">{audit.actions.map((action) => <ActionCard key={action.action_id} action={action} />)}</ol></section>
  </div>;
}

function RunSummaryCard({ run }: { run: ThreadRunSummary }) {
  const status = runStatusLabel(run.status);
  return <span className="block w-full"><span className="flex flex-wrap justify-between gap-2"><span className="font-medium">{status}</span><span className="text-xs text-muted-foreground">{stamp(run.created_at)}</span></span><span className="mt-1 block text-xs text-muted-foreground">{run.strategy} - {run.action_count} action{run.action_count === 1 ? "" : "s"}{run.has_pending_approval ? " - approval pending" : ""}</span></span>;
}

function ActionCard({ action }: { action: AuditAction }) {
  const [expanded, setExpanded] = useState(false);
  const copy = async () => { if (action.fingerprint && navigator.clipboard) await navigator.clipboard.writeText(action.fingerprint); };
  return <li className="rounded-lg border p-3"><div className="flex flex-wrap items-start justify-between gap-2"><div><p className="font-medium">{action.sequence}. {action.tool_name}</p><p className="text-xs text-muted-foreground">{action.required_capabilities.join(", ") || "No declared capabilities"} · {action.dispatched ? "Dispatched" : "Not dispatched"}</p></div><span className="rounded bg-muted px-2 py-0.5 text-xs">{action.status.replaceAll("_", " ")}</span></div>
    {action.reason_code && <p className="mt-2 text-xs text-muted-foreground">{readableReason(action.reason_code)}</p>}
    {action.approval_required && <p className="mt-2 text-xs">Approval: {action.approval?.status || "pending"} · {action.dispatched ? "Executed after approval." : "Not dispatched."}</p>}
    <pre className="mt-2 max-h-32 overflow-auto rounded bg-muted/70 p-2 text-xs whitespace-pre-wrap">{previewText(action.arguments_preview)}</pre>
    <div className="mt-2 flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setExpanded((open) => !open)}>{expanded ? "Hide transitions" : "Show transitions"}</Button><Button type="button" size="sm" variant="ghost" onClick={copy} disabled={!action.fingerprint}>Copy action fingerprint</Button><span className="self-center text-xs text-muted-foreground" title={action.fingerprint || undefined}>{shortFingerprint(action.fingerprint)}</span></div>
    {expanded && <div className="mt-2 border-t pt-2 text-xs"><p className="mb-1 text-muted-foreground">The fingerprint identifies this exact approved action; it is not a digital signature.</p><ul className="space-y-1">{action.events.map((event, index) => <li key={`${event.state}-${index}`}>{event.state.replaceAll("_", " ")} · {stamp(event.timestamp)}{event.reason_code ? ` · ${event.reason_code}` : ""}</li>)}</ul></div>}
  </li>;
}
