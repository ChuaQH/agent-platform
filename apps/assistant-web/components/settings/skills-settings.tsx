"use client";

import { useSkills } from "@/contexts/skills-context";
import { SkillToggleRow } from "@/components/settings/skill-toggle-row";

export function SkillsSettings() {
  const { skills, loading, error, setSkillEnabled } = useSkills();

  return (
    <div>
      <h2 className="text-lg font-semibold text-foreground">Skills</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Choose which skills the assistant can use. Skills extend the assistant
        with specialised instructions or behaviours for specific tasks.
      </p>

      {loading && (
        <p className="mt-6 text-sm text-muted-foreground">Loading skills...</p>
      )}

      {error && (
        <p className="mt-6 text-sm text-destructive">{error}</p>
      )}

      {!loading && !error && (
        <div className="mt-6 divide-y divide-border">
          {skills.map((skill) => (
            <SkillToggleRow
              key={skill.id}
              skill={skill}
              onToggle={setSkillEnabled}
            />
          ))}
        </div>
      )}
    </div>
  );
}
