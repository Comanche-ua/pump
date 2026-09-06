import { cn } from "@/lib/utils";

export function PulseMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("text-primary", className)} aria-hidden>
      <rect width="32" height="32" rx="6" fill="currentColor" className="text-bg-subtle" />
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="text-primary">
        <path d="M7 23a11 11 0 0 1 18 0" />
        <path d="M11 20.5a6.5 6.5 0 0 1 10 0" />
        <path d="M16 6.5v16" />
      </g>
    </svg>
  );
}
