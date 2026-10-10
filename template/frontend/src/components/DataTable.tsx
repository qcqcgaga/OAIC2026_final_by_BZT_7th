/**
 * Generic UI primitive (route template kit).
 *
 * Contains NO task-specific content: no column names, no entity names, no seed
 * data, no business logic. Everything is props-driven.
 *
 * Why it exists: tabular screens are read through native table semantics
 * (table / row / columnheader / cell) and the empty state is asserted too.
 * The empty message uses role="status" on purpose: role="alert" is a scarce
 * per-page resource and must be reserved for the application's own feedback.
 */
import type { ReactNode } from 'react';

export type SortDirection = 'ascending' | 'descending';

export interface DataTableColumn<T> {
  /** Key into the row object, or a stable identifier when `render` is used. */
  key: string;
  /** Visible header text. */
  header: string;
  sortable?: boolean;
  render?: (row: T, index: number) => ReactNode;
  align?: 'left' | 'right' | 'center';
}

export interface DataTableProps<T> {
  /** Accessible name of the table (rendered as its caption). */
  caption: string;
  columns: Array<DataTableColumn<T>>;
  rows: T[];
  rowKey: (row: T, index: number) => string;
  sort?: { key: string; direction: SortDirection };
  onSortChange?: (key: string) => void;
  /** Shown (as role="status") when there are no rows. */
  emptyMessage?: string;
  className?: string;
}

function cellValue<T>(row: T, column: DataTableColumn<T>, index: number): ReactNode {
  if (column.render) {
    return column.render(row, index);
  }
  const value = (row as Record<string, unknown>)[column.key];
  if (value === null || value === undefined || value === '') {
    return '';
  }
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export default function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  sort,
  onSortChange,
  emptyMessage = 'Nothing to show yet.',
  className = '',
}: DataTableProps<T>) {
  return (
    <div className={className}>
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => {
              const isSorted = sort?.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={
                    column.sortable && isSorted
                      ? sort?.direction === 'descending'
                        ? 'descending'
                        : 'ascending'
                      : undefined
                  }
                  className="border-b border-slate-200 px-3 py-2 font-medium text-slate-600"
                >
                  {column.sortable && onSortChange ? (
                    <button
                      type="button"
                      onClick={() => onSortChange(column.key)}
                      className="font-medium text-slate-700 hover:underline"
                    >
                      {column.header}
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)} className="border-b border-slate-100">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`px-3 py-2 text-slate-800 ${
                    column.align === 'right'
                      ? 'text-right'
                      : column.align === 'center'
                        ? 'text-center'
                        : 'text-left'
                  }`}
                >
                  {cellValue(row, column, index)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 ? (
        <p role="status" className="mt-3 text-sm text-slate-600">
          {emptyMessage}
        </p>
      ) : null}
    </div>
  );
}
