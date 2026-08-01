<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

import {
  executionTraceApi,
  type ExecutionTraceArtifact,
  type ExecutionTraceArtifactRef,
  type ExecutionTraceConfig,
  type ExecutionTraceDetail,
  type ExecutionTraceEvent,
  type ExecutionTraceLink,
  type ExecutionTraceOverview,
  type ExecutionTraceSummary,
} from '@/api/v1';
import { useModuleI18n } from '@/i18n/composables';
import { useToast } from '@/utils/toast';

const PAGE_SIZE = 50;
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
const selectedTraceId = ref<string | null>(null);
const detail = ref<ExecutionTraceDetail | null>(null);
const statusFilter = ref<string | null>(null);
const operationFilter = ref('');
const degradedOnly = ref(false);
const loading = ref(false);
const listLoading = ref(false);
const detailLoading = ref(false);
const configSaving = ref(false);
const maintenanceLoading = ref(false);
const loadError = ref('');
const clearDialog = ref(false);
const deleteDialog = ref(false);
const artifactDialog = ref(false);
const artifactLoading = ref(false);
const artifact = ref<ExecutionTraceArtifact | null>(null);
const selectedArtifact = ref<ExecutionTraceArtifactRef | null>(null);

const statusOptions = computed(() => [
  { title: tm('filters.allStatuses'), value: null },
  ...['running', 'success', 'skipped', 'error', 'cancelled', 'incomplete'].map(
    (status) => ({ title: status, value: status }),
  ),
]);

const canLoadMore = computed(() => hasMore.value && traces.value.length > 0);
const spanRows = computed(() => {
  const spans = detail.value?.spans || [];
  const byId = new Map(spans.map((span) => [span.span_id, span]));
  return spans.map((span) => {
    let depth = 0;
    let parentId = span.parent_span_id;
    const visited = new Set<string>();
    while (parentId && byId.has(parentId) && !visited.has(parentId) && depth < 24) {
      visited.add(parentId);
      depth += 1;
      parentId = byId.get(parentId)?.parent_span_id;
    }
    return { span, depth };
  });
});
const eventsBySpan = computed(() => groupBySpan<ExecutionTraceEvent>(detail.value?.events || []));
const artifactsBySpan = computed(() =>
  groupBySpan<ExecutionTraceArtifactRef>(detail.value?.artifact_refs || []),
);
const linksBySpan = computed(() => groupBySpan<ExecutionTraceLink>(detail.value?.links || []));

function unwrap<T>(response: { data?: { status?: string; data?: T; message?: string | null } }): T {
  if (response.data?.status !== 'ok' || response.data.data === undefined) {
    throw new Error(response.data?.message || 'Execution Trace request failed');
  }
  return response.data.data;
}

function groupBySpan<T extends { span_id: string }>(items: T[]): Map<string, T[]> {
  const grouped = new Map<string, T[]>();
  for (const item of items) {
    const group = grouped.get(item.span_id) || [];
    group.push(item);
    grouped.set(item.span_id, group);
  }
  return grouped;
}

function statusColor(status: string): string {
  return {
    running: 'primary',
    success: 'success',
    skipped: 'secondary',
    error: 'error',
    cancelled: 'warning',
    incomplete: 'warning',
  }[status] || 'secondary';
}

function formatTime(value?: number | null): string {
  if (!value) {
    return '–';
  }
  return new Date(value * 1000).toLocaleString();
}

function formatDuration(startedAt: number, endedAt?: number | null): string {
  if (!endedAt) {
    return '–';
  }
  const milliseconds = Math.max(0, Math.round((endedAt - startedAt) * 1000));
  if (milliseconds < 1000) {
    return `${milliseconds} ms`;
  }
  if (milliseconds < 60_000) {
    return `${(milliseconds / 1000).toFixed(2)} s`;
  }
  return `${Math.floor(milliseconds / 60_000)}m ${Math.round((milliseconds % 60_000) / 1000)}s`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value || {}, null, 2);
}

async function loadConfig() {
  config.value = unwrap(await executionTraceApi.config());
}

async function loadOverview() {
  overview.value = unwrap(await executionTraceApi.overview());
}

async function loadTraces(reset = true) {
  if (listLoading.value) {
    return;
  }
  listLoading.value = true;
  try {
    if (reset) {
      hasMore.value = false;
    }
    const last = reset ? null : traces.value[traces.value.length - 1] || null;
    const response = await executionTraceApi.list({
      limit: PAGE_SIZE,
      status: statusFilter.value || undefined,
      operation: operationFilter.value.trim() || undefined,
      degraded: degradedOnly.value || undefined,
      before_ended_at: last?.ended_at || last?.started_at,
      before_trace_id: last?.trace_id,
    });
    const items = unwrap<{ items: ExecutionTraceSummary[] }>(response).items;
    traces.value = reset ? items : [...traces.value, ...items];
    hasMore.value = items.length === PAGE_SIZE;
    if (selectedTraceId.value && !traces.value.some((item) => item.trace_id === selectedTraceId.value)) {
      selectedTraceId.value = null;
      detail.value = null;
    }
  } finally {
    listLoading.value = false;
  }
}

async function refresh() {
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([loadConfig(), loadOverview(), loadTraces(true)]);
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : tm('messages.loadFailed');
    toastError(tm('messages.loadFailed'));
  } finally {
    loading.value = false;
  }
}

async function updateEnabled(enabled: boolean | null) {
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

async function selectTrace(trace: ExecutionTraceSummary) {
  selectedTraceId.value = trace.trace_id;
  detailLoading.value = true;
  artifact.value = null;
  selectedArtifact.value = null;
  try {
    detail.value = unwrap(await executionTraceApi.detail(trace.trace_id));
  } catch {
    detail.value = null;
    toastError(tm('messages.loadFailed'));
  } finally {
    detailLoading.value = false;
  }
}

async function runCleanup() {
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

async function clearCompleted() {
  maintenanceLoading.value = true;
  try {
    const result = unwrap(await executionTraceApi.clear());
    clearDialog.value = false;
    selectedTraceId.value = null;
    detail.value = null;
    toastSuccess(tm('messages.clearDone', { count: result.deleted }));
    await refresh();
  } catch {
    toastError(tm('messages.clearFailed'));
  } finally {
    maintenanceLoading.value = false;
  }
}

async function deleteSelectedTrace() {
  if (!selectedTraceId.value) {
    return;
  }
  maintenanceLoading.value = true;
  try {
    unwrap(await executionTraceApi.remove(selectedTraceId.value));
    deleteDialog.value = false;
    selectedTraceId.value = null;
    detail.value = null;
    toastSuccess(tm('messages.deleteDone'));
    await refresh();
  } catch {
    toastError(tm('messages.deleteFailed'));
  } finally {
    maintenanceLoading.value = false;
  }
}

async function openArtifact(ref: ExecutionTraceArtifactRef) {
  artifactLoading.value = true;
  selectedArtifact.value = ref;
  artifact.value = null;
  artifactDialog.value = true;
  try {
    artifact.value = unwrap(await executionTraceApi.artifact(ref.content_hash));
  } catch {
    toastError(tm('messages.artifactFailed'));
  } finally {
    artifactLoading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div class="dashboard-page execution-trace-page">
    <v-container fluid class="dashboard-shell pa-4 pa-md-6">
      <div class="dashboard-header">
        <div class="dashboard-header-main">
          <h1 class="dashboard-title">{{ tm('title') }}</h1>
          <p class="dashboard-subtitle">{{ tm('subtitle') }}</p>
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
            @click="refresh"
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

      <div class="dashboard-overview-grid">
        <div class="dashboard-card dashboard-overview-card">
          <div class="dashboard-card-icon"><v-icon>mdi-chart-timeline-variant</v-icon></div>
          <div class="dashboard-card-label">{{ tm('overview.traces24h') }}</div>
          <div class="dashboard-card-value">{{ overview.traces_24h }}</div>
        </div>
        <div class="dashboard-card dashboard-overview-card">
          <div class="dashboard-card-icon"><v-icon>mdi-progress-clock</v-icon></div>
          <div class="dashboard-card-label">{{ tm('overview.running') }}</div>
          <div class="dashboard-card-value">{{ overview.running }}</div>
        </div>
        <div class="dashboard-card dashboard-overview-card">
          <div class="dashboard-card-icon"><v-icon>mdi-alert-circle-outline</v-icon></div>
          <div class="dashboard-card-label">{{ tm('overview.errors24h') }}</div>
          <div class="dashboard-card-value">{{ overview.errors_24h }}</div>
        </div>
        <div class="dashboard-card dashboard-overview-card">
          <div class="dashboard-card-icon"><v-icon>mdi-database-outline</v-icon></div>
          <div class="dashboard-card-label">{{ tm('overview.storage') }}</div>
          <div class="dashboard-card-value">{{ formatBytes(overview.physical_size) }}</div>
        </div>
      </div>

      <section class="dashboard-card dashboard-card--padded mb-5">
        <div class="dashboard-section-head">
          <div class="dashboard-section-title">{{ tm('filters.title') }}</div>
        </div>
        <div class="trace-filters">
          <v-select
            v-model="statusFilter"
            :items="statusOptions"
            :label="tm('filters.status')"
            clearable
            density="compact"
            hide-details
            variant="outlined"
            @update:model-value="loadTraces(true)"
          />
          <v-text-field
            v-model="operationFilter"
            :label="tm('filters.operation')"
            clearable
            density="compact"
            hide-details
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            @click:clear="loadTraces(true)"
            @keyup.enter="loadTraces(true)"
          />
          <v-checkbox
            v-model="degradedOnly"
            :label="tm('filters.degraded')"
            density="compact"
            hide-details
            @update:model-value="loadTraces(true)"
          />
        </div>
      </section>

      <div class="dashboard-split-grid trace-split-grid">
        <section class="dashboard-card trace-list-card">
          <div class="trace-section-header">
            <div>
              <div class="dashboard-section-title">{{ tm('list.title') }}</div>
              <div class="dashboard-section-subtitle">{{ tm('list.subtitle') }}</div>
            </div>
          </div>
          <v-progress-linear v-if="listLoading" color="primary" indeterminate />
          <div v-else-if="!traces.length" class="trace-empty">{{ tm('list.empty') }}</div>
          <div v-else class="trace-list">
            <button
              v-for="trace in traces"
              :key="trace.trace_id"
              class="trace-list-row"
              :class="{ selected: trace.trace_id === selectedTraceId }"
              type="button"
              @click="selectTrace(trace)"
            >
              <div class="trace-list-row-main">
                <div class="trace-operation">{{ trace.operation }}</div>
                <div class="trace-row-meta">
                  <span>{{ formatTime(trace.started_at) }}</span>
                  <span>{{ formatDuration(trace.started_at, trace.ended_at) }}</span>
                  <span v-if="trace.plugin_id">{{ trace.plugin_id }}</span>
                </div>
              </div>
              <div class="trace-list-row-status">
                <v-chip :color="statusColor(trace.status)" size="x-small" variant="tonal">
                  {{ trace.status }}
                </v-chip>
                <v-icon v-if="trace.degraded" color="warning" size="18">mdi-alert-outline</v-icon>
              </div>
            </button>
          </div>
          <div v-if="canLoadMore" class="trace-load-more">
            <v-btn variant="text" :loading="listLoading" @click="loadTraces(false)">
              {{ tm('actions.loadMore') }}
            </v-btn>
          </div>
        </section>

        <section class="dashboard-card trace-detail-card">
          <template v-if="detailLoading">
            <div class="trace-empty"><v-progress-circular color="primary" indeterminate /></div>
          </template>
          <template v-else-if="detail">
            <div class="trace-section-header trace-detail-header">
              <div>
                <div class="dashboard-section-title">{{ tm('detail.title') }}</div>
                <div class="dashboard-section-subtitle">{{ tm('detail.subtitle') }}</div>
                <code class="trace-id">{{ detail.trace.trace_id }}</code>
              </div>
              <v-btn
                color="error"
                icon="mdi-delete-outline"
                size="small"
                variant="text"
                :disabled="detail.trace.status === 'running' || maintenanceLoading"
                @click="deleteDialog = true"
              />
            </div>

            <div class="trace-detail-summary">
              <v-chip :color="statusColor(detail.trace.status)" size="small" variant="tonal">
                {{ detail.trace.status }}
              </v-chip>
              <span>{{ detail.trace.operation }}</span>
              <span>{{ formatDuration(detail.trace.started_at, detail.trace.ended_at) }}</span>
              <span v-if="detail.trace.outcome">{{ tm('detail.outcome') }}: {{ detail.trace.outcome }}</span>
            </div>

            <div v-if="detail.trace.degradation_reasons?.length" class="trace-degradation">
              <strong>{{ tm('detail.reasons') }}</strong>
              <v-chip
                v-for="reason in detail.trace.degradation_reasons"
                :key="reason"
                color="warning"
                size="x-small"
                variant="tonal"
              >
                {{ reason }}
              </v-chip>
            </div>

            <div class="trace-detail-block">
              <div class="trace-block-title">{{ tm('detail.spans') }}</div>
              <div v-for="row in spanRows" :key="row.span.span_id" class="trace-span-row">
                <div class="trace-span-line" :style="{ paddingLeft: `${row.depth * 18}px` }">
                  <span class="trace-span-indent" :class="{ root: row.depth === 0 }"></span>
                  <span class="trace-span-operation">{{ row.span.operation }}</span>
                  <v-chip :color="statusColor(row.span.status)" size="x-small" variant="tonal">
                    {{ row.span.status }}
                  </v-chip>
                  <span class="trace-span-duration">{{ formatDuration(row.span.started_at, row.span.ended_at) }}</span>
                  <v-icon v-if="row.span.degraded" color="warning" size="16">mdi-alert-outline</v-icon>
                </div>
                <details v-if="Object.keys(row.span.attributes || {}).length" class="trace-json-details">
                  <summary>{{ tm('detail.attributes') }}</summary>
                  <pre>{{ formatJson(row.span.attributes) }}</pre>
                </details>
                <div v-if="eventsBySpan.get(row.span.span_id)?.length" class="trace-record-group">
                  <div class="trace-record-label">{{ tm('detail.events') }}</div>
                  <div v-for="event in eventsBySpan.get(row.span.span_id)" :key="`${event.span_id}-${event.event_index}`" class="trace-record">
                    <span>{{ event.name }}</span>
                    <span>{{ formatTime(event.occurred_at) }}</span>
                  </div>
                </div>
                <div v-if="artifactsBySpan.get(row.span.span_id)?.length" class="trace-record-group">
                  <div class="trace-record-label">{{ tm('detail.artifacts') }}</div>
                  <div v-for="ref in artifactsBySpan.get(row.span.span_id)" :key="`${ref.span_id}-${ref.ref_index}`" class="trace-artifact-row">
                    <div>
                      <strong>{{ ref.role }}</strong>
                      <span>{{ ref.media_type || 'application/octet-stream' }}</span>
                      <span>{{ formatBytes(ref.captured_size || ref.logical_size || 0) }}</span>
                      <v-chip v-if="ref.truncated" color="warning" size="x-small" variant="tonal">truncated</v-chip>
                    </div>
                    <v-btn size="x-small" variant="text" @click="openArtifact(ref)">
                      {{ tm('actions.viewArtifact') }}
                    </v-btn>
                  </div>
                </div>
                <div v-if="linksBySpan.get(row.span.span_id)?.length" class="trace-record-group">
                  <div class="trace-record-label">{{ tm('detail.links') }}</div>
                  <div v-for="link in linksBySpan.get(row.span.span_id)" :key="`${link.span_id}-${link.link_index}`" class="trace-record">
                    <span>{{ link.relation }}</span>
                    <code>{{ link.target_trace_id || link.target_span_id || '–' }}</code>
                  </div>
                </div>
              </div>
            </div>
          </template>
          <div v-else class="trace-empty">{{ tm('detail.empty') }}</div>
        </section>
      </div>
    </v-container>

    <v-dialog v-model="artifactDialog" max-width="960">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ tm('artifact.title') }}</v-card-title>
        <v-card-text>
          <v-progress-linear v-if="artifactLoading" color="primary" indeterminate />
          <template v-else-if="artifact">
            <v-alert v-if="selectedArtifact?.truncated" class="mb-3" density="compact" type="warning" variant="tonal">
              {{ tm('artifact.truncated') }}
            </v-alert>
            <pre class="artifact-content">{{ artifact.content || tm('artifact.empty') }}</pre>
          </template>
          <div v-else class="trace-empty">{{ tm('artifact.empty') }}</div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="artifactDialog = false">{{ tm('actions.close') }}</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

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
          <v-btn color="error" variant="tonal" :loading="maintenanceLoading" @click="deleteSelectedTrace">{{ tm('actions.confirm') }}</v-btn>
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

.trace-filters {
  display: grid;
  grid-template-columns: minmax(180px, 0.7fr) minmax(220px, 1.3fr) auto;
  align-items: center;
  gap: 14px;
}

.trace-split-grid {
  align-items: start;
}

.trace-list-card,
.trace-detail-card {
  min-height: 500px;
}

.trace-section-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 20px 16px;
  border-bottom: 1px solid var(--dashboard-border);
}

.trace-list {
  max-height: 720px;
  overflow: auto;
}

.trace-list-row {
  display: flex;
  width: 100%;
  min-width: 0;
  padding: 14px 18px;
  border: 0;
  border-bottom: 1px solid var(--dashboard-border);
  color: inherit;
  cursor: pointer;
  text-align: left;
  background: transparent;
}

.trace-list-row:hover,
.trace-list-row.selected {
  background: var(--dashboard-soft);
}

.trace-list-row-main {
  min-width: 0;
  flex: 1;
}

.trace-list-row-status {
  display: flex;
  align-items: center;
  align-self: flex-start;
  gap: 8px;
  padding-left: 12px;
}

.trace-operation {
  overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-row-meta,
.trace-detail-summary,
.trace-degradation,
.trace-artifact-row > div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.trace-row-meta span + span::before {
  margin-right: 8px;
  color: var(--dashboard-subtle);
  content: '·';
}

.trace-load-more,
.trace-empty {
  display: flex;
  min-height: 140px;
  align-items: center;
  justify-content: center;
  padding: 24px;
  color: var(--dashboard-muted);
  text-align: center;
}

.trace-detail-card {
  padding-bottom: 20px;
}

.trace-detail-header {
  align-items: flex-start;
}

.trace-id {
  display: inline-block;
  max-width: 100%;
  margin-top: 10px;
  color: var(--dashboard-subtle);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.trace-detail-summary,
.trace-degradation,
.trace-detail-block {
  margin: 16px 20px 0;
}

.trace-degradation {
  align-items: center;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(var(--v-theme-warning), 0.08);
}

.trace-block-title {
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 700;
}

.trace-span-row {
  padding: 10px 0;
  border-top: 1px solid var(--dashboard-border);
}

.trace-span-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.trace-span-indent {
  display: inline-block;
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border: 2px solid rgb(var(--v-theme-primary));
  border-radius: 50%;
}

.trace-span-indent.root {
  background: rgb(var(--v-theme-primary));
}

.trace-span-operation {
  min-width: 0;
  overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-span-duration {
  margin-left: auto;
  color: var(--dashboard-muted);
  font-size: 12px;
  white-space: nowrap;
}

.trace-json-details {
  margin: 8px 0 0 27px;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.trace-json-details summary {
  cursor: pointer;
}

.trace-json-details pre,
.artifact-content {
  max-height: 340px;
  margin: 8px 0 0;
  padding: 12px;
  overflow: auto;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.04);
  color: var(--dashboard-text);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.trace-record-group {
  margin: 8px 0 0 27px;
}

.trace-record-label {
  margin-bottom: 4px;
  color: var(--dashboard-muted);
  font-size: 12px;
  font-weight: 650;
}

.trace-record,
.trace-artifact-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  padding: 4px 0;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.trace-record code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-content {
  max-height: 65vh;
  margin: 0;
}

@media (max-width: 960px) {
  .trace-filters {
    grid-template-columns: 1fr;
  }

  .trace-split-grid {
    grid-template-columns: 1fr;
  }
}
</style>
