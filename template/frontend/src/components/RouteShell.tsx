/**
 * Generic UI primitive (route template kit).
 *
 * Contains NO task-specific content: no route names, no entity names, no seed
 * data, no business logic. Everything is props-driven.
 *
 * Why it exists: the application shell (logo, primary navigation, page body)
 * is where locator collisions happen. Two rules are baked in:
 *   - navigation entries are LINKS inside a named navigation landmark; a
 *     landmark entry must not be given role="button", otherwise it shadows a
 *     same-named page action and the page action never fires;
 *   - the logo keeps a stable accessible name and is a plain link home.
 * Do not mount this shell with page content that duplicates the requirements'
 * own layout: pass the pages in as children.
 */
import type { ReactNode } from 'react';

export interface RouteShellNavItem {
  label: string;
  href: string;
  /** Marks the current entry (aria-current="page"). */
  current?: boolean;
}

export interface RouteShellProps {
  /** Accessible name of the home/logo link. */
  logoLabel: string;
  logoTarget?: string;
  navItems?: RouteShellNavItem[];
  /** Accessible name of the navigation landmark. */
  navLabel?: string;
  /** Optional chrome actions (search box, sign-in link, ...). */
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
}

export default function RouteShell({
  logoLabel,
  logoTarget = '/',
  navItems = [],
  navLabel = 'Primary',
  actions,
  footer,
  children,
  className = '',
}: RouteShellProps) {
  return (
    <div className={`flex min-h-screen flex-col bg-slate-50 ${className}`}>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-4 px-6 py-3">
          <a href={logoTarget} className="font-semibold text-slate-900">
            {logoLabel}
          </a>
          {navItems.length > 0 ? (
            <nav aria-label={navLabel} className="flex items-center gap-3 text-sm">
              {navItems.map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  aria-current={item.current ? 'page' : undefined}
                  className={
                    item.current ? 'font-medium text-slate-900' : 'text-slate-600 hover:text-slate-900'
                  }
                >
                  {item.label}
                </a>
              ))}
            </nav>
          ) : null}
          {actions ? <div className="ml-auto flex items-center gap-3">{actions}</div> : null}
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-8">{children}</main>

      {footer ? <footer className="border-t border-slate-200 bg-white">{footer}</footer> : null}
    </div>
  );
}
