# OAIC 2026 智能体软件工厂黑客松 · 第 7 名提交（Team BZT）

> **OAIC 2026 Agent Software Factory Hackathon — 7th Place Submission**
>
> ARC-Bench 平台的 **Codex 路线**参赛实现：给定一份结构化需求文档，由编码智能体
> 全自动生成**可运行、可通过评测**的全栈 Web 应用，并在严格的 token 预算约束下
> 最大化通过率。

---

## 一、赛事与任务

ARC-Bench（Agent Software Factory，智能体软件工厂）要求参赛路线在**无人干预**下，
把一份需求文档（`requirements.yaml`，含模块拆分、fixture 实体、seed 数据状态限定、
可访问性细节等）自动变成可运行的全栈应用。评测方随后：

- 启动产物，按需求中的**可访问名 / 角色逐字断言** UI 行为；
- 核对 fixture 实体在页面间的可达性与 seed 状态的一致性；
- 用公式对"通过率 × 开销"联合打分（见下）。

本路线固定 **OpenAI Codex CLI 0.154.0** 作为编码智能体，`main.py` 全程编排。

## 二、评分模型与策略

```
S(p, b) = p / (b / (1.2·p))^e        e = 0.1（实际开销 ≤ 1.2p）或 0.2（超支）
```

- `p` = 两任务综合通过率，`b` = 总开销
- 开销 ≤ 通过率预算的 1.2 倍时按 `e=0.1` 奖励，超支则按 `e=0.2` 惩罚
- **在超支区，1% 的通过率增益 ≈ 节省 6% 的开销**——因此本路线的所有机制只认一条
  口径：**通过率第一优先，省 token 只在"不损通过率"时采纳**。

## 三、为什么难

| 挑战 | 说明 |
| --- | --- |
| 逐字断言 | 评测用 `getByRole(role, { name: /^…$/i })` 按可访问名/角色**逐字**匹配；实测 88–100% 的断言名逐字出现在需求文本里（-t 任务 97–100%），"名字差一个字符"直接判失败。 |
| 预算公式 | 通过率与开销非线性耦合，超支惩罚翻倍——盲目求稳会把分数烧光，盲目省 token 会丢通过率。 |
| 平台约束 | 平台只注入已知变量清单、从不执行 `db:seed`（启动只建表）、外网仅白名单——"seed 源写对了但库里没有"这类漂移在正式评测里不会自愈。 |
| 协议差异 | Codex 0.154.0 只剩 Responses 协议，而决赛网关的 `/v1/responses` 实际是 chat-completions 版式——直连必 422（`tools[0]: missing field 'function'`）。 |
| 不可逆交付 | 交付后无人工检查环节，一切缺陷都直接进正式评测——必须有一个**不依赖模型自觉**的出厂质量门。 |

## 四、方案总览

```
需求树
  │
  ├─ ① 模板复制         React+Express+SQLite 全栈骨架（含测试脚手架）
  ├─ ② 契约转录         需求树 → 可断言契约索引（确定性、0 token）
  ├─ ③ 逐模块实现       Codex 会话 × 模块；零改动自动续跑；瞬态错误退避重试
  │                     ├─ 前端交互前先读 arcbench-frontend-spec 技能
  │                     └─ 参考截图经视觉模型转文本注入 prompt
  ├─ ④ verify 硬门      L1 产物自带测试套件 + L2 通用冒烟/语义检查
  │                     ├─ 未过 → 恰好一轮自动修复 → 复验（fail-closed）
  │                     └─ 数据库对账（0 token、只读、不需浏览器）
  └─ ⑤ 记账交付         token 用量、rollout 审计轨迹、契约索引随产物出厂
```

## 五、核心机制

### 1. 模板先行（`template/`）

run 开始即把全栈模板复制进产物，结构确定、单端口可跑：

- **前端** React + Vite + Tailwind，含通用组件原语（EntityCard / NamedForm /
  DataTable / RouteShell——props 驱动、零业务逻辑）
- **后端** Express + SQLite，含建表/seed/运行时测试工具
- **测试** Vitest（前后端）+ Playwright E2E 骨架

好处：agent 不必从零搭架构，把轮次花在需求本身上；自带测试套件同时是 verify 门
L1 层的输入。

### 2. 契约先行（`contract_index.py` + 四条铁律）

**为什么可行**：标准可访问性语义按"角色 + 可访问名"定位元素。因此契约面
可以在实现前由 Python **确定性转录**，而不是让模型凭记忆"猜名字"。

- 转录产物写进 `.codex/assertable-contracts.{md,json}`（0 token、不进 prompt，
  供 verify 探针与诊断复用）
- 每个模块 prompt 内嵌**四条铁律**（任何档位都保留）：精确可访问名 / 精确角色 /
  hover 用 opacity 常驻可访问性树 / fixture 限定词——这四条是可访问性评测逐字
  断言的全部根因

### 3. 技能分发（`skills/`）

4 个技能随产物分发、按需注入指令：

| 技能 | 作用 |
| --- | --- |
| `arcbench-frontend-spec` | 前端交互设计规范：精确可访问名、表单语义、hover/键盘可达性——写 UI 前必读 |
| `arcbench-checkpoint` | git 检查点：初始化/提交/恢复/重置工作区，统一提交历史刷新信号 |
| `arcbench-runtime-signals` | 进度事件：run 启停/阶段状态/前端刷新，走 `runner-events.jsonl` |
| `arcbench-traceability` | 追踪资产：需求/场景/接口/测试/调用边，写 `.arc/traceability` |

遥测类技能（checkpoint / signals / traceability）只服务运行期展示、不参与代码生成，
默认**不注入**（把 agent 轮次留给实现本身）。

### 4. 交付前验证硬门（`verify/verify.py`）

出厂前跑两级检查，**fail-closed**——冒烟不可用时不允许跳过验证直接出货：

- **L1**：运行产物自带的 `npm test` 前后端套件（永远可用、app 通用）
- **L2**：app 无关的数据契约冒烟——fixture 实体跨页可达性（含分页翻找）、
  可访问名/角色冲突与遮蔽检测、seed 源逐条对账、fixture 状态限定词
  （pinned / archived / label）核对、出厂库只读 SELECT 对账（`db_reconcile`）
- **HARD GATE**：blocking 缺陷 → **恰好一轮**自动修复后复验；再不过也照实交付
  （防止修复轮无限烧 token）

### 5. 协议桥（`responses_shim.py`）

纯标准库实现的本地转换层：在 127.0.0.1 起一次性 HTTP 服务，把 Codex 的 Responses
请求翻译成网关的 chat-completions 请求，再把 chat SSE 回流翻译回 Responses SSE
（含 usage 与工具调用历史往返）。解决 Codex 0.154.0 与决赛网关协议不对齐导致的
422 问题，是全路线能跑通模型调用的前提。

### 6. 工程兜底

- **零实现证据续跑**：模块回合结束后比较 `frontend/src` 与 `backend/src` 快照，
  一个字节都没变（模型"只说话不调工具"）就在同一线程追加续跑回合，最多 2 轮
- **断点续跑**：已完成模块写入 `completed_modules.json`，重跑/恢复不重复烧 token
- **瞬态错误重试**：网关瞬态错误识别 + 指数退避，不把网络抖动误判为模块失败
- **视觉参考注入**：需求引用的截图经视觉模型转成文本分析段注入 prompt，
  图片缺失/API 报错 fail-open 降级纯文本
- **记账与审计**：全程 token 用量汇报；CLI rollout 轨迹拷贝进产物
  `.arc/codex-rollouts/`（上限 30MB）供对账

## 六、工程化与可复现性

- **唯一事实来源**：所有功能开关集中在 `main.py` 顶部 `FEATURE_FLAGS` 一张表，
  不读环境变量（平台不透传自定义变量）；启动打印 `[codex] features: ...` 一行，
  本次 run 实际生效值核对只看这行。
- **逐字节等价验证**：省 token 手段（紧凑序列化、契约外置）均按
  "语义不变、输出逐字节等价"的实证门槛采纳，而不是凭感觉改 prompt：

  | 手段 | 实测 |
  | --- | --- |
  | 需求子树紧凑 JSON（`prompt_compact`） | 子树块 −30.6%~−33.9%，整条 prompt −16.2%~−26.4% |
  | 契约外置文件（`contract_inline`） | prompt −18.4%~−51.8%；两者叠加 −44.8%~−64.6% |
  | 执行复杂度档位 L2 | 省 35–50% token |
  | 执行复杂度档位 L1B / L1C | 省 15–25% token |

- **单变量对照档位**：`ARCBENCH_EFFORT_DEFAULT`（L0 完整契约基线 / L1A 保留测试 /
  L1B 允许构建检查 / L1C 禁测试 / L2 契约压缩为四条铁律）；L1A/L1B/L1C 只差
  "测试策略"一个变量，专为 A/B 对照决定长期默认档而设计。

## 七、目录结构

```
.
├── main.py                    # 编排入口：模板→契约→逐模块实现→验证门→记账
├── contract_index.py          # 需求树 → 可断言契约索引（确定性、0 token）
├── responses_shim.py          # Responses↔chat-completions 本地协议桥
├── token_usage.py             # token 记账与汇报
├── visual_refs.py             # 参考截图 → 视觉模型 → 文本注入 prompt
├── verify/verify.py           # 交付前验证硬门（L1 套件 + L2 冒烟 + HARD GATE）
├── skills/                    # 随产物分发的 4 个技能（frontend-spec 必读）
├── template/                  # React+Express 全栈模板（含测试脚手架）
└── arcbench-agent-runtime/    # 工作区运行时 SDK（事件/追踪/git 助手，经 requirements 安装）
```

## 八、运行

平台容器中由 runner 调用入口；本地复现：

```bash
pip install -r requirements.txt

OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.arc-bench.com/v1 MODEL=... \
python main.py --requirement-path <需求目录> --output-dir <产物目录>
```

`verify/verify.py` 独立于主流程，可对任意已生成产物单独复跑验证。

## 九、备注

- 名次：**第 7 名**（Team BZT）
- 模型：Codex CLI 驱动，具体模型由平台注入（`MODEL` 环境变量）
- 每个功能开关的完整"作用 / 翻转风险 / 对分数影响"逐条注释在 `main.py` 顶部
  `FEATURE_FLAGS` 表中，是理解全部设计取舍的第一入口
