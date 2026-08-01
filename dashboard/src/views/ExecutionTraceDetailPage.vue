<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import {
  executionTraceApi,
  type ExecutionTraceDetail,
  type ExecutionTraceSpan,
} from '@/api/v1';
import SpanDetailDialog from '@/components/execution-trace/SpanDetailDialog.vue';
import SpanWaterfall from '@/components/execution-trace/SpanWaterfall.vue';
import { useI18n, useModuleI18n } from '@/i18n/composables';
import { useToast } from '@/utils/toast';
import {
  executionTraceDuration,
  executionTraceSpanLabel,
  executionTraceStatusColor,
  formatExecutionTraceDateTime,
  formatExecutionTraceDuration,
  isRunningTraceStatus,
  safeExecutionTraceJson,
  traceSummaryRevision,
} from '@/utils/executionTrace';

const props = defineProps<{
  traceId: string;
}>();

const ACTIVE_DETAIL_POLL_INTERVAL_MS = 2_500;
const LIVE_CLOCK_INTERVAL_MS = 1_000;

const router = useRouter();
const { locale } = useI18n();
const { tm } = useModuleI18n('features/execution-trace');
const { error: toastError, success: toastSuccess } = useToast();

const detail = ref<ExecutionTraceDetail | null>(null);
const loading = ref(true);
const refreshing = ref(false);
const deleting = ref(false);
const errorMessage = ref('');
const deleteDialog = ref(false);
const spanDialog = ref(false);
const selectedSpan = ref<ExecutionTraceSpan | null>(null);
const currentSeconds = ref(Date.now() / 1000);
const appliedRevision = ref<number | null>(null);

let traceEpoch = 0;
let issuedRequestId = 0;
let lastAppliedRequestId = 0;
let latestIssuedRequestId = 0;
let pollRequestInFlight = false;
let detailPollTimer: ReturnType<typeof window.setInterval> | null = null;
let clockTimer: ReturnType<typeof window.setInterval> | null = null;

const trace = computed(() => detail.value?.trace || null);
const running = computed(() => isRunningTraceStatus(trace.value?.status));
const duration = computed(() =>
  trace.value ? executionTraceDuration(trace.value, currentSeconds.value) : null,
);
const activeOperation = computed(() => {
  const latestRunningSpan = [...(detail.value?.spans || [])]
    .filter((span) => isRunningTraceStatus(span.status))
    .sort((left, right) => right.started_at - left.started_at)[0];
  if (latestRunningSpan) {
    return executionTraceSpanLabel(latestRunningSpan);
  }
  return trace.value?.active_span_operation || tm('table.noActiveOperation');
});
const contextEntries = computed(() => {
  const attributes = trace.value?.attributes || {};
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
});
const counts = computed(() => ({
  spans: detail.value?.spans.length || 0,
  events: detail.value?.events.length || 0,
  artifacts: detail.value?.artifact_refs.length || 0,
  links: detail.value?.links.length || 0,
}));

function unwrap<T>(response: { data?: { status?: string; data?: T; message?: string | null } }): T {
  if (response.data?.status !== 'ok' || response.data.data === undefined) {
    throw new Error(response.data?.message || 'Execution Trace request failed');
  }
  return response.data.data;
}

function formatDateTime(value: unknown): string {
  return formatExecutionTraceDateTime(value, locale.value);
}

function shouldApplySnapshot(requestId: number, incoming: ExecutionTraceDetail): boolean {
  const revision = traceSummaryRevision(incoming);
  if (revision !== null && appliedRevision.value !== null) {
    if (revision < appliedRevision.value) {
      return false;
    }
    if (revision === appliedRevision.value && requestId < lastAppliedRequestId) {
      return false;
    }
  } else if (revision === null && requestId < lastAppliedRequestId) {
    return false;
  }
  return true;
}

async function loadDetail(options: { polling?: boolean } = {}): Promise<void> {
  if (options.polling && pollRequestInFlight) {
    return;
  }
  const epoch = traceEpoch;
  const requestId = ++issuedRequestId;
  latestIssuedRequestId = requestId;
  if (options.polling) {
    pollRequestInFlight = true;
  }
  if (detail.value) {
    refreshing.value = true;
  } else {
    loading.value = true;
  }
  try {
    const incoming = unwrap(await executionTraceApi.detail(props.traceId));
    if (epoch !== traceEpoch || !shouldApplySnapshot(requestId, incoming)) {
      return;
    }
    detail.value = incoming;
    if (selectedSpan.value) {
      selectedSpan.value = incoming.spans.find(
        (span) => span.span_id === selectedSpan.value?.span_id,
      ) || selectedSpan.value;
    }
    const revision = traceSummaryRevision(incoming);
    if (revision !== null) {
      appliedRevision.value = revision;
    }
    lastAppliedRequestId = Math.max(lastAppliedRequestId, requestId);
    errorMessage.value = '';
  } catch (error) {
    if (epoch === traceEpoch && requestId === latestIssuedRequestId) {
      errorMessage.value = error instanceof Error ? error.message : tm('messages.loadFailed');
    }
  } finally {
    if (options.polling) {
      pollRequestInFlight = false;
    }
    if (epoch === traceEpoch && requestId === latestIssuedRequestId) {
      loading.value = false;
      refreshing.value = false;
      syncLiveTimers();
    }
  }
}

function stopLiveTimers(): void {
  if (detailPollTimer !== null) {
    window.clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
  if (clockTimer !== null) {
    window.clearInterval(clockTimer);
    clockTimer = null;
  }
}

function syncLiveTimers(): void {
  stopLiveTimers();
  if (document.hidden || !running.value) {
    return;
  }
  detailPollTimer = window.setInterval(() => {
    void loadDetail({ polling: true });
  }, ACTIVE_DETAIL_POLL_INTERVAL_MS);
  clockTimer = window.setInterval(() => {
    currentSeconds.value = Date.now() / 1000;
  }, LIVE_CLOCK_INTERVAL_MS);
}

function handleVisibilityChange(): void {
  if (!document.hidden && running.value) {
    currentSeconds.value = Date.now() / 1000;
    void loadDetail({ polling: true });
  }
  syncLiveTimers();
}

function resetTrace(): void {
  traceEpoch += 1;
  issuedRequestId = 0;
  lastAppliedRequestId = 0;
  latestIssuedRequestId = 0;
  pollRequestInFlight = false;
  detail.value = null;
  selectedSpan.value = null;
  spanDialog.value = false;
  appliedRevision.value = null;
  errorMessage.value = '';
  loading.value = true;
  refreshing.value = false;
  currentSeconds.value = Date.now() / 1000;
  stopLiveTimers();
  void loadDetail();
}

function openSpan(span: ExecutionTraceSpan): void {
  selectedSpan.value = span;
  spanDialog.value = true;
}

async function deleteTrace(): Promise<void> {
  if (!trace.value || running.value) {
    return;
  }
  deleting.value = true;
  try {
    unwrap(await executionTraceApi.remove(props.traceId));
    deleteDialog.value = false;
    toastSuccess(tm('messages.deleteDone'));
    await router.replace({ name: 'ExecutionTrace' });
  } catch {
    toastError(tm('messages.deleteFailed'));
  } finally {
    deleting.value = false;
  }
}

watch(running, syncLiveTimers);
watch(
  () => props.traceId,
  resetTrace,
);

onMounted(() => {
  document.addEventListener('visibilitychange', handleVisibilityChange);
  resetTrace();
});

onBeforeUnmount(() => {
  traceEpoch += 1;
  stopLiveTimers();
  document.removeEventListener('visibilitychange', handleVisibilityChange);
});
</script>

<template>
  <div class="dashboard-page execution-trace-page">
    <v-container fluid class="dashboard-shell pa-4 pa-md-6">
      <div class="dashboard-header trace-detail-page-header">
        <div class="dashboard-header-main">
          <v-btn
            class="back-button"
            color="primary"
            prepend-icon="mdi-arrow-left"
            size="small"
            variant="text"
            @click="router.push({ name: 'ExecutionTrace' })"
          >
            {{ tm('actions.backToOverview') }}
          </v-btn>
          <h1 class="dashboard-title">{{ trace?.operation || tm('detail.title') }}</h1>
          <p class="dashboard-subtitle trace-id" :title="traceId">{{ traceId }}</p>
        </div>
        <div class="dashboard-header-actions">
          <v-chip v-if="trace" :color="executionTraceStatusColor(trace.status)" size="small" variant="tonal">
            {{ trace.status }}
          </v-chip>
          <v-chip v-if="running" color="info" size="small" variant="tonal">
            <v-icon start size="14">mdi-radiobox-marked</v-icon>
            {{ tm('detail.live') }}
          </v-chip>
          <v-btn
            color="primary"
            prepend-icon="mdi-refresh"
            variant="text"
            :loading="refreshing"
            @click="loadDetail()"
          >
            {{ tm('actions.refresh') }}
          </v-btn>
          <v-btn
            color="error"
            prepend-icon="mdi-delete-outline"
            variant="tonal"
            :disabled="!trace || running || deleting"
            @click="deleteDialog = true"
          >
            {{ tm('actions.delete') }}
          </v-btn>
        </div>
      </div>

      <v-progress-linear v-if="loading" class="mb-4" color="primary" indeterminate />
      <v-alert v-if="errorMessage" class="mb-4" density="compact" type="error" variant="tonal">
        {{ errorMessage }}
      </v-alert>

      <template v-if="trace && detail">
        <section class="trace-detail-stats">
          <div class="dashboard-card trace-detail-stat">
            <span>{{ tm('detail.duration') }}</span>
            <strong>{{ formatExecutionTraceDuration(duration) }}</strong>
            <small>{{ formatDateTime(trace.started_at) }}</small>
          </div>
          <div class="dashboard-card trace-detail-stat">
            <span>{{ tm('detail.spans') }}</span>
            <strong>{{ counts.spans }}</strong>
            <small>{{ tm('detail.activeOperation') }}: {{ activeOperation }}</small>
          </div>
          <div class="dashboard-card trace-detail-stat">
            <span>{{ tm('detail.events') }}</span>
            <strong>{{ counts.events }}</strong>
            <small>{{ tm('detail.artifacts') }}: {{ counts.artifacts }}</small>
          </div>
          <div class="dashboard-card trace-detail-stat">
            <span>{{ tm('detail.links') }}</span>
            <strong>{{ counts.links }}</strong>
            <small>{{ tm('detail.revision') }} {{ appliedRevision ?? '–' }}</small>
          </div>
          <div class="dashboard-card trace-detail-stat" :class="{ degraded: trace.degraded }">
            <span>{{ tm('detail.observability') }}</span>
            <strong>{{ trace.degraded ? tm('detail.degraded') : tm('detail.complete') }}</strong>
            <small>{{ trace.outcome || trace.status }}</small>
          </div>
        </section>

        <section class="trace-detail-layout">
          <article class="dashboard-card waterfall-card">
            <div class="waterfall-heading">
              <div>
                <div class="dashboard-section-title">{{ tm('detail.waterfall') }}</div>
                <div class="dashboard-section-subtitle">{{ tm('detail.waterfallSubtitle') }}</div>
              </div>
              <span v-if="running" class="live-indicator"><span />{{ tm('detail.live') }}</span>
            </div>
            <SpanWaterfall
              v-if="detail.spans.length"
              :spans="detail.spans"
              :trace-started-at="trace.started_at"
              :current-seconds="running ? currentSeconds : null"
              @select="openSpan"
            />
            <div v-else class="trace-empty">
              {{ running ? tm('detail.waitingForSpans') : tm('detail.noSpans') }}
            </div>
          </article>

          <aside class="trace-detail-side">
            <article class="dashboard-card dashboard-card--padded">
              <div class="dashboard-section-title">{{ tm('detail.runInfo') }}</div>
              <dl class="trace-info-list">
                <div>
                  <dt>{{ tm('table.operation') }}</dt>
                  <dd>{{ trace.operation }}</dd>
                </div>
                <div>
                  <dt>{{ tm('table.source') }}</dt>
                  <dd>{{ trace.source || '–' }} / {{ trace.kind || '–' }}</dd>
                </div>
                <div v-if="trace.plugin_id">
                  <dt>{{ tm('table.plugin') }}</dt>
                  <dd class="mono">{{ trace.plugin_id }}</dd>
                </div>
                <div>
                  <dt>{{ tm('detail.activeOperation') }}</dt>
                  <dd>{{ activeOperation }}</dd>
                </div>
                <div>
                  <dt>{{ tm('detail.started') }}</dt>
                  <dd>{{ formatDateTime(trace.started_at) }}</dd>
                </div>
                <div>
                  <dt>{{ tm('detail.ended') }}</dt>
                  <dd>{{ formatDateTime(trace.ended_at) }}</dd>
                </div>
              </dl>
            </article>

            <article v-if="contextEntries.length" class="dashboard-card dashboard-card--padded">
              <div class="dashboard-section-title">{{ tm('detail.context') }}</div>
              <dl class="trace-info-list">
                <div v-for="entry in contextEntries" :key="entry.key">
                  <dt>{{ entry.label }}</dt>
                  <dd>{{ entry.value }}</dd>
                </div>
              </dl>
            </article>

            <article v-if="trace.degradation_reasons?.length" class="dashboard-card dashboard-card--padded degradation-card">
              <div class="dashboard-section-title">{{ tm('detail.reasons') }}</div>
              <div class="degradation-reasons">
                <v-chip
                  v-for="reason in trace.degradation_reasons"
                  :key="reason"
                  color="warning"
                  size="small"
                  variant="tonal"
                >
                  {{ reason }}
                </v-chip>
              </div>
            </article>

            <article v-if="Object.keys(trace.attributes || {}).length" class="dashboard-card dashboard-card--padded">
              <div class="dashboard-section-title">{{ tm('detail.rootAttributes') }}</div>
              <pre class="trace-json-block">{{ safeExecutionTraceJson(trace.attributes) }}</pre>
            </article>
          </aside>
        </section>
      </template>
    </v-container>

    <SpanDetailDialog
      v-model="spanDialog"
      :span="selectedSpan"
      :events="detail?.events || []"
      :artifact-refs="detail?.artifact_refs || []"
      :links="detail?.links || []"
      :current-seconds="running ? currentSeconds : null"
    />

    <v-dialog v-model="deleteDialog" max-width="520">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ tm('confirm.deleteTitle') }}</v-card-title>
        <v-card-text>{{ tm('confirm.deleteText') }}</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="deleting" @click="deleteDialog = false">{{ tm('actions.cancel') }}</v-btn>
          <v-btn color="error" variant="tonal" :loading="deleting" @click="deleteTrace">{{ tm('actions.confirm') }}</v-btn>
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

.trace-detail-page-header {
  align-items: flex-start;
}

.back-button {
  margin: -6px 0 4px -8px;
}

.trace-id {
  max-width: 880px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.trace-detail-stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}

.trace-detail-stat {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 16px;
}

.trace-detail-stat > span {
  color: var(--dashboard-muted);
  font-size: 12px;
  font-weight: 600;
}

.trace-detail-stat > strong {
  overflow: hidden;
  font-size: 24px;
  line-height: 1.1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-detail-stat > small {
  overflow: hidden;
  color: var(--dashboard-subtle);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-detail-stat.degraded {
  border-color: rgba(var(--v-theme-warning), 0.45);
  background: rgba(var(--v-theme-warning), 0.07);
}

.trace-detail-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 20px;
  align-items: start;
}

.waterfall-card {
  min-width: 0;
  overflow: hidden;
}

.waterfall-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px 16px;
  border-bottom: 1px solid var(--dashboard-border);
}

.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: rgb(var(--v-theme-info));
  font-size: 12px;
  white-space: nowrap;
}

.live-indicator > span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  animation: live-pulse 1.5s ease-in-out infinite;
}

@keyframes live-pulse {
  50% {
    opacity: 0.3;
    transform: scale(0.8);
  }
}

.trace-detail-side {
  display: grid;
  gap: 20px;
}

.trace-info-list {
  display: grid;
  gap: 0;
  margin: 14px 0 0;
}

.trace-info-list > div {
  display: grid;
  gap: 4px;
  padding: 10px 0;
  border-bottom: 1px solid var(--dashboard-border);
}

.trace-info-list > div:last-child {
  border-bottom: 0;
}

.trace-info-list dt {
  color: var(--dashboard-muted);
  font-size: 11px;
}

.trace-info-list dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
}

.degradation-card {
  border-color: rgba(var(--v-theme-warning), 0.35);
}

.degradation-reasons {
  display: flex;
  gap: 8px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.trace-json-block {
  max-height: 340px;
  margin: 14px 0 0;
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

.trace-empty {
  display: grid;
  min-height: 220px;
  place-items: center;
  padding: 24px;
  color: var(--dashboard-muted);
  text-align: center;
}

@media (max-width: 1420px) {
  .trace-detail-layout {
    grid-template-columns: 1fr;
  }

  .trace-detail-side {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .trace-detail-stats {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .trace-detail-stats,
  .trace-detail-side {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .waterfall-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}

@media (max-width: 480px) {
  .trace-detail-stats,
  .trace-detail-side {
    grid-template-columns: 1fr;
  }
}
</style>
