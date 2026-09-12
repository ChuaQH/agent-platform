"use client";

import {
  type AppendMessage,
  type ThreadMessageLike,
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AddToolResultOptions,
} from "@assistant-ui/react";
import { useState, useCallback, useRef, useEffect } from "react";
import { useThreadListAdapter, ThreadListRefreshContext } from "@/lib/thread-list-adapter";
import { BackendMessageIdContext } from "@/lib/backend-ids";
import { ThreadIdContext } from "@/lib/thread-id";
import {
  ApprovalProvider,
  approvalStatusFromBackend,
  type ApprovalState,
  type PendingApprovalResponse,
} from "@/lib/approval-context";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

let messageCounter = 0;
function nextId(): string {
  messageCounter++;
  return `msg-${Date.now()}-${messageCounter}`;
}

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aca_token");
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyPart = Record<string, any> & { type: string };

export function MyRuntimeProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [messages, setMessages] = useState<readonly ThreadMessageLike[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const [backendIds, setBackendIds] = useState<Map<string, number>>(new Map());
  const currentThreadIdRef = useRef<string | null>(null);
  const [approvals, setApprovals] = useState<Record<string, ApprovalState>>({});
  const approvalsRef = useRef<Record<string, ApprovalState>>({});

  const setApproval = useCallback((approval: ApprovalState) => {
    setApprovals((previous) => {
      const next = { ...previous, [approval.action_id]: { ...previous[approval.action_id], ...approval } };
      approvalsRef.current = next;
      return next;
    });
  }, []);

  const refreshApprovalsForThread = useCallback(async (id: string) => {
    const token = getToken();
    if (!token || currentThreadIdRef.current !== id) return;
    const response = await fetch(`${API_URL}/api/threads/${id}/pending-approvals`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok || currentThreadIdRef.current !== id) return;
    const data = await response.json() as { approvals?: PendingApprovalResponse[] };
    const incoming = data.approvals || [];
    setApprovals((previous) => {
      if (currentThreadIdRef.current !== id) return previous;
      const next = { ...previous };
      for (const item of incoming) {
        next[item.action_id] = {
          ...next[item.action_id], ...item,
          durableStatus: approvalStatusFromBackend(item.status ?? "pending_approval", item.decision ?? null),
          uiStatus: next[item.action_id]?.uiStatus === "resuming" ? "resuming" : "idle",
        };
      }
      approvalsRef.current = next;
      return next;
    });
  }, []);

  const onSwitchToNewThread = useCallback(() => {
    setThreadId(null);
    setMessages([]);
    currentThreadIdRef.current = null;
    approvalsRef.current = {};
    setApprovals({});
  }, []);

  const onSwitchToThread = useCallback((id: string) => {
    setThreadId(id);
    setInitialLoading(true);
    currentThreadIdRef.current = id;
    approvalsRef.current = {};
    setApprovals({});

    (async () => {
      try {
        const token = getToken();
        const res = await fetch(`${API_URL}/api/threads/${id}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error("Failed to load thread");
        const data = await res.json();
        const msgs: ThreadMessageLike[] = [];
        const newIds = new Map<string, number>();
        for (let i = 0; i < (data.messages || []).length; i++) {
          const m = data.messages[i];

          if (m.role === "tool") {
            if (msgs.length > 0) {
              const lastIdx = msgs.length - 1;
              const last = msgs[lastIdx];
              if (last.role === "assistant") {
                const merged = [
                  ...(Array.isArray(last.content) ? last.content : []),
                  {
                    type: "tool-call",
                    toolCallId: m.tool_call_id || "",
                    toolName: m.tool_name || "",
                    args: m.tool_result?.arguments || {},
                    result: m.tool_result?.result,
                    status: { type: "complete" },
                  },
                ] as AnyPart[];
                msgs[lastIdx] = {
                  ...last,
                  content: merged as unknown as ThreadMessageLike["content"],
                } as ThreadMessageLike;
              }
            }
            continue;
          }

          if (m.role === "user") {
            const fid = nextId();
            newIds.set(fid, m.id);
            msgs.push({
              id: fid,
              role: "user",
              content: [{ type: "text", text: m.content || "" }] as unknown as ThreadMessageLike["content"],
              parentId: msgs.length === 0 ? null : msgs[msgs.length - 1]?.id ?? null,
            } as ThreadMessageLike);
            continue;
          }

          const parts: AnyPart[] = [];

          if (m.reasoning) {
            parts.push({ type: "reasoning", text: m.reasoning });
          }

          if (m.content) {
            parts.push({ type: "text", text: m.content });
          }

          if (msgs.length > 0 && msgs[msgs.length - 1].role === "assistant") {
            const lastIdx = msgs.length - 1;
            const last = msgs[lastIdx];
            const merged = [
              ...(Array.isArray(last.content) ? last.content : []),
              ...parts,
            ] as AnyPart[];
            if (last.id) {
              newIds.set(last.id, m.id);
            }
            msgs[lastIdx] = {
              ...last,
              content: merged as unknown as ThreadMessageLike["content"],
            } as ThreadMessageLike;
          } else if (parts.length > 0) {
            const fid = nextId();
            newIds.set(fid, m.id);
            msgs.push({
              id: fid,
              role: "assistant",
              content: parts as unknown as ThreadMessageLike["content"],
              parentId: msgs.length === 0 ? null : msgs[msgs.length - 1]?.id ?? null,
            } as ThreadMessageLike);
          }
        }
        if (data.usage && msgs.length > 0) {
          const last = msgs[msgs.length - 1];
          if (last.role === "assistant") {
            const merged = [
              ...(Array.isArray(last.content) ? last.content : []),
              { type: "text", text: `\n\n> ${data.usage}` },
            ] as AnyPart[];
            msgs[msgs.length - 1] = {
              ...last,
              content: merged as unknown as ThreadMessageLike["content"],
            } as ThreadMessageLike;
          }
        }
        if (currentThreadIdRef.current !== id) return;
        setMessages(msgs);
        setBackendIds(newIds);
        await refreshApprovalsForThread(id);
      } catch {
        // ignore
      } finally {
        if (currentThreadIdRef.current === id) setInitialLoading(false);
      }
    })();
  }, [refreshApprovalsForThread]);

  // Track the active thread for sidebar highlighting
  const threadList = useThreadListAdapter({
    threadId,
    onSwitchToNewThread,
    onSwitchToThread,
  });

  const updateAssistantMessage = useCallback((assistantId: string, updater: (content: AnyPart[]) => void, running = true) => {
    setMessages((previous) => previous.map((message) => {
      if (message.id !== assistantId) return message;
      const content: AnyPart[] = Array.isArray(message.content) ? [...message.content] as AnyPart[] : [];
      updater(content);
      return { ...message, content: content as unknown as ThreadMessageLike["content"], status: { type: running ? "running" : "complete" } } as ThreadMessageLike;
    }));
  }, []);

  const fetchApproval = useCallback(async (runId: string, actionId: string, assistantMessageId?: string) => {
    const token = getToken();
    if (!token) return;
    const response = await fetch(`${API_URL}/api/runs/${runId}/actions/${actionId}/approval`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok || !currentThreadIdRef.current) return;
    const item = await response.json() as PendingApprovalResponse;
    setApproval({ ...item, decision: item.decision ?? null, durableStatus: approvalStatusFromBackend(item.status ?? "pending_approval", item.decision ?? null), uiStatus: "idle", assistantMessageId });
  }, [setApproval]);

  const handleSseEvent = useCallback(async (
    event: Record<string, unknown>, assistantId: string, expectedThreadId: string | null,
  ): Promise<"waiting" | "done" | "error" | null> => {
    if (expectedThreadId && currentThreadIdRef.current !== expectedThreadId) return null;
    const update = (updater: (content: AnyPart[]) => void, running = true) => updateAssistantMessage(assistantId, updater, running);
    switch (event.type) {
      case "reasoning": update((content) => content.push({ type: "reasoning", text: String(event.text || "") })); break;
      case "tool_call": update((content) => content.push({ type: "tool-call", toolCallId: String(event.id || ""), toolName: String(event.name || ""), args: event.arguments, status: { type: "running" } })); break;
      case "tool_result": {
        update((content) => {
          const index = content.findIndex((part) => part.type === "tool-call" && part.toolCallId === event.id);
          if (index >= 0) content[index] = { ...content[index], status: { type: "complete" }, result: event.result };
          else content.push({ type: "tool-call", toolCallId: String(event.id || ""), toolName: String(event.name || ""), args: {}, result: event.result, status: { type: "complete" } });
        });
        if (typeof event.action_id === "string") {
          setApprovals((previous) => {
            const current = previous[event.action_id as string];
            if (!current) return previous;
            const updated: ApprovalState = { ...current, durableStatus: current.durableStatus === "denied" || current.durableStatus === "expired" ? current.durableStatus : "resolved", uiStatus: "idle", executionStatus: typeof event.status === "string" ? event.status : undefined };
            const next: Record<string, ApprovalState> = { ...previous, [event.action_id as string]: updated };
            approvalsRef.current = next;
            return next;
          });
        }
        break;
      }
      case "text": update((content) => { const last = content[content.length - 1]; if (last?.type === "text" && typeof last.text === "string") last.text += String(event.content || ""); else content.push({ type: "text", text: String(event.content || "") }); }); break;
      case "usage": {
        const tokens = typeof event.total_tokens === "number" ? (event.total_tokens >= 1000 ? `${(event.total_tokens / 1000).toFixed(1)}K` : String(event.total_tokens)) : "";
        const time = typeof event.latency_ms === "number" ? (event.latency_ms >= 1000 ? `${(event.latency_ms / 1000).toFixed(1)}s` : `${event.latency_ms}ms`) : "";
        const parts = [typeof event.turns === "number" && `${event.turns} turn${event.turns === 1 ? "" : "s"}`, tokens && `${tokens} tokens`, time, typeof event.cached_tokens === "number" && event.cached_tokens > 0 && `${event.cached_tokens} cached`].filter(Boolean);
        if (parts.length) update((content) => content.push({ type: "text", text: `\n\n> ${parts.join(" · ")}` }));
        break;
      }
      case "approval_required": {
        if (typeof event.run_id === "string" && typeof event.action_id === "string") {
          const existing = approvalsRef.current[event.action_id];
          if (!existing) await fetchApproval(event.run_id, event.action_id, assistantId);
        }
        break;
      }
      case "waiting_for_approval": {
        setIsRunning(false);
        update(() => {}, false);
        return "waiting";
      }
      case "error": update((content) => content.push({ type: "text", text: `\n\n*${String(event.message || "Something went wrong")}*` }), false); setIsRunning(false); return "error";
      case "limit_reached": update((content) => content.push({ type: "text", text: "\n\n*Reached the maximum number of steps. Please continue in a new message.*" })); break;
      case "done": update(() => {}, false); setIsRunning(false); return "done";
    }
    return null;
  }, [fetchApproval, updateAssistantMessage]);

  const consumeSse = useCallback(async (response: Response, assistantId: string, expectedThreadId: string | null) => {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("No response body");
    const decoder = new TextDecoder(); let buffer = ""; let terminal: "waiting" | "done" | "error" | null = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n"); buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try { const state = await handleSseEvent(JSON.parse(line.slice(6)) as Record<string, unknown>, assistantId, expectedThreadId); if (state) terminal = state; } catch { /* malformed event */ }
      }
    }
    return terminal;
  }, [handleSseEvent]);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      if (Object.values(approvalsRef.current).some((approval) => approval.durableStatus === "pending" && approval.uiStatus !== "error")) return;
      const textContent = message.content
        .filter(
          (p): p is { type: "text"; text: string } => p.type === "text"
        )
        .map((p) => p.text)
        .join("\n");

      if (!textContent.trim()) return;

      const userMsgId = nextId();
      const userMsg: ThreadMessageLike = {
        id: userMsgId,
        role: "user",
        content: [{ type: "text", text: textContent }],
      };

      const assistantId = nextId();
      const assistantMsg = {
        id: assistantId,
        role: "assistant" as const,
        content: [] as ThreadMessageLike["content"],
        status: { type: "running" as const },
        parentId: userMsgId,
      } as ThreadMessageLike;

      setMessages((prev) => {
        const lastMsg = prev[prev.length - 1];
        const userWithParent = { ...userMsg, parentId: (lastMsg?.id as string) ?? null } as ThreadMessageLike;
        return [...prev, userWithParent, assistantMsg];
      });
      setIsRunning(true);

      const token = getToken();
      const controller = new AbortController();
      abortRef.current = controller;

      const requestMessages = messages.map(convertToOpenAI);
      requestMessages.push({ role: "user", content: textContent });

      const updateAssistant = (updater: (content: AnyPart[]) => void) => {
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m;
            const content: AnyPart[] = Array.isArray(m.content)
              ? [...m.content] as AnyPart[]
              : [];
            updater(content);
            return {
              ...m,
              content: content as unknown as ThreadMessageLike["content"],
              status: { type: "running" },
            };
          })
        );
      };

      try {
        const res = await fetch(`${API_URL}/api/chat`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            messages: requestMessages,
            thread_id: currentThreadIdRef.current || undefined,
            client_message_id: userMsgId,
          }),
          signal: controller.signal,
        });

        if (res.status === 409) {
          // Duplicate submission: this user message was already accepted by
          // the backend (a retried request). No second execution happens;
          // reload the thread to show the existing conversation instead.
          const tid = currentThreadIdRef.current;
          setMessages((prev) =>
            prev.map((m) =>
              m.id !== assistantId ? m
                : ({ ...m, status: { type: "complete" } } as ThreadMessageLike)
            )
          );
          if (tid) {
            onSwitchToThread(tid);
          } else {
            threadList.refresh();
          }
          return;
        }

        if (!res.ok) throw new Error(`Backend error: ${res.status}`);

        const responseThreadId = res.headers.get("X-Thread-Id");
        if (responseThreadId) {
          currentThreadIdRef.current = responseThreadId;
          setThreadId(responseThreadId);
        }

        await consumeSse(res, assistantId, responseThreadId || currentThreadIdRef.current);

        const reader = { read: async () => ({ done: true, value: undefined as Uint8Array | undefined }) };
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";

        while (false) {
          const { done, value } = await reader!.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            try {
              const event = JSON.parse(line.slice(6));

              switch (event.type) {
                case "reasoning": {
                  updateAssistant((content) => {
                    content.push({ type: "reasoning", text: event.text });
                  });
                  break;
                }
                case "tool_call": {
                  updateAssistant((content) => {
                    content.push({
                      type: "tool-call",
                      toolCallId: event.id,
                      toolName: event.name,
                      args: event.arguments,
                      status: { type: "running" },
                    });
                  });
                  break;
                }
                case "tool_result": {
                  updateAssistant((content) => {
                    const idx = content.findIndex(
                      (p) => p.type === "tool-call" && p.toolCallId === event.id
                    );
                    if (idx !== -1) {
                      const part = { ...content[idx] };
                      part.status = { type: "complete" };
                      part.result = event.result;
                      content[idx] = part;
                    }
                  });
                  break;
                }
                case "text": {
                  updateAssistant((content) => {
                    const last = content[content.length - 1];
                    if (last?.type === "text" && typeof last.text === "string") {
                      last.text += event.content;
                    } else {
                      content.push({ type: "text", text: event.content });
                    }
                  });
                  break;
                }
                case "usage": {
                  const tokens = event.total_tokens
                    ? event.total_tokens >= 1000
                      ? `${(event.total_tokens / 1000).toFixed(1)}K`
                      : String(event.total_tokens)
                    : "";
                  const time = event.latency_ms
                    ? event.latency_ms >= 1000
                      ? `${(event.latency_ms / 1000).toFixed(1)}s`
                      : `${event.latency_ms}ms`
                    : "";
                  const parts = [];
                  if (event.turns) parts.push(`${event.turns} turn${event.turns > 1 ? "s" : ""}`);
                  if (tokens) parts.push(`${tokens} tokens`);
                  if (time) parts.push(time);
                  if (event.cached_tokens > 0) parts.push(`${event.cached_tokens} cached`);
                  const summary = parts.join(" · ");
                  if (summary) {
                    updateAssistant((content) => {
                      content.push({ type: "text", text: `\n\n> ${summary}` });
                    });
                  }
                  break;
                }
                case "error": {
                  updateAssistant((content) => {
                    content.push({ type: "text", text: `\n\n*${event.message}*` });
                  });
                  setMessages((prev) =>
                    prev.map((m) =>
                      m.id !== assistantId ? m
                        : ({ ...m, status: { type: "incomplete", reason: "error" } } as ThreadMessageLike)
                    )
                  );
                  break;
                }
                case "limit_reached": {
                  updateAssistant((content) => {
                    content.push({
                      type: "text",
                      text: "\n\n*Reached the maximum number of steps. Please continue in a new message.*",
                    });
                  });
                  break;
                }
                case "done": {
                  setMessages((prev) =>
                    prev.map((m) =>
                      m.id !== assistantId ? m
                        : ({ ...m, status: { type: "complete" } } as ThreadMessageLike)
                    )
                  );
                  break;
                }
              }
            } catch {
              // skip
            }
          }
        }

        threadList.refresh();

        if (currentThreadIdRef.current) {
          const tid = currentThreadIdRef.current;
          const tok = getToken();
          fetch(`${API_URL}/api/threads/${tid}`, {
            headers: tok ? { Authorization: `Bearer ${tok}` } : {},
          })
            .then((res) => res.json())
            .then((data) => {
              const bmsgs = (data.messages || []) as { id: number; role: string; content: string | null }[];
              for (let i = bmsgs.length - 1; i >= 0; i--) {
                if (bmsgs[i].role === "assistant" && bmsgs[i].content) {
                  setBackendIds((ids) => new Map(ids).set(assistantId, bmsgs[i].id));
                  break;
                }
              }
            })
            .catch(() => {});
        }
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") {
          setMessages((prev) =>
            prev.map((m) =>
              m.id !== assistantId ? m
                : ({ ...m, status: { type: "incomplete", reason: "cancelled" } } as ThreadMessageLike)
            )
          );
        } else {
          const errorText = err instanceof Error ? err.message : "Something went wrong";
          updateAssistant((content) => {
            content.push({ type: "text", text: `\n\n*Error: ${errorText}*` });
          });
          setMessages((prev) =>
            prev.map((m) =>
              m.id !== assistantId ? m
                : ({ ...m, status: { type: "incomplete", reason: "error" } } as ThreadMessageLike)
            )
          );
        }
        threadList.refresh();
      } finally {
        setIsRunning(false);
        abortRef.current = null;
      }
    },
    [messages, threadList.refresh, consumeSse]
  );

  const decideApproval = useCallback(async (actionId: string, decision: "approved" | "denied", reason?: string) => {
    const approval = approvalsRef.current[actionId];
    const activeThread = currentThreadIdRef.current;
    if (!approval || !activeThread || approval.durableStatus !== "pending" || approval.uiStatus !== "idle" || Date.parse(approval.expires_at) <= Date.now()) {
      if (approval && Date.parse(approval.expires_at) <= Date.now()) setApproval({ ...approval, durableStatus: "expired", uiStatus: "idle" });
      return;
    }
    setApproval({ ...approval, uiStatus: "submitting_decision", error: undefined });
    const token = getToken();
    if (!token) return;
    try {
      const decisionResponse = await fetch(`${API_URL}/api/runs/${approval.run_id}/actions/${approval.action_id}/${decision === "approved" ? "approve" : "deny"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ expected_fingerprint: approval.fingerprint, ...(decision === "denied" && reason ? { reason } : {}) }),
      });
      if (!decisionResponse.ok) {
        if (decisionResponse.status === 409) await refreshApprovalsForThread(activeThread);
        throw new Error(decisionResponse.status === 404 ? "This approval is no longer available." : "The approval decision could not be accepted.");
      }
      const decided = { ...approval, durableStatus: decision, uiStatus: "resuming" as const, error: undefined };
      setApproval(decided);
      // Deliberately bodyless: the backend resumes only durable server state.
      const resumeResponse = await fetch(`${API_URL}/api/runs/${approval.run_id}/resume`, {
        method: "POST", headers: { Authorization: `Bearer ${token}` },
      });
      if (!resumeResponse.ok) {
        if (resumeResponse.status === 409) await refreshApprovalsForThread(activeThread);
        throw new Error(resumeResponse.status === 422 ? "This paused run is no longer valid." : "The run could not be resumed. Refresh to check its status.");
      }
      const assistantId = approval.assistantMessageId || [...messages].reverse().find((message) => message.role === "assistant")?.id;
      if (!assistantId) throw new Error("The resumed message is no longer available. Refresh the thread.");
      setIsRunning(true);
      const terminal = await consumeSse(resumeResponse, String(assistantId), activeThread);
      if (!terminal) {
        setApproval({ ...decided, uiStatus: "error", error: "Connection ended before the run reported completion. Refresh to check its durable status." });
        setIsRunning(false);
      }
      threadList.refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to continue this run.";
      const current = approvalsRef.current[actionId] || approval;
      setApproval({ ...current, uiStatus: "error", error: message });
      setIsRunning(false);
    }
  }, [consumeSse, messages, refreshApprovalsForThread, setApproval, threadList]);

  const onCancel = useCallback(async () => {
    abortRef.current?.abort();
  }, []);

  const onAddToolResult = useCallback(
    ({ messageId, toolCallId, result }: AddToolResultOptions) => {
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== messageId) return m;
          if (!Array.isArray(m.content)) return m;
          const content = m.content.map((p) => {
            if (
              p.type === "tool-call" &&
              "toolCallId" in p &&
              (p as AnyPart).toolCallId === toolCallId
            ) {
              return { ...p, result } as typeof p;
            }
            return p;
          });
          return { ...m, content } as ThreadMessageLike;
        })
      );
    },
    []
  );

  const convertMessage = useCallback(
    (message: ThreadMessageLike) => message,
    []
  );

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages,
    isRunning,
    onNew,
    onCancel,
    onAddToolResult,
    convertMessage,
    adapters: {
      threadList: {
        threadId: threadList.threadId,
        threads: threadList.threads,
        isLoading: threadList.isLoading,
        onSwitchToNewThread: threadList.onSwitchToNewThread,
        onSwitchToThread: threadList.onSwitchToThread,
        onDelete: threadList.onDelete,
        onRename: threadList.onRename,
      },
    },
  });
  const approvalValue = {
    approvals: Object.values(approvals),
    hasWaitingApproval: Object.values(approvals).some((approval) => approval.durableStatus === "pending" || approval.uiStatus === "submitting_decision" || approval.uiStatus === "resuming"),
    decide: decideApproval,
    refreshForThread: refreshApprovalsForThread,
  };

  if (initialLoading) {
    return (
      <ApprovalProvider value={approvalValue}>
        <AssistantRuntimeProvider runtime={runtime}>
          <div className="flex h-full items-center justify-center">
            <div className="animate-pulse text-sm text-muted-foreground">Loading chat...</div>
          </div>
        </AssistantRuntimeProvider>
      </ApprovalProvider>
    );
  }

  return (
    <ApprovalProvider value={approvalValue}>
      <AssistantRuntimeProvider runtime={runtime}>
        <ThreadListRefreshContext.Provider value={threadList.refresh}>
          <BackendMessageIdContext.Provider value={backendIds}>
            <ThreadIdContext.Provider value={threadId}>{children}</ThreadIdContext.Provider>
          </BackendMessageIdContext.Provider>
        </ThreadListRefreshContext.Provider>
      </AssistantRuntimeProvider>
    </ApprovalProvider>
  );
}

function convertToOpenAI(msg: ThreadMessageLike): Record<string, unknown> {
  const content = Array.isArray(msg.content)
    ? msg.content
        .filter((p) => p.type === "text" && "text" in p)
        .map((p) => (p as { text: string }).text)
        .join("\n")
    : (msg.content as string);

  const result: Record<string, unknown> = { role: msg.role, content };

  if (Array.isArray(msg.content)) {
    const toolCalls = msg.content
      .filter((p) => p.type === "tool-call")
      .map((p: AnyPart) => ({
        id: p.toolCallId || "",
        type: "function",
        function: {
          name: p.toolName || "",
          arguments: JSON.stringify(p.args || {}),
        },
        result: p.result,
        name: p.toolName,
      }));
    if (toolCalls.length > 0) {
      result["tool_calls"] = toolCalls;
    }
  }

  return result;
}
