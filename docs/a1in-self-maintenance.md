# A1in AstrBot Core 自维护说明

本仓库是 A1in 自维护的 AstrBot Core fork，不向上游提交 PR。`origin` (`A1in-SY/AstrBot`) 是唯一允许推送的仓库；`upstream` (`AstrBotDevs/AstrBot`) 仅用于读取官方 release tag 和比较差异。`master` 是上游观察/同步线，不承载自定义功能，也不作为生产镜像的浮动来源。

## 当前自维护版本线

本次补丁的来源和发布关系如下：

```text
官方基线 v4.26.8
       +
逐轮 LLM lifecycle hooks
       =
A1in 发布 a1in-v4.26.8.1
```

发布时保留所有官方历史 tags，并用自有带注释 tag 固化每个自维护版本。临时功能分支仅用于开发；自有 tag 已推送并验证后，可以删除该临时分支，使常态分支只保留 `master`。

## 镜像发布规则

`.github/workflows/a1in-release-image.yml` 只在推送格式为 `a1in-v<上游版本>.<自维护修订号>` 的 tag 时运行。例如：

| 用途 | 命名 |
| --- | --- |
| Git release tag | `a1in-v4.26.8.1` |
| 人可读 GHCR tag | `ghcr.io/a1in-sy/astrbot:v4.26.8-a1in.1` |
| 不可变 GHCR tag | `ghcr.io/a1in-sy/astrbot:sha-<12 位 commit SHA>` |

工作流会构建 dashboard 并为镜像写入 OCI labels：自有 revision、自有 release tag 和官方基线 tag。它绝不会构建或发布 `latest`。

生产部署必须固定到已验证的 image digest，而不是人可读 tag。例如：

```text
ghcr.io/a1in-sy/astrbot@sha256:<verified-digest>
```

每次切换 digest 前保留当前 digest；回滚时直接改回前一个已验证 digest。

## 发布步骤

1. 从一个官方稳定 tag 创建临时功能分支，禁止从未发布的 `master` 提交开始。
2. 完成代码、定向测试、全量 Core 测试、插件兼容测试和容器冒烟测试。
3. 提交后创建带注释的自有 tag，例如 `a1in-v4.26.8.1`。
4. 仅推送这个 tag，由自有 GHCR 工作流构建两种镜像 tag。
5. 记录实际 image digest，并用该 digest 更新部署配置；验证成功后再删除临时分支。

## 升级到新的官方版本

只在官方发布新的稳定 tag 时升级，绝不把官方 `master` 的未发布提交直接合入自维护生产线。

1. 比较旧官方 tag 与新官方 tag，重点审查 `BaseAgentRunHooks`、`ToolLoopAgentRunner`、上下文压缩和 Provider API 的变化。
2. 从新官方 tag 创建新的临时功能分支。
3. 将自维护 hook 提交 cherry-pick 到该分支，解决冲突并重新验证。
4. 发布新的自有 tag 和对应 GHCR image digest，例如 `a1in-v4.26.9.1` 与 `v4.26.9-a1in.1`。
5. 保留旧自有 tag 和旧 digest，作为可立即回滚的版本。

插件若依赖逐轮 LLM hooks，必须先检查 `AGENT_LLM_HOOKS_API_VERSION >= 1`。不能仅凭 AstrBot 版本号判断能力存在，因为官方 `v4.26.8` 不包含该自维护 API。
