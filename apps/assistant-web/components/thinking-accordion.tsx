"use client";

import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { BrainIcon, ChevronDownIcon } from "lucide-react";
import { useScrollLock } from "@assistant-ui/react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

const ANIMATION_DURATION = 200;

export type ThinkingAccordionProps = React.ComponentProps<
  typeof Collapsible
> & {
  streaming?: boolean;
};

export function ThinkingAccordion({
  className,
  streaming,
  children,
  ...props
}: ThinkingAccordionProps) {
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

  const isOpen = userOpen ?? streaming ?? false;

  const prevStreamingRef = useRef(streaming);
  useLayoutEffect(() => {
    if (prevStreamingRef.current === streaming) return;
    prevStreamingRef.current = streaming;
    if (userOpen === null) lockScroll();
  }, [streaming, userOpen, lockScroll]);

  const handleOpenChange = useCallback(
    (open: boolean) => {
      lockScroll();
      setUserOpen(open);
    },
    [lockScroll],
  );

  return (
    <Collapsible
      ref={collapsibleRef}
      data-slot="thinking-accordion-root"
      open={isOpen}
      onOpenChange={handleOpenChange}
      className={cn("group/thinking my-2 rounded-lg border", className)}
      style={
        {
          "--animation-duration": `${ANIMATION_DURATION}ms`,
        } as React.CSSProperties
      }
      {...props}
    >
      <CollapsibleTrigger
        data-slot="thinking-accordion-trigger"
        className={cn(
          "group/trigger flex w-full cursor-pointer items-center gap-2 px-4 py-2",
          "font-medium text-sm hover:bg-muted/50 rounded-t-lg",
          "data-[state=closed]:rounded-b-lg",
        )}
      >
        <BrainIcon className="size-4 shrink-0" />
        <span className="relative inline-block leading-none">
          <span>Thinking</span>
          {streaming && (
            <span
              aria-hidden
              className="shimmer pointer-events-none absolute inset-0 motion-reduce:animate-none"
            >
              Thinking
            </span>
          )}
        </span>
        <ChevronDownIcon
          className={cn(
            "ml-auto size-4 shrink-0 transition-transform duration-(--animation-duration)",
            "ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none",
            "group-data-[state=closed]/trigger:-rotate-90",
            "group-data-[state=open]/trigger:rotate-0",
          )}
        />
      </CollapsibleTrigger>
      <CollapsibleContent
        data-slot="thinking-accordion-content"
        className={cn(
          "relative overflow-hidden text-sm outline-none",
          "ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
          "data-[state=closed]:animate-collapsible-up",
          "data-[state=open]:animate-collapsible-down",
          "data-[state=closed]:fill-mode-forwards",
          "data-[state=closed]:pointer-events-none",
          "data-[state=open]:duration-(--animation-duration)",
          "data-[state=closed]:duration-(--animation-duration)",
        )}
      >
        <div className="border-t p-3">{children}</div>
      </CollapsibleContent>
    </Collapsible>
  );
}
