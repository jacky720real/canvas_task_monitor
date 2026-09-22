# canvas_task_monitor

本地学习任务监控工具。定时采集 **Canvas LMS** 的作业与课程公告、**Microsoft 365** 邮箱中的相关邮件，
在本地 SQLite 中对比历史快照识别变更，再交给 LLM 按固定模板抽取成结构化任务，并按紧迫度 / 重要度打分。

任务最终归入三大板块：**作业 / 活动 / 额外提醒**，可以像便签清单一样打勾标记完成。

## 项目定位

本项目有三种使用形态，**共用同一套业务层**：

| 形态 | 入口 | 说明 |
| --- | --- | --- |
| 独立运行 | `python cli_main.py ...` | 只装核心依赖即可用，不依赖任何外部系统 |
| MCP 服务 | `python mcp_server.py` | 暴露工具给支持 MCP 的 Agent 调用 |
| DSH 插件 | `dsh_plugin.py` | 作为个人 AI 工作台（DeepSeek Harness）的一个插件 |

**核心约束：业务层永远不感知宿主。** 宿主（CLI / MCP / DSH）升级、换协议、甚至挂掉，
`src/canvas_task_monitor/` 下的核心逻辑都不受影响。

## 架构

依赖倒置：宿主只依赖契约层，业务层不 import 任何宿主 SDK。

```
   ┌────────────────────────────────────────────────┐
   │  适配层 (可随时替换，宿主破坏性更新只改这层)     │
   │  cli_main.py  mcp_server.py  dsh_plugin.py     │
   └───────────────────┬────────────────────────────┘
                       │ 只调用 ↓
   ┌───────────────────▼────────────────────────────┐
   │  契约层 contracts/plugin.py                     │
   │  PluginFacade Protocol + API 版本号             │
   └───────────────────┬────────────────────────────┘
                       │ 由 ↓ 实现
   ┌───────────────────▼────────────────────────────┐
   │  业务层 services/ (Poller, Facade)              │
   │  ★ 禁止 import mcp / dsh / argparse / click ★   │
   └───────────────────┬────────────────────────────┘
                       │
   ┌───────────────────▼────────────────────────────┐
   │  领域层 core / storage / connectors / ai / diff │
   └────────────────────────────────────────────────┘
```

数据流：`connectors 采集` → `snapshots 落库` → `diff 比对出变更` →
`ai 按模板抽取 + 代码算分` → `tasks 落库` → `facade 对外提供能力`。

## 目录结构

```
canvas_task_monitor/
├── config/                     # 配置与 Prompt 模板（与代码解耦，独立迭代）
│   ├── settings.yaml
│   └── templates/task_extract_template.yaml
├── src/canvas_task_monitor/
│   ├── core/                   # 配置、模型、日志、哈希、限流
│   ├── contracts/              # 宿主契约（Protocol），不含任何宿主依赖
│   ├── storage/                # SQLite 仓储层
│   ├── connectors/             # Canvas / Graph / IMAP 采集器
│   ├── diff/                   # 变更检测
│   ├── ai/                     # Prompt as Code：模板、构建、调用、抽取
│   ├── services/               # 业务编排（bootstrap / facade / poller / task_service）
│   └── interfaces/             # 对外 DTO
├── cli_main.py                 # 入口 1：CLI
├── mcp_server.py               # 入口 2：MCP 服务
├── dsh_plugin.py               # 入口 3：DSH 插件
└── tests/
```

## 核心理念：Prompt as Code

**AI 只做"填空"，不做"创作"。**

- 提示词以 YAML 模板形式固化在 `config/templates/task_extract_template.yaml`，
  模板内含 `system_prompt`、`user_prompt_template`、`output_schema`。
- `user_prompt_template` 只允许 `{{ now_iso }}` 与 `{{ changes_json }}` 两个占位符，
  构建时只做字符串替换，不拼接任何额外提示。
- LLM 输出必须通过 `jsonschema` 校验；校验失败则记录日志、丢弃、**不写快照**，下一轮重试。
- 没有检测到变更时**绝不调用 LLM**（省 token 的关键）。
- **`score` 不由 LLM 输出**，由代码根据 `urgency × 权重 + importance × 权重` 计算，
  公式可通过 `settings.yaml` 的 `ai.score_weights` 调整。

## 快速开始

```bash
# 1. 安装（不带 extras 即可使用 CLI）
pip install -e .

# 2. 生成本地配置
copy .env.example .env      # Windows；macOS/Linux 用 cp
# 编辑 .env，填入 CANVAS_BASE_URL / CANVAS_TOKEN / LLM_* 等

# 3. 使用
python cli_main.py poll     # 立即轮询一次
python cli_main.py watch    # 后台持续轮询（Ctrl-C 退出）
python cli_main.py list     # 查看任务表格
python cli_main.py show 1   # 查看任务详情
python cli_main.py done 1   # 标记完成
```

其他形态：

```bash
pip install -e ".[mcp]"     # 安装 MCP 依赖后
python mcp_server.py

pip install -e ".[dsh]"     # 安装 DSH 依赖后
python dsh_plugin.py
```

### 邮箱接入决策树

优先级顺序：

```
① IMAP（最容易跑通，推荐先用）
② Graph client_credentials（需要租户管理员同意）
③ Graph Authorization Code（需要实现 OAuth 回调，后续可加）
```

切换方式：改 `config/settings.yaml` 里 `mail.provider` 字段（`graph` / `imap`），代码无需改动。
Graph 的 `client_credentials` 模式要求租户管理员授予应用级 `Mail.Read` 权限，学生个人账号
通常申请不到 —— 申请不下来就切 IMAP，别在这条路上耗时间。

> **切换 provider 会导致邮件任务在本地被视作新条目**：`imap:{Message-ID}` 与
> `graph:{internetMessageId}` 前缀不同，同一封邮件在切换后会被当成两条任务，
> 已完成的勾选状态不会迁移。建议选定 provider 后不要频繁切换。

## 开发

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

核心架构约束由 `tests/test_import_isolation.py` 自动守护：业务层一旦出现
`mcp` / `dsh` / `deepseek` / `click` / `argparse` 的 import，测试立即失败。

## 文档

以下章节将在后续阶段补全：

- 架构决策说明（为什么 status 不被 upsert 覆盖、为什么先 LLM 成功再写快照等）
- 抗宿主破坏性更新
- 双形态并行验证命令

（邮箱接入决策树见上文「快速开始 → 邮箱接入决策树」。）
