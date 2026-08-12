<template>
  <div ref="detailRoot" class="script-task-detail">
    <section>
      <div class="script-detail-heading">
        <div class="script-detail-title-group">
          <span class="text-subtitle-1 font-weight-medium">
            {{ tm("editor.source") }}
          </span>
          <v-chip size="small" variant="tonal" color="info">
            {{ languageDisplayName }}
          </v-chip>
        </div>
        <v-btn
          variant="text"
          size="small"
          prepend-icon="mdi-content-copy"
          :disabled="!source"
          @click="copySource"
        >
          {{ tm("editor.copySource") }}
        </v-btn>
      </div>
      <p class="script-readonly-hint">
        {{ tm("editor.sourceReadonly") }}
      </p>
      <div class="script-source-viewer">
        <div
          v-if="highlightedSource"
          class="script-source-highlighted"
          v-html="highlightedSource"
        />
        <pre v-else class="script-source-fallback">{{
          source || tm("editor.sourceUnavailable")
        }}</pre>
      </div>
    </section>

    <v-card variant="tonal" class="mt-4 state-card">
      <v-card-title class="text-subtitle-1 d-flex align-center">
        <span>{{ tm("editor.state") }}</span>
        <v-spacer />
        <v-btn
          variant="text"
          color="error"
          size="small"
          :loading="resetting"
          :disabled="resetting"
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
import { computed, onMounted, ref, shallowRef } from "vue";
import { cronApi } from "@/api/v1";
import { useModuleI18n } from "@/i18n/composables";
import type {
  CronApiErrorEnvelope,
  ScriptCronJobDetail,
  ScriptLanguageRegistry,
} from "@/types/cron";
import { copyToClipboard } from "@/utils/clipboard";
import { askForConfirmation, useConfirmDialog } from "@/utils/confirmDialog";
import { ensureShikiLanguages, renderShikiCode } from "@/utils/shiki";

const props = defineProps<{
  detail: ScriptCronJobDetail;
  languages?: ScriptLanguageRegistry | null;
  isDark?: boolean;
}>();

const emit = defineEmits<{
  (e: "state-reset", value: ScriptCronJobDetail): void;
}>();

const { tm } = useModuleI18n("features/cron");
const confirmDialog = useConfirmDialog();
const detailRoot = ref<HTMLElement | null>(null);
const shikiHighlighter = shallowRef<Awaited<
  ReturnType<typeof ensureShikiLanguages>
> | null>(null);
const shikiReady = ref(false);
const resetting = ref(false);
const snackbar = ref({ show: false, message: "", color: "success" });

const source = computed(() => props.detail.script.source || "");
const languageVersion = computed(
  () => props.detail.script.language_version || "",
);
const sourceLanguage = computed(() =>
  languageVersion.value.startsWith("astrbot-python-subset/")
    ? "python"
    : "text",
);
const languageDisplayName = computed(() => {
  const match = props.languages?.versions?.find(
    (item) => item.language_version === languageVersion.value,
  );
  return (
    match?.display_name || languageVersion.value || tm("editor.languageUnknown")
  );
});
const highlightedSource = computed(() => {
  if (!shikiReady.value || !shikiHighlighter.value || !source.value) {
    return "";
  }
  try {
    return renderShikiCode(
      shikiHighlighter.value,
      source.value,
      sourceLanguage.value,
      props.isDark ? "dark" : "light",
    );
  } catch (error) {
    console.warn("Failed to highlight script task source.", error);
    return "";
  }
});
const stateText = computed(() => {
  try {
    return JSON.stringify(props.detail.script.state ?? {}, null, 2);
  } catch {
    return String(props.detail.script.state ?? {});
  }
});

function toast(message: string, color: "success" | "error") {
  snackbar.value = { show: true, message, color };
}

async function copySource() {
  const copied = await copyToClipboard(source.value, {
    container: detailRoot.value,
  });
  toast(
    tm(copied ? "messages.sourceCopySuccess" : "messages.sourceCopyFailed"),
    copied ? "success" : "error",
  );
}

async function confirmResetState() {
  const confirmed = await askForConfirmation(
    tm("editor.confirmResetState"),
    confirmDialog,
  );
  if (!confirmed) return;
  resetting.value = true;
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
  }
}

onMounted(async () => {
  try {
    shikiHighlighter.value = await ensureShikiLanguages();
    shikiReady.value = true;
  } catch (error) {
    console.warn(
      "Failed to initialize script task syntax highlighting.",
      error,
    );
  }
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
.script-detail-heading,
.script-detail-title-group {
  display: flex;
  align-items: center;
  gap: 10px;
}

.script-detail-heading {
  justify-content: space-between;
}

.script-detail-title-group {
  min-width: 0;
  flex-wrap: wrap;
}

.script-readonly-hint {
  margin: 4px 0 10px;
  color: rgba(var(--v-theme-on-surface), 0.64);
  font-size: 12px;
}

.script-source-viewer {
  max-height: 360px;
  overflow: auto;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.18);
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.script-source-highlighted {
  min-width: max-content;
}

.script-source-highlighted :deep(pre.shiki) {
  min-width: max-content;
  margin: 0;
  padding: 16px;
  border-radius: 0;
}

.script-source-highlighted :deep(code),
.script-source-fallback {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  white-space: pre;
}

.script-source-fallback {
  min-width: max-content;
  margin: 0;
  padding: 16px;
  color: rgb(var(--v-theme-on-surface));
  font-size: 13px;
  line-height: 1.55;
}

.state-card {
  background: rgba(var(--v-theme-on-surface), 0.05);
}

.state-json {
  max-height: 240px;
  margin: 0;
  overflow: auto;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
