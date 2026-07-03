"use client";

import { createContext, useContext } from "react";

/** Maps frontend-generated message IDs (msg-{ts}-{n}) to database message IDs (integer). */
export const BackendMessageIdContext = createContext<Map<string, number>>(new Map());

export function useBackendMessageId(frontendId: string): number | undefined {
  const map = useContext(BackendMessageIdContext);
  return map.get(frontendId);
}
