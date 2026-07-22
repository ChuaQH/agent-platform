"use client";

import { useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { useEffect, useCallback } from "react";
import { MyRuntimeProvider } from "@/app/MyRuntimeProvider";
import { ThreadList } from "@/components/thread-list";
import { Thread } from "@/components/thread";
import { SettingsDialog } from "@/components/settings/settings-dialog";
import { SkillsProvider } from "@/contexts/skills-context";
import { ToolsProvider } from "@/contexts/tools-context";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { MenuIcon, SettingsIcon, LogOutIcon, ChevronUpIcon } from "lucide-react";
import { useState } from "react";

export default function Home() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    if (!loading && !user) {
      router.push("/login");
    }
  }, [user, loading, router]);

  const handleLogout = useCallback(async () => {
    await logout();
    router.push("/login");
  }, [logout, router]);

  const initials = user?.display_name
    ? user.display_name
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : user?.email?.charAt(0).toUpperCase() ?? "?";

  if (loading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-background">
        <div className="animate-pulse text-muted-foreground">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <MyRuntimeProvider>
      <SkillsProvider>
        <ToolsProvider>
        <div className="flex h-dvh bg-background">
          {/* Sidebar overlay (mobile) */}
          {sidebarOpen && (
            <div
              className="fixed inset-0 z-40 bg-black/50 lg:hidden"
              onClick={() => setSidebarOpen(false)}
            />
          )}

          {/* Sidebar */}
          <aside
            className={`
              fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-border bg-sidebar
              transition-transform duration-200 ease-in-out
              lg:relative lg:translate-x-0
              ${sidebarOpen ? "translate-x-0" : "-translate-x-full"}
            `}
          >
            <div className="flex items-center justify-between border-b border-border p-3">
              <h2 className="text-sm font-semibold text-sidebar-foreground">
                Chats
              </h2>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 lg:hidden"
                onClick={() => setSidebarOpen(false)}
              >
                <MenuIcon className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto p-2">
              <ThreadList />
            </div>

            {/* User menu at the bottom of sidebar */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button className="flex w-full items-center gap-3 border-t border-border p-3 text-left hover:bg-sidebar-accent transition-colors">
                  <Avatar size="sm">
                    <AvatarFallback>{initials}</AvatarFallback>
                  </Avatar>
                  <div className="flex-1 min-w-0">
                    <p className="truncate text-sm font-medium text-sidebar-foreground">
                      {user.display_name || user.email}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {user.email}
                    </p>
                  </div>
                  <ChevronUpIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" sideOffset={4} align="center" className="min-w-56">
                <DropdownMenuItem onClick={() => setSettingsOpen(true)}>
                  <SettingsIcon className="h-4 w-4" />
                  Settings
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={handleLogout}>
                  <LogOutIcon className="h-4 w-4" />
                  Log out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </aside>

          {/* Main chat area */}
          <main className="flex flex-1 flex-col min-w-0">
            {/* Mobile header */}
            <div className="flex items-center gap-3 border-b border-border px-4 py-2 lg:hidden">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setSidebarOpen(true)}
              >
                <MenuIcon className="h-4 w-4" />
              </Button>
              <span className="text-sm font-semibold">Assistant Agent</span>
            </div>

            <Thread />
          </main>
        </div>

        {/* Settings dialog */}
        <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
        </ToolsProvider>
      </SkillsProvider>
    </MyRuntimeProvider>
  );
}
