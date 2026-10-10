/**
 * Barrel for the generic UI primitives (route template kit).
 *
 * These components are task-agnostic: no entity names, no seed data, no
 * business logic, no routes or pages. Import what is useful and delete the
 * rest; nothing here is wired into the application shell by default.
 */
export { default as EntityCard } from './EntityCard';
export type { EntityCardProps, EntityCardNameAs, EntityCardStatus } from './EntityCard';

export { default as NamedForm, sharesWordRoot, wordRoots } from './NamedForm';
export type { NamedFormProps } from './NamedForm';

export { default as DataTable } from './DataTable';
export type { DataTableProps, DataTableColumn, SortDirection } from './DataTable';

export { default as RouteShell } from './RouteShell';
export type { RouteShellProps, RouteShellNavItem } from './RouteShell';
