"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, useRef, useEffect } from "react";
import { useAuth } from "@/contexts/AuthContext";

export default function NavBar() {
  const { user, logout, loading } = useAuth();
  const pathname = usePathname();
  const router   = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef  = useRef<HTMLDivElement>(null);

  // Close dropdown on outside click
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  function handleLogout() {
    logout();
    setMenuOpen(false);
    router.push("/");
  }

  function active(href: string) {
    return pathname === href || pathname.startsWith(href + "/");
  }

  return (
    <nav className="sticky top-0 z-50 bg-white border-b border-gray-200 shadow-sm">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center h-12 gap-6">

          {/* Brand */}
          <Link
            href="/"
            className="text-lg font-extrabold text-brand tracking-tight shrink-0"
          >
            JobJarvis
          </Link>

          {/* Nav links */}
          <div className="flex items-center gap-1">
            <NavLink href="/" label="Jobs" current={active("/_jobs") || pathname === "/"} />
            {user && (
              <NavLink href="/matches" label="My Matches" current={active("/matches")} />
            )}
            {user && (
              <NavLink href="/review" label="Review" current={active("/review")} />
            )}
            <NavLink href="/analytics" label="Analytics" current={active("/analytics")} />
            <NavLink href="/companies" label="Companies" current={active("/companies")} />
            {user && (
              <NavLink href="/dashboard" label="My Applications" current={active("/dashboard")} />
            )}
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Auth area */}
          {loading ? (
            <div className="w-24 h-7 bg-gray-100 animate-pulse rounded-lg" />
          ) : user ? (
            /* ── User menu ── */
            <div className="relative" ref={menuRef}>
              <button
                onClick={() => setMenuOpen((v) => !v)}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg
                           text-sm font-medium text-gray-700 hover:bg-gray-50
                           border border-gray-200 transition-colors"
              >
                {/* Avatar initials */}
                <span className="w-6 h-6 rounded-full bg-brand text-white text-xs
                                 font-bold flex items-center justify-center shrink-0">
                  {(user.full_name ?? user.email).charAt(0).toUpperCase()}
                </span>
                <span className="max-w-[120px] truncate hidden sm:block">
                  {user.full_name ?? user.email}
                </span>
                <svg className="w-3.5 h-3.5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {menuOpen && (
                <div className="absolute right-0 top-full mt-1.5 w-52 bg-white rounded-xl
                                border border-gray-200 shadow-lg py-1 z-50">
                  <div className="px-4 py-2.5 border-b border-gray-100">
                    <p className="text-xs font-semibold text-gray-500">Signed in as</p>
                    <p className="text-sm font-medium text-gray-900 truncate">{user.email}</p>
                  </div>
                  <Link
                    href="/dashboard"
                    onClick={() => setMenuOpen(false)}
                    className="flex items-center gap-2 px-4 py-2 text-sm text-gray-700
                               hover:bg-gray-50 transition-colors"
                  >
                    📋 My Applications
                  </Link>
                  <button
                    onClick={handleLogout}
                    className="w-full flex items-center gap-2 px-4 py-2 text-sm text-red-600
                               hover:bg-red-50 transition-colors"
                  >
                    ↩ Sign out
                  </button>
                </div>
              )}
            </div>
          ) : (
            /* ── Login / Signup buttons ── */
            <div className="flex items-center gap-2">
              <Link
                href="/login"
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-gray-600
                           hover:text-brand hover:bg-blue-50 border border-transparent
                           transition-colors"
              >
                Log in
              </Link>
              <Link
                href="/signup"
                className="px-3 py-1.5 rounded-lg text-sm font-semibold text-white
                           bg-brand hover:bg-blue-700 transition-colors shadow-sm"
              >
                Sign up
              </Link>
            </div>
          )}
        </div>
      </div>
    </nav>
  );
}

function NavLink({
  href,
  label,
  current,
}: {
  href: string;
  label: string;
  current: boolean;
}) {
  return (
    <Link
      href={href}
      className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
        current
          ? "bg-blue-50 text-brand"
          : "text-gray-600 hover:text-brand hover:bg-blue-50"
      }`}
    >
      {label}
    </Link>
  );
}
