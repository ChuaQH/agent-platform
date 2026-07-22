"use client";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { id: "skills", label: "Skills" },
  { id: "tools", label: "Tools" },
] as const;

export type SettingsTab = (typeof NAV_ITEMS)[number]["id"];

export function SettingsNavigation({
  activeTab,
  onTabChange,
}: {
  activeTab: SettingsTab;
  onTabChange: (tab: SettingsTab) => void;
}) {
  return (
    <nav className="w-48 shrink-0 border-r border-border py-6 px-4">
      {NAV_ITEMS.map((item) => (
        <button
          key={item.id}
          onClick={() => onTabChange(item.id)}
          className={cn(
            "flex w-full items-center rounded-md px-3 py-2 text-sm transition-colors",
            activeTab === item.id
              ? "bg-accent font-medium text-accent-foreground"
              : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          )}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}
