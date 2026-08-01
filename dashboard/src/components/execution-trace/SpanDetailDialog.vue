<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import {
  executionTraceApi,
  type ExecutionTraceArtifact,
  type ExecutionTraceArtifactRef,
  type ExecutionTraceEvent,
  type ExecutionTraceLink,
  type ExecutionTraceSpan,
} from '@/api/v1';
import { useI18n, useModuleI18n } from '@/i18n/composables';
import {
  executionTraceDuration,
  executionTraceSpanLabel,
  executionTraceSpanLowerBound,
  executionTraceStatusColor,
  formatExecutionTraceBytes,
  formatExecutionTraceDateTime,
  formatExecutionTraceDuration,
  safeExecutionTraceJson,
} from '@/utils/executionTrace';

const props = defineProps<{
  modelValue: boolean;
  span: ExecutionTraceSpan | null;
  events: ExecutionTraceEvent[];
  artifactRefs: ExecutionTraceArtifactRef[];
  links: ExecutionTraceLink[];
  currentSeconds?: number | null;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
}>();

const { locale } = useI18n();
const { tm } = useModuleI18n('features/execution-trace');

const artifactBodies = ref<Record<string, ExecutionTraceArtifact>>({});
const artifactLoading = ref<Record<string, boolean>>({});
const artifactErrors = ref<Record<string, string>>({});
const expandedArtifacts = ref<Record<string, boolean>>({});
let artifactRequestEpoch = 0;

const spanEvents = computed(() =>
  props.span ? props.events.filter((event) => event.span_id === props.span?.span_id) : [],
);
const spanArtifactRefs = computed(() =>
  props.span
    ? props.artifactRefs.filter((artifactRef) => artifactRef.span_id === props.span?.span_id)
    : [],
);
const spanLinks = computed(() =>
  props.span ? props.links.filter((link) => link.span_id === props.span?.span_id) : [],
);
const spanDuration = computed(() =>
  props.span ? executionTraceDuration(props.span, props.currentSeconds ?? null) : null,
);
const hasAttributes = computed(() => Object.keys(props.span?.attributes || {}).length > 0);

watch(
  () => `${props.modelValue}:${props.span?.trace_id || ''}:${props.span?.span_id || ''}`,
  () => {
    artifactRequestEpoch += 1;
    artifactBodies.value = {};
    artifactLoading.value = {};
    artifactErrors.value = {};
    expandedArtifacts.value = {};
  },
);

onBeforeUnmount(() => {
  artifactRequestEpoch += 1;
});

function close() {
  emit('update:modelValue', false);
}

function formatDateTime(value: unknown): string {
  return formatExecutionTraceDateTime(value, locale.value);
}

function formatTarget(link: ExecutionTraceLink): string {
  if (link.target_trace_id && link.target_span_id) {
    return `${link.target_trace_id} / ${link.target_span_id}`;
  }
  return link.target_trace_id || link.target_span_id || '–';
}

function artifactStatus(artifactRef: ExecutionTraceArtifactRef): string {
  return String(
    artifactBodies.value[artifactRef.content_hash]?.metadata?.artifact_status
      || artifactRef.artifact_status
      || '',
  );
}

function artifactRefKey(artifactRef: ExecutionTraceArtifactRef): string {
  return `${artifactRef.span_id}:${artifactRef.ref_index}`;
}

function formatArtifactContent(artifact: ExecutionTraceArtifact): string {
  const mediaType = String(artifact.metadata?.media_type || '').toLowerCase();
  if (mediaType.includes('/json') || mediaType.includes('+json')) {
    try {
      return JSON.stringify(JSON.parse(artifact.content), null, 2);
    } catch {
      return artifact.content;
    }
  }
  return artifact.content;
}

async function loadArtifactContent(artifactRef: ExecutionTraceArtifactRef): Promise<void> {
  const contentHash = artifactRef.content_hash;
  const currentStatus = artifactStatus(artifactRef);
  if (
    artifactBodies.value[contentHash]
    || artifactLoading.value[contentHash]
    || (currentStatus && currentStatus !== 'available')
  ) {
    return;
  }

  const requestEpoch = artifactRequestEpoch;
  artifactLoading.value = { ...artifactLoading.value, [contentHash]: true };
  artifactErrors.value = { ...artifactErrors.value, [contentHash]: '' };
  try {
    const response = await executionTraceApi.artifact(contentHash);
    if (response.data.status !== 'ok') {
      throw new Error(response.data.message || tm('messages.artifactFailed'));
    }
    if (requestEpoch === artifactRequestEpoch) {
      artifactBodies.value = {
        ...artifactBodies.value,
        [contentHash]: response.data.data,
      };
    }
  } catch {
    if (requestEpoch === artifactRequestEpoch) {
      artifactErrors.value = {
        ...artifactErrors.value,
        [contentHash]: tm('messages.artifactFailed'),
      };
    }
  } finally {
    if (requestEpoch === artifactRequestEpoch) {
      artifactLoading.value = { ...artifactLoading.value, [contentHash]: false };
    }
  }
}

function toggleArtifactContent(artifactRef: ExecutionTraceArtifactRef): void {
  const contentHash = artifactRef.content_hash;
  const refKey = artifactRefKey(artifactRef);
  const expanded = !expandedArtifacts.value[refKey];
  expandedArtifacts.value = { ...expandedArtifacts.value, [refKey]: expanded };
  if (expanded) {
    void loadArtifactContent(artifactRef);
  }
}

function retryArtifactContent(artifactRef: ExecutionTraceArtifactRef): void {
  artifactErrors.value = { ...artifactErrors.value, [artifactRef.content_hash]: '' };
  void loadArtifactContent(artifactRef);
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="980"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <v-card class="span-dialog">
      <v-card-title class="dialog-head">
        <div class="dialog-title-wrap">
          <span class="dialog-eyebrow">{{ tm('spanDialog.eyebrow') }}</span>
          <strong>{{ span ? executionTraceSpanLabel(span) : tm('spanDialog.loading') }}</strong>
        </div>
        <v-btn icon="mdi-close" size="small" variant="text" @click="close" />
      </v-card-title>
      <v-divider />
      <v-card-text class="dialog-body">
        <template v-if="span">
          <div class="span-summary">
            <v-chip :color="executionTraceStatusColor(span.status)" size="small" variant="tonal">
              {{ span.status }}
            </v-chip>
            <v-chip size="small" variant="outlined" color="primary">
              {{ span.kind || tm('spanDialog.unknownKind') }}
            </v-chip>
            <v-chip v-if="executionTraceSpanLowerBound(span)" size="small" color="warning" variant="tonal">
              {{ tm('spanDialog.lowerBound') }}
            </v-chip>
            <v-chip v-if="span.degraded" size="small" color="warning" variant="tonal">
              {{ tm('spanDialog.degraded') }}
            </v-chip>
          </div>

          <dl class="detail-grid">
            <div>
              <dt>{{ tm('spanDialog.operation') }}</dt>
              <dd>{{ executionTraceSpanLabel(span) }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.source') }}</dt>
              <dd>{{ span.source || '–' }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.spanId') }}</dt>
              <dd class="mono">{{ span.span_id }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.parentSpan') }}</dt>
              <dd class="mono">{{ span.parent_span_id || tm('spanDialog.root') }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.started') }}</dt>
              <dd>{{ formatDateTime(span.started_at) }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.ended') }}</dt>
              <dd>{{ formatDateTime(span.ended_at) }}</dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.duration') }}</dt>
              <dd class="mono">
                {{ formatExecutionTraceDuration(spanDuration) }}
                <template v-if="executionTraceSpanLowerBound(span)">+</template>
              </dd>
            </div>
            <div>
              <dt>{{ tm('spanDialog.outcome') }}</dt>
              <dd>{{ span.outcome || '–' }}</dd>
            </div>
          </dl>

          <section v-if="hasAttributes" class="detail-section">
            <h3>{{ tm('detail.attributes') }}</h3>
            <pre class="json-block">{{ safeExecutionTraceJson(span.attributes) }}</pre>
          </section>

          <section class="detail-section">
            <h3>{{ tm('detail.events') }} <span>{{ spanEvents.length }}</span></h3>
            <div v-if="spanEvents.length" class="record-list">
              <article
                v-for="event in spanEvents"
                :key="`${event.span_id}-${event.event_index}`"
                class="record-card"
              >
                <div class="record-head">
                  <strong>{{ event.name }}</strong>
                  <span>{{ formatDateTime(event.occurred_at) }}</span>
                </div>
                <details v-if="Object.keys(event.attributes || {}).length" class="record-details">
                  <summary>{{ tm('detail.attributes') }}</summary>
                  <pre class="json-block">{{ safeExecutionTraceJson(event.attributes) }}</pre>
                </details>
              </article>
            </div>
            <p v-else class="empty-copy">{{ tm('detail.noEvents') }}</p>
          </section>

          <section class="detail-section">
            <h3>{{ tm('detail.artifacts') }} <span>{{ spanArtifactRefs.length }}</span></h3>
            <div v-if="spanArtifactRefs.length" class="record-list">
              <article
                v-for="artifactRef in spanArtifactRefs"
                :key="`${artifactRef.span_id}-${artifactRef.ref_index}`"
                class="record-card"
              >
                <div class="record-head">
                  <strong>{{ artifactRef.role }}</strong>
                  <span>{{ artifactRef.media_type || 'application/octet-stream' }}</span>
                </div>
                <dl class="reference-grid">
                  <div>
                    <dt>{{ tm('spanDialog.artifactHash') }}</dt>
                    <dd class="mono">{{ artifactRef.content_hash }}</dd>
                  </div>
                  <div>
                    <dt>{{ tm('spanDialog.capturedSize') }}</dt>
                    <dd>{{ formatExecutionTraceBytes(artifactRef.captured_size ?? artifactRef.logical_size) }}</dd>
                  </div>
                  <div>
                    <dt>{{ tm('spanDialog.artifactStatus') }}</dt>
                    <dd>{{ artifactStatus(artifactRef) || '–' }}</dd>
                  </div>
                  <div>
                    <dt>{{ tm('spanDialog.truncated') }}</dt>
                    <dd>{{ artifactRef.truncated ? tm('spanDialog.yes') : tm('spanDialog.no') }}</dd>
                  </div>
                </dl>
                <details v-if="Object.keys(artifactRef.metadata || {}).length" class="record-details">
                  <summary>{{ tm('spanDialog.metadata') }}</summary>
                  <pre class="json-block">{{ safeExecutionTraceJson(artifactRef.metadata) }}</pre>
                </details>
                <div class="artifact-actions">
                  <v-btn
                    color="primary"
                    size="small"
                    variant="tonal"
                    :loading="artifactLoading[artifactRef.content_hash]"
                    @click="toggleArtifactContent(artifactRef)"
                  >
                    {{ expandedArtifacts[artifactRefKey(artifactRef)]
                      ? tm('artifact.hide')
                      : tm('artifact.show') }}
                  </v-btn>
                </div>
                <div
                  v-if="expandedArtifacts[artifactRefKey(artifactRef)]"
                  class="artifact-content-panel"
                >
                  <v-progress-linear
                    v-if="artifactLoading[artifactRef.content_hash]"
                    color="primary"
                    indeterminate
                  />
                  <v-alert
                    v-else-if="artifactErrors[artifactRef.content_hash]"
                    density="compact"
                    type="error"
                    variant="tonal"
                  >
                    <div class="artifact-error-row">
                      <span>{{ artifactErrors[artifactRef.content_hash] }}</span>
                      <v-btn
                        size="small"
                        variant="text"
                        @click="retryArtifactContent(artifactRef)"
                      >
                        {{ tm('artifact.retry') }}
                      </v-btn>
                    </div>
                  </v-alert>
                  <template v-else-if="artifactBodies[artifactRef.content_hash]">
                    <v-alert
                      v-if="artifactRef.truncated
                        || artifactBodies[artifactRef.content_hash].metadata?.truncated"
                      class="artifact-content-alert"
                      density="compact"
                      type="warning"
                      variant="tonal"
                    >
                      {{ tm('artifact.truncated') }}
                    </v-alert>
                    <p v-if="artifactStatus(artifactRef) !== 'available'" class="empty-copy">
                      {{ tm('artifact.unavailable', { status: artifactStatus(artifactRef) || 'unknown' }) }}
                    </p>
                    <p
                      v-else-if="artifactBodies[artifactRef.content_hash].content.length === 0"
                      class="empty-copy"
                    >
                      {{ tm('artifact.emptyValue') }}
                    </p>
                    <pre
                      v-else
                      class="artifact-content"
                      v-text="formatArtifactContent(artifactBodies[artifactRef.content_hash])"
                    />
                  </template>
                  <p v-else class="empty-copy">
                    {{ artifactStatus(artifactRef) === 'available'
                      ? tm('artifact.empty')
                      : tm('artifact.unavailable', {
                        status: artifactStatus(artifactRef) || 'unknown',
                      }) }}
                  </p>
                </div>
              </article>
            </div>
            <p v-else class="empty-copy">{{ tm('detail.noArtifacts') }}</p>
          </section>

          <section class="detail-section">
            <h3>{{ tm('detail.links') }} <span>{{ spanLinks.length }}</span></h3>
            <div v-if="spanLinks.length" class="record-list">
              <article
                v-for="link in spanLinks"
                :key="`${link.span_id}-${link.link_index}`"
                class="record-card"
              >
                <div class="record-head">
                  <strong>{{ link.relation }}</strong>
                  <code>{{ formatTarget(link) }}</code>
                </div>
                <details v-if="Object.keys(link.attributes || {}).length" class="record-details">
                  <summary>{{ tm('detail.attributes') }}</summary>
                  <pre class="json-block">{{ safeExecutionTraceJson(link.attributes) }}</pre>
                </details>
              </article>
            </div>
            <p v-else class="empty-copy">{{ tm('detail.noLinks') }}</p>
          </section>
        </template>
      </v-card-text>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.span-dialog {
  border: 1px solid var(--dashboard-border);
  border-radius: 18px;
}

.dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px 16px;
}

.dialog-title-wrap {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.dialog-title-wrap strong {
  overflow-wrap: anywhere;
}

.dialog-eyebrow {
  color: var(--dashboard-subtle);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 0.12em;
}

.dialog-body {
  padding: 20px 22px 24px;
}

.span-summary {
  display: flex;
  gap: 8px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}

.detail-grid,
.reference-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
}

.detail-grid > div,
.reference-grid > div {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--dashboard-border);
  border-radius: 10px;
  background: var(--dashboard-soft);
}

.detail-grid dt,
.reference-grid dt {
  color: var(--dashboard-muted);
  font-size: 11px;
}

.detail-grid dd,
.reference-grid dd {
  margin: 5px 0 0;
  overflow-wrap: anywhere;
  font-size: 13px;
}

.detail-section {
  margin-top: 22px;
}

.detail-section h3 {
  margin: 0 0 10px;
  font-size: 15px;
}

.detail-section h3 span {
  color: var(--dashboard-subtle);
  font-size: 12px;
  font-weight: 500;
}

.record-list {
  display: grid;
  gap: 10px;
}

.record-card {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--dashboard-border);
  border-radius: 10px;
}

.record-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  font-size: 13px;
}

.record-head strong,
.record-head code {
  min-width: 0;
  overflow-wrap: anywhere;
}

.record-head span {
  flex: 0 0 auto;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.reference-grid {
  margin-top: 10px;
  gap: 8px;
}

.reference-grid > div {
  padding: 9px 10px;
  background: rgba(var(--v-theme-on-surface), 0.025);
}

.record-details {
  margin-top: 10px;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.record-details summary {
  cursor: pointer;
}

.artifact-actions {
  display: flex;
  margin-top: 12px;
}

.artifact-content-panel {
  margin-top: 10px;
  padding: 12px;
  overflow: hidden;
  border: 1px solid var(--dashboard-border);
  border-radius: 10px;
  background: rgba(var(--v-theme-on-surface), 0.025);
}

.artifact-content-alert {
  margin-bottom: 10px;
}

.artifact-error-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.artifact-content {
  max-height: 460px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.04);
  color: var(--dashboard-text);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.json-block {
  max-height: 320px;
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

.empty-copy {
  margin: 0;
  color: var(--dashboard-muted);
  font-size: 13px;
}

@media (max-width: 640px) {
  .detail-grid,
  .reference-grid {
    grid-template-columns: 1fr;
  }

  .record-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 5px;
  }
}
</style>
