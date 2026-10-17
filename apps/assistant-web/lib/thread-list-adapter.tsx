"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface BackendThread {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ThreadData {
  status: "regular";
  id: string;
  title?: string;
}

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aca_token");
}

async function authFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_URL}${path}`, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    throw new Error((err as { error?: string }).error || `HTTP ${res.status}`);
  }
  return res.json();
}

interface UseThreadListAdapterOptions {
  threadId: string | null;
  onSwitchToNewThread: () => void;
  onSwitchToThread: (threadId: string) => void;
}

export function useThreadListAdapter({
  threadId,
  onSwitchToNewThread,
  onSwitchToThread,
}: UseThreadListAdapterOptions) {
  const [threads, setThreads] = useState<ThreadData[]>([]);
  const [archivedThreads, setArchivedThreads] = useState<ThreadData[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setIsLoading(true);
      const data = await authFetch<BackendThread[]>("/api/threads");
      setThreads(
        data.map((t) => ({
          status: "regular" as const,
          id: t.id,
          title: t.title,
        }))
      );
    } catch {
      // ignore
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onDelete = useCallback(
    async (id: string) => {
      try {
        await authFetch(`/api/threads/${id}`, { method: "DELETE" });
        if (id === threadId) {
          onSwitchToNewThread();
        }
        await refresh();
      } catch {
        // ignore
      }
    },
    [onSwitchToNewThread, refresh, threadId]
  );

  const onRename = useCallback(
    async (threadId: string, newTitle: string) => {
      try {
        await authFetch(`/api/threads/${threadId}/title`, {
          method: "PUT",
          body: JSON.stringify({ title: newTitle }),
        });
        refresh();
      } catch {
        // ignore
      }
    },
    [refresh]
  );

  return {
    threadId: threadId ?? undefined,
    threads,
    archivedThreads,
    isLoading,
    onSwitchToNewThread,
    onSwitchToThread,
    onDelete,
    onRename,
    refresh,
  };
}

// Shared context so EditTitle in thread-list.tsx can trigger a refresh after rename
export const ThreadListRefreshContext = createContext<(() => void) | null>(null);

export function useThreadListRefresh() {
  return useContext(ThreadListRefreshContext);
}
