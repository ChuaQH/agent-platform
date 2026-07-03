"use client";

import { createContext, useContext } from "react";

export const ThreadIdContext = createContext<string | null>(null);

export function useThreadId(): string | null {
  return useContext(ThreadIdContext);
}
