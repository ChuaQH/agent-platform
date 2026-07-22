"use client";

import { Switch } from "@/components/ui/switch";
import type { Skill } from "@/contexts/skills-context";

export function SkillToggleRow({
  skill,
  onToggle,
}: {
  skill: Skill;
  onToggle: (skillId: number, enabled: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-foreground">{skill.name}</p>
        <p className="mt-0.5 text-sm text-muted-foreground">
          {skill.description}
        </p>
      </div>
      <Switch
        checked={skill.enabled}
        onCheckedChange={(checked: boolean) => onToggle(skill.id, checked)}
        className="mt-0.5 shrink-0"
      />
    </div>
  );
}
