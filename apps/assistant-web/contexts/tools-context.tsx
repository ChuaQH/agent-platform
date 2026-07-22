"use client";

import React, { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from "react";

export interface Tool {
  name: string;
  description: string;
  enabled: boolean;
  always_enabled: boolean;
}

interface ToolsContextValue {
  tools: Tool[];
  loading: boolean;
  error: string | null;
  setToolEnabled: (toolName: string, enabled: boolean) => void;
}

const ToolsContext = createContext<ToolsContextValue | null>(null);

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aca_token");
}

export function ToolsProvider({ children }: { children: ReactNode }) {
  const [tools, setTools] = useState<Tool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchTools = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = getToken();
      const res = await fetch(`${API_URL}/api/tools`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Failed to fetch tools: ${res.status}`);
      const data: Tool[] = await res.json();
      setTools(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load tools");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTools();
  }, [fetchTools]);

  const toolsRef = useRef(tools);
  toolsRef.current = tools;

  const setToolEnabled = useCallback(
    async (toolName: string, enabled: boolean) => {
      const tool = toolsRef.current.find((t) => t.name === toolName);
      if (tool?.always_enabled) return;

      setTools((prev) =>
        prev.map((t) => (t.name === toolName ? { ...t, enabled } : t))
      );
      try {
        const token = getToken();
        const res = await fetch(
          `${API_URL}/api/tools/${encodeURIComponent(toolName)}/toggle`,
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: JSON.stringify({ enabled }),
          }
        );
        if (!res.ok) throw new Error(`Toggle failed: ${res.status}`);
      } catch {
        setTools((prev) =>
          prev.map((t) =>
            t.name === toolName ? { ...t, enabled: !enabled } : t
          )
        );
      }
    },
    []
  );

  return (
    <ToolsContext.Provider value={{ tools, loading, error, setToolEnabled }}>
      {children}
    </ToolsContext.Provider>
  );
}

export function useTools(): ToolsContextValue {
  const ctx = useContext(ToolsContext);
  if (!ctx) {
    throw new Error("useTools must be used within a ToolsProvider");
  }
  return ctx;
}
