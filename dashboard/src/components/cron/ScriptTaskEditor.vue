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
          :disabled="isEditing"
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
    </v-row>

    <div class="editor-shell mt-3">
      <VueMonacoEditor
        v-model="sourceText"
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
          {{
            validation.valid ? tm("editor.valid") : invalidLabel
          }}
        </v-chip>
      </div>
      <div v-if="diagnostics.length" class="diagnostics-panel">
        <div
          v-for="(diagnostic, index) in diagnostics"
          :key="index"
          class="diagnostic-row"
          @click="jumpToDiagnostic(diagnostic)"
        >
          <v-icon
            size="small"
            color="error"
            class="mr-1"
          >
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
import { computed, onMounted, reactive, ref, watch } from "vue";
import VueMonacoEditor from "@guolao/vue-monaco-editor";
import type { editor } from "monaco-editor";
import { useI18n } from "vue-i18n";
import { cronApi } from "@/api/v1";

const props = defineProps<{
  modelValue: Record<string, any>;
  detail?: Record<string, any> | null;
  languages?: Record<string, any> | null;
  isEditing?: boolean;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: Record<string, any>): void;
}>();

const { t, tm } = useI18n();
const snackbar = ref({ show: false, message: "", color: "success" });

const local = reactive<Record<string, any>>({
  name: "",
  note: "",
  bound_umo: "",
  cron_expression: "",
  run_once: false,
  run_at: "",
  timezone: "",
  enabled: true,
  source: "",
  language_version: "astrbot-python-subset/v1",
});

const sourceText = ref("");
const validating = ref(false);
const validation = ref<Record<string, any> | null>(null);
const diagnostics = ref<Record<string, any>[]>([]);
const resetting = ref(false);
const stateResettingDisabled = ref(false);
const monacoEditor = ref<editor.IStandaloneCodeEditor | null>(null);
const markerOwner = "astrbot-script-validator";
let validateGeneration = 0;

const editorOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  lineNumbers: "on" as const,
  scrollBeyondLastLine: false,
  automaticLayout: true,
  tabSize: 4,
};

const invalidLabel = computed(() =>
  t("editor.invalid", { count: String(validation.value?.total_diagnostics ?? 0) }),
);

function toast(message: string, color: "success" | "error" | "warning") {
  snackbar.value = { show: true, message, color };
}

const languageVersions = computed(() => {
  const registry = props.languages;
  if (!registry?.versions?.length) {
    return [
      {
        language_version: "astrbot-python-subset/v1",
        display_name: "astrbot-python-subset v1",
      },
    ];
  }
  return registry.versions;
});

function syncFromProps() {
  const value = props.modelValue || {};
  Object.assign(local, {
    name: value.name ?? "",
    note: value.note ?? "",
    bound_umo: value.bound_umo ?? value.session ?? "",
    cron_expression: value.cron_expression ?? "",
    run_once: Boolean(value.run_once),
    run_at: toDatetimeLocalValue(value.run_at),
    timezone: value.timezone ?? "",
    enabled: value.enabled !== false,
    source: value.source ?? "",
    language_version: value.language_version ?? "astrbot-python-subset/v1",
  });
  sourceText.value = local.source;
  validation.value = null;
  diagnostics.value = [];
  clearMarkers();
}

function toDatetimeLocalValue(value: any): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset();
  const localDate = new Date(date.getTime() - offset * 60_000);
  return localDate.toISOString().slice(0, 16);
}

function emitValue() {
  emit("update:modelValue", {
    name: local.name,
    note: local.note,
    bound_umo: local.bound_umo,
    cron_expression: local.run_once ? null : local.cron_expression,
    run_once: local.run_once,
    run_at: local.run_at ? new Date(local.run_at).toISOString() : "",
    timezone: local.timezone,
    enabled: local.enabled,
    source: sourceText.value,
    language_version: local.language_version,
  });
}

function onEditorMount(editorInstance: editor.IStandaloneCodeEditor) {
  monacoEditor.value = editorInstance;
}

function clearMarkers() {
  const monaco = (window as any).monaco;
  if (!monaco) return;
  monaco.editor.setModelMarkers(
    monacoEditor.value?.getModel() ?? monaco.editor.getModels()[0],
    markerOwner,
    [],
  );
}

async function validateSource() {
  validating.value = true;
  const generation = ++validateGeneration;
  try {
    const res: any = await cronApi.validateScript(
      sourceText.value,
      local.language_version,
    );
    if (generation !== validateGeneration) return;
    const data = res.data ?? res;
    validation.value = data;
    diagnostics.value = (data.diagnostics ?? []).slice(0, 50);
    applyMarkers(data.diagnostics ?? []);
  } catch (e: any) {
    if (generation !== validateGeneration) return;
    toast(e?.response?.data?.message || "validation failed", "error");
  } finally {
    if (generation === validateGeneration) {
      validating.value = false;
    }
  }
}

function applyMarkers(items: Record<string, any>[]) {
  const monaco = (window as any).monaco;
  const model = monacoEditor.value?.getModel() ?? monaco.editor?.getModels()[0];
  if (!monaco || !model) return;
  const markers: any[] = [];
  for (const item of items) {
    for (const occurrence of item.occurrences ?? []) {
      markers.push({
        severity: monaco.MarkerSeverity.Error,
        message: item.message,
        startLineNumber: occurrence.line,
        startColumn: occurrence.column,
        endLineNumber: occurrence.end_line ?? occurrence.line,
        endColumn: occurrence.end_column ?? occurrence.column + 1,
      });
    }
  }
  monaco.editor.setModelMarkers(model, markerOwner, markers);
}

function jumpToDiagnostic(diagnostic: Record<string, any>) {
  const first = diagnostic.occurrences?.[0];
  if (!first || !monacoEditor.value) return;
  monacoEditor.value.revealPositionInCenter({
    lineNumber: first.line,
    column: first.column,
  });
  monacoEditor.value.setPosition({
    lineNumber: first.line,
    column: first.column,
  });
  monacoEditor.value.focus();
}

async function confirmResetState() {
  if (!props.detail?.job_id) return;
  if (!window.confirm(tm("editor.confirmResetState"))) return;
  resetting.value = true;
  stateResettingDisabled.value = true;
  try {
    const res: any = await cronApi.resetState(props.detail.job_id);
    const data = res.data ?? res;
    if (data?.script) {
      props.detail!.script = data.script;
    }
    toast(tm("messages.stateReset"), "success");
  } catch (e: any) {
    toast(e?.response?.data?.message || tm("messages.stateResetFailed"), "error");
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
  () => [props.modelValue, props.detail],
  () => syncFromProps(),
  { deep: true, immediate: true },
);

watch(
  () => [sourceText.value, local.language_version],
  () => {
    validation.value = null;
    diagnostics.value = [];
    clearMarkers();
    emitValue();
  },
);

onMounted(() => {
  syncFromProps();
});
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
