/**
 * Generic UI primitive (route template kit).
 *
 * Contains NO task-specific content: no entity names, no seed data, no field
 * names, no business logic. Everything is props-driven, so it can be reused,
 * adapted or deleted freely.
 *
 * Why it exists: list/detail screens keep re-implementing the same accessible
 * structure, and the accessible names/roles are what end-to-end checks read.
 * The structure encoded here is the one that survives those checks:
 *   - the card is an <article> named by its title;
 *   - status indicators live in DESCENDANT elements (a locator scoped to the
 *     card only matches descendants, never the card root itself);
 *   - hover-revealed actions use opacity, never visibility/display, so they
 *     stay in the accessibility tree;
 *   - the name is rendered with a real role (heading / button / link).
 */
import { useMemo, type ReactNode } from 'react';

export type EntityCardNameAs = 'heading' | 'button' | 'link';

export interface EntityCardStatus {
  /** Visible text of the status indicator, e.g. "Pinned". */
  label: string;
  /** Optional tooltip/title text; defaults to the label. */
  title?: string;
}

export interface EntityCardProps {
  /** Accessible name of the card and of its title control. */
  title: string;
  /** Which role carries the title. Use the role the requirements ask for. */
  nameAs?: EntityCardNameAs;
  /** Activation handler for nameAs="button". */
  onActivate?: () => void;
  /** Target for nameAs="link". */
  href?: string;
  /** Status indicators rendered as descendant elements. */
  status?: EntityCardStatus[];
  /** Hide the action row until hover/focus (opacity only, never display). */
  revealActionsOnHover?: boolean;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}

let cardSeq = 0;

export default function EntityCard({
  title,
  nameAs = 'heading',
  onActivate,
  href,
  status,
  revealActionsOnHover = false,
  actions,
  children,
  className = '',
}: EntityCardProps) {
  const titleId = useMemo(() => `entity-card-title-${(cardSeq += 1)}`, []);

  const titleNode =
    nameAs === 'button' ? (
      <button
        type="button"
        onClick={onActivate}
        className="text-left font-semibold text-slate-900 hover:underline"
      >
        {title}
      </button>
    ) : nameAs === 'link' ? (
      <a
        href={href || '#'}
        onClick={onActivate}
        className="font-semibold text-slate-900 hover:underline"
      >
        {title}
      </a>
    ) : (
      <h3 id={titleId} className="m-0 text-base font-semibold text-slate-900">
        {title}
      </h3>
    );

  return (
    <article
      aria-labelledby={nameAs === 'heading' ? titleId : undefined}
      aria-label={nameAs === 'heading' ? undefined : title}
      className={`group rounded-lg border border-slate-200 bg-white p-4 shadow-sm ${className}`}
    >
      <div className="flex items-start justify-between gap-3">
        {titleNode}
        {status && status.length > 0 ? (
          // Intentionally unnamed: a container accessible name here could share a
          // word root with a field label in the surrounding page, which makes a
          // label lookup resolve to the container instead of the field. The
          // indicators themselves carry the queryable text/title.
          <ul className="m-0 flex list-none items-center gap-2 p-0">
            {status.map((item) => (
              <li key={item.label}>
                <span
                  title={item.title || item.label}
                  className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
                >
                  {item.label}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {children ? <div className="mt-2 text-sm text-slate-700">{children}</div> : null}

      {actions ? (
        <div
          className={
            revealActionsOnHover
              ? 'mt-3 flex flex-wrap items-center gap-2 opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-within:opacity-100'
              : 'mt-3 flex flex-wrap items-center gap-2'
          }
        >
          {actions}
        </div>
      ) : null}
    </article>
  );
}
