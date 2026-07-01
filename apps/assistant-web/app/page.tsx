"use client";

import { useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { MyRuntimeProvider } from "@/app/MyRuntimeProvider";
import { ThreadList } from "@/components/thread-list";
import { Thread } from "@/components/thread";
import { MenuIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useState } from "react";

export default function Home() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    if (!loading && !user) {
      router.push("/login");
    }
  }, [user, loading, router]);

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
          {/* User info */}
          <div className="border-t border-border p-3">
            <p className="truncate text-sm font-medium text-sidebar-foreground">
              {user.display_name || user.email}
            </p>
            <p className="truncate text-xs text-muted-foreground">
              {user.email}
            </p>
          </div>
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
            <span className="text-sm font-semibold">ACA Assistant</span>
          </div>

          <Thread />
        </main>
      </div>
    </MyRuntimeProvider>
  );
}
