<script setup lang="ts">
import { computed } from 'vue';

import type {
  ExecutionTraceArtifactRef,
  ExecutionTraceEvent,
  ExecutionTraceLink,
  ExecutionTraceSpan,
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
                    <dd>{{ artifactRef.artifact_status || '–' }}</dd>
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
