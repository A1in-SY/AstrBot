<script setup lang="ts">
import { computed } from 'vue';

import type { ExecutionTraceSpan } from '@/api/v1';
import { useModuleI18n } from '@/i18n/composables';
import {
  buildExecutionTraceSpanRows,
  executionTraceBarGeometry,
  executionTraceDuration,
  executionTraceSpanCategory,
  executionTraceSpanCategoryColor,
  executionTraceSpanLabel,
  executionTraceSpanLowerBound,
  executionTraceStatusColor,
  executionTraceTimeline,
  formatExecutionTraceDuration,
  isFailedTraceStatus,
  isRunningTraceStatus,
  SPAN_LEGEND_ITEMS,
} from '@/utils/executionTrace';

const props = withDefaults(defineProps<{
  spans: ExecutionTraceSpan[];
  traceStartedAt: number;
  currentSeconds?: number | null;
}>(), {
  currentSeconds: null,
});

const emit = defineEmits<{
  select: [span: ExecutionTraceSpan];
}>();

const { tm } = useModuleI18n('features/execution-trace');

const rows = computed(() => buildExecutionTraceSpanRows(props.spans));
const timeline = computed(() =>
  executionTraceTimeline(props.spans, props.traceStartedAt, props.currentSeconds),
);
const ticks = computed(() =>
  [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    ratio,
    label: formatExecutionTraceDuration(timeline.value.duration * ratio),
  })),
);
const legend = computed(() => {
  const present = new Set(props.spans.map((span) => executionTraceSpanCategory(span)));
  return SPAN_LEGEND_ITEMS
    .filter((item) => present.has(item.key))
    .map((item) => ({
      key: item.key,
      label: tm(item.labelKey),
      color: item.color,
    }));
});

function spanDuration(span: ExecutionTraceSpan): number | null {
  return executionTraceDuration(span, props.currentSeconds);
}

function spanBarStyle(span: ExecutionTraceSpan): Record<string, string> {
  const geometry = executionTraceBarGeometry(span, timeline.value, props.currentSeconds);
  return {
    left: geometry.zeroDuration
      ? `min(${geometry.leftPercent}%, calc(100% - 2px))`
      : `${geometry.leftPercent}%`,
    width: geometry.zeroDuration ? '2px' : `${Math.max(0.2, geometry.widthPercent)}%`,
    backgroundColor: executionTraceSpanCategoryColor(span),
  };
}
</script>

<template>
  <div class="waterfall-shell">
    <div v-if="legend.length" class="waterfall-legend">
      <span v-for="item in legend" :key="item.key" class="legend-item">
        <span class="legend-dot" :style="{ backgroundColor: item.color }" />
        <span>{{ item.label }}</span>
      </span>
      <span class="legend-hint">{{ tm('waterfall.hint') }}</span>
    </div>

    <div class="waterfall-scroll">
      <div class="waterfall-grid">
        <div class="waterfall-head-name">{{ tm('waterfall.span') }}</div>
        <div class="waterfall-axis">
          <span
            v-for="tick in ticks"
            :key="tick.ratio"
            class="axis-tick"
            :style="{ left: `${tick.ratio * 100}%` }"
          >
            {{ tick.label }}
          </span>
        </div>
        <div class="waterfall-head-meta">{{ tm('waterfall.durationStatus') }}</div>

        <template v-for="row in rows" :key="row.span.span_id">
          <button
            type="button"
            class="span-name-cell"
            :style="{ paddingLeft: `${12 + Math.min(row.depth, 6) * 14}px` }"
            @click="emit('select', row.span)"
          >
            <span v-if="row.depth" class="tree-branch">└</span>
            <span
              class="kind-dot"
              :style="{ backgroundColor: executionTraceSpanCategoryColor(row.span) }"
            />
            <span
              class="span-name"
              :title="executionTraceSpanLabel(row.span)"
            >
              {{ executionTraceSpanLabel(row.span) }}
            </span>
            <span
              v-if="row.orphaned"
              class="orphan-mark"
              :title="tm('waterfall.orphanHint')"
            >
              !
            </span>
          </button>

          <button
            type="button"
            class="span-track-cell"
            @click="emit('select', row.span)"
          >
            <span class="span-track" aria-hidden="true">
              <span
                v-for="tick in ticks"
                :key="tick.ratio"
                class="grid-line"
                :style="{ left: `${tick.ratio * 100}%` }"
              />
              <span
                class="span-bar"
                :class="{
                  'is-running': isRunningTraceStatus(row.span.status),
                  'is-lower-bound': executionTraceSpanLowerBound(row.span),
                  'is-failed': isFailedTraceStatus(row.span.status),
                }"
                :style="spanBarStyle(row.span)"
              />
            </span>
            <span class="span-meta" aria-hidden="true">
              <span class="track-duration">
                {{ formatExecutionTraceDuration(spanDuration(row.span)) }}
                <template v-if="executionTraceSpanLowerBound(row.span)">+</template>
              </span>
              <v-chip
                :color="executionTraceStatusColor(row.span.status)"
                size="x-small"
                variant="tonal"
              >
                {{ row.span.status }}
              </v-chip>
            </span>
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.waterfall-shell {
  min-width: 0;
  overflow: hidden;
}

.waterfall-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--dashboard-border);
  color: var(--dashboard-muted);
  font-size: 12px;
  flex-wrap: wrap;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.legend-dot,
.kind-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.legend-hint {
  margin-left: auto;
  color: var(--dashboard-subtle);
}

.waterfall-scroll {
  min-width: 0;
  overflow-x: auto;
}

.waterfall-grid {
  display: grid;
  width: 100%;
  min-width: 680px;
  grid-template-columns: clamp(180px, 22vw, 280px) minmax(280px, 1fr) 118px;
}

.waterfall-head-name,
.waterfall-axis,
.waterfall-head-meta {
  position: sticky;
  top: 0;
  z-index: 2;
  min-height: 42px;
  border-bottom: 1px solid var(--dashboard-border-strong);
  background: var(--dashboard-surface);
}

.waterfall-head-name {
  left: 0;
  z-index: 3;
  display: flex;
  align-items: center;
  padding: 0 14px;
  color: var(--dashboard-muted);
  font-size: 12px;
  font-weight: 700;
}

.waterfall-axis {
  position: relative;
  min-width: 0;
  overflow: hidden;
}

.waterfall-head-meta {
  display: grid;
  place-items: center;
  padding: 0 6px;
  color: var(--dashboard-muted);
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
  text-align: center;
}

.axis-tick {
  position: absolute;
  bottom: 10px;
  color: var(--dashboard-subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
  font-size: 10px;
  transform: translateX(-50%);
  white-space: nowrap;
}

.axis-tick:first-child {
  transform: none;
}

.axis-tick:last-child {
  transform: translateX(-100%);
}

.span-name-cell,
.span-track-cell {
  min-height: 44px;
  border: 0;
  border-bottom: 1px solid var(--dashboard-border);
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.span-name-cell:hover,
.span-track-cell:hover {
  background: var(--dashboard-soft);
}

.span-name-cell:focus-visible,
.span-track-cell:focus-visible {
  z-index: 4;
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: -2px;
}

.span-name-cell {
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
  background: var(--dashboard-surface);
  text-align: left;
}

.tree-branch {
  flex: 0 0 auto;
  color: var(--dashboard-subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.span-name {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.orphan-mark {
  display: inline-grid;
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  place-items: center;
  border-radius: 50%;
  background: rgba(var(--v-theme-warning), 0.14);
  color: rgb(var(--v-theme-warning));
  font-size: 10px;
  font-weight: 800;
}

.span-track {
  position: relative;
  height: 100%;
  min-width: 0;
  overflow: hidden;
  pointer-events: none;
  text-align: left;
}

.span-track-cell {
  display: grid;
  min-width: 0;
  grid-column: 2 / 4;
  grid-template-columns: minmax(0, 1fr) 118px;
  padding: 0;
  text-align: left;
}

.grid-line {
  position: absolute;
  inset: 0 auto 0 0;
  width: 1px;
  background: var(--dashboard-border);
  pointer-events: none;
}

.span-bar {
  position: absolute;
  top: 10px;
  display: flex;
  height: 23px;
  min-width: 2px;
  align-items: center;
  border-radius: 5px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.24);
}

.span-bar.is-running,
.span-bar.is-lower-bound {
  background-image: repeating-linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.18) 0,
    rgba(255, 255, 255, 0.18) 6px,
    transparent 6px,
    transparent 12px
  );
}

.span-bar.is-failed {
  box-shadow: inset 0 0 0 2px rgb(var(--v-theme-error));
}

.span-bar.is-failed::after {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: rgba(var(--v-theme-error), 0.16);
  content: '';
}

.span-meta {
  display: flex;
  height: 100%;
  min-width: 0;
  align-items: flex-end;
  justify-content: center;
  flex-direction: column;
  gap: 2px;
  padding: 4px 8px;
  pointer-events: none;
  text-align: right;
}

.track-duration {
  max-width: 100%;
  overflow: hidden;
  color: var(--dashboard-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 720px) {
  .waterfall-grid {
    min-width: 560px;
    grid-template-columns: 170px minmax(220px, 1fr) 90px;
  }

  .legend-hint {
    width: 100%;
    margin-left: 0;
  }

  .axis-tick:nth-child(2),
  .axis-tick:nth-child(4) {
    display: none;
  }

  .span-track-cell {
    grid-template-columns: minmax(0, 1fr) 90px;
  }
}
</style>
