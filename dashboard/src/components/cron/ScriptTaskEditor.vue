<template>
  <div class="script-task-editor">
    <v-row dense>
      <v-col cols="12" md="6">
        <v-text-field
          v-model="local.name"
          :label="tm('form.name')"
          variant="outlined"
          density="comfortable"
          hide-details
        />
      </v-col>
      <v-col cols="12" md="6">
        <v-text-field
          v-model="local.bound_umo"
          :label="tm('editor.boundUmo')"
          variant="outlined"
          density="comfortable"
          hide-details
          placeholder="platform_id:GroupMessage:session_id"
        />
      </v-col>
    </v-row>

    <v-textarea
      v-model="local.note"
      :label="tm('form.note')"
      variant="outlined"
      density="comfortable"
      rows="2"
      hide-details
      class="mt-3"
    />

    <v-row dense class="mt-3">
      <v-col cols="12" md="4">
        <v-select
          v-model="local.language_version"
          :items="languageVersions"
          item-title="display_name"
          item-value="language_version"
          :label="tm('editor.language')"
          variant="outlined"
          density="comfortable"
          hide-details
        />
      </v-col>
      <v-col cols="12" md="4">
        <v-text-field
          v-model="local.cron_expression"
          :label="tm('form.cron')"
          :placeholder="tm('form.cronPlaceholder')"
          variant="outlined"
          density="comfortable"
          hide-details
          :disabled="local.run_once"
        />
      </v-col>
      <v-col cols="12" md="4">
        <v-switch
          v-model="local.run_once"
          :label="tm('editor.runOnce')"
          density="compact"
          hide-details
          class="mt-1"
        />
      </v-col>
    </v-row>

    <v-row dense class="mt-2">
      <v-col cols="12" md="6">
        <v-text-field
          v-if="local.run_once"
          v-model="local.run_at"
          :label="tm('form.runAt')"
          type="datetime-local"
          variant="outlined"
          density="comfortable"
          hide-details
        />
      </v-col>
      <v-col cols="12" md="6">
        <v-text-field
          v-model="local.timezone"
          :label="tm('form.timezone')"
          variant="outlined"
          density="comfortable"
          hide-details
          placeholder="Asia/Shanghai"
        />
      </v-col>
      <v-col cols="12" md="6">
        <v-switch
          v-model="local.enabled"
          :label="tm('form.enabled')"
          density="compact"
          hide-details
          class="mt-1"
        />
      </v-col>
    </v-row>

    <div class="editor-shell mt-3">
      <VueMonacoEditor
        v-model:value="sourceText"
        language="python"
        theme="vs-dark"
        :options="editorOptions"
        class="script-monaco"
        @mount="onEditorMount"
      />
      <div class="editor-toolbar">
        <v-btn
          variant="tonal"
          color="primary"
          size="small"
          :loading="validating"
          prepend-icon="mdi-check-decagram-outline"
          @click="validateSource"
        >
          {{ tm("editor.validate") }}
        </v-btn>
        <v-chip
          v-if="validation"
          size="small"
          :color="validation.valid ? 'success' : 'error'"
          variant="tonal"
        >
          {{ validation.valid ? tm("editor.valid") : invalidLabel }}
        </v-chip>
      </div>
      <div v-if="diagnostics.length" class="diagnostics-panel">
        <div
          v-for="(diagnostic, index) in diagnostics"
          :key="index"
          class="diagnostic-row"
          @click="jumpToDiagnostic(diagnostic)"
        >
          <v-icon size="small" color="error" class="mr-1">
            mdi-alert-circle-outline
          </v-icon>
          <span class="diagnostic-code">{{ diagnostic.code }}</span>
          <span class="diagnostic-message">{{ diagnostic.message }}</span>
          <span
            v-if="diagnostic.occurrences?.length"
            class="diagnostic-location"
          >
            {{ diagnostic.occurrences[0].line }}:{{
              diagnostic.occurrences[0].column
            }}
          </span>
        </div>
      </div>
    </div>

    <v-card v-if="stateText" variant="tonal" class="mt-3 state-card">
      <v-card-title class="text-subtitle-1 d-flex align-center">
        <span>{{ tm("editor.state") }}</span>
        <v-spacer />
        <v-btn
          variant="text"
          color="error"
          size="small"
          :loading="resetting"
          :disabled="stateResettingDisabled"
          @click="confirmResetState"
        >
          {{ tm("editor.resetState") }}
        </v-btn>
      </v-card-title>
      <v-card-text>
        <pre class="state-json">{{ stateText }}</pre>
      </v-card-text>
    </v-card>

    <v-snackbar
      v-model="snackbar.show"
      :color="snackbar.color"
      location="top"
      timeout="3000"
    >
      {{ snackbar.message }}
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  reactive,
  ref,
  shallowRef,
  watch,
} from "vue";
import VueMonacoEditor from "@guolao/vue-monaco-editor";
import type { editor } from "monaco-editor";
import { cronApi } from "@/api/v1";
import { useModuleI18n } from "@/i18n/composables";
import type {
  CronApiErrorEnvelope,
  ScriptCronJobDetail,
  ScriptDiagnostic,
  ScriptLanguageRegistry,
  ScriptTaskForm,
  ScriptValidationResult,
} from "@/types/cron";
import {
  createEmptyScriptTaskForm,
  DEFAULT_SCRIPT_LANGUAGE_VERSION,
} from "@/types/cron";

type MonacoApi = typeof import("monaco-editor");

const props = defineProps<{
  modelValue: ScriptTaskForm;
  detail?: ScriptCronJobDetail | null;
  languages?: ScriptLanguageRegistry | null;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: ScriptTaskForm): void;
  (e: "state-reset", value: ScriptCronJobDetail): void;
}>();

const { tm } = useModuleI18n("features/cron");
const snackbar = ref({ show: false, message: "", color: "success" });

const local = reactive<ScriptTaskForm>(createEmptyScriptTaskForm());

const sourceText = ref("");
const validating = ref(false);
const validation = ref<ScriptValidationResult | null>(null);
const diagnostics = ref<ScriptDiagnostic[]>([]);
const resetting = ref(false);
const stateResettingDisabled = ref(false);
const monacoEditor = shallowRef<editor.IStandaloneCodeEditor | null>(null);
const monacoApi = shallowRef<MonacoApi | null>(null);
const markerOwner = "astrbot-script-validator";
let validateGeneration = 0;
let propSyncGeneration = 0;
let syncingFromProps = false;
let lastEmittedValue: ScriptTaskForm | null = null;

const editorOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  lineNumbers: "on" as const,
  scrollBeyondLastLine: false,
  automaticLayout: true,
  tabSize: 4,
};

const invalidLabel = computed(() =>
  tm("editor.invalid", {
    count: String(validation.value?.total_diagnostics ?? 0),
  }),
);

function toast(message: string, color: "success" | "error" | "warning") {
  snackbar.value = { show: true, message, color };
}

const languageVersions = computed(() => {
  const registry = props.languages;
  if (!registry?.versions?.length) {
    return [
      {
        language_version: DEFAULT_SCRIPT_LANGUAGE_VERSION,
        display_name: "astrbot-python-subset v1",
      },
    ];
  }
  return registry.versions;
});

function syncFromProps() {
  const generation = ++propSyncGeneration;
  syncingFromProps = true;
  const value = props.modelValue;
  Object.assign(local, {
    name: value.name ?? "",
    note: value.note ?? "",
    bound_umo: value.bound_umo ?? "",
    cron_expression: value.cron_expression ?? "",
    run_once: Boolean(value.run_once),
    run_at: toDatetimeLocalValue(value.run_at),
    timezone: value.timezone ?? "",
    enabled: value.enabled !== false,
    source: value.source ?? "",
    language_version: value.language_version || DEFAULT_SCRIPT_LANGUAGE_VERSION,
  });
  sourceText.value = local.source;
  validation.value = null;
  diagnostics.value = [];
  clearMarkers();
  void nextTick(() => {
    if (generation === propSyncGeneration) {
      syncingFromProps = false;
    }
  });
}

function toDatetimeLocalValue(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset();
  const localDate = new Date(date.getTime() - offset * 60_000);
  return localDate.toISOString().slice(0, 16);
}

function emitValue() {
  if (syncingFromProps) return;
  const value: ScriptTaskForm = {
    name: local.name,
    note: local.note,
    bound_umo: local.bound_umo,
    cron_expression: local.cron_expression,
    run_once: local.run_once,
    run_at: local.run_at,
    timezone: local.timezone,
    enabled: local.enabled,
    source: sourceText.value,
    language_version: local.language_version,
  };
  lastEmittedValue = value;
  emit("update:modelValue", value);
}

function onEditorMount(
  editorInstance: editor.IStandaloneCodeEditor,
  monaco: MonacoApi,
) {
  monacoEditor.value = editorInstance;
  monacoApi.value = monaco;
}

function clearMarkers() {
  const monaco = monacoApi.value;
  const model = monacoEditor.value?.getModel();
  if (!monaco || !model) return;
  monaco.editor.setModelMarkers(model, markerOwner, []);
}

async function validateSource() {
  validating.value = true;
  const generation = ++validateGeneration;
  try {
    const res = await cronApi.validateScript(
      sourceText.value,
      local.language_version,
    );
    if (generation !== validateGeneration) return;
    if (res.data.status !== "ok") {
      throw new Error(res.data.message || tm("messages.validationFailed"));
    }
    const data = res.data.data;
    validation.value = data;
    diagnostics.value = (data.diagnostics ?? []).slice(0, 50);
    applyMarkers(data.diagnostics ?? []);
  } catch (error: unknown) {
    if (generation !== validateGeneration) return;
    toast(apiErrorMessage(error, tm("messages.validationFailed")), "error");
  } finally {
    if (generation === validateGeneration) {
      validating.value = false;
    }
  }
}

function applyMarkers(items: ScriptDiagnostic[]) {
  const monaco = monacoApi.value;
  const model = monacoEditor.value?.getModel();
  if (!monaco || !model) return;
  const markers: editor.IMarkerData[] = [];
  for (const item of items) {
    for (const occurrence of item.occurrences ?? []) {
      markers.push({
        severity: monaco.MarkerSeverity.Error,
        message: item.message || item.code || "Script validation error",
        startLineNumber: occurrence.line || 1,
        startColumn: occurrence.column || 1,
        endLineNumber: occurrence.end_line || occurrence.line || 1,
        endColumn: occurrence.end_column || (occurrence.column || 1) + 1,
      });
    }
  }
  monaco.editor.setModelMarkers(model, markerOwner, markers);
}

function jumpToDiagnostic(diagnostic: ScriptDiagnostic) {
  const first = diagnostic.occurrences?.[0];
  if (!first || !monacoEditor.value) return;
  const lineNumber = first.line || 1;
  const column = first.column || 1;
  monacoEditor.value.revealPositionInCenter({
    lineNumber,
    column,
  });
  monacoEditor.value.setPosition({
    lineNumber,
    column,
  });
  monacoEditor.value.focus();
}

async function confirmResetState() {
  if (!props.detail?.job_id) return;
  if (!window.confirm(tm("editor.confirmResetState"))) return;
  resetting.value = true;
  stateResettingDisabled.value = true;
  try {
    const res = await cronApi.resetState(props.detail.job_id);
    if (res.data.status !== "ok") {
      throw new Error(res.data.message || tm("messages.stateResetFailed"));
    }
    emit("state-reset", res.data.data);
    toast(tm("messages.stateReset"), "success");
  } catch (error: unknown) {
    toast(apiErrorMessage(error, tm("messages.stateResetFailed")), "error");
  } finally {
    resetting.value = false;
    stateResettingDisabled.value = false;
  }
}

const stateText = computed(() => {
  const state = props.detail?.script?.state;
  if (!state) return "";
  try {
    return JSON.stringify(state, null, 2);
  } catch {
    return String(state);
  }
});

watch(
  () => props.modelValue,
  (value) => {
    if (lastEmittedValue && sameFormValue(value, lastEmittedValue)) {
      lastEmittedValue = null;
      return;
    }
    lastEmittedValue = null;
    syncFromProps();
  },
  { deep: true, immediate: true },
);

watch(
  () => [
    local.name,
    local.note,
    local.bound_umo,
    local.cron_expression,
    local.run_once,
    local.run_at,
    local.timezone,
    local.enabled,
    sourceText.value,
    local.language_version,
  ],
  emitValue,
);

watch(
  () => [sourceText.value, local.language_version],
  () => {
    if (syncingFromProps) return;
    validateGeneration += 1;
    validating.value = false;
    validation.value = null;
    diagnostics.value = [];
    clearMarkers();
  },
);

onBeforeUnmount(() => {
  validateGeneration += 1;
  clearMarkers();
  monacoEditor.value = null;
  monacoApi.value = null;
});

function sameFormValue(left: ScriptTaskForm, right: ScriptTaskForm): boolean {
  return (
    left.name === right.name &&
    left.note === right.note &&
    left.bound_umo === right.bound_umo &&
    left.cron_expression === right.cron_expression &&
    left.run_once === right.run_once &&
    left.run_at === right.run_at &&
    left.timezone === right.timezone &&
    left.enabled === right.enabled &&
    left.source === right.source &&
    left.language_version === right.language_version
  );
}

function apiErrorMessage(error: unknown, fallback: string): string {
  const responseData = (error as { response?: { data?: CronApiErrorEnvelope } })
    .response?.data;
  const message =
    responseData?.message ||
    (error instanceof Error ? error.message : "") ||
    fallback;
  const code = responseData?.data?.code;
  return code ? `${message} (${code})` : message;
}
</script>

<style scoped>
.editor-shell {
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 8px;
  overflow: hidden;
}
.script-monaco {
  height: 320px;
}
.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-top: 1px solid rgba(128, 128, 128, 0.2);
  background: rgba(128, 128, 128, 0.08);
}
.diagnostics-panel {
  max-height: 220px;
  overflow: auto;
  border-top: 1px solid rgba(128, 128, 128, 0.2);
}
.diagnostic-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}
.diagnostic-row:hover {
  background: rgba(128, 128, 128, 0.1);
}
.diagnostic-code {
  font-family: monospace;
  color: #ef5350;
  white-space: nowrap;
}
.diagnostic-message {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.diagnostic-location {
  font-family: monospace;
  color: rgba(128, 128, 128, 0.9);
}
.state-card {
  background: rgba(128, 128, 128, 0.06);
}
.state-json {
  max-height: 240px;
  overflow: auto;
  font-family: monospace;
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
