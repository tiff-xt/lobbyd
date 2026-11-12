"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession } from "@/lib/useSession";
import { Logo } from "./Logo";

export function Header() {
  const { signedIn, user, signOut } = useSession();
  const pathname = usePathname();
  const link = (href: string, text: string) => (
    <Link
      href={href}
      className={`rounded-md px-3 py-1.5 text-sm transition-colors hover:text-text ${
        pathname === href ? "text-text" : "text-muted"
      }`}
    >
      {text}
    </Link>
  );

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-bg/80 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-2 px-4 sm:px-6">
        <Link href="/" className="mr-3 flex items-center gap-2" aria-label="lobbyd home">
          <Logo />
          <span className="font-display text-lg font-bold tracking-wide">lobbyd</span>
        </Link>
        <nav className="flex items-center">
          {link("/", "Tournaments")}
          {signedIn && link("/orders", "My tickets")}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          {signedIn ? (
            <>
              <span className="hidden text-sm text-muted sm:inline">{user?.display_name}</span>
              <button type="button" className="btn btn-ghost !py-1.5 text-sm" onClick={signOut}>
                Sign out
              </button>
            </>
          ) : (
            <Link href="/login" className="btn btn-ghost !py-1.5 text-sm">
              Sign in
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
