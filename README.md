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

> **注意**：`pip install -e ".[all]"` 会因为 DSH SDK 占位包名而失败。
> 当前请用 `pip install -e ".[mcp,dev]"`，等 DSH 文档到位后再装 `.[dsh]`。

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

## 架构决策说明

### 1. 为什么 `status` 不被 upsert 覆盖

`tasks.status`（pending / done）是**用户勾选出来的**本地状态，代表人的意志；而
`TaskRepo.upsert()` 的写入源是"LLM 抽取结果 + 自动轮询"。若 `SET` 子句包含 `status`，
用户刚勾选完成的任务会在下一轮轮询后被打回 pending。

因此 SQL 的 `SET` 子句刻意排除 `status`（见 `storage/task_repo.py` 内的注释），
唯一能改状态的入口是 `set_status()`，只由用户显式操作触发。
回归用例：`tests/test_task_repo.py::test_upsert_does_not_overwrite_user_status`。

### 2. 为什么先 LLM 成功再写快照

`Poller.poll_once()` 的顺序是：拉数据 → 检测变更 → 调 LLM → **LLM 成功后**才写快照。
若先写快照再调 LLM，一旦 LLM 失败，下一轮检测会认为"这批变更已处理过"而跳过，
**任务就永久丢失了**。现在的顺序保证：`llm_ok=False` 时本轮不写快照、不写任务，
下轮会重新检测到同一批变更并重试（`change_log` 仍留痕，`processed=0`）。

### 3. 为什么用字段白名单算 hash 而不是全量 payload

Canvas 返回体里的 `updated_at` / `html_url` 这类字段几乎每次请求都在变。
若对全量 payload 算 hash，每轮都会"误报变更"，进而每轮都调用 LLM —— token 直接烧光。

所以 `core/hashing.py` 为每个 source 维护一份**字段白名单**（如 `canvas_assignment`
只取 name / description / due_at / points_possible / submission_types），只对白名单内的
字段算 sha256。这样"内容真的变了"才触发变更。

### 4. 为什么无变更不调 LLM

`detect_changes()` 返回空列表时，`Poller` 直接 `continue`，`TaskExtractor.extract([])`
也会**立即返回 `([], True)` 且不发出任何 LLM 请求**。轮询是每 10 分钟一次的常态动作，
绝大多数轮次其实什么都没变；省 token 的关键就在这条分支上。

### 5. 为什么 score 由代码计算而不交给 LLM

- **LLM 算术不稳定**，5×10 + 3×8 这种式子它可能算错；
- **公式需要可调**：权重放在 `settings.yaml` 的 `ai.score_weights`，改配置即改分；
- `score` 本质是**确定性派生字段**，不是 AI 判断。

所以 `output_schema` 里**没有** `score`，`system_prompt` 明确禁止输出它；
extractor 在 schema 校验前会剥掉模型违规输出的 `score`（定向清理，其它多余字段仍被严格拒绝），
再用 `compute_score()` 重算并 clamp 到 0~100。

### 6. 为什么 Container 的配置预检在 Database 之前

`Container.__init__` 的第 3 步是 `_validate_all_config()`（零副作用），第 4 步才建库。
早期版本把校验放在建连接器那一步，结果是"缺配置"时报错前**已经创建了一个空的
`data/tasks.db`** —— 用户只是想看报错，却发现自己多了一个库，很诡异。

现在预检先于一切副作用执行：`校验`与`构造`分离（`_validate_all_config` 只校验、
`_build_connectors` 只构造）。回归用例：
`tests/test_config_validation.py::test_validation_is_side_effect_free`。

### 7. 为什么用 `find_spec` 而非 `import` 探测 DSH SDK

`dsh_plugin.py` 需要知道"DSH SDK 在不在"，但**不应该真的 import 它**：
插件框架常在模块顶层注册 handler、起后台线程、读环境变量，
在"探测"阶段执行这些代码会产生难以排查的副作用。
`importlib.util.find_spec("dsh")` 只查 import 系统、不执行模块。

真正的 SDK 调用等 DSH 文档到位后，写在专门的 `_register_with_dsh()` 里。

## 抗宿主破坏性更新

### 三层解耦

```
   ┌────────────────────────────────────────────────┐
   │  适配层 (可随时替换，宿主破坏性更新只改这层)     │
   │  cli_main.py  mcp_server.py  dsh_plugin.py     │
   └───────────────────┬────────────────────────────┘
                       │ 只调用 ↓
   ┌───────────────────▼────────────────────────────┐
   │  契约层 contracts/plugin.py                     │
   │  PluginFacade Protocol + PLUGIN_API_VERSION     │
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

这条约束由 `tests/test_import_isolation.py` 自动守护：只要它绿，宿主换版本就不会波及业务层。

### 实战案例：mcp 2.x 破坏性更新

#### 事件

mcp SDK 从 1.x 升级到 2.x 时，将 `FastMCP` 类改名为 `MCPServer`，
`mcp.server.fastmcp` 模块被移除。迁移指南：
https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver

#### 本项目的影响范围

- 业务层 (`src/canvas_task_monitor/`)：**0 行改动**
- 适配层 (`mcp_server.py`)：**6 行改动**（新增 try/except 嵌套 import shim）
- 测试：**既有冒烟全绿**，无需修改

#### 为什么能做到

1. 业务逻辑全部在 `services/facade.py` 及以下，不感知 mcp 存在
2. `mcp_server.py` 是薄包装——每个 tool 函数体 ≤ 3 行
3. 契约层 `contracts/plugin.py` 定义了宿主无关的 Protocol

#### 教训

选宿主 SDK 时优先选"薄适配层"架构：把宿主 SDK 的所有 import 集中在入口文件顶部，
用 try/except 包裹；**不要在业务代码里 import 任何宿主**。

### 升级 DSH（或 MCP）时的操作清单

1. 只改对应的入口文件 —— `dsh_plugin.py`（或 `mcp_server.py`）
2. 若宿主契约本身变了（不是 SDK 包名/类名，而是调用语义）：
   先 bump `contracts/plugin.py` 的 `PLUGIN_API_VERSION`，再逐个适配三个入口
3. 跑 `pytest`（尤其 `tests/test_import_isolation.py`）确认业务层没被污染
4. 用 `python dsh_plugin.py` / `python mcp_server.py` 自检块做一次人工 sanity check

### 判断"是否需要动业务层"的口诀

| 变化类型 | 改哪里 |
| --- | --- |
| 只是「换个方式调用已有能力」 | 入口文件 |
| 「多了一个业务动作」 | `services/` + `contracts/`，入口跟着加一行路由 |
| 「数据源 / 算法变了」 | 对应领域层（connectors / diff / ai） |
| **适配宿主** | **永远不该动业务层** |

## 双形态并行验证命令

```bash
# 独立形态（只需核心依赖）
pip install -e .
python cli_main.py --version
python cli_main.py list

# MCP 形态
pip install -e ".[mcp]"
python mcp_server.py

# DSH 形态（缺 SDK 时走自检块）
python dsh_plugin.py

# 全部测试
pip install -e ".[dev]"
pytest
ruff check .
```

## Web UI（可选）

```bash
python web_main.py                  # 自动打开浏览器 http://127.0.0.1:8765
python web_main.py --port 9000      # 指定端口
python web_main.py --no-browser     # 不自动打开浏览器
```

零第三方依赖（标准库 `http.server` + 单文件 `web_ui/index.html`）。
它和 `cli_main.py` 一样是**薄适配层**：4 个端点全部转发给 `container.facade.invoke`，
不写任何业务逻辑。**刻意不提供 `/api/poll`** —— 轮询会真实调用外部 API 并消耗
LLM token，不该是一个能一键触发的动作（要轮询请用 `cli_main.py poll` / `watch`）。

> **开发期调用说明**：`pip install -e .` 后 `ctm` / `ctm-web` 命令不可用（setuptools
> editable 安装对根级入口文件的已知限制）。请用 `python cli_main.py ...` /
> `python web_main.py ...`。非 editable 安装时 console script 正常生效。

## 首次启动 checklist

- [ ] 复制 `.env.example` 为 `.env`
- [ ] 填 Canvas 配置：`CANVAS_BASE_URL`、`CANVAS_TOKEN`
- [ ] 填邮箱配置（**优先 IMAP**）：`IMAP_HOST`、`IMAP_USER`、`IMAP_PASSWORD`
- [ ] 填 LLM 配置：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`
- [ ] `pip install -e ".[dev]"`
- [ ] `python cli_main.py --version` 输出正常
- [ ] `python cli_main.py poll` 跑一次（首次会拉全量，可能慢）
- [ ] `python cli_main.py list` 看任务
- [ ] 可选：`python cli_main.py watch` 后台跑
