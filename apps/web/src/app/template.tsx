import type { ReactNode } from "react";

/**
 * A template is re-created on every navigation (a layout is not), so each
 * page eases in as you arrive.
 */
export default function Template({ children }: { children: ReactNode }) {
  return <div className="rise">{children}</div>;
}
