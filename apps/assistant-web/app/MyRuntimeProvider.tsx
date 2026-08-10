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

  const onSwitchToNewThread = useCallback(() => {
    setThreadId(null);
    setMessages([]);
    currentThreadIdRef.current = null;
  }, []);

  const onSwitchToThread = useCallback((id: string) => {
    setThreadId(id);
    setInitialLoading(true);
    currentThreadIdRef.current = id;

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
        setMessages(msgs);
        setBackendIds(newIds);
      } catch {
        // ignore
      } finally {
        setInitialLoading(false);
      }
    })();
  }, []);

  // Track the active thread for sidebar highlighting
  const threadList = useThreadListAdapter({
    threadId,
    onSwitchToNewThread,
    onSwitchToThread,
  });

  const onNew = useCallback(
    async (message: AppendMessage) => {
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

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
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
    [messages, threadList.refresh]
  );

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

  if (initialLoading) {
    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <div className="flex h-full items-center justify-center">
          <div className="animate-pulse text-sm text-muted-foreground">
            Loading chat...
          </div>
        </div>
      </AssistantRuntimeProvider>
    );
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadListRefreshContext.Provider value={threadList.refresh}>
        <BackendMessageIdContext.Provider value={backendIds}>
          <ThreadIdContext.Provider value={threadId}>
            {children}
          </ThreadIdContext.Provider>
        </BackendMessageIdContext.Provider>
      </ThreadListRefreshContext.Provider>
    </AssistantRuntimeProvider>
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
