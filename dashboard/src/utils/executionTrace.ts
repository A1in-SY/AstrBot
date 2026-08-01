import type { ExecutionTraceSpan, ExecutionTraceSummary } from '@/api/v1';

export interface ExecutionTraceSpanRow {
  span: ExecutionTraceSpan;
  depth: number;
  orphaned: boolean;
}

export interface ExecutionTraceTimeline {
  origin: number;
  end: number;
  duration: number;
}

export interface ExecutionTraceBarGeometry {
  leftPercent: number;
  widthPercent: number;
  zeroDuration: boolean;
}

const FAILED_STATUSES = new Set(['error', 'failed']);
const RUNNING_STATUSES = new Set(['running', 'pending']);

export function numericEpoch(value: unknown): number | null {
  const epoch = Number(value);
  return Number.isFinite(epoch) && epoch > 0 ? epoch : null;
}

export function isRunningTraceStatus(status: unknown): boolean {
  return RUNNING_STATUSES.has(String(status || '').trim().toLowerCase());
}

export function isFailedTraceStatus(status: unknown): boolean {
  return FAILED_STATUSES.has(String(status || '').trim().toLowerCase());
}

export function isTerminalTraceStatus(status: unknown): boolean {
  return !isRunningTraceStatus(status);
}

export function executionTraceStatusColor(status: unknown): string {
  switch (String(status || '').trim().toLowerCase()) {
    case 'running':
      return 'primary';
    case 'success':
      return 'success';
    case 'skipped':
      return 'secondary';
    case 'error':
    case 'failed':
      return 'error';
    case 'cancelled':
    case 'incomplete':
      return 'warning';
    default:
      return 'secondary';
  }
}

export function formatExecutionTraceDateTime(
  value: unknown,
  locale: string,
): string {
  const epoch = numericEpoch(value);
  if (epoch === null) {
    return '–';
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(epoch * 1000));
}

export function formatExecutionTraceDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return '–';
  }
  const milliseconds = Math.max(0, Math.round(seconds * 1000));
  if (milliseconds < 1000) {
    return `${milliseconds} ms`;
  }
  if (milliseconds < 60_000) {
    const precision = milliseconds < 10_000 ? 2 : 1;
    return `${(milliseconds / 1000).toFixed(precision)} s`;
  }
  const minutes = Math.floor(milliseconds / 60_000);
  const remainingSeconds = Math.floor((milliseconds % 60_000) / 1000);
  if (minutes < 60) {
    return `${minutes}m ${remainingSeconds}s`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function executionTraceDuration(
  item: Pick<ExecutionTraceSummary | ExecutionTraceSpan, 'started_at' | 'ended_at' | 'status'>,
  nowSeconds: number | null = null,
): number | null {
  const startedAt = numericEpoch(item.started_at);
  if (startedAt === null) {
    return null;
  }
  const endedAt = numericEpoch(item.ended_at);
  const effectiveEnd = endedAt ?? (isRunningTraceStatus(item.status) ? nowSeconds : null);
  return effectiveEnd === null ? null : Math.max(0, effectiveEnd - startedAt);
}

function spanSort(left: ExecutionTraceSpan, right: ExecutionTraceSpan): number {
  const leftStartedAt = numericEpoch(left.started_at) ?? 0;
  const rightStartedAt = numericEpoch(right.started_at) ?? 0;
  if (leftStartedAt !== rightStartedAt) {
    return leftStartedAt - rightStartedAt;
  }
  return left.span_id.localeCompare(right.span_id);
}

/**
 * Flatten a parent/child Span graph into waterfall rows without hiding corrupt
 * telemetry. Missing parents, self references, and cycles remain visible as
 * orphaned roots rather than disappearing from the trace.
 */
export function buildExecutionTraceSpanRows(
  spans: ExecutionTraceSpan[],
): ExecutionTraceSpanRow[] {
  const byId = new Map<string, ExecutionTraceSpan>();
  for (const span of spans) {
    if (span.span_id && !byId.has(span.span_id)) {
      byId.set(span.span_id, span);
    }
  }

  const children = new Map<string, ExecutionTraceSpan[]>();
  const roots: Array<{ span: ExecutionTraceSpan; orphaned: boolean }> = [];
  for (const span of [...byId.values()].sort(spanSort)) {
    const parentId = span.parent_span_id || null;
    if (!parentId) {
      roots.push({ span, orphaned: false });
      continue;
    }
    if (parentId === span.span_id || !byId.has(parentId)) {
      roots.push({ span, orphaned: true });
      continue;
    }
    const childSpans = children.get(parentId) || [];
    childSpans.push(span);
    children.set(parentId, childSpans);
  }
  for (const childSpans of children.values()) {
    childSpans.sort(spanSort);
  }

  const rows: ExecutionTraceSpanRow[] = [];
  const visited = new Set<string>();
  const visit = (span: ExecutionTraceSpan, depth: number, orphaned: boolean) => {
    if (visited.has(span.span_id)) {
      return;
    }
    visited.add(span.span_id);
    rows.push({ span, depth, orphaned });
    for (const child of children.get(span.span_id) || []) {
      visit(child, depth + 1, false);
    }
  };

  for (const root of roots.sort((left, right) => spanSort(left.span, right.span))) {
    visit(root.span, 0, root.orphaned);
  }
  for (const span of [...byId.values()].sort(spanSort)) {
    if (!visited.has(span.span_id)) {
      visit(span, 0, true);
    }
  }
  return rows;
}

export function executionTraceTimeline(
  spans: ExecutionTraceSpan[],
  traceStartedAt: number,
  nowSeconds: number | null = null,
): ExecutionTraceTimeline {
  let origin = numericEpoch(traceStartedAt) ?? 0;
  let end = origin;
  for (const span of spans) {
    const startedAt = numericEpoch(span.started_at);
    if (startedAt !== null) {
      origin = origin > 0 ? Math.min(origin, startedAt) : startedAt;
      end = Math.max(end, startedAt);
    }
    const endedAt = numericEpoch(span.ended_at);
    if (endedAt !== null) {
      end = Math.max(end, endedAt);
    } else if (isRunningTraceStatus(span.status) && nowSeconds !== null) {
      end = Math.max(end, nowSeconds);
    }
  }
  if (nowSeconds !== null) {
    end = Math.max(end, nowSeconds);
  }
  return {
    origin,
    end,
    duration: Math.max(1e-3, end - origin),
  };
}

export function executionTraceBarGeometry(
  span: ExecutionTraceSpan,
  timeline: ExecutionTraceTimeline,
  nowSeconds: number | null = null,
): ExecutionTraceBarGeometry {
  const startedAt = Math.max(timeline.origin, numericEpoch(span.started_at) ?? timeline.origin);
  const explicitEnd = numericEpoch(span.ended_at);
  const end = Math.max(
    startedAt,
    explicitEnd ?? (isRunningTraceStatus(span.status) ? nowSeconds ?? startedAt : startedAt),
  );
  const width = Math.max(0, end - startedAt);
  return {
    leftPercent: Math.min(100, Math.max(0, ((startedAt - timeline.origin) / timeline.duration) * 100)),
    widthPercent: Math.min(100, Math.max(0, (width / timeline.duration) * 100)),
    zeroDuration: width === 0,
  };
}

export function executionTraceSpanColor(span: Pick<ExecutionTraceSpan, 'kind' | 'status'>): string {
  if (isFailedTraceStatus(span.status)) {
    return 'rgb(var(--v-theme-error))';
  }
  switch (String(span.kind || '').trim().toLowerCase()) {
    case 'model':
    case 'model_call':
    case 'llm':
      return 'rgb(var(--v-theme-info))';
    case 'tool':
    case 'tool_call':
      return 'rgb(var(--v-theme-warning))';
    case 'skill':
    case 'mcp':
      return 'rgb(var(--v-theme-secondary))';
    case 'delivery':
    case 'tts':
    case 'stt':
      return 'rgb(var(--v-theme-success))';
    case 'agent':
    case 'pipeline':
    case 'message':
    case 'phase':
      return 'rgb(var(--v-theme-primary))';
    default:
      return 'rgba(var(--v-theme-on-surface), 0.5)';
  }
}

export function executionTraceSpanLabel(span: Pick<ExecutionTraceSpan, 'operation' | 'kind'>): string {
  return String(span.operation || '').trim() || String(span.kind || '').trim() || '–';
}

export type ExecutionTraceCategoryKey =
  | 'plugin'
  | 'message_pipeline'
  | 'scheduled'
  | 'agent'
  | 'tool'
  | 'provider'
  | 'delivery'
  | 'other';

export function executionTraceCategory(
  trace: Pick<ExecutionTraceSummary, 'source' | 'kind' | 'attributes'>,
): ExecutionTraceCategoryKey {
  const source = String(trace.source || '');
  const kind = String(trace.kind || '');
  if (source === 'plugin') {
    return 'plugin';
  }
  if (kind === 'pipeline') {
    return 'message_pipeline';
  }
  if (kind === 'agent') {
    return (trace.attributes?.trigger_reason as string | undefined) === 'cron'
      ? 'scheduled'
      : 'agent';
  }
  if (kind === 'tool') {
    return 'tool';
  }
  if (kind === 'provider') {
    return 'provider';
  }
  if (kind === 'delivery') {
    return 'delivery';
  }
  return 'other';
}

export function executionTraceCategoryHint(
  trace: Pick<ExecutionTraceSummary, 'source' | 'kind' | 'attributes' | 'plugin_id'>,
): string | null {
  if (trace.source === 'plugin') {
    return trace.plugin_id || null;
  }
  if (executionTraceCategory(trace) === 'scheduled') {
    const job = trace.attributes?.cron_job as { name?: unknown } | undefined;
    const name = typeof job?.name === 'string' ? job.name.trim() : '';
    return name || null;
  }
  return null;
}

export function executionTraceSpanLowerBound(span: ExecutionTraceSpan): boolean {
  return span.duration_is_lower_bound === true
    || span.attributes?.duration_is_lower_bound === true;
}

export function traceSummaryRevision(detail: {
  revision?: number | null;
  trace?: { revision?: number | null };
}): number | null {
  const direct = Number(detail.revision);
  if (Number.isFinite(direct) && direct >= 0) {
    return direct;
  }
  const nested = Number(detail.trace?.revision);
  return Number.isFinite(nested) && nested >= 0 ? nested : null;
}

export function formatExecutionTraceBytes(bytes: number | null | undefined): string {
  const safeBytes = Number(bytes);
  if (!Number.isFinite(safeBytes) || safeBytes < 0) {
    return '–';
  }
  if (safeBytes < 1024) {
    return `${safeBytes} B`;
  }
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = safeBytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

export function safeExecutionTraceJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}
