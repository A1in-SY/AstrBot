# A1in AstrBot 私有 Fork 维护规范

本仓库是 A1in 长期维护的 AstrBot 私有 fork，不向官方仓库提交 PR。本文定义分支职责、版本语义、上游同步、镜像发布和生产升级规则。

## 1. 基本原则

```text
官方 AstrBot 负责提供稳定基线和公开兼容契约。
A1in fork 负责私有功能、发布、镜像、部署和回滚。
```

`a1in-` 是发行者命名空间，不表示“只有一个小补丁”。任何包含 A1in 自维护代码的正式发行版都必须使用该前缀，避免与官方 AstrBot release 混淆。

`origin` (`A1in-SY/AstrBot`) 是唯一允许推送的仓库。`upstream` (`AstrBotDevs/AstrBot`) 只能用于 fetch 官方 branch、tag 和比较差异；其 push URL 必须保持禁用。

## 2. 分支与 tag 规范

| 引用 | 职责 | 生命周期 |
| --- | --- | --- |
| `master` | A1in 私有开发主干。所有已审查的私有功能和已吸收的官方稳定更新都在这里。 | 长期保留 |
| `upstream/master` | 官方开发动态的只读观察对象。 | 仅 fetch，不合并进生产 release |
| `feature/<topic>` | A1in 私有功能开发。 | 合并到 `master` 后删除 |
| `fix/<topic>` | A1in 缺陷修复。 | 合并到 `master` 后删除 |
| `sync/upstream-vX.Y.Z` | 将一个官方稳定 tag 吸收到私有主干的同步分支。 | 同步 release 后删除 |
| `release/<topic>` | 版本准备、发布阻断修复和最终验证。 | release tag 验证后删除 |
| `a1in-vX.Y.Z.N` | A1in 不可变正式 source release tag。 | 永久保留 |
| `archive/*` | 历史迁移、未部署重打包等例外的可追溯归档 tag。 | 永久保留 |

不要创建一个同时承担“官方镜像”和“私有主干”职责的 branch。官方观察由 `upstream/master` 完成；`origin/master` 是 A1in 的产品开发线。

所有短期 branch 应通过 PR 或等价审查合入 `master`。禁止将私有功能直接提交到 `upstream`，禁止把 `upstream/master` 的未发布提交直接 merge 到生产 release。

## 3. 双版本模型

一个 A1in release 同时包含“官方兼容版本”和“A1in 发行版本”。两者不能互相覆盖。

| 字段 | 位置 | 语义 | `a1in-v4.26.8.2` 示例 |
| --- | --- | --- | --- |
| 官方 Git 基线 | 官方 annotated tag | 吸收上游变更的稳定边界 | `v4.26.8` |
| 上游兼容版本 | `astrbot/__init__.py` 的 `__version__` | 插件、备份、Dashboard 兼容和官方 API 基线 | `4.26.8` |
| Python 包版本 | `pyproject.toml` 的 `[project].version` | 必须与 `__version__` 相同 | `4.26.8` |
| A1in 发行版本 | `astrbot/a1in_release.py` 的 `A1IN_RELEASE` | A1in source release 的唯一源码内身份 | `a1in-v4.26.8.2` |
| A1in 发行修订号 | `A1IN_RELEASE_REVISION` | 同一官方基线内的 A1in release 序号 | `2` |
| 上游基线 tag | `A1IN_UPSTREAM_BASE_TAG` | 由 `__version__` 推导的官方 tag | `v4.26.8` |

更新官方版本时，必须同步更新 `pyproject.toml` 与 `astrbot/__init__.py`。不要把 A1in release string 写进 `__version__`：该字段仍被 Dashboard 下载、插件兼容、备份和版本比较逻辑使用。

### A1in release 命名

```text
Git source tag:       a1in-v<官方主版本>.<官方次版本>.<官方补丁>.<A1in修订号>
Human image tag:      v<官方主版本>.<官方次版本>.<官方补丁>-a1in.<A1in修订号>
Immutable deployment: ghcr.io/a1in-sy/astrbot@sha256:<digest>
```

示例：

| 场景 | Git tag | Human image tag |
| --- | --- | --- |
| 官方 `v4.26.8` 上的第 2 个 A1in release | `a1in-v4.26.8.2` | `v4.26.8-a1in.2` |
| 官方 `v4.26.9` 同步完成后的第一个 A1in release | `a1in-v4.26.9.1` | `v4.26.9-a1in.1` |
| 官方未来 `v4.27.0` 同步完成后的第一个 A1in release | `a1in-v4.27.0.1` | `v4.27.0-a1in.1` |

裸 `v4.27.0` 保留给官方 release 语义，不可用于 A1in 自行发布。已经推送或部署的 A1in tag 和 human image tag 永不重写、永不移动；发现问题时发布下一个修订号。`a1in-v4.26.8.1` 的重打包是“旧版本未部署”的一次性历史例外，archive tag 已保留证据，不得作为后续惯例。

## 4. 运行时身份与 Dashboard

`astrbot/a1in_release.py` 是 A1in 发行身份的源码内单一事实来源。运行时至少应暴露：

```text
Upstream compatibility: v4.26.8
A1in release:           a1in-v4.26.8.2
Source revision:         <image build commit>
Agent LLM hooks API:     1
```

这些信息会出现在启动日志、Dashboard stats API、登录页和 Dashboard 顶栏。`A1IN_SOURCE_REVISION` 由 release image build 注入，不应手工伪造为生产来源。

Dashboard 有两个不同的版本文件：

| 文件 | 值 | 用途 |
| --- | --- | --- |
| `dist/assets/version` | 官方兼容 tag，例如 `v4.26.8` | Core 与 Dashboard 的兼容性判定 |
| `dist/assets/a1in-release` | A1in release tag，例如 `a1in-v4.26.8.2` | 构建产物身份与排障 |

不得把 `a1in-v...` 写进 `dist/assets/version`。该文件必须继续与 Core 的官方兼容版本匹配，否则 Core 会把已捆绑 Dashboard 误判为版本不兼容。

## 5. 官方自更新策略

A1in release 默认不允许 Core 或 Dashboard 从官方 AstrBot 源自动下载和覆盖自身。维护者必须通过 A1in source release、GHCR image digest 和受控部署流程更新。

默认行为：

- Dashboard 的官方 update check 只报告 A1in 管理模式，不查询官方 release feed。
- 官方 Core update、Dashboard update、内置 Dashboard 更新命令和 CLI Dashboard 下载入口被拒绝。
- 生产 compose 不得设置 `A1IN_ALLOW_OFFICIAL_UPDATES=1`。

`A1IN_ALLOW_OFFICIAL_UPDATES=1` 只允许用于受控测试或明确的维护者诊断；它不是正常升级方式，也不能出现在生产环境、镜像默认环境或长期配置中。

## 6. 正常私有功能发布

在同一官方基线内开发私有功能时：

1. 从 `master` 创建 `feature/<topic>` 或 `fix/<topic>`。
2. 完成实现、审查、Core 测试、Dashboard 检查和插件兼容测试。
3. 合并到 `master`。
4. 在 `release/<topic>` 上更新 `astrbot/a1in_release.py` 的正式 release 值并完成最终验证。
5. 将 release branch 合并到 `master`，创建 annotated tag，例如 `a1in-v4.26.8.3`。
6. 只推送该 tag，让 `.github/workflows/a1in-release-image.yml` 构建多架构镜像。
7. 校验 OCI labels、source revision、Dashboard compatibility version 和最终 image digest。
8. 部署时只改为已验证的 digest；保留当前 digest 和 rollback material。
9. 验证完成后删除短期 release branch。

release workflow 必须验证：Git tag、`A1IN_RELEASE`、`A1IN_RELEASE_REVISION`、`A1IN_UPSTREAM_BASE_TAG` 和官方基线 tag 一致。它不发布 `latest`。

## 7. 同步新的官方稳定版本

官方发布新的稳定 tag 后，例如 `v4.26.9`：

1. `git fetch upstream --tags --prune`，先比较旧基线和新 tag。
2. 审查 Agent hooks、`ToolLoopAgentRunner`、context manager / compressor、Provider API、Dashboard 和插件 API 的变化。
3. 从当前 A1in `master` 创建 `sync/upstream-v4.26.9`。
4. 将官方 `v4.26.9` tag merge 到该同步分支；不要 merge `upstream/master`。
5. 解决冲突，明确记录所有 A1in 行为偏差。
6. 将 `pyproject.toml` 与 `astrbot/__init__.py` 更新为 `4.26.9`，并将下一发行身份准备为 `a1in-v4.26.9.1`。
7. 完成 Core 全量测试、Dashboard 检查、插件降级 / 完整 Trace 兼容测试和容器 smoke test。
8. 合并同步分支到 `master`，创建 `a1in-v4.26.9.1`，构建镜像并按 digest 部署。
9. 验证后删除同步分支，保留旧 A1in tag 和旧 digest 作为回滚点。

官方 `master` 仅供观察。除紧急安全事件且有单独记录外，生产 release 只能以官方稳定 tag 为上游同步边界。

## 8. API 兼容与 A1in 扩展

每个 A1in release 对外应明确声明：

```text
官方兼容基线：<upstream tag>
A1in release：<a1in tag>
已知 A1in 扩展：<capability list>
已知行为偏差：<documented deviations>
```

私有功能优先设计为显式、可检测、可文档化的扩展。插件不能只通过 `AstrBot >= X.Y.Z` 推断 A1in 私有能力。

例如逐轮 LLM lifecycle hooks 使用：

```python
AGENT_LLM_HOOKS_API_VERSION >= 1
```

API v1 的回调签名、round 语义、Core 耗时定义和隐私边界不得悄悄改变。新增可选字段必须保持旧插件可忽略；改变既有语义时提升 capability API 版本。

## 9. 镜像、部署与回滚

| 对象 | 要求 |
| --- | --- |
| Source release | A1in annotated Git tag |
| Image | 多架构 GHCR image，带 source / revision / upstream-base / release OCI labels |
| 生产引用 | 完整 image digest，绝不使用 `latest` |
| 插件 | 记录精确插件 tag / commit，并检查 capability compatibility |
| 回滚 | 保留当前前一版 image、compose、data snapshot、checksums 和 digest |
| 临时文件 | 传输归档、临时导入目录和已验证 staging 文件应在验收后删除 |

生产升级不由 Core 内置更新器完成。顺序固定为：构建与验证 → 推送 A1in tag / image → 更新部署 digest → 健康检查 → 受控业务验收。不要对 Docker 执行未核对范围的 `image prune -a`。

## 10. 发布前检查清单

- [ ] `master`、release branch、tag 的引用关系符合本规范。
- [ ] `pyproject.toml` 与 `astrbot/__init__.py` 的官方兼容版本一致。
- [ ] `A1IN_RELEASE` 与拟发布 Git tag 一致。
- [ ] `A1IN_UPSTREAM_BASE_TAG` 与官方基线 tag 一致。
- [ ] 官方稳定 tag 是 release commit 的祖先；未引入未发布官方 `master` 提交。
- [ ] Core lint、定向测试、全量测试、Dashboard check 和插件兼容测试通过。
- [ ] Dashboard `assets/version` 是官方兼容 tag，`assets/a1in-release` 是 A1in release tag。
- [ ] OCI labels、目标架构 manifest 和 image digest 已核对。
- [ ] 生产 compose 使用新 digest；旧 digest 和回滚数据已记录。
- [ ] 已确认 `A1IN_ALLOW_OFFICIAL_UPDATES` 未出现在生产环境。
