"use client";

import { useTools } from "@/contexts/tools-context";
import { ToolToggleRow } from "@/components/settings/tool-toggle-row";

export function ToolsSettings() {
  const { tools, loading, error, setToolEnabled } = useTools();

  return (
    <div>
      <h2 className="text-lg font-semibold text-foreground">Tools</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Choose which tools the assistant can use. Disabled tools will not be
        available to the assistant during conversations.
      </p>

      {loading && (
        <p className="mt-6 text-sm text-muted-foreground">Loading tools...</p>
      )}

      {error && (
        <p className="mt-6 text-sm text-destructive">{error}</p>
      )}

      {!loading && !error && (
        <div className="mt-6 divide-y divide-border">
          {tools.map((tool) => (
            <ToolToggleRow
              key={tool.name}
              tool={tool}
              onToggle={setToolEnabled}
            />
          ))}
        </div>
      )}
    </div>
  );
}
