<template>
  <div class="script-task-editor">
    <div class="script-section-heading">
      <span class="text-subtitle-1 font-weight-medium">
        {{ tm("editor.source") }}
      </span>
    </div>

    <v-select
      v-model="languageVersion"
      :items="languageVersions"
      item-title="display_name"
      item-value="language_version"
      :label="tm('editor.language')"
      variant="outlined"
      density="comfortable"
      hide-details
      class="mb-3"
    />

    <div class="editor-shell">
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
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import VueMonacoEditor from "@guolao/vue-monaco-editor";
import type { editor } from "monaco-editor";
import { cronApi } from "@/api/v1";
import { useModuleI18n } from "@/i18n/composables";
import type {
  CronApiErrorEnvelope,
  ScriptDiagnostic,
  ScriptLanguageRegistry,
  ScriptSourceDraft,
  ScriptValidationResult,
} from "@/types/cron";
import { DEFAULT_SCRIPT_LANGUAGE_VERSION } from "@/types/cron";

type MonacoApi = typeof import("monaco-editor");

const props = defineProps<{
  modelValue: ScriptSourceDraft;
  languages?: ScriptLanguageRegistry | null;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: ScriptSourceDraft): void;
}>();

const { tm } = useModuleI18n("features/cron");
const snackbar = ref({ show: false, message: "", color: "success" });
const validating = ref(false);
const validation = ref<ScriptValidationResult | null>(null);
const diagnostics = ref<ScriptDiagnostic[]>([]);
const monacoEditor = shallowRef<editor.IStandaloneCodeEditor | null>(null);
const monacoApi = shallowRef<MonacoApi | null>(null);
const markerOwner = "astrbot-script-validator";
let validateGeneration = 0;

const sourceText = computed({
  get: () => props.modelValue.source,
  set: (source: string) => {
    emit("update:modelValue", { ...props.modelValue, source });
  },
});

const languageVersion = computed({
  get: () =>
    props.modelValue.language_version || DEFAULT_SCRIPT_LANGUAGE_VERSION,
  set: (language_version: string | undefined) => {
    if (!language_version) return;
    emit("update:modelValue", { ...props.modelValue, language_version });
  },
});

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

const languageVersions = computed(() => {
  if (props.languages?.versions?.length) {
    return props.languages.versions;
  }
  return [
    {
      language_version: DEFAULT_SCRIPT_LANGUAGE_VERSION,
      display_name: "astrbot-python-subset v1",
    },
  ];
});

function toast(message: string, color: "success" | "error" | "warning") {
  snackbar.value = { show: true, message, color };
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
      languageVersion.value,
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
  const position = {
    lineNumber: first.line || 1,
    column: first.column || 1,
  };
  monacoEditor.value.revealPositionInCenter(position);
  monacoEditor.value.setPosition(position);
  monacoEditor.value.focus();
}

watch(
  () => [sourceText.value, languageVersion.value],
  () => {
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
.script-section-heading {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
}

.editor-shell {
  overflow: hidden;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 8px;
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
  color: #ef5350;
  font-family: monospace;
  white-space: nowrap;
}

.diagnostic-message {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.diagnostic-location {
  color: rgba(128, 128, 128, 0.9);
  font-family: monospace;
}
</style>
