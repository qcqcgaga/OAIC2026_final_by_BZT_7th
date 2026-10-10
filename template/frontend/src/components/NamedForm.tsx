/**
 * Generic UI primitive (route template kit).
 *
 * Contains NO task-specific content: no field names, no entity names, no seed
 * data, no business logic. Everything is props-driven.
 *
 * Why it exists: a data-entry form must expose an accessible name, and that
 * name must NOT share a word root with any field label inside it. Field
 * resolvers match ancestors before descendants, so a form named "Note form"
 * with a field labelled "Note" makes the field lookup hit the form element and
 * the fill/click then fails. This component centralises the correct shape and
 * warns (development builds only) when a clash is introduced.
 */
import { useEffect, type FormEvent, type ReactNode } from 'react';

export const MIN_WORD_LENGTH = 3;

/** Lower-cased alphabetic word roots of a label (length >= MIN_WORD_LENGTH). */
export function wordRoots(label: string): string[] {
  return String(label || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((word) => word.length >= MIN_WORD_LENGTH);
}

/** True when two labels share at least one word root. */
export function sharesWordRoot(a: string, b: string): boolean {
  const left = new Set(wordRoots(a));
  return wordRoots(b).some((word) => left.has(word));
}

export interface NamedFormProps {
  /** Accessible name of the form element (role="form"). */
  name: string;
  /** Field labels inside the form, used for the development-time clash warning. */
  fieldLabels?: string[];
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  children: ReactNode;
  className?: string;
  /**
   * Keep native browser validation off so that validation is expressed in
   * application code (assertable, cross-browser) rather than as a browser
   * bubble that automated checks cannot read.
   */
  noValidate?: boolean;
}

export default function NamedForm({
  name,
  fieldLabels = [],
  onSubmit,
  children,
  className = '',
  noValidate = true,
}: NamedFormProps) {
  const labelSignature = fieldLabels.join('|');

  useEffect(() => {
    const isDev = Boolean((import.meta as { env?: { DEV?: boolean } }).env?.DEV);
    if (!isDev || fieldLabels.length === 0) {
      return;
    }
    const clashes = fieldLabels.filter((label) => sharesWordRoot(name, label));
    if (clashes.length > 0) {
      console.warn(
        `[NamedForm] aria-label ${JSON.stringify(name)} shares a word root with field ` +
          `label(s) ${clashes.map((c) => JSON.stringify(c)).join(', ')}. A label lookup ` +
          'matches the form element before the field, so filling the field will fail. ' +
          'Rename the form (or the field) so they share no word root.',
      );
    }
    // labelSignature keeps the dependency stable without depending on array identity.
  }, [name, labelSignature]);

  return (
    <form
      role="form"
      aria-label={name}
      noValidate={noValidate}
      onSubmit={onSubmit}
      className={`flex flex-col gap-3 ${className}`}
    >
      {children}
    </form>
  );
}
