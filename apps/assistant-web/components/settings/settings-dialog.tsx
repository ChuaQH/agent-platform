"use client";

import { useState } from "react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { SettingsNavigation, type SettingsTab } from "@/components/settings/settings-navigation";
import { SkillsSettings } from "@/components/settings/skills-settings";
import { ToolsSettings } from "@/components/settings/tools-settings";

export function SettingsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [activeTab, setActiveTab] = useState<SettingsTab>("skills");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton
        className="flex max-h-[85vh] w-full max-w-[800px] flex-col overflow-hidden rounded-xl p-0 sm:max-w-[800px]"
      >
        <div className="flex flex-1 overflow-hidden">
          <SettingsNavigation activeTab={activeTab} onTabChange={setActiveTab} />
          <div className="flex-1 overflow-y-auto px-8 py-6">
            {activeTab === "skills" && <SkillsSettings />}
            {activeTab === "tools" && <ToolsSettings />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
