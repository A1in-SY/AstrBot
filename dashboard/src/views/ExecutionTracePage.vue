<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import {
  executionTraceApi,
  type ExecutionTraceConfig,
  type ExecutionTraceFilterOptions,
  type ExecutionTraceOverview,
  type ExecutionTraceSummary,
  pluginApi,
} from '@/api/v1';
import { useI18n, useModuleI18n } from '@/i18n/composables';
import { useToast } from '@/utils/toast';
import {
  executionTraceDuration,
  executionTraceCategory,
  executionTraceCategoryHint,
  executionTraceStatusColor,
  formatExecutionTraceBytes,
  formatExecutionTraceDateTime,
  formatExecutionTraceDuration,
  isFailedTraceStatus,
  isRunningTraceStatus,
} from '@/utils/executionTrace';

const PAGE_SIZE = 50;
const OVERVIEW_POLL_INTERVAL_MS = 15_000;
const LIVE_CLOCK_INTERVAL_MS = 1_000;

const router = useRouter();
const { locale } = useI18n();
const { tm } = useModuleI18n('features/execution-trace');
const { error: toastError, success: toastSuccess } = useToast();

const config = ref<ExecutionTraceConfig>({
  enabled: true,
  runtime_available: false,
});
const overview = ref<ExecutionTraceOverview>({
  traces_24h: 0,
  running: 0,
  errors_24h: 0,
  physical_size: 0,
});
const traces = ref<ExecutionTraceSummary[]>([]);
const hasMore = ref(false);
const statusFilter = ref<string | null>(null);
const categoryFilter = ref('');
const pluginFilter = ref('');
const localSearch = ref('');
const degradedOnly = ref(false);
const filterOptions = ref<ExecutionTraceFilterOptions>({
  categories: [],
  plugins: [],
});
const pluginDisplayNames = ref<Map<string, string>>(new Map());
const loading = ref(false);
const listLoading = ref(false);
const configSaving = ref(false);
const maintenanceLoading = ref(false);
const loadError = ref('');
const clearDialog = ref(false);
const deleteDialog = ref(false);
const deletingTrace = ref<ExecutionTraceSummary | null>(null);
const currentSeconds = ref(Date.now() / 1000);

let listRequestId = 0;
let overviewPollTimer: ReturnType<typeof window.setInterval> | null = null;
let clockTimer: ReturnType<typeof window.setInterval> | null = null;

const statusOptions = computed(() => [
  { title: tm('filters.allStatuses'), value: null },
  ...['running', 'success', 'skipped', 'error', 'cancelled', 'incomplete'].map(
    (status) => ({ title: status, value: status }),
  ),
]);
const categoryOptions = computed(() => [
  { title: tm('filters.allTypes'), value: '' },
  ...filterOptions.value.categories.map((option) => ({
    title: `${tm(`categories.${option.key}`)} (${option.count})`,
    value: option.key,
  })),
]);
const pluginOptions = computed(() => [
  { title: tm('filters.allPlugins'), value: '' },
  ...filterOptions.value.plugins.map((option) => ({
    title: `${pluginDisplayName(option.plugin_id) || option.plugin_id} (${option.count})`,
    value: option.plugin_id,
  })),
]);
const filterSignature = computed(() => JSON.stringify([
  statusFilter.value,
  categoryFilter.value,
  String(pluginFilter.value || '').trim(),
  degradedOnly.value,
]));
const hasFilters = computed(() =>
  Boolean(
    statusFilter.value
    || categoryFilter.value
    || String(pluginFilter.value || '').trim()
    || localSearch.value.trim()
    || degradedOnly.value,
  ),
);
const visibleTraces = computed(() => {
  const query = localSearch.value.trim().toLocaleLowerCase(locale.value);
  if (!query) {
    return traces.value;
  }
  return traces.value.filter((trace) => {
    const attributes = trace.attributes || {};
    const haystack = [
      trace.trace_id,
      trace.operation,
      trace.source,
      trace.kind,
      trace.plugin_id,
      trace.active_span_operation,
      attributes.group_name,
      attributes.group_umo,
      attributes.summary_mode,
      attributes.trigger_reason,
    ]
      .filter((value) => value !== null && value !== undefined)
      .join('\n')
      .toLocaleLowerCase(locale.value);
    return haystack.includes(query);
  });
});
const traceStats = computed(() => ({
  loaded: visibleTraces.value.length,
  running: visibleTraces.value.filter((trace) => isRunningTraceStatus(trace.status)).length,
  success: visibleTraces.value.filter((trace) => trace.status === 'success').length,
  failed: visibleTraces.value.filter(
    (trace) => isFailedTraceStatus(trace.status) || ['cancelled', 'incomplete'].includes(trace.status),
  ).length,
  degraded: visibleTraces.value.filter((trace) => trace.degraded).length,
}));
const hasRunningTrace = computed(() => traces.value.some((trace) => isRunningTraceStatus(trace.status)));

function unwrap<T>(response: { data?: { status?: string; data?: T; message?: string | null } }): T {
  if (response.data?.status !== 'ok' || response.data.data === undefined) {
    throw new Error(response.data?.message || 'Execution Trace request failed');
  }
  return response.data.data;
}

function formatDateTime(value: unknown): string {
  return formatExecutionTraceDateTime(value, locale.value);
}

function summaryCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '–' : String(value);
}

function pluginDisplayName(pluginId: string): string {
  return pluginDisplayNames.value.get(pluginId) || '';
}

function traceCategory(trace: ExecutionTraceSummary): string {
  return executionTraceCategory(trace);
}

function traceCategoryHint(trace: ExecutionTraceSummary): string {
  const hint = executionTraceCategoryHint(trace);
  if (trace.source === 'plugin') {
    return pluginDisplayName(hint || '') || hint || '';
  }
  return hint || '';
}

function traceContext(trace: ExecutionTraceSummary): Array<{ key: string; label: string; value: string }> {
  const attributes = trace.attributes || {};
  const keys = [
    ['group_name', 'context.groupName'],
    ['group_umo', 'context.groupUmo'],
    ['summary_mode', 'context.summaryMode'],
    ['trigger_reason', 'context.triggerReason'],
  ] as const;
  return keys.flatMap(([key, labelKey]) => {
    const value = attributes[key];
    if (value === null || value === undefined || String(value).trim() === '') {
      return [];
    }
    return [{ key, label: tm(labelKey), value: String(value) }];
  });
}

async function loadConfig(): Promise<void> {
  config.value = unwrap(await executionTraceApi.config());
}

async function loadOverview(): Promise<void> {
  overview.value = unwrap(await executionTraceApi.overview());
}

async function loadFilterOptions(): Promise<void> {
  filterOptions.value = unwrap(await executionTraceApi.filterOptions());
}

async function loadPluginNames(): Promise<void> {
  try {
    const plugins = unwrap(await pluginApi.list());
    const map = new Map<string, string>();
    for (const plugin of plugins || []) {
      const author = String(plugin?.author || 'unknown').toLowerCase().replace(/\//g, '_');
      const name = String(plugin?.name || 'unknown').toLowerCase().replace(/\//g, '_');
      map.set(`${author}/${name}`, plugin?.display_name || plugin?.name || '');
    }
    pluginDisplayNames.value = map;
  } catch {
    pluginDisplayNames.value = new Map();
  }
}

async function loadTraces(reset = true): Promise<void> {
  const requestId = ++listRequestId;
  const signature = filterSignature.value;
  listLoading.value = true;
  try {
    const last = reset ? null : traces.value[traces.value.length - 1] || null;
    const response = await executionTraceApi.list({
      limit: PAGE_SIZE,
      status: statusFilter.value || undefined,
      category: categoryFilter.value || undefined,
      plugin_id: String(pluginFilter.value || '').trim() || undefined,
      degraded: degradedOnly.value || undefined,
      before_ended_at: last?.ended_at || last?.started_at,
      before_trace_id: last?.trace_id,
    });
    const items = unwrap<{ items: ExecutionTraceSummary[] }>(response).items;
    if (requestId !== listRequestId || signature !== filterSignature.value) {
      return;
    }
    traces.value = reset ? items : [...traces.value, ...items];
    hasMore.value = items.length === PAGE_SIZE;
    loadError.value = '';
  } catch (error) {
    if (requestId === listRequestId && signature === filterSignature.value) {
      loadError.value = error instanceof Error ? error.message : tm('messages.loadFailed');
    }
  } finally {
    if (requestId === listRequestId) {
      listLoading.value = false;
      syncLiveTimers();
    }
  }
}

async function refresh(options: { polling?: boolean } = {}): Promise<void> {
  if (options.polling && (loading.value || listLoading.value)) {
    return;
  }
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([
      loadConfig(),
      loadOverview(),
      loadFilterOptions(),
      loadPluginNames(),
      loadTraces(true),
    ]);
  } catch (error) {
    if (!options.polling) {
      loadError.value = error instanceof Error ? error.message : tm('messages.loadFailed');
      toastError(tm('messages.loadFailed'));
    }
  } finally {
    loading.value = false;
  }
}

async function updateEnabled(enabled: boolean | null): Promise<void> {
  if (enabled === null) {
    return;
  }
  const previous = config.value.enabled;
  configSaving.value = true;
  try {
    config.value = unwrap(await executionTraceApi.updateConfig({ enabled }));
  } catch {
    config.value = { ...config.value, enabled: previous };
    toastError(tm('messages.updateFailed'));
  } finally {
    configSaving.value = false;
  }
}

function reloadForFilters(): void {
  void loadTraces(true);
}

function resetFilters(): void {
  statusFilter.value = null;
  categoryFilter.value = '';
  pluginFilter.value = '';
  localSearch.value = '';
  degradedOnly.value = false;
  void loadTraces(true);
}

function openTrace(trace: ExecutionTraceSummary): void {
  void router.push({ name: 'ExecutionTraceDetail', params: { traceId: trace.trace_id } });
}

function askDelete(trace: ExecutionTraceSummary): void {
  if (isRunningTraceStatus(trace.status)) {
    return;
  }
  deletingTrace.value = trace;
  deleteDialog.value = true;
}

async function deleteTrace(): Promise<void> {
  if (!deletingTrace.value) {
    return;
  }
  maintenanceLoading.value = true;
  try {
    unwrap(await executionTraceApi.remove(deletingTrace.value.trace_id));
    deleteDialog.value = false;
    deletingTrace.value = null;
    toastSuccess(tm('messages.deleteDone'));
    await refresh();
  } catch {
    toastError(tm('messages.deleteFailed'));
  } finally {
    maintenanceLoading.value = false;
  }
}

async function runCleanup(): Promise<void> {
  maintenanceLoading.value = true;
  try {
    const result = unwrap(await executionTraceApi.cleanup());
    toastSuccess(tm('messages.cleanupDone', { count: result.deleted }));
    await refresh();
  } catch {
    toastError(tm('messages.cleanupFailed'));
  } finally {
    maintenanceLoading.value = false;
  }
}

async function clearCompleted(): Promise<void> {
  maintenanceLoading.value = true;
  try {
    const result = unwrap(await executionTraceApi.clear());
    clearDialog.value = false;
    toastSuccess(tm('messages.clearDone', { count: result.deleted }));
    await refresh();
  } catch {
    toastError(tm('messages.clearFailed'));
  } finally {
    maintenanceLoading.value = false;
  }
}

function stopLiveTimers(): void {
  if (overviewPollTimer !== null) {
    window.clearInterval(overviewPollTimer);
    overviewPollTimer = null;
  }
  if (clockTimer !== null) {
    window.clearInterval(clockTimer);
    clockTimer = null;
  }
}

function syncLiveTimers(): void {
  stopLiveTimers();
  if (document.hidden) {
    return;
  }
  overviewPollTimer = window.setInterval(() => {
    void refresh({ polling: true });
  }, OVERVIEW_POLL_INTERVAL_MS);
  if (hasRunningTrace.value) {
    clockTimer = window.setInterval(() => {
      currentSeconds.value = Date.now() / 1000;
    }, LIVE_CLOCK_INTERVAL_MS);
  }
}

function handleVisibilityChange(): void {
  if (!document.hidden) {
    currentSeconds.value = Date.now() / 1000;
    void refresh({ polling: true });
  }
  syncLiveTimers();
}

onMounted(() => {
  document.addEventListener('visibilitychange', handleVisibilityChange);
  void refresh();
  syncLiveTimers();
});

onBeforeUnmount(() => {
  stopLiveTimers();
  document.removeEventListener('visibilitychange', handleVisibilityChange);
});
</script>

<template>
  <div class="dashboard-page execution-trace-page">
    <v-container fluid class="dashboard-shell pa-4 pa-md-6">
      <div class="dashboard-header">
        <div class="dashboard-header-main">
          <h1 class="dashboard-title">{{ tm('title') }}</h1>
          <p class="dashboard-subtitle">{{ tm('subtitle') }}</p>
          <div class="dashboard-header-meta trace-header-meta">
            <span class="dashboard-pill">
              <v-icon size="16">mdi-chart-timeline-variant</v-icon>
              {{ tm('overview.traces24h') }} {{ overview.traces_24h }}
            </span>
            <span class="dashboard-pill">
              <v-icon size="16">mdi-database-outline</v-icon>
              {{ tm('overview.storage') }} {{ formatExecutionTraceBytes(overview.physical_size) }}
            </span>
          </div>
        </div>
        <div class="dashboard-header-actions">
          <v-chip
            v-if="!config.runtime_available"
            color="warning"
            size="small"
            variant="tonal"
          >
            {{ tm('unavailable') }}
          </v-chip>
          <v-switch
            :model-value="config.enabled"
            :disabled="configSaving || !config.runtime_available"
            :loading="configSaving"
            color="primary"
            density="compact"
            hide-details
            inset
            @update:model-value="updateEnabled"
          >
            <template #label>
              <span>{{ config.enabled ? tm('recording') : tm('paused') }}</span>
            </template>
          </v-switch>
          <v-btn
            color="primary"
            prepend-icon="mdi-refresh"
            variant="text"
            :loading="loading"
            @click="refresh()"
          >
            {{ tm('actions.refresh') }}
          </v-btn>
          <v-btn
            color="secondary"
            prepend-icon="mdi-broom"
            variant="tonal"
            :loading="maintenanceLoading"
            @click="runCleanup"
          >
            {{ tm('actions.cleanup') }}
          </v-btn>
          <v-btn
            color="error"
            prepend-icon="mdi-delete-sweep-outline"
            variant="tonal"
            :disabled="maintenanceLoading"
            @click="clearDialog = true"
          >
            {{ tm('actions.clear') }}
          </v-btn>
        </div>
      </div>

      <v-alert v-if="loadError" class="mb-4" density="compact" type="error" variant="tonal">
        {{ loadError }}
      </v-alert>

      <section class="trace-status-grid">
        <div class="dashboard-card trace-status-card">
          <div class="trace-status-icon"><v-icon>mdi-format-list-bulleted</v-icon></div>
          <div class="trace-status-label">{{ tm('stats.loaded') }}</div>
          <div class="trace-status-value">{{ traceStats.loaded }}</div>
          <div class="trace-status-note">{{ tm('stats.loadedNote') }}</div>
        </div>
        <div class="dashboard-card trace-status-card is-running">
          <div class="trace-status-icon"><v-icon>mdi-progress-clock</v-icon></div>
          <div class="trace-status-label">{{ tm('stats.running') }}</div>
          <div class="trace-status-value">{{ traceStats.running }}</div>
          <div class="trace-status-note">{{ tm('stats.runningNote') }}</div>
        </div>
        <div class="dashboard-card trace-status-card is-success">
          <div class="trace-status-icon"><v-icon>mdi-check-circle-outline</v-icon></div>
          <div class="trace-status-label">{{ tm('stats.success') }}</div>
          <div class="trace-status-value">{{ traceStats.success }}</div>
          <div class="trace-status-note">{{ tm('stats.successNote') }}</div>
        </div>
        <div class="dashboard-card trace-status-card is-failed">
          <div class="trace-status-icon"><v-icon>mdi-alert-circle-outline</v-icon></div>
          <div class="trace-status-label">{{ tm('stats.failed') }}</div>
          <div class="trace-status-value">{{ traceStats.failed }}</div>
          <div class="trace-status-note">{{ tm('stats.failedNote') }}</div>
        </div>
        <div class="dashboard-card trace-status-card is-degraded">
          <div class="trace-status-icon"><v-icon>mdi-alert-outline</v-icon></div>
          <div class="trace-status-label">{{ tm('stats.degraded') }}</div>
          <div class="trace-status-value">{{ traceStats.degraded }}</div>
          <div class="trace-status-note">{{ tm('stats.degradedNote') }}</div>
        </div>
      </section>

      <section class="dashboard-card dashboard-card--padded mb-5">
        <div class="dashboard-section-head">
          <div>
            <div class="dashboard-section-title">{{ tm('filters.title') }}</div>
            <div class="dashboard-section-subtitle">{{ tm('filters.subtitle') }}</div>
          </div>
          <v-btn v-if="hasFilters" size="small" variant="text" @click="resetFilters">
            {{ tm('actions.resetFilters') }}
          </v-btn>
        </div>
        <div class="trace-filters">
          <v-text-field
            v-model="localSearch"
            :label="tm('filters.search')"
            clearable
            density="compact"
            hide-details
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
          />
          <v-select
            v-model="statusFilter"
            :items="statusOptions"
            :label="tm('filters.status')"
            clearable
            density="compact"
            hide-details
            variant="outlined"
            @update:model-value="reloadForFilters"
          />
          <v-select
            v-model="categoryFilter"
            :items="categoryOptions"
            :label="tm('filters.type')"
            clearable
            density="compact"
            hide-details
            variant="outlined"
            @update:model-value="reloadForFilters"
          />
          <v-select
            v-model="pluginFilter"
            :items="pluginOptions"
            :label="tm('filters.plugin')"
            clearable
            density="compact"
            hide-details
            variant="outlined"
            @update:model-value="reloadForFilters"
          />
          <v-checkbox
            v-model="degradedOnly"
            :label="tm('filters.degraded')"
            density="compact"
            hide-details
            @update:model-value="reloadForFilters"
          />
        </div>
      </section>

      <section class="dashboard-card trace-table-card">
        <div class="trace-table-heading">
          <div>
            <div class="dashboard-section-title">{{ tm('list.title') }}</div>
            <div class="dashboard-section-subtitle">{{ tm('list.subtitle') }}</div>
          </div>
          <span class="trace-result-count">{{ tm('list.resultCount', { count: visibleTraces.length }) }}</span>
        </div>
        <v-progress-linear v-if="listLoading" color="primary" indeterminate />
        <div v-else-if="!visibleTraces.length" class="trace-empty">{{ tm('list.empty') }}</div>
        <div v-else class="trace-table-wrap">
          <div class="trace-table-head" aria-hidden="true">
            <span>{{ tm('table.time') }}</span>
            <span>{{ tm('table.trace') }}</span>
            <span>{{ tm('table.type') }}</span>
            <span>{{ tm('table.status') }}</span>
            <span>{{ tm('table.metrics') }}</span>
            <span>{{ tm('table.actions') }}</span>
          </div>
          <article
            v-for="trace in visibleTraces"
            :key="trace.trace_id"
            class="trace-row"
            role="link"
            tabindex="0"
            @click="openTrace(trace)"
            @keydown.enter.self="openTrace(trace)"
          >
            <div class="trace-time" :data-label="tm('table.time')">
              <strong>{{ formatDateTime(trace.started_at) }}</strong>
              <span>{{ tm('table.ended') }} {{ formatDateTime(trace.ended_at) }}</span>
            </div>
            <div class="trace-identity" :data-label="tm('table.trace')">
              <code :title="trace.trace_id">{{ trace.trace_id }}</code>
              <div v-if="traceContext(trace).length" class="trace-context">
                <span v-for="entry in traceContext(trace)" :key="entry.key" :title="`${entry.label}: ${entry.value}`">
                  {{ entry.label }}: {{ entry.value }}
                </span>
              </div>
            </div>
            <div class="trace-source" :data-label="tm('table.type')">
              <strong :title="`${trace.source} / ${trace.kind} / ${trace.operation}`">
                {{ tm(`categories.${traceCategory(trace)}`) }}
              </strong>
              <span v-if="traceCategoryHint(trace)">{{ traceCategoryHint(trace) }}</span>
            </div>
            <div class="trace-state" :data-label="tm('table.status')">
              <div>
                <v-chip :color="executionTraceStatusColor(trace.status)" size="x-small" variant="tonal">
                  {{ trace.status }}
                </v-chip>
                <v-chip v-if="trace.degraded" color="warning" size="x-small" variant="tonal">
                  {{ tm('table.degraded') }}
                </v-chip>
              </div>
              <span v-if="trace.outcome">{{ trace.outcome }}</span>
            </div>
            <div class="trace-metrics" :data-label="tm('table.metrics')">
              <strong>{{ formatExecutionTraceDuration(executionTraceDuration(trace, currentSeconds)) }}</strong>
              <span>
                {{ tm('table.counts', {
                  spans: summaryCount(trace.span_count),
                  events: summaryCount(trace.event_count),
                  artifacts: summaryCount(trace.artifact_count),
                  links: summaryCount(trace.link_count),
                }) }}
              </span>
            </div>
            <div class="trace-row-actions" :data-label="tm('table.actions')">
              <v-btn color="primary" size="small" variant="text" @click.stop="openTrace(trace)">
                {{ tm('actions.view') }}
              </v-btn>
              <v-btn
                color="error"
                size="small"
                variant="text"
                :disabled="isRunningTraceStatus(trace.status) || maintenanceLoading"
                @click.stop="askDelete(trace)"
              >
                {{ tm('actions.delete') }}
              </v-btn>
            </div>
          </article>
        </div>
        <div v-if="hasMore" class="trace-load-more">
          <v-btn variant="tonal" :loading="listLoading" @click="loadTraces(false)">
            {{ tm('actions.loadMore') }}
          </v-btn>
        </div>
      </section>
    </v-container>

    <v-dialog v-model="clearDialog" max-width="520">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ tm('confirm.clearTitle') }}</v-card-title>
        <v-card-text>{{ tm('confirm.clearText') }}</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="maintenanceLoading" @click="clearDialog = false">{{ tm('actions.cancel') }}</v-btn>
          <v-btn color="error" variant="tonal" :loading="maintenanceLoading" @click="clearCompleted">{{ tm('actions.confirm') }}</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="deleteDialog" max-width="520">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ tm('confirm.deleteTitle') }}</v-card-title>
        <v-card-text>{{ tm('confirm.deleteText') }}</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="maintenanceLoading" @click="deleteDialog = false">{{ tm('actions.cancel') }}</v-btn>
          <v-btn color="error" variant="tonal" :loading="maintenanceLoading" @click="deleteTrace">{{ tm('actions.confirm') }}</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
@import '@/styles/dashboard-shell.css';

.execution-trace-page {
  min-height: 100%;
}

.trace-header-meta {
  margin-top: 12px;
}

.trace-status-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}

.trace-status-card {
  min-width: 0;
  padding: 16px;
}

.trace-status-icon {
  display: inline-flex;
  width: 32px;
  height: 32px;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: var(--dashboard-soft);
  color: rgb(var(--v-theme-primary));
}

.trace-status-label {
  margin-top: 10px;
  color: var(--dashboard-muted);
  font-size: 12px;
  font-weight: 600;
}

.trace-status-value {
  margin-top: 4px;
  font-size: 27px;
  font-weight: 700;
  line-height: 1.1;
}

.trace-status-note {
  min-height: 30px;
  margin-top: 6px;
  color: var(--dashboard-subtle);
  font-size: 11px;
  line-height: 1.45;
}

.trace-status-card.is-running .trace-status-icon {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
}

.trace-status-card.is-success .trace-status-icon {
  background: rgba(var(--v-theme-success), 0.12);
  color: rgb(var(--v-theme-success));
}

.trace-status-card.is-failed .trace-status-icon {
  background: rgba(var(--v-theme-error), 0.12);
  color: rgb(var(--v-theme-error));
}

.trace-status-card.is-degraded .trace-status-icon {
  background: rgba(var(--v-theme-warning), 0.12);
  color: rgb(var(--v-theme-warning));
}

.trace-filters {
  display: grid;
  grid-template-columns: repeat(3, minmax(180px, 1fr));
  align-items: center;
  gap: 12px;
}

.trace-table-card {
  min-width: 0;
  overflow: hidden;
  container-type: inline-size;
}

.trace-table-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px 16px;
  border-bottom: 1px solid var(--dashboard-border);
}

.trace-result-count {
  color: var(--dashboard-muted);
  font-size: 13px;
  white-space: nowrap;
}

.trace-table-wrap {
  width: 100%;
  min-width: 0;
  overflow: hidden;
}

.trace-table-head,
.trace-row {
  display: grid;
  width: 100%;
  min-width: 0;
  grid-template-columns:
    minmax(0, 1.25fr)
    minmax(0, 1.35fr)
    minmax(0, 0.9fr)
    minmax(0, 0.8fr)
    minmax(0, 0.95fr)
    110px;
  gap: 14px;
}

.trace-table-head {
  padding: 11px 18px;
  border-bottom: 1px solid var(--dashboard-border);
  background: rgba(var(--v-theme-on-surface), 0.018);
  color: var(--dashboard-muted);
  font-size: 11px;
  font-weight: 700;
}

.trace-row {
  align-items: center;
  padding: 14px 18px;
  border: 0;
  border-bottom: 1px solid var(--dashboard-border);
  cursor: pointer;
  outline: 0;
}

.trace-row:hover {
  background: var(--dashboard-soft);
}

.trace-row:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: -2px;
}

.trace-row > div {
  min-width: 0;
}

.trace-time,
.trace-identity,
.trace-source,
.trace-state,
.trace-metrics {
  display: grid;
  gap: 4px;
}

.trace-time strong,
.trace-identity code,
.trace-source strong,
.trace-metrics strong {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-time span,
.trace-source span,
.trace-state > span,
.trace-metrics span {
  min-width: 0;
  overflow: hidden;
  color: var(--dashboard-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-identity code {
  color: var(--dashboard-text);
  font-size: 11px;
}

.trace-context {
  display: flex;
  gap: 4px;
  overflow: hidden;
  flex-wrap: wrap;
}

.trace-context span {
  max-width: 100%;
  overflow: hidden;
  color: var(--dashboard-subtle);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-state > div,
.trace-row-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.trace-row-actions {
  justify-content: flex-start;
}

.trace-empty,
.trace-load-more {
  display: flex;
  min-height: 160px;
  align-items: center;
  justify-content: center;
  padding: 24px;
  color: var(--dashboard-muted);
  text-align: center;
}

.trace-load-more {
  min-height: auto;
  border-top: 1px solid var(--dashboard-border);
}

@media (max-width: 1320px) {
  .trace-status-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 920px) {
  .trace-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@container (max-width: 1080px) {
  .trace-table-head {
    display: none;
  }

  .trace-table-wrap {
    display: grid;
    gap: 12px;
    padding: 12px;
  }

  .trace-row {
    grid-template-columns: repeat(2, minmax(0, 1fr)) 110px;
    gap: 14px 18px;
    padding: 16px;
    border: 1px solid var(--dashboard-border);
    border-radius: 12px;
  }

  .trace-row > div::before {
    display: block;
    margin-bottom: 5px;
    color: var(--dashboard-subtle);
    content: attr(data-label);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  .trace-row-actions {
    grid-column: 3;
    grid-row: 1 / span 3;
    align-self: stretch;
    justify-content: flex-start;
    flex-direction: column;
  }
}

@container (max-width: 720px) {
  .trace-table-wrap {
    padding: 10px;
  }

  .trace-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .trace-row-actions {
    grid-column: auto;
    grid-row: auto;
    align-items: center;
    justify-content: flex-start;
    flex-direction: row;
  }
}

@media (max-width: 640px) {
  .trace-status-grid,
  .trace-filters {
    grid-template-columns: 1fr;
  }

  .trace-table-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
