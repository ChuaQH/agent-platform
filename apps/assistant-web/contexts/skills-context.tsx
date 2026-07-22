"use client";

import React, { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from "react";

export interface Skill {
  id: number;
  name: string;
  description: string;
  enabled: boolean;
  isEditable?: boolean;
}

interface SkillsContextValue {
  skills: Skill[];
  loading: boolean;
  error: string | null;
  setSkillEnabled: (skillId: number, enabled: boolean) => void;
}

const SkillsContext = createContext<SkillsContextValue | null>(null);

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aca_token");
}

interface ApiSkillEntry {
  id: number;
  name: string;
  description: string;
  enabled: boolean;
}

interface ApiSkillsResponse {
  platform: ApiSkillEntry[];
  user: ApiSkillEntry[];
}

export function SkillsProvider({ children }: { children: ReactNode }) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSkills = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = getToken();
      const res = await fetch(`${API_URL}/api/skills`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Failed to fetch skills: ${res.status}`);
      const data: ApiSkillsResponse = await res.json();
      const all = [...data.platform, ...data.user];
      setSkills(all);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load skills");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  const setSkillEnabled = useCallback(
    async (skillId: number, enabled: boolean) => {
      // Optimistic update
      setSkills((prev) =>
        prev.map((s) => (s.id === skillId ? { ...s, enabled } : s))
      );
      try {
        const token = getToken();
        const res = await fetch(`${API_URL}/api/skills/${skillId}/toggle`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ enabled }),
        });
        if (!res.ok) throw new Error(`Toggle failed: ${res.status}`);
      } catch {
        // Revert on failure
        setSkills((prev) =>
          prev.map((s) => (s.id === skillId ? { ...s, enabled: !enabled } : s))
        );
      }
    },
    []
  );

  return (
    <SkillsContext.Provider value={{ skills, loading, error, setSkillEnabled }}>
      {children}
    </SkillsContext.Provider>
  );
}

export function useSkills(): SkillsContextValue {
  const ctx = useContext(SkillsContext);
  if (!ctx) {
    throw new Error("useSkills must be used within a SkillsProvider");
  }
  return ctx;
}
