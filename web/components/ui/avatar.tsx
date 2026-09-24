import * as React from "react";
import { cn } from "@/lib/utils";

export interface AvatarProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** One or two characters rendered inside the circle. */
  initials: string;
}

/** Minimal initials avatar (no Radix yet — kept dependency-free). */
const Avatar = React.forwardRef<HTMLSpanElement, AvatarProps>(
  ({ className, initials, ...props }, ref) => (
    <span
      ref={ref}
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 select-none place-items-center rounded-full bg-surface-sunken text-2xs font-semibold text-ink-secondary",
        className
      )}
      {...props}
    >
      {initials}
    </span>
  )
);
Avatar.displayName = "Avatar";

export { Avatar };
