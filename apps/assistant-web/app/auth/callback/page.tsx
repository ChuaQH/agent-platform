"use client";

import { useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";

function CallbackHandler() {
  const searchParams = useSearchParams();

  useEffect(() => {
    const token = searchParams.get("token");
    if (token) {
      localStorage.setItem("aca_token", token);
      window.location.href = "/";
    } else {
      window.location.href = "/login?error=google_auth_failed";
    }
  }, [searchParams]);

  return (
    <div className="animate-pulse text-muted-foreground">
      Signing you in...
    </div>
  );
}

export default function AuthCallbackPage() {
  return (
    <div className="flex h-dvh items-center justify-center bg-background">
      <Suspense
        fallback={
          <div className="animate-pulse text-muted-foreground">
            Signing you in...
          </div>
        }
      >
        <CallbackHandler />
      </Suspense>
    </div>
  );
}
