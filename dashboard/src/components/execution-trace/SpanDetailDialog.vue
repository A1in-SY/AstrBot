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

type DiagnosticEntry = { key: string; label: string; value: string };
type DiagnosticGroup = { key: string; label: string; entries: DiagnosticEntry[] };

const OUTBOUND_ROUTE_FIELDS = [
  'api_family', 'sdk_operation', 'http_method', 'base_url', 'resource_path',
  'route_resolution', 'streaming', 'timeout_seconds', 'proxy_configured',
] as const;
const OUTBOUND_PARAMETER_FIELDS = [
  'provider_id', 'provider_type', 'requested_model', 'effective_model', 'model',
  'temperature', 'top_p', 'top_k', 'seed', 'presence_penalty', 'frequency_penalty',
  'stop_count', 'response_format', 'modalities', 'tool_count', 'tool_choice',
  'tool_choice_name', 'parallel_tool_calls', 'token_limit_field', 'token_limit_value',
  'reasoning_effort', 'thinking_type', 'thinking_budget_tokens', 'thinking_budget',
  'thinking_level', 'store', 'dimensions', 'encoding_format', 'language', 'voice',
  'speed', 'sample_rate', 'top_n', 'return_documents',
] as const;
const OUTBOUND_ATTEMPT_FIELDS = [
  'request_variant_count', 'attempt_count', 'retry_count', 'recovery_count',
  'parameter_transformation_count', 'ignored_parameter_count',
] as const;
const OUTBOUND_RESPONSE_FIELDS = [
  'status_code', 'remote_request_id', 'transport_metadata_available',
  'time_to_first_chunk_ms', 'response_chunk_count', 'finish_reason', 'response_id_hash',
  'usage_input_tokens', 'usage_input_cached_tokens', 'usage_input_other_tokens',
  'usage_output_tokens', 'usage_total_tokens', 'result_chars', 'audio_bytes',
  'audio_chunk_count', 'audio_duration_seconds', 'time_to_first_frame_ms',
  'recognized_language', 'server_duration_seconds',
  'vector_count', 'embedding_dimensions', 'result_count', 'score_min', 'score_max',
  'search_result_count', 'sse_event_count', 'remote_run_id_hash', 'partial',
] as const;

const DOMAIN_FIELDS: Record<string, readonly string[]> = {
  agent: [
    'runner', 'capture_scope', 'initial_message_count', 'available_tool_count',
    'tool_timeout_seconds', 'max_steps', 'step_count', 'model_call_count',
    'tool_call_count', 'final_message_count', 'forced_final', 'aborted',
    'messages_before', 'messages_after', 'context_tokens_before',
    'context_tokens_after', 'context_tokens_estimate', 'yield_count',
    'termination_reason', 'trigger_reason', 'compressor', 'tokens_before',
    'tokens_after', 'retained_message_count', 'summarized_message_count',
    'dropped_message_count', 'compression_model_call_count',
    'fallback_truncation', 'retained_token_ratio',
    'external_agent', 'external_api_mode', 'remote_resource_id_hash', 'plan_mode',
    'subagent_enabled',
  ],
  tool: [
    'tool_name', 'tool_class', 'execution_mode', 'tool_timeout_seconds',
    'executor_yield_count', 'result_kind', 'result_block_count', 'result_block_types',
    'agent_visible_result_count', 'background_submission',
  ],
  mcp: [
    'mcp_server_name', 'mcp_transport', 'mcp_remote_host', 'mcp_resource_path',
    'mcp_protocol_method', 'mcp_connection_ready', 'reconnect_count', 'mcp_is_error',
    'mcp_error_code',
  ],
  pipeline: [
    'message_type', 'component_count', 'component_type_counts', 'has_reply',
    'has_media', 'activated_handler_count', 'stopped', 'result_type',
    'input_candidate_count', 'provider_call_count', 'pipeline_retry_count',
    'converted_count', 'fallback_to_text_count', 'final_mode',
    'audio_bytes', 'audio_chunk_count', 'time_to_first_frame_ms',
  ],
  delivery: [
    'event_class', 'adapter_method', 'platform_id', 'platform_name', 'streaming',
    'fallback_requested', 'fallback_used', 'component_count', 'component_types',
    'semantic_chunk_count', 'time_to_first_delivery_chunk_ms', 'return_type',
    'platform_message_id_hash', 'error_category',
  ],
  history: [
    'trigger_source', 'conversation_id_hash', 'pending_message_count',
    'role_distribution', 'checkpoint_present', 'checkpoint_count',
    'token_usage_present', 'write_performed', 'write_result', 'skip_reason',
  ],
  plugin: [
    'plugin_name', 'plugin_version', 'handler_full_name', 'event_type', 'priority',
    'invocation_index', 'yield_count', 'result_mutation', 'termination_category',
    'exception_type',
  ],
  skill: [
    'skill_name', 'skill_source_type', 'skill_source', 'skill_runtime',
    'skill_content_bytes', 'skill_line_count', 'skill_reference_count',
    'skill_asset_count', 'skill_load_status', 'skill_error_category',
  ],
  background: [
    'task_id', 'tool_name', 'background_kind', 'queue_delay_ms', 'worker_state',
    'worker_outcome', 'history_persistence_state', 'result_delivery_state',
  ],
};

function diagnosticValue(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return tm('diagnostics.unavailable');
  }
  if (typeof value === 'object') {
    return safeExecutionTraceJson(value);
  }
  return String(value);
}

function diagnosticEntries(fields: readonly string[], includeMissing = false): DiagnosticEntry[] {
  const attributes = props.span?.attributes || {};
  return fields
    .filter((key) => includeMissing || Object.prototype.hasOwnProperty.call(attributes, key))
    .map((key) => ({
      key,
      label: diagnosticFieldLabel(key),
      value: diagnosticValue(attributes[key]),
    }));
}

function diagnosticFieldLabel(key: string): string {
  const translated = tm(`diagnostics.fields.${key}`);
  return translated.startsWith('[MISSING:') ? key : translated;
}

function domainGroupKey(operation: string): string | null {
  if (operation === 'tool.background.run') return 'background';
  if (operation === 'conversation.history.persist') return 'history';
  if (operation === 'message.send' || operation === 'response.deliver') return 'delivery';
  if (operation === 'mcp.tool.call') return 'mcp';
  if (operation === 'skill.load') return 'skill';
  if (operation === 'plugin.handler' || operation === 'plugin.hook') return 'plugin';
  if (operation.startsWith('agent.')) return 'agent';
  if (operation === 'tool.call') return 'tool';
  if (operation === 'message.process' || operation.endsWith('.pipeline')) return 'pipeline';
  return null;
}

const diagnosticGroups = computed<DiagnosticGroup[]>(() => {
  if (!props.span) return [];
  const groups: DiagnosticGroup[] = [];
  const hasOutbound = Object.prototype.hasOwnProperty.call(props.span.attributes || {}, 'api_family');
  const routeEntries = diagnosticEntries(OUTBOUND_ROUTE_FIELDS, hasOutbound);
  const parameterEntries = diagnosticEntries(OUTBOUND_PARAMETER_FIELDS);
  const attemptEntries = diagnosticEntries(OUTBOUND_ATTEMPT_FIELDS, hasOutbound);
  const responseEntries = diagnosticEntries(OUTBOUND_RESPONSE_FIELDS, hasOutbound);
  for (const [key, entries] of [
    ['route', routeEntries],
    ['parameters', parameterEntries],
    ['attempts', attemptEntries],
    ['response', responseEntries],
  ] as const) {
    if (entries.length) groups.push({ key, label: tm(`diagnostics.groups.${key}`), entries });
  }
  const domainKey = domainGroupKey(props.span.operation);
  if (domainKey) {
    const entries = diagnosticEntries(DOMAIN_FIELDS[domainKey] || []);
    if (entries.length) {
      groups.unshift({ key: domainKey, label: tm(`diagnostics.groups.${domainKey}`), entries });
    }
  }
  return groups;
});

const outboundTimeline = computed(() =>
  spanEvents.value.filter((event) => event.name.startsWith('outbound.')),
);

function eventRelativeTime(event: ExecutionTraceEvent): string {
  if (!props.span) return '–';
  const elapsedMs = Math.max(0, (Number(event.occurred_at) - Number(props.span.started_at)) * 1000);
  return `+${elapsedMs.toFixed(elapsedMs < 10 ? 2 : 0)} ms`;
}

function outboundEventLabel(name: string): string {
  const key = name.split('.').join('_');
  const translated = tm(`diagnostics.events.${key}`);
  return translated.startsWith('[MISSING:') ? name : translated;
}

function artifactVariantLabel(artifactRef: ExecutionTraceArtifactRef): string | null {
  if (artifactRef.role !== 'outbound.effective_request') return null;
  const metadata = artifactRef.metadata || {};
  const variant = String(metadata.variant_index ?? '–');
  const schema = String(metadata.schema_version ?? '–');
  const sanitized = metadata.sanitized === true
    ? tm('diagnostics.sanitized')
    : tm('diagnostics.sanitizationUnknown');
  return tm('diagnostics.variant', { variant, schema, sanitized });
}

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

          <section v-if="diagnosticGroups.length" class="detail-section diagnostics-section">
            <h3>{{ tm('diagnostics.title') }}</h3>
            <div class="diagnostic-groups">
              <article v-for="group in diagnosticGroups" :key="group.key" class="diagnostic-card">
                <h4>{{ group.label }}</h4>
                <dl class="diagnostic-grid">
                  <div v-for="entry in group.entries" :key="entry.key">
                    <dt>{{ entry.label }}</dt>
                    <dd :class="{ mono: entry.key.includes('url') || entry.key.includes('path') }">
                      {{ entry.value }}
                    </dd>
                  </div>
                </dl>
              </article>
            </div>
          </section>

          <section v-if="outboundTimeline.length" class="detail-section">
            <h3>{{ tm('diagnostics.timeline') }} <span>{{ outboundTimeline.length }}</span></h3>
            <ol class="attempt-timeline">
              <li
                v-for="event in outboundTimeline"
                :key="`${event.span_id}-${event.event_index}`"
                :class="`is-${event.name.split('.').slice(-1)[0] || 'event'}`"
              >
                <span class="timeline-dot" />
                <div class="timeline-content">
                  <div class="record-head">
                    <strong>{{ outboundEventLabel(event.name) }}</strong>
                    <span>{{ eventRelativeTime(event) }}</span>
                  </div>
                  <dl v-if="Object.keys(event.attributes || {}).length" class="timeline-attributes">
                    <div v-for="(value, key) in event.attributes" :key="key">
                      <dt>{{ diagnosticFieldLabel(String(key)) }}</dt>
                      <dd>{{ diagnosticValue(value) }}</dd>
                    </div>
                  </dl>
                </div>
              </li>
            </ol>
          </section>

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
                  <div class="artifact-title">
                    <strong>{{ artifactRef.role }}</strong>
                    <small v-if="artifactVariantLabel(artifactRef)">
                      {{ artifactVariantLabel(artifactRef) }}
                    </small>
                  </div>
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

.diagnostic-groups {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.diagnostic-card {
  min-width: 0;
  padding: 13px 14px;
  border: 1px solid var(--dashboard-border);
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(var(--v-theme-primary), 0.04), transparent 55%);
}

.diagnostic-card h4 {
  margin: 0 0 11px;
  color: rgb(var(--v-theme-primary));
  font-size: 12px;
  letter-spacing: 0.03em;
}

.diagnostic-grid {
  display: grid;
  gap: 8px;
  margin: 0;
}

.diagnostic-grid > div {
  display: grid;
  grid-template-columns: minmax(100px, 0.72fr) minmax(0, 1.28fr);
  gap: 10px;
}

.diagnostic-grid dt,
.timeline-attributes dt {
  color: var(--dashboard-muted);
  font-size: 11px;
}

.diagnostic-grid dd,
.timeline-attributes dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
  white-space: pre-wrap;
}

.attempt-timeline {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.attempt-timeline li {
  position: relative;
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr);
  gap: 8px;
  min-height: 48px;
}

.attempt-timeline li:not(:last-child)::before {
  position: absolute;
  top: 18px;
  bottom: -2px;
  left: 6px;
  width: 1px;
  background: var(--dashboard-border);
  content: '';
}

.timeline-dot {
  z-index: 1;
  width: 13px;
  height: 13px;
  margin-top: 3px;
  border: 3px solid rgb(var(--v-theme-surface));
  border-radius: 50%;
  background: rgb(var(--v-theme-primary));
  box-shadow: 0 0 0 1px var(--dashboard-border);
}

.attempt-timeline .is-retry .timeline-dot,
.attempt-timeline .is-recovered .timeline-dot {
  background: rgb(var(--v-theme-warning));
}

.attempt-timeline .is-failed .timeline-dot {
  background: rgb(var(--v-theme-error));
}

.attempt-timeline .is-completed .timeline-dot {
  background: rgb(var(--v-theme-success));
}

.timeline-content {
  min-width: 0;
  padding: 0 0 14px;
}

.timeline-attributes {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 14px;
  margin: 6px 0 0;
}

.timeline-attributes > div {
  display: flex;
  gap: 5px;
}

.artifact-title {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.artifact-title small {
  color: var(--dashboard-muted);
  font-size: 11px;
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
  .reference-grid,
  .diagnostic-groups {
    grid-template-columns: 1fr;
  }

  .diagnostic-grid > div {
    grid-template-columns: 1fr;
    gap: 2px;
  }

  .record-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 5px;
  }
}
</style>
