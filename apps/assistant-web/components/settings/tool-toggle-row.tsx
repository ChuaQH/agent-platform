"use client";

import { Switch } from "@/components/ui/switch";
import type { Tool } from "@/contexts/tools-context";

export function ToolToggleRow({
  tool,
  onToggle,
}: {
  tool: Tool;
  onToggle: (toolName: string, enabled: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-foreground">{tool.name}</p>
        <p className="mt-0.5 text-sm text-muted-foreground">
          {tool.description}
        </p>
      </div>
      <Switch
        checked={tool.enabled}
        disabled={tool.always_enabled}
        onCheckedChange={(checked: boolean) => onToggle(tool.name, checked)}
        className="mt-0.5 shrink-0"
      />
    </div>
  );
}
