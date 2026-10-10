from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

import yaml

import token_usage
from visual_refs import build_visual_block


# ===========================================================================
# 用户配置区：Codex 执行复杂度档位（L0 / L1A / L1B / L1C / L2）
# ---------------------------------------------------------------------------
# 档位由用户配置：直接改下面的 ARCBENCH_EFFORT_DEFAULT，或用环境变量
# ARCBENCH_EFFORT 覆盖（优先级更高），例如：ARCBENCH_EFFORT=3 python3 main.py
#
#   L0  (0) —— 基线（旧行为）：完整 11 条自证契约 + 逐条证据罗列 + 读 SKILL.md
#              + verify gate。最稳、最贵。
#   L1A (1) —— 轻减·保留测试（最保守）：契约一字不改，只砍服务/浏览全项目/
#              结论一行；测试套件照跑。工程保障最接近 L0，省得最少。
#   L1B (2) —— 轻减·允许构建检查（折中）：同 L1A，但禁完整测试套件，允许跑
#              一次 build/tsc 抓编译错。省 15–25%，工程风险居中。
#   L1C (3) —— 轻减·禁测试（最激进）：同 L1A，且禁跑测试套件。省 15–25%，
#              工程风险最高。
#   L2  (4) —— 中减：契约压缩为四条铁律（精确可访问名/角色/hover opacity/
#              fixture 限定词），免逐条证据；仍读 SKILL.md + verify gate 兜底。
#              评分风险低，省 35–50% token。
# ---------------------------------------------------------------------------
# 铁律不可删：四条铁律是可访问性评测逐字断言的全部根因（CONTRACT.md），
# 任何档位都保留；L2 只是压缩表述。
# 实验提示：L1A / L1B / L1C 只差"测试策略"一个变量，其余完全相同，
# 适合做对照实验决定长期默认档。
# ===========================================================================
ARCBENCH_EFFORT_DEFAULT = 0


# ===========================================================================
# 用户配置区：功能开关（唯一事实来源就是下面这张表）
# ---------------------------------------------------------------------------
# 这些开关以前从环境变量读，但环境变量这条路在平台上是空的：平台容器不注入
# 任何 ARCBENCH_* 变量，本地 .env 既不进 staging 也不进产物，所以真正生效的
# 一直是代码里的默认值——"以为关了其实开着"（平台日志曾显示已设为 0 的开关
# 仍被读取并执行）。现在开关只认这张表：值随产物一起提交、一次 run 内
# 不会被任何外部因素影响，也不再区分"正向键/反向键"。
#
# 规则：
#   1. 改行为 = 改这张表的字面量；除这里以外没有第二处默认值。
#   2. 环境变量不再参与判定。残留的旧变量只会在启动时打一条 WARNING 说明
#      "已忽略"，取值与表里一致时连告警都不打。
#   3. 本次 run 的实际生效值全部打印在 `[codex] features: ...` 一行，核对只看这行。
#   4. 本地 A/B / 逐字节比较：把本文件载入成模块后改 `FEATURE_FLAGS[...]` 再调
#      取 prompt 或产物的函数，不要再设环境变量。
# ===========================================================================
FEATURE_FLAGS: dict[str, bool] = {
    # =========================================================================
    # 开关注释速读（每条 = 作用 / 翻转后的逻辑变化与风险 / 对比赛结果的影响）
    # 比赛得分 S(p,b) = p / (b/(1.2·p))^e，e=0.1 奖励(实际开销≤1.2p) / 0.2 惩罚(超支)，
    # p=两任务综合通过率、b=总开销；超支区 1% p 的分数增益 ≈ 省 1% b 的 6 倍。
    # ⇒ 通过率第一优先，省 token 只在"不损 p"时采纳；下文"影响"均按此口径写。
    # =========================================================================

    # frontend_spec —— 前端交互规范 skill（arcbench-frontend-spec）注入
    #   作用：True 时每个模块 prompt 要求 agent 实现前端交互前先读该 skill，并把
    #        skill 复制进产物；skill 固化精确可访问名 / 精确角色 / hover 用 opacity
    #        常驻可访问性树 / 表单语义等通用可访问性规则。
    #   翻转：False = 不注入指令、产物不含该 skill；agent 失去 UI 契约约束，
    #        极易产出 getByRole(role,{name:/^…$/i}) 定位不到的页面。
    #   影响：True → 通过率显著↑（评测以可访问名/角色逐字断言）；
    #        代价是每模块 prompt 多一段指令文本（token 略增，可被
    #        prompt_compact / contract_inline 抵消）；False → token 略省、通过率风险大，不建议关。
    "frontend_spec": True,

    # verify_gate —— 交付前自检硬门（L1 自带测试 + L2 冒烟/语义 + gate）
    #   作用：True 时交付前跑 verify/verify.py，gate 报 blocking 缺陷则触发
    #        **唯一一轮**自动修复后复验——这是"没验过不出货"的评分兜底
    #        （冒烟不可用时故意 fail-closed，不允许跳过验证直接出货）。
    #   翻转：False = 完全跳过 verify 与修复轮（唯一"跳过兜底"路径）：省一轮修复的
    #        token 与时间，但产物不再有任何质量关卡，逻辑/结构缺陷直接进正式评测。
    #   影响：True → 拦截低级缺陷、通过率更稳（代价是可能多一轮修复的 token）；
    #        False → 失去唯一一次自动修复机会，分数波动大。这是评分兜底，别关。
    "verify_gate": True,

    # db_reconcile —— 出厂数据库对账门（K2）
    #   作用：True 时 verify 在交付前只读打开**真正被 npm start 使用的那个库**
    #        （backend/database.db，与 init_db 的 cwd 口径一致），把 seed 源里声明的
    #        每个实体值与每个 archived/pinned 状态逐条 SELECT 核对；缺失/状态不符
    #        计入 blocking，交给既有的恰好一轮修复轮。0 token、不写库、不需要浏览器
    #        （冒烟不可用时照样 blocking）。
    #   翻转：False = 不查出厂库。平台从不执行 db:seed、启动只建表，所以"seed 源写对了
    #        但库里没有"永远查不出来——曾有 run 因此一次丢掉 3 例 GIVEN 夹具，
    #        seed 里的 GIVEN 工作表被自测改名后照样出厂（-1 例潜在）。
    #   影响：True → 通过率↑（夹具状态漂移从方差变成硬拦截）；代价 = 只在真有漂移时
    #        多一轮修复（≈一个线程的 token），无漂移时零开销。别关。
    "db_reconcile": True,

    # module_continuation —— 模块回合"零实现证据即续跑"（K1）
    #   作用：True 时每个模块回合结束后比较 frontend/src 与 backend/src 的文件快照；
    #        一个字节都没变（模型只说话不调工具）就在**同一线程**追加续跑回合，最多
    #        MODULE_CONTINUATION_MAX 次，仍零改动才放行并打 WARNING 到 .arc/stdout.log。
    #   翻转：False = 回合返回即判成功并写 completed_modules.json。曾有三个模块会话
    #        就是这样"只读代码就结束"，整段丢掉 7 例已验证通过的用例。
    #   影响：True → 消除"模块被跳过但记为完成"型整段回退，run 间方差显著变小；
    #        代价 = 仅在检测到零改动时才多花回合（合法空模块最多多 2 轮后放行）。
    "module_continuation": True,

    # visual —— 参考截图交视觉模型转文字注入 prompt
    #   作用：True 时对子树引用的参考图做一次视觉模型调用（VISUAL_MODEL，缺省回退主模型），
    #        把图转成文本分析段注入每个模块 prompt；图片缺失/无配置/API 报错只打 warning
    #        并跳过该图，run 始终以纯文本继续（fail-open），结果缓存于 .arc/visual_refs_cache.json。
    #   翻转：False = 纯文本 prompt，带图题（首页/产品页布局类）失去视觉信息。
    #   影响：True → 带图题通过率↑（参考图信息缺失时首页/产品页布局类易失分），
    #        代价 = 每张图一次视觉调用 + 分析文本进每个模块 prompt（token↑）；
    #        False → 省 token、带图题有风险。平台不注入 VISUAL_* 时自动按主模型回退。
    "visual": True,

    # telemetry —— 运行时遥测三 skill（git 检查点/进度/可追溯性）
    #   作用：只服务平台运行时的进度展示，不参与也不约束代码生成，每次调用消耗
    #        agent 轮次（token 和时间）。
    #   翻转：True = 注入指令 + 复制 skill，把实现轮次让给遥测；False = 轮次留给实现本身。
    #   影响：对 S(p,b) 无任何正向作用，只增加总开销 b（超支区惩罚 β=0.2）；
    #        保持 False。
    "telemetry": False,

    # verbose_log —— [codex][verbose] 诊断行
    #   作用：True 时打印 verbose 级调试日志（completed ids / rollout 拷贝数等）。
    #   翻转：只影响日志量，不改 prompt、不改产物、不影响得分。
    #   影响：无得分影响；本地调试可临时开，正式提交保持 False 避免刷屏。
    "verbose_log": False,

    # codex_rollouts —— 把 CLI rollout 轨迹拷进产物 .arc/codex-rollouts/
    #   作用：True 时把 $CODEX_HOME/sessions/**/rollout-*.jsonl（含逐次 token_usage_record）
    #        复制到产物做审计对账，上限 30MB（未变更不重拷）；只读 $CODEX_HOME，不改变
    #        CLI 行为，失败只告警。
    #   翻转：False = 不复制；产物变小、token 审计轨迹缺失。
    #   影响：无得分影响；注意 30MB 上限与提交体积约束（~50MB），若产物逼近上传上限可关。
    "codex_rollouts": True,

    # responses_shim —— codex Responses 协议 → 网关 chat 协议的本地桥
    #   作用：True 时在进程内 127.0.0.1 起一个纯标准库转换层（responses_shim.py）：
    #        codex 仍按 wire_api=responses 说话，桥把请求翻成 chat 发往
    #        OPENAI_BASE_URL，再把 chat SSE 翻回 Responses SSE（含 usage / 工具调用
    #        历史往返）。必需性：codex 0.154.0 只剩 responses 协议，而决赛网关的
    #        /v1/responses 实际是 chat 版式——直连必 422
    #        "tools[0]: missing field `function`"（2026-10-06 决赛实测）。
    #   翻转：False = codex 直连网关（决赛网关必失败；仅当网关恢复原生 Responses 才可关）。
    #   影响：关 = 模型调用全挂（该题 0 分）；开 = 正常运行。除网关换协议外不要关。
    "responses_shim": True,

    # prompt_compact —— 需求子树紧凑 JSON 序列化（S1a）
    #   作用：True 时 subtree 用 separators=(",",":") 无缩进进 prompt
    #        （实测子树块 −30.6%~−33.9%，整条 prompt −16.2%~−26.4%，逐字节等价）。
    #   翻转：False = 回旧 indent=2 格式，token 回升。
    #   影响：语义不变（已逐字节验证），纯省 token → 直接降 b、减轻超支惩罚；
    #        无通过率风险，建议保持 True。
    "prompt_compact": False,  # ——False为关闭功能

    # contract_inline —— 路线契约清单内联 vs 外置文件（S1b）
    #   作用：False = 契约写进产物 .codex/ARCBENCH_CONTRACT.md，prompt 只留一行指针
    #        （实测 prompt −18.4%~−51.8%，与 prompt_compact 叠加 −44.8%~−64.6%）；
    #        写文件失败自动回内联（fail-open，绝不出现指针指向不存在文件）。
    #   翻转：True = 契约全文内联进每个模块 prompt，token 回升。
    #   影响：省 token 降 b；风险点是"agent 是否真去读外置文件"——已用逐字节输出
    #        等价性验证过，但换模型/版本后若读文件不稳可回 True（回退写法 = True）。
    "contract_inline": True,  # ——True为关闭功能

    # template_kit —— 模板携带任务无关通用组件库（S2′）
    #   作用：True 时拷贝 template 时带上 frontend/src/components（EntityCard /
    #        NamedForm / DataTable / RouteShell：props 驱动、零实体名/seed/业务逻辑、
    #        默认不被任何页面挂载——按参赛须知 §三.2 属合规的通用脚手架）。
    #   翻转：False = 模板减去该目录（26 文件基线，SHA-256 全等）。
    #   影响：True → agent 少写一套 a11y 正确的原语，少触发 gate 修复轮
    #        （每个修复轮 = 一次线程 = 直接 token），通过率与开销双受益；
    #        合规上必须保持"任务无关"，一旦塞入当前任务内容即触红线 1。
    "template_kit": False,  # ——False为关闭功能

    # contract_index —— 需求树 → 可断言契约索引（S5）
    #   作用：True 时把需求里已写明的可断言契约（引号可访问名 / 角色短语 / aria 状态 /
    #        seed 实体与状态限定词等）转录成 .codex/assertable-contracts.{md,json}；
    #        纯 Python、0 token、不注入任何模块 prompt。
    #   翻转：False = 不写该文件。
    #   影响：只供 verify 的 contract_probes 与人工/后续 S2/S3 复用，不参与生成、
    #        不进 prompt、不 gate；对得分无直接影响，开着有益无害。 
    "contract_index": False,  # ——False为关闭功能

    # contract_probes —— verify 报告追加契约覆盖 advisory 段（S3′）  
    #   作用：True 时 verify 报告追加"需求逐字契约名有多少出现在交付源码"的
    #        advisory 覆盖段，作为修复轮的行动清单；**永不 blocking**（覆盖率与分数
    #        不同向：名字写全 ≠ 行为正确，当判据会误伤）。
    #   翻转：False = 报告无该段。
    #   影响：不改 gate 判定、不影响得分；只作诊断，开/关皆可。 
    "contract_probes": False,  # ——False为关闭功能

    # verify_browser_install —— verify 无 chromium 时尝试 playwright install
    #   作用：True 时 L2 冒烟发现容器无 chromium 会先尝试安装一次再验证；
    #        False = 直接记 skipped（gate 降级 not-verifiable，不阻塞，静态探针照跑）。
    #   翻转：True → 本地无浏览器时冒烟更可能完成（gate 更可信），但安装耗时，
    #        且依赖外网 CDN——正式平台外网仅白名单（npm 等软件源），若被限制会拖慢
    #        run 甚至失败；本地调试需要时可临时开，正式提交保持 False。
    #   影响：间接——冒烟可用时 gate 更稳、通过率更可预期；安装失败不阻塞 run。
    "verify_browser_install": False,
}

# K1 的续跑回合上限（只认这个字面量，与 FEATURE_FLAGS 同处、同样不读环境变量）：
# 模块回合结束时若 frontend/src 与 backend/src 一个字节都没变，就在同一线程上追加
# 续跑回合，最多这么多次；用完仍零改动则打 WARNING 后放行（合法的空模块不该 fail 整轮）。
MODULE_CONTINUATION_MAX = 2

# 旧的环境变量名 -> 开关键。只用于"这个变量已经失效"的告警，不参与任何判定。
_RETIRED_FLAG_ENV: dict[str, str] = {
    "ARCBENCH_FRONTEND_SPEC": "frontend_spec",
    "ARCBENCH_VERIFY": "verify_gate",
    "ARCBENCH_VISUAL": "visual",
    "ARCBENCH_TELEMETRY": "telemetry",
    "ARCBENCH_VERBOSE": "verbose_log",
    "ARCBENCH_CODEX_ROLLOUTS": "codex_rollouts",
    "ARCBENCH_PROMPT_COMPACT": "prompt_compact",
    "ARCBENCH_CONTRACT_INLINE": "contract_inline",
    "ARCBENCH_TEMPLATE_KIT": "template_kit",
    "ARCBENCH_CONTRACT_INDEX": "contract_index",
    "ARCBENCH_CONTRACT_INDEX_ADVISORY": "contract_index",
    "ARCBENCH_CONTRACT_PROBES": "contract_probes",
    "ARCBENCH_VERIFY_BROWSER": "verify_browser_install",
    "ARCBENCH_EFFORT": "（见 ARCBENCH_EFFORT_DEFAULT）",
}


def flag(name: str) -> bool:
    """开关取值：只认 FEATURE_FLAGS 里的字面量。"""
    return bool(FEATURE_FLAGS[name])


def feature_summary() -> str:
    """渲染本次 run 的全部生效开关；核对开关只看 `[codex] features:` 这一行。"""
    parts = [f"effort={effort_label()}"]
    parts += [f"{key}={FEATURE_FLAGS[key]!r}" for key in sorted(FEATURE_FLAGS)]
    return ", ".join(parts)


def _lenient_env_bool(raw: str) -> bool | None:
    """尽力把旧变量的取值解析成 bool；解析不了返回 None（只用于告警措辞）。"""
    text = raw.split("#", 1)[0].strip().strip("\"'").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def report_retired_flag_env() -> None:
    """把容器里残留的旧开关变量报出来，说明它们已不参与判定。"""
    stale: list[str] = []
    for env_name, key in _RETIRED_FLAG_ENV.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        parsed = _lenient_env_bool(raw)
        if key in FEATURE_FLAGS and parsed == FEATURE_FLAGS[key]:
            continue  # 取值恰好与表里一致，不值得打扰
        stale.append(f"{env_name}={raw!r}")
    if stale:
        print(
            "[codex] WARNING: these env vars no longer control anything (switches "
            "live in FEATURE_FLAGS at the top of Codex/main.py); ignored: "
            + ", ".join(stale),
            flush=True,
        )


@dataclass(frozen=True)
class RequirementModule:
    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]


# Frontend interaction design spec (arcbench-frontend-spec skill). Toggled by
# FEATURE_FLAGS["frontend_spec"].
FRONTEND_SPEC_SKILL = "arcbench-frontend-spec"

# Runtime telemetry skills (git checkpoints, progress signals, traceability).
# They only serve the ARC-Bench runtime frontend, never the generated code, and
# cost agent turns to run, so FEATURE_FLAGS["telemetry"] keeps them off by
# default and Codex spends its turns on the implementation itself.
TELEMETRY_SKILLS = (
    "arcbench-checkpoint",
    "arcbench-runtime-signals",
    "arcbench-traceability",
)

# S2': the generic UI primitives shipped inside the template. They are
# task-agnostic by construction (props-driven, no entity names, no seed data, no
# business logic, not mounted by any page) - the competition rule allows generic
# scaffolding/component libraries and forbids task-specific pre-implementation.
TEMPLATE_KIT_REL = Path("frontend") / "src" / "components"


def _template_copy_ignore(template_dir: Path, output_dir: Path):
    """copytree ignore callback for the template sync.

    Two jobs:

    * Drop the generic component kit when the switch is off.
    * Never overwrite a destination entry that already exists. The route is
      re-runnable (resume, and evolution runs whose output already holds the
      previous stage) and a raw copytree would write every template path back
      to its starter content, wiping that earlier work. Existing directories
      still recurse, so a partially-populated tree keeps its files and gets
      only the missing ones filled in.
    """
    kit_on = flag("template_kit")

    def ignore(dirpath: str, names: list[str]) -> list[str]:
        source_dir = Path(dirpath)
        try:
            relative = source_dir.resolve().relative_to(template_dir.resolve())
        except ValueError:
            return []
        skipped: list[str] = []
        if (
            not kit_on
            and relative == TEMPLATE_KIT_REL.parent
            and TEMPLATE_KIT_REL.name in names
        ):
            skipped.append(TEMPLATE_KIT_REL.name)
        dest_dir = output_dir / relative
        for name in names:
            if name in skipped:
                continue
            destination = dest_dir / name
            if not destination.exists():
                continue  # missing -> fill it in
            if (source_dir / name).is_dir() and destination.is_dir():
                continue  # existing dir -> recurse to fill its missing children
            skipped.append(name)  # existing file (or type clash) -> never overwrite
        return skipped

    return ignore


# The kit's documentation, injected into the delivered README only when the kit is
# actually carried. Keeping it out of template/README.md is what makes "kit off"
# byte-identical to the template without it: a baked-in section would advertise
# primitives that were not copied.
_KIT_README_SECTION = """
## Generic UI Primitives (task-agnostic)

`frontend/src/components/` ships four small, props-driven primitives:

| Component | Contract it encodes |
|---|---|
| `EntityCard` | `<article>` named by its title; status indicators in **descendant** elements; hover actions revealed with `opacity` only; title rendered with a real role (heading / button / link) |
| `NamedForm` | `role="form"` with an accessible name that shares no word root with its field labels; native controls; browser validation off so validation stays assertable |
| `DataTable` | native table semantics, sortable header buttons with `aria-sort`, and a named empty state (`role="status"`) |
| `RouteShell` | logo link + named navigation landmark whose entries are **links** (never `role="button"`) + page body slot |

They are deliberately free of task content: no entity names, no seed data, no routes, no
pages, and they are **not mounted anywhere** by default — `App.tsx` renders only the
starter page. Use what helps, adapt it, and delete what does not; importing them is
optional. Never add task-specific names, seed values or business logic to these files.
"""

_KIT_README_ANCHOR = "\n## Prerequisites"


def _add_component_kit_section(output_dir: Path) -> None:
    """Document the shipped primitives in the delivered README (kit on only).

    Byte-level edit on purpose: the copy pipeline is byte-exact, and text-mode writes
    would re-encode line endings on Windows.
    """
    readme = output_dir / "README.md"
    if not readme.is_file():
        return
    section = _KIT_README_SECTION.encode("utf-8")
    anchor = _KIT_README_ANCHOR.encode("utf-8")
    raw = readme.read_bytes()
    if section in raw:
        return  # already documented (README kept across re-runs) - do not duplicate
    if anchor in raw:
        raw = raw.replace(anchor, section + anchor, 1)
    else:
        raw = raw.rstrip(b"\r\n") + b"\n" + section
    readme.write_bytes(raw)


_EFFORT_LABELS = {0: "L0", 1: "L1A", 2: "L1B", 3: "L1C", 4: "L2"}


def effort_level() -> int:
    """The configured execution-complexity gear.

    Levels: 0=L0, 1=L1A, 2=L1B, 3=L1C, 4=L2. The value is the head-of-file
    ARCBENCH_EFFORT_DEFAULT constant and nothing else - no environment variable
    participates. An out-of-range constant warns and falls back to L0 instead of
    failing the run.
    """
    level = ARCBENCH_EFFORT_DEFAULT
    if level not in (0, 1, 2, 3, 4):
        print(
            f"[codex] WARNING: invalid ARCBENCH_EFFORT_DEFAULT={level!r}; "
            "falling back to L0",
            flush=True,
        )
        return 0
    return level


def effort_label(level: int | None = None) -> str:
    """Human-readable gear label, e.g. 1 -> 'L1A'. Defaults to current level."""
    return _EFFORT_LABELS.get(effort_level() if level is None else level, f"L{level}")


def _verbose(*args: Any) -> None:
    """Print a verbose-level diagnostic; shown by FEATURE_FLAGS["verbose_log"]."""
    if flag("verbose_log"):
        print("[codex][verbose]", *args, flush=True)


# --- Token usage capture (S0) ----------------------------------------------
# Streaming a turn is the only way to see per-model-call usage (the blocking
# `Thread.run` returns a single cumulative snapshot per turn). The SDK pieces
# below are private, so their absence only disables the finer granularity.
try:  # pragma: no cover - depends on the pinned SDK layout
    from openai_codex._run import _collect_turn_result as _sdk_collect_turn_result
except Exception:  # noqa: BLE001
    _sdk_collect_turn_result = None

try:  # pragma: no cover - depends on the pinned SDK layout
    from openai_codex import TurnHandle as _SdkTurnHandle
except Exception:  # noqa: BLE001
    _SdkTurnHandle = None

# Set once a streaming attempt fails, so later modules use the blocking path
# instead of paying for another broken attempt.
_STREAMING_DISABLED = False
_STREAMING_NOTICE_SHOWN = False


def streaming_available(thread: Any) -> bool:
    """Pre-flight check before starting a turn (never mid-turn)."""
    global _STREAMING_NOTICE_SHOWN
    if _STREAMING_DISABLED:
        return False
    reason = ""
    if _sdk_collect_turn_result is None:
        reason = "the SDK exposes no _collect_turn_result"
    elif not hasattr(thread, "turn"):
        reason = "the SDK thread exposes no turn()"
    elif _SdkTurnHandle is not None and not hasattr(_SdkTurnHandle, "stream"):
        reason = "TurnHandle exposes no stream()"
    if not reason:
        return True
    if not _STREAMING_NOTICE_SHOWN:
        _STREAMING_NOTICE_SHOWN = True
        print(
            f"[codex] note: per-call token capture unavailable ({reason}); "
            "recording per-turn totals only",
            flush=True,
        )
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Implement an ARC-Bench task with Codex.")
    parser.add_argument("requirement_path", help="Directory containing requirements.yaml.")
    parser.add_argument("--output-dir", required=True, help="Target directory for the generated project.")
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="Task type supplied by ARC-Bench.",
    )
    return parser.parse_args()


def copy_template_contents(template_dir: Path, output_dir: Path) -> None:
    """Sync the bundled template's children into the output directory.

    Fill-missing only: an existing destination file is never overwritten, so a
    re-run (resume, or an evolution run whose output already holds the previous
    stage) keeps the work already there. Existing directories still recurse, so
    their missing files are filled in. A fresh/empty output directory receives
    the full template byte-for-byte.
    """
    if not template_dir.is_dir():
        # ARC-Bench assembles the authoritative task template before invoking
        # the agent (and evolution runs already contain the previous-stage
        # application). Reference archives intentionally omit a duplicate
        # template directory, so preserve the prepared output in that case.
        if output_dir.is_dir() and any(output_dir.iterdir()):
            print(
                f"[codex] template dir missing ({template_dir}); output already "
                "populated, skipping copy",
                flush=True,
            )
            return
        print(
            f"[codex] ERROR: starter template directory not found: {template_dir}",
            flush=True,
        )
        raise FileNotFoundError(f"Starter template directory not found: {template_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    ignore = _template_copy_ignore(template_dir, output_dir)
    copied = 0
    kept = 0
    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml":
            continue
        destination = output_dir / source.name
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                kept += 1
                _verbose(f"kept (type clash, not overwritten): {source.name}")
                continue
            shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
        else:
            if destination.exists():
                kept += 1
                _verbose(f"kept existing template item: {source.name}")
                continue
            shutil.copy2(source, destination)
        copied += 1
        _verbose(f"copied template item: {source.name}")
    if flag("template_kit"):
        _add_component_kit_section(output_dir)
    kit_note = "with" if flag("template_kit") else "without"
    kept_note = f"; kept {kept} existing item(s) as-is" if kept else ""
    print(
        f"[codex] copied {copied} template item(s) to {output_dir} "
        f"({kit_note} the generic component kit){kept_note}",
        flush=True,
    )


def copy_skills_to_output(skills_dir: Path, output_dir: Path, exclude: set[str] | None = None) -> Path:
    """Make the bundled ARC-Bench skills available inside Codex's workspace.

    Optional ``exclude`` skips top-level skill directories (used to uninstall a
    skill from the produced artifact without deleting its source).
    """
    if not skills_dir.is_dir():
        print(f"[codex] ERROR: skills directory not found: {skills_dir}", flush=True)
        raise FileNotFoundError(f"Skills directory not found: {skills_dir}")
    destination = output_dir / ".codex" / "skills"
    exclude = exclude or set()
    if not exclude:
        shutil.copytree(skills_dir, destination, dirs_exist_ok=True)
        print(f"[codex] copied all skills to {destination}", flush=True)
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    excluded = 0
    for source in sorted(skills_dir.iterdir()):
        if source.name in exclude:
            excluded += 1
            _verbose(f"excluded skill: {source.name}")
            continue
        target = destination / source.name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        copied += 1
        _verbose(f"copied skill: {source.name}")
    print(
        f"[codex] copied {copied} skill(s) to {destination} (excluded {excluded})",
        flush=True,
    )
    return destination


def load_root_modules(requirements_dir: Path) -> list[RequirementModule]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        print(
            f"[codex] ERROR: requirements.yaml not found: {requirements_path}",
            flush=True,
        )
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")

    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        detail = (
            f"id={payload.get('id')!r}"
            if isinstance(payload, dict)
            else f"payload type={type(payload).__name__}"
        )
        print(
            f"[codex] ERROR: requirements.yaml must contain a ROOT mapping ({detail})",
            flush=True,
        )
        raise ValueError("requirements.yaml must contain a ROOT mapping")

    raw_children = payload.get("children", [])
    children = [item for item in raw_children if isinstance(item, dict)]
    if not children:
        print("[codex] ERROR: ROOT must contain at least one child module", flush=True)
        raise ValueError("ROOT must contain at least one child module")
    if isinstance(raw_children, list) and len(children) != len(raw_children):
        _verbose(f"filtered {len(raw_children) - len(children)} non-dict ROOT child entrie(s)")

    modules: list[RequirementModule] = []
    for index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            print(f"[codex] ERROR: ROOT child {index} has no id", flush=True)
            raise ValueError(f"ROOT child {index} has no id")
        name = str(subtree.get("name") or node_id).strip()
        modules.append(
            RequirementModule(
                index=index,
                total=len(children),
                node_id=node_id,
                name=name,
                subtree=subtree,
            )
        )
        _verbose(f"module {index}/{len(children)}: {node_id} - {name}")
    print(f"[codex] loaded {len(modules)} ROOT module(s) from {requirements_path}", flush=True)
    return modules


CODEX_ROLLOUT_DIR = ".arc/codex-rollouts"
CODEX_ROLLOUT_MAX_MB = 30.0
_ROLLOUT_LIMIT_WARNED = False


def _collect_codex_rollouts(output_dir: Path, since: float | None = None) -> int:
    """Copy the CLI's rollout JSONL into the artifact (S0 cross-check source).

    The Codex CLI mirrors every model response's usage into
    ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` (``token_usage_record`` events
    carrying response_id, per-call and per-turn/thread totals). That home lives
    inside the container and dies with it, while the in-process ledger only
    keeps what this route parsed. Copying the files out gives an independent,
    finer-grained audit trail — and unlike pointing CODEX_HOME elsewhere, it
    touches nothing the CLI reads.

    Best effort: returns the number of files copied; any failure warns and
    returns 0. Toggled by FEATURE_FLAGS["codex_rollouts"].
    """
    global _ROLLOUT_LIMIT_WARNED
    if not flag("codex_rollouts"):
        return 0
    try:
        source = os.environ.get("CODEX_HOME", "").strip()
        home = Path(source) if source else Path.home() / ".codex"
        sessions = home / "sessions"
        if not sessions.is_dir():
            return 0
        target_root = Path(output_dir) / CODEX_ROLLOUT_DIR
        copied = 0
        budget = CODEX_ROLLOUT_MAX_MB * 1024 * 1024
        used = sum(f.stat().st_size for f in target_root.rglob("*") if f.is_file()) if target_root.is_dir() else 0
        for path in sorted(sessions.rglob("rollout-*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since:
                continue
            if used + stat.st_size > budget:
                if not _ROLLOUT_LIMIT_WARNED:
                    _ROLLOUT_LIMIT_WARNED = True
                    print(
                        f"[codex] WARNING: rollout copies stopped at {CODEX_ROLLOUT_MAX_MB:.0f}MB "
                        "(set FEATURE_FLAGS['codex_rollouts']=False to opt out)",
                        flush=True,
                    )
                break
            target = target_root / path.relative_to(sessions)
            try:
                if target.is_file() and target.stat().st_size == stat.st_size:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            except OSError:
                continue
            used += stat.st_size
            copied += 1
        return copied
    except Exception as exc:  # noqa: BLE001 - an audit copy must never break a run
        print(f"[codex] WARNING: rollout copy failed: {exc}", flush=True)
        return 0


_RESPONSES_SHIM: Any = None


def _ensure_responses_shim() -> Any:
    """起一次 Responses→chat 转换层（进程内单例；见 responses_shim.py 顶部注释）。

    决赛网关的 /v1/responses 是 chat 协议别名，codex 0.154.0 只会说 responses——
    没有这层桥，模型调用必 422。启动失败只告警并保持直连（决不因此中断 run）。
    """
    global _RESPONSES_SHIM
    if not flag("responses_shim"):
        return None
    if _RESPONSES_SHIM is None:
        try:
            route_root = str(Path(__file__).resolve().parent)
            if route_root not in sys.path:
                sys.path.insert(0, route_root)
            from responses_shim import start_responses_shim_from_env

            _RESPONSES_SHIM = start_responses_shim_from_env() or False
        except Exception as exc:  # noqa: BLE001 - 桥起不来也要继续跑（回退直连）
            print(f"[codex] WARNING: responses->chat shim unavailable: {exc}", flush=True)
            _RESPONSES_SHIM = False
    return _RESPONSES_SHIM or None


def _codex_config(CodexConfig: Any) -> Any:
    env = os.environ.copy()
    overrides: list[str] = []
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        overrides.append(f"openai_base_url={json.dumps(base_url)}")
        # Route model calls through a custom provider whose name is NOT "OpenAI"
        # so the Codex runtime never selects remote compaction v2: the ARC-Bench
        # gateway / DeepSeek backend does not implement OpenAI's native
        # compaction protocol, which fatals with "remote compaction v2 expected
        # exactly one compaction output item". Compaction then falls back to the
        # local summarization path, which the gateway serves like any model call.
        overrides.append("model_provider=arcbench")
        overrides.append(f"model_providers.arcbench.name={json.dumps('arc-bench-gateway')}")
        shim = _ensure_responses_shim()
        provider_base_url = shim.base_url if shim is not None else base_url
        overrides.append(f"model_providers.arcbench.base_url={json.dumps(provider_base_url)}")
        overrides.append(f"model_providers.arcbench.env_key={json.dumps('OPENAI_API_KEY')}")
        overrides.append(f"model_providers.arcbench.wire_api={json.dumps('responses')}")
        if shim is not None:
            print(
                f"[codex] responses->chat shim active: codex -> {provider_base_url} -> {base_url}",
                flush=True,
            )
        print(
            f"[codex] OPENAI_BASE_URL override active ({len(overrides)} config override(s))",
            flush=True,
        )
        _verbose(f"config overrides: {overrides}")
    else:
        print("[codex] OPENAI_BASE_URL unset; no config overrides", flush=True)
    return CodexConfig(env=env, config_overrides=tuple(overrides))


def _developer_instructions(
    skills_dir: Path,
    include_frontend_spec: bool = True,
    include_telemetry: bool = True,
) -> str:
    lines = [
        "You implement an ARC-Bench application in the current working directory.",
        "The directory already contains an initialized starter application. Preserve existing work — but where existing behavior conflicts with the contract or the requirement you are implementing, change the existing code: the contract wins.",
        "",
        "Each request supplies exactly one direct child subtree of ROOT. Implement that subtree,",
        "including all of its descendants, in the current project. Do not ask for or read the",
        "complete requirements.yaml. Make practical code changes and leave the application runnable.",
        "",
        "Requirement-named accounts must work end-to-end: seed their credentials (or a",
        "registration path) so sign-in succeeds, and show the signed-in username as visible",
        "page text. Requirement-named entities must be visible and clickable at every hop",
        "of the path that reaches them — the intermediate listing/browse page included —",
        "not only on their own details page.",
    ]
    if include_telemetry:
        lines += [
            "",
            f"ARC-Bench skills are available in {skills_dir}:",
            "- Read arcbench-runtime-signals/SKILL.md to report module progress when useful.",
            "- Read arcbench-traceability/SKILL.md when recording generated interfaces or tests.",
            "- Read arcbench-checkpoint/SKILL.md when creating a coherent git checkpoint.",
            "Run their scripts from the current project with --project-dir .; do not hand-write",
            "runner events or traceability JSON when a bundled script covers the action.",
        ]
    if include_frontend_spec:
        lines += [
            "",
            "- Read arcbench-frontend-spec/SKILL.md before implementing frontend interaction code",
            "(buttons, menus, dialogs, notifications, hover behavior, forms).",
        ]
    if flag("template_kit"):
        lines += [
            "",
            "- The project ships generic UI primitives under frontend/src/components",
            "(EntityCard, NamedForm, DataTable, RouteShell). They are task-agnostic helpers for",
            "accessible names/roles and are NOT a page or feature: reuse, adapt or ignore them.",
        ]
    return "\n".join(lines).strip()


# Full 11-item assertable-contract checklist (used verbatim by L0 and L1).
# L0 appends the per-item evidence dump; L1 compresses the verdicts and adds
# execution constraints. Kept separate from the header so both can share it.
_FULL_CONTRACT_ITEMS = """\
1. Every named fixture entity this subtree mentions (e.g. a note titled
   "Study schedule", a book named "book_8_2", a shelf named "Shelf 4.1",
   a user account, a product named "Hummingbird detail t-shirt") MUST exist
   verbatim in the backend seed data, and be reachable on the initial screen
   OR on a page one click from a primary navigation link. On ANY listing page
   (products, books, shelves, posts, orders), a named fixture MUST render on
   the FIRST PAGE of that listing: list items must be locatable on the first
   page of a listing — an entity that only appears after clicking page 2+ is
   unreachable from the initial listing. Adjust seed order / page size so
   every named entity lands on page 1; the gate probe `fixtures` blocks on
   "only visible after pagination". Any
   display name the requirement does NOT quote
   (user first/last name, alias, custom title) MUST be keyword-neutral:
   no action verb or field word root (delete/remove/edit/rename/create/
   update/save/cart/name/address/search/view/...) — these names render
   in global chrome (header full-name link, sidebar, breadcrumb) where
   substring locators collide with them — see
   arcbench-frontend-spec/SKILL.md rule G.
   Every named ACCOUNT must work end-to-end: seed its credentials (or a
   registration path) so sign-in succeeds, and the username MUST render as
   visible text once signed in. "Reachable" means visible clickable text at
   EVERY hop of the path: an entity that is only visible on its own details
   page but not in the intermediate listing/browse step that links to it is
   unreachable from a click path — render the entity's name on each
   intermediate hop too.
   Self-tests open every fixture through the SAME chain the requirement
   describes (search → result link → the entity's page → sub-tab → row),
   starting from the home page — a direct URL goto proves the page
   renders, never that the navigation chain works.
2. Every data-entry <form> has an accessible name
   (aria-label="Login form" style); a logo/home link has "logo" in its
   accessible name; list items expose entity names in their accessible
   name.
3. Every state-changing action shows assertable feedback: create/save
   navigates to the created entity's details page; delete/archive shows
   wording matching the action. A primary dialog/form action
   (Apply/Save/Create/...) MUST NOT be rendered disabled as a
   pre-validation gate: a scenario that clicks it and expects the quoted
   error message must be able to click it; with invalid input the click
   lands, the quoted error message appears, and the previous result stays
   unchanged. Validate on submit, never by disabling the control.
4. The authenticated homepage renders the state-widget sections the
   requirements name (recent drafts / recently viewed / favourites /
   recently updated).
5. No global control, data-entry <form>, toolbar/group/dialog/aside or
   list-row wrapper (li/div) carries a form-field word root or a page
   entity name in its accessible name. A form's own aria-label must share
   NO WORD with any field label inside it ("Catalog lookup" / "Recipient
   information" are fine; "Address form" AND "New address details" both
   still shadow the "Address" field, because the field resolver takes the
   first getByLabel(/word/i) hit in document order and an ancestor always
   precedes its descendants, so fill() then fails with "Element is not an
   <input>"). A list-row wrapper must not repeat the row's entity name.
   The gate probe `label-shadowing` blocks on any hit; renaming that keeps
   the word does not clear it. The authoritative word-root list and
   container rules live in arcbench-frontend-spec/SKILL.md rule D2.
6. Every interactive control this subtree's scenario text quotes by name
    (e.g. "Take a note", "Settings", "Undo", "Archive", "More options",
    "Pin note", "Note editor", "Search") has an accessible name EXACTLY
    equal to that quoted name — no ellipsis, no ": detail" / "— detail"
    suffix, no extra " menu"/" button"/" note" suffix, no decorative
    prefix. Decoration belongs in visible text/placeholder/title, never
    in aria-label; a visible-text button may pair exact text with an
    aria-label (visible "Take a note..." + aria-label="Take a note").
    Screenshot visible text is NOT authoritative — the scenario-quoted
    name is (see arcbench-frontend-spec/SKILL.md rule B). The same verbatim
    rule covers user-visible TEXT the requirement quotes: error/status/
    empty-state messages render byte-for-byte as visible text — "between
    <min> and <max>" and "from <min> to <max>" are different template
    wordings (substitute the actual values, keep the surrounding words).
    When a record "shows <detail text> and provides a link/button named
    <name>", the detail text MUST be nested INSIDE that control (a scoped
    text lookup matches descendants only) while the control keeps its
    exact accessible name via aria-label. A fixture string targeted by an
    UNSCOPED exact-text lookup (typically the signed-in username, or an
    entity name repeated in a widget) MUST render as its own exact text
    exactly once per page: chrome + content duplication resolves the
    lookup to 2 elements.
    A dialog the requirement describes as CONTAINING a named control
    renders that control in EVERY state the dialog can be in (view / edit
    / existing record), with the record's text as the control's value — a
    "view mode" that swaps the named text box for static text is a broken
    dialog. A state indicator renders exactly once per state word family:
    when the requirement gives alternate wordings ("<A>" or an "<B>
    marker"), one canonical indicator carries the state, and a transient
    confirmation message must not reuse the same words.
    A dialog opened from a quoted launcher is itself named exactly after
    that launcher label (never a summary of its contents — see SKILL.md
    rule D2). Indicator glyphs (note dot, caret) contribute no textContent
    to their container. A rejected submission value (duplicate tag/name)
    renders as its own exact-text element — count 1, never nested inside
    the message element. Copy for an absence requirement ("no secrets /
    tokens") never contains the forbidden noun itself. Counts render as a
    "<n> <noun>" text template (1 reaction / 0 reactions) as their own
    element, and a toggle that zeroes a count keeps the zero state visible.
    Metadata lines render as "Label: value" (label wording + colon + exact
    value). See SKILL.md rules D2/F.
7. Control roles match standard accessibility semantics: navigation
    entries, action list items and primary CTAs a user may click are
    <button> (or explicit role="button"), not bare links (entity records
    on a listing page are the exception — they carry the link role, see
    below); selectable dialog options
    (labels/tags/flags) are native checkboxes or groups, not
    button-simulated toggles; an input the scenario calls a textbox is
    type="text" (type="search" resolves to role searchbox, which does not
    match getByRole('textbox')) — see arcbench-frontend-spec/SKILL.md rules
    A and D1. A selector control's family follows the requirement's own
    interaction description — never a single noun: a described workflow
    that OPENS a visible selector and selects a named choice through its
    visible list (names a textbox to filter/type into, says "clicks the
    exact '<name>' option", or describes clicking entries of a list the
    flow opens) renders the ARIA option contract — trigger role="combobox" +
    aria-haspopup="listbox" + aria-expanded, popup role="listbox", items
    role="option" carrying the exact quoted names; a requirement that
    merely names the chosen value through the control ("choose a '<Field>'
    option named '<Value>'", "chooses '<Value>' in '<Field>'") is NOT an
    interaction — the bare word "option" is a value noun, never a family
    signal; a description that is SILENT about the control type, calls it
    merely a "combobox"/"field", or says only "selects <value>" renders a
    native <select> carrying the exact aria-label and the exact quoted
    option texts (its implicit role IS combobox, and it stays drivable by
    both a role query and a selectOption-style API). Self-tests mirror the
    requirement's described
    interaction — option-role clicks for a described list flow, the
    selection API for a plain "selects <value>" flow. On a listing/home
    page the bare `link` role belongs to the listed entity records (their
    accessible name is the entity name) and to nothing else — chrome,
    navigation and action controls keep button semantics, so an unscoped
    link-role query resolves to the listed records — unless the
    requirement text itself calls a chrome element a link. See SKILL.md
    rules D4/D5. Global accessible-name conflict detection (generic rule):
    global naming must not duplicate page-action names. A chrome entry may
    lose its button role ONLY when it duplicates a page action's name on the
    same page AND a global button query would hit the chrome entry first —
    e.g. a header "Sign in" as a plain link on /login so the login form's
    "SIGN IN" submit is the only button with that name. A navigation entry
    that lives inside <nav>/<aside>/role="complementary" is NEVER a candidate
    for that demotion: landmark-scoped button queries require the entry to
    keep its button role. Prefer making the page action win document order,
    or giving it its own requirement qualifier. Gate probes: `name-shadow`
    (exact chrome-button ↔ page-action collisions) and `landmark-roles`
    (blocks a demoted landmark entry).
    Every aria-haspopup popup carries the matching container role
    (role="menu" for a menu trigger, role="listbox" for a listbox trigger),
    and entries inside keep the role the requirement gives them. Every
    modal provides a named "Close" exit besides confirm/cancel. In-cell
    overlay chips must let double-click bubble to the cell's edit handler
    (a center-point locator.dblclick() must reach the inline editor).
    Global chrome (the top search control) stays mounted on EVERY route
    under its exact name — exactly one element with the global name per
    page; a page-scoped filter must not reuse it. Where existing code
    conflicts (a route that hides or replaces the global control), change
    the existing code: keep the global control mounted and give the
    page-scoped control a distinct name — "preserve existing work" never
    outranks this contract.
    Every command the requirement places in a menu renders as an entry
    whenever that menu is open — its presence is never conditioned on
    runtime state, and the dialog it opens renders unconditionally (a save
    flow must not dead-end on missing client state; fall back to the
    server-side current entity or surface the quoted message).
    A state-changing action
    keeps its target present in its new state (visible after reload, never
    a transient toast). A detail page's heading names the entity by the
    identifier the requirement quotes when opening it. See SKILL.md rules
    D1/D2/D4/D6/D7.
8. When the scenario expresses an action through a keyboard shortcut —
   copy/cut/paste (Ctrl/Cmd+C/X/V) — implement the real clipboard path:
   onCopy/onCut/onPaste handlers with a clipboardData text round-trip
   (the selected range serializes to plain text and a paste inserts it at
   the target), so the shortcut works through actual key events.
   Self-tests press the keys (page.keyboard.press('Control+V')) —
   invoking a handler directly proves nothing about the keyboard path.
9. Hover-revealed action rows stay in the accessibility tree: revealed
    with opacity only, never visibility/display-hidden (see
    arcbench-frontend-spec/SKILL.md rule A).
10. Every "Seed data: ..." clause in this subtree's requirement text is a
   STATE contract, not just an existence list. Reproduce each qualifier
   verbatim in the backend seed data: "that is not pinned" -> the note
   is seeded WITHOUT pinned:true; "with the \"Work\" label" / "without
   the \"Work\" label" -> the note's labels include/exclude it; "with a
   white background" -> color stays default/white; "active"/"archived"
   -> the note starts unarchived/archived; "default label X" -> a label
   row named X exists. Do not flip or omit a state qualifier (a seed
   that pins a note the requirement says is not pinned fails the gate).
   When listing seed evidence, state the QUALIFIER outcome too, e.g.
   '"Meeting agenda 2.8.2" -> pinned=false (requirement: not pinned)'.
   In multi-row history/feed/log seeds, an invented row must not sort
   AHEAD of a row for an entity the requirement names in the list's
   default order (a first()/latest-style lookup then reads the invented
   row): order seeded rows so the requirement-named entity's newest event
   is the newest of its kind. See SKILL.md rule G.
11. Seed initial state is derived from the requirement document's GIVEN/WHEN
   steps (arrange-act): a state-changing action — delete/remove/trash,
   archive, pin, favorite — requires its target to START in the pre-action
   state: a "deletable" note must be on the main list (NOT pre-trashed), a
   book whose scenario clicks "Favorite" must NOT be pre-favorited, an
   "archived" action needs an active target. Never pre-set an end-state just
   to fill a view. When the scenario's WHEN step acts on the entity first
   ("clicks Delete" -> target starts present), the seed must provide that
   pre-action state; the gate blocks on pre-set end-states."""

# Execution constraints for the L1 family (A/B/C differ ONLY in the test
# strategy — everything else is identical, so they form a clean controlled
# experiment). Ordered by strength, A (most conservative) -> C (most
# aggressive). L0 does NOT get these so the baseline keeps the original
# behavior byte-for-byte.
#
# L1A: keep the test suite (and the full checklist), cut only the
# cheap things — no server, no whole-project re-browsing, one-line summary.
# Most conservative: closest to L0 in engineering safety.
_BEHAVIOR_CONSTRAINT_A = """\
The codebase is already substantially complete. Implement this module with
the minimal diff: do not restructure existing code, do not add unrelated
features, and do not start a long-running server. Read only the files this
module needs; do not re-browse the whole project."""

# L1B: forbid the full test suite but ALLOW one build / type-check pass so
# compile errors are still caught cheaply (recommended middle ground).
_BEHAVIOR_CONSTRAINT_B = """\
The codebase is already substantially complete. Implement this module in ONE
pass with the minimal diff: do not restructure existing code, do not add
unrelated features, and do not start a long-running server. Do not run the
full test suite, but you MAY run a single build or type-check (e.g.
npm run build / tsc) to catch compile errors before finishing. Read only the
files this module needs; do not re-browse the whole project."""

# L1C: forbid running the test suite entirely (max token saving, most risk).
# Most aggressive of the L1 family.
_BEHAVIOR_CONSTRAINT_C = """\
The codebase is already substantially complete. Implement this module in ONE
pass with the minimal diff: do not restructure existing code, do not add
unrelated features, do not run the test suite, and do not start a long-running
server. Read only the files this module needs; do not re-browse the whole
project."""

# L2 compact contract: the four iron rules behind the strict-locator failure
# classes (CONTRACT.md), plus codebase-convention guidance to replace the
# verbose checklist. Verify silently; report only deviations.
_COMPACT_CONTRACT = """\
Data contract (verify each silently; report only deviations):
1. Accessible names quoted in the scenario text match EXACTLY — no ellipsis,
   no "menu" / ": detail" / "— detail" suffix, no decorative prefix.
   Screenshot visible text is NOT authoritative; the scenario-quoted name is.
2. Roles: clickable CTAs are <button> (or explicit role="button"), not bare
   links; selectable dialog options are native checkboxes or groups; a
   scenario "textbox" input is type="text", not type="search".
3. Hover-revealed action rows stay in the accessibility tree: reveal with
   opacity only, never visibility/display-hidden.
4. Every "Seed data: ..." clause is a STATE contract: reproduce each qualifier
   verbatim (pinned / not pinned, labeled / unlabeled, archived / active,
   default color); do not flip or omit a qualifier.
5. Seed initial state follows the requirement's WHEN step: a note the
   scenario DELETES first must start on the main list (not pre-trashed); a
   book the scenario clicks "Favorite" on must start unfavorited. Never
   pre-set an end-state to fill a view.
For form aria-labels, logo naming, feedback wording and homepage widgets,
follow the existing patterns already established in this codebase.
Finish with a one-line summary of files changed."""

_CONTRACT_HEADER = (
    "Data contract (assertable-contract checklist — verify each item before\n"
    "finishing; the post-implementation gate re-checks these automatically):\n"
)

# S1b: the checklist above is re-sent on every turn of every module thread. When
# externalised it is written once to the artifact and the prompt keeps only a
# pointer, saving ~6.4k chars per module (L0). The file is route-generic: it
# carries no task-specific content (no fixture names, no seed, no implementation),
# which keeps it inside the competition rule for pre-placed scaffolding.
CONTRACT_FILE = ".codex/ARCBENCH_CONTRACT.md"
_CONTRACT_FILE_READY = False

_CONTRACT_POINTER = (
    f"The assertable-contract checklist is at `{CONTRACT_FILE}` in the current project\n"
    "(route-generic rules only — no task-specific content). Read it before implementing\n"
    "frontend interaction code.\n"
)

_FINISH_L0 = (
    "\nBefore finishing, list each fixture entity -> seed evidence "
    "(file + row)\n-> reachable page, plus the outcome of each "
    "checklist item above.\n\n"
    "Finish by summarizing the files changed. Do not start a "
    "long-running server."
)

_FINISH_L1 = (
    "\nBefore finishing, verify each checklist item silently and "
    "give its verdict in\none short line — no long evidence dump.\n\n"
    "Finish by summarizing the files changed in one line.\n\n"
)


def _inline_contract_section(effort: int) -> str:
    """The legacy contract + execution-constraint block (must stay byte-stable).

    L0:  full 11-item checklist + per-item evidence dump (original behavior).
    L1A: full checklist verbatim; test suite kept; verdicts one short line.
         Most conservative of the L1 family (closest to L0).
    L1B: same as L1A but one build/type-check allowed instead of the suite.
    L1C: same as L1A but test suite forbidden. Most aggressive of L1 family.
    L2:  checklist replaced by the four iron rules; L1C-style (most
         aggressive) constraints.
    """
    if effort == 0:
        return _CONTRACT_HEADER + _FULL_CONTRACT_ITEMS + _FINISH_L0
    if effort in (1, 2, 3):
        base = _CONTRACT_HEADER + _FULL_CONTRACT_ITEMS + _FINISH_L1
        if effort == 1:
            return base + _BEHAVIOR_CONSTRAINT_A
        if effort == 2:
            return base + _BEHAVIOR_CONSTRAINT_B
        return base + _BEHAVIOR_CONSTRAINT_C
    return _BEHAVIOR_CONSTRAINT_C + "\n\n" + _COMPACT_CONTRACT


def _external_contract_section(effort: int) -> str:
    """S1b shape: pointer instead of the checklist body."""
    if effort == 0:
        return _CONTRACT_HEADER + _CONTRACT_POINTER + _FINISH_L0
    if effort in (1, 2, 3):
        base = _CONTRACT_HEADER + _CONTRACT_POINTER + "\n" + _FINISH_L1
        if effort == 1:
            return base + _BEHAVIOR_CONSTRAINT_A
        if effort == 2:
            return base + _BEHAVIOR_CONSTRAINT_B
        return base + _BEHAVIOR_CONSTRAINT_C
    return _BEHAVIOR_CONSTRAINT_C + "\n\n" + _CONTRACT_HEADER + _CONTRACT_POINTER


def _contract_section(effort: int) -> str:
    """Contract block for the module prompt (inline by default)."""
    if flag("contract_inline") or not _CONTRACT_FILE_READY:
        return _inline_contract_section(effort)
    return _external_contract_section(effort)


def _write_contract_file(output_dir: Path, effort: int) -> Path:
    """S1b: write the route-generic checklist once, so it is read, not re-sent."""
    global _CONTRACT_FILE_READY
    body = _COMPACT_CONTRACT if effort == 4 else _FULL_CONTRACT_ITEMS
    target = output_dir / CONTRACT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# ARC-Bench route contract (generic, task-agnostic)\n\n"
        "Written by the Codex route (Codex/main.py, S1b). It holds the route's generic\n"
        "assertable-contract checklist - the same text otherwise delivered inline in every\n"
        "module prompt. It contains **no task-specific content**: no fixture names, no seed\n"
        "data, no page or component implementation, and nothing from the current task's\n"
        "requirements. Read it before implementing frontend interaction code; the\n"
        "post-implementation gate re-checks these items automatically.\n\n---\n\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    _CONTRACT_FILE_READY = True
    return target


def _module_prompt(
    module: RequirementModule,
    requirements_dir: Path,
    completed_ids: list[str],
    skills_dir: Path,
    visual_block: str = "",
) -> str:
    completed = ", ".join(completed_ids) if completed_ids else "none"
    effort = effort_level()
    compact = flag("prompt_compact")
    subtree_json = json.dumps(
        module.subtree,
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )
    prompt = textwrap.dedent(
        f"""
        Implement ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}

        Requirement source directory: {requirements_dir}
        Previously completed ROOT modules: {completed}
        Skills directory: {skills_dir}

        Implement this complete subtree in the current target directory:
        ```json
        {subtree_json}
        ```
        """
    ).strip()
    prompt += "\n\n" + _contract_section(effort)
    if visual_block:
        prompt += "\n" + visual_block
    return prompt


_COMPACTION_FAILURE_MARKERS = ("remote compact", "compaction")

# 网关把上游瞬时故障包成 HTTP 400 + code=proxy_error（如 "connection reset by peer"）；
# codex 视 400 为不可重试、直接判 turn 失败。命中这些标记时等待恢复后以新线程重跑模块
# （覆盖 shim 重试预算耗尽、以及响应已开流后的失败——那类 shim 无法安全重试）。
# 标记口径与 responses_shim._PROXY_TRANSIENT_MARKERS / self-agent/engine/thin.py 同源。
_TRANSIENT_GATEWAY_MARKERS = (
    "proxy_error",
    "read tcp",
    "write tcp",
    "connection reset",
    "i/o timeout",
    "upstream",
)
_TRANSIENT_RETRY_WAITS = (30.0, 60.0)  # 模块级瞬时重试前等待（第 2/3 次尝试前）


def _is_transient_gateway_error(message: str) -> bool:
    text = message.lower()
    return any(marker in text for marker in _TRANSIENT_GATEWAY_MARKERS)


def _wait_before_transient_retry(
    module_id: str, message: str, attempt: int, max_attempts: int
) -> None:
    wait = _TRANSIENT_RETRY_WAITS[min(attempt - 1, len(_TRANSIENT_RETRY_WAITS) - 1)]
    print(
        f"[codex] transient gateway error on {module_id}; waiting {int(wait)}s for "
        f"recovery, then retrying with a fresh thread ({attempt}/{max_attempts}): {message}",
        flush=True,
    )
    time.sleep(wait)


def _load_completed(progress_file: Path) -> list[str]:
    """Load the on-disk list of completed module ids for resumability."""
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _verbose(f"no progress file at {progress_file}; starting fresh")
        return []
    except (OSError, ValueError) as exc:
        print(
            f"[codex] WARNING: could not read progress file {progress_file}: "
            f"{exc}; starting fresh",
            flush=True,
        )
        return []
    ids = data.get("completed_ids") if isinstance(data, dict) else None
    result = [str(item) for item in ids] if isinstance(ids, list) else []
    _verbose(f"progress file {progress_file}: {len(result)} completed id(s) loaded")
    return result


def _save_completed(progress_file: Path, completed_ids: list[str]) -> None:
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        progress_file.write_text(
            json.dumps({"completed_ids": completed_ids}, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(
            f"[codex] ERROR: failed to write progress file {progress_file}: {exc}",
            flush=True,
        )
        raise
    _verbose(f"progress saved ({len(completed_ids)} completed) -> {progress_file}")


def _disable_streaming(reason: str) -> None:
    """Stop attempting per-call capture after the first failure."""
    global _STREAMING_DISABLED
    if not _STREAMING_DISABLED:
        _STREAMING_DISABLED = True
        print(
            f"[codex] WARNING: per-call token capture disabled ({reason}); "
            "recording per-turn totals only",
            flush=True,
        )


def _record_usage_notification(
    output_dir: Path,
    event: Any,
    *,
    phase: str,
    module_id: str,
    module_index: int,
    attempt: int,
    model: str | None,
) -> None:
    """Record one model response's usage from a turn-stream notification."""
    payload = getattr(event, "payload", None)
    usage = getattr(payload, "token_usage", None)
    if usage is None:
        return
    per_call = token_usage.breakdown_to_dict(getattr(usage, "last", None))
    if not per_call:
        return
    token_usage.append(
        output_dir,
        {
            "phase": phase,
            "kind": "model_call",
            "module_id": module_id,
            "module_index": module_index,
            "attempt": attempt,
            "model": model or "",
            "effort": effort_label(),
            "thread_id": str(getattr(payload, "thread_id", "") or ""),
            "turn_id": str(getattr(payload, "turn_id", "") or ""),
            "context_window": getattr(usage, "model_context_window", None),
            **per_call,
        },
    )


def _record_turn_total(
    output_dir: Path,
    result: Any,
    *,
    phase: str,
    module_id: str,
    module_index: int,
    attempt: int,
    model: str | None,
) -> None:
    """Record the turn's cumulative usage (also the plain-run fallback)."""
    usage = getattr(result, "usage", None)
    totals = token_usage.breakdown_to_dict(getattr(usage, "total", None))
    if not totals:
        return
    token_usage.append(
        output_dir,
        {
            "phase": phase,
            "kind": "turn_total",
            "module_id": module_id,
            "module_index": module_index,
            "attempt": attempt,
            "model": model or "",
            "effort": effort_label(),
            "turn_id": str(getattr(result, "id", "") or ""),
            "context_window": getattr(usage, "model_context_window", None),
            **totals,
        },
    )


def _run_turn(
    thread: Any,
    prompt: str,
    *,
    output_dir: Path,
    phase: str,
    module_id: str,
    module_index: int,
    attempt: int,
    model: str | None,
) -> Any:
    """Run exactly one turn, recording token usage at the finest available grain.

    Preferred path: ``thread.turn()`` + ``handle.stream()``, which emits one
    token-usage notification per model response (input / cached / cache-write /
    output / reasoning, attributed to the turn). When the SDK does not expose
    that surface, fall back to the blocking ``thread.run()``, which still records
    the turn's cumulative totals.
    """
    meta = {
        "phase": phase,
        "module_id": module_id,
        "module_index": module_index,
        "attempt": attempt,
        "model": model,
    }
    if streaming_available(thread):
        handle = thread.turn(prompt)
        turn_id = getattr(handle, "id", None)
        if turn_id:
            def relay() -> Any:
                for event in handle.stream():
                    try:
                        _record_usage_notification(output_dir, event, **meta)
                    except Exception as exc:  # noqa: BLE001 - capture is best effort
                        print(f"[codex] WARNING: usage capture failed: {exc}", flush=True)
                    yield event

            try:
                result = _sdk_collect_turn_result(relay(), turn_id=turn_id)
            except Exception as exc:  # noqa: BLE001 - keep the run's failure semantics
                _disable_streaming(f"stream failed: {type(exc).__name__}: {exc}")
                raise
            _record_turn_total(output_dir, result, **meta)
            return result
        _disable_streaming("turn handle exposes no id")
    result = thread.run(prompt)
    _record_turn_total(output_dir, result, **meta)
    return result


def _src_snapshot(output_dir: Path) -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) of every file under the two source trees.

    External evidence that a module turn actually wrote code: a turn that only
    reads files leaves this snapshot identical, no matter what it reported.
    """
    snapshot: dict[str, tuple[int, int]] = {}
    for relative_root in ("frontend/src", "backend/src"):
        root = output_dir / relative_root
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                snapshot[path.relative_to(output_dir).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _snapshot_changed(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> bool:
    """True when any source file appeared, disappeared, or was rewritten."""
    return before != after


def _continuation_prompt(module: RequirementModule, round_index: int) -> str:
    """Prompt for a continuation turn on the same thread (K1)."""
    return textwrap.dedent(
        f"""
        Continuation round {round_index}: your previous turn changed NO file under
        frontend/src or backend/src, so module {module.node_id} - {module.name} is not
        implemented yet. Reading code and describing a plan is not implementation.

        Implement this module NOW, in this turn: write the files, wire the routes and
        components, and add the seed data its acceptance points require. Do not spend
        the turn on further exploration.
        """
    ).strip()


def _continue_until_files_change(
    thread: Any,
    result: Any,
    before: dict[str, tuple[int, int]],
    *,
    output_dir: Path,
    module: RequirementModule,
    attempt: int,
    model: str | None,
) -> Any:
    """Reopen the turn until the module leaves file evidence (K1).

    A Codex turn ends as soon as the model answers with text and no tool call, so a
    "plan-only" turn looks like a clean success from here: main.py used to record it
    as a completed module while nothing was written. The file snapshot is the only
    judgement that does not depend on the model's own report, so an unimplemented
    module is reopened on the same thread (its context is already loaded) instead of
    being signed off. After MODULE_CONTINUATION_MAX rounds with no change the module
    is still released - with a warning - because a module that genuinely needs no
    code change is legal, and failing the whole run for it would be worse.
    """
    for round_index in range(1, MODULE_CONTINUATION_MAX + 1):
        if _snapshot_changed(before, _src_snapshot(output_dir)):
            return result
        print(
            f"[codex] {module.node_id}: turn ended with ZERO source file changes; "
            f"continuation round {round_index}/{MODULE_CONTINUATION_MAX}",
            flush=True,
        )
        try:
            continued = _run_turn(
                thread,
                _continuation_prompt(module, round_index),
                output_dir=output_dir,
                phase="module_continuation",
                module_id=module.node_id,
                module_index=module.index,
                attempt=attempt,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - continuation must never be fatal
            print(
                f"[codex] WARNING: continuation round {round_index} failed on "
                f"{module.node_id}: {type(exc).__name__}: {exc}; shipping the module as-is",
                flush=True,
            )
            return result
        if getattr(continued, "error", None) is not None:
            print(
                f"[codex] WARNING: continuation round {round_index} returned an error on "
                f"{module.node_id}: {continued.error}; shipping the module as-is",
                flush=True,
            )
            return result
        result = continued
    if not _snapshot_changed(before, _src_snapshot(output_dir)):
        print(
            f"[codex] WARNING: module {module.node_id} produced no file change after "
            f"{MODULE_CONTINUATION_MAX} continuation round(s); accepting it as complete",
            flush=True,
        )
    return result


def _run_module_with_retry(
    codex: Codex,
    output_dir: Path,
    module: RequirementModule,
    requirements_dir: Path,
    completed_ids: list[str],
    skills_dir: Path,
    model: str | None,
    max_attempts: int = 3,
    include_frontend_spec: bool = True,
    include_telemetry: bool = True,
    visual_block: str = "",
) -> Any:
    """Implement one module on a fresh thread.

    A fresh thread per module keeps every turn's context small, so the Codex
    runtime never auto-compacts across modules (remote compaction v2 is
    incompatible with the ARC-Bench gateway / DeepSeek backend). If the runtime
    still fatals on compaction, retry the module on a brand-new thread.
    Transient gateway failures (proxy_error 400s / connection resets) wait for
    recovery and retry the same way instead of killing the whole run.
    """
    for attempt in range(1, max_attempts + 1):
        _verbose(
            f"module {module.node_id}: starting attempt {attempt}/{max_attempts} "
            f"(model={model}, cwd={output_dir})"
        )
        thread = codex.thread_start(
            cwd=str(output_dir),
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            model=model,
            developer_instructions=_developer_instructions(
                skills_dir, include_frontend_spec, include_telemetry
            ),
        )
        prompt = _module_prompt(module, requirements_dir, completed_ids, skills_dir, visual_block)
        _verbose(f"module {module.node_id}: prompt is {len(prompt)} chars")
        source_before = _src_snapshot(output_dir)
        try:
            result = _run_turn(
                thread,
                prompt,
                output_dir=output_dir,
                phase="module",
                module_id=module.node_id,
                module_index=module.index,
                attempt=attempt,
                model=model,
            )
        except RuntimeError as exc:
            message = str(exc)
            if _is_transient_gateway_error(message) and attempt < max_attempts:
                _wait_before_transient_retry(module.node_id, message, attempt, max_attempts)
                continue
            if any(marker in message for marker in _COMPACTION_FAILURE_MARKERS) and attempt < max_attempts:
                print(
                    f"[codex] compaction failed on {module.node_id}; retrying with a "
                    f"fresh thread ({attempt}/{max_attempts}): {message}",
                    flush=True,
                )
                continue
            print(
                f"[codex] ERROR: RuntimeError on {module.node_id} "
                f"(attempt {attempt}/{max_attempts}): {message}",
                flush=True,
            )
            raise
        if getattr(result, "error", None) is not None:
            message = str(result.error)
            if _is_transient_gateway_error(message) and attempt < max_attempts:
                _wait_before_transient_retry(module.node_id, message, attempt, max_attempts)
                continue
            print(
                f"[codex] ERROR: Codex failed on {module.node_id}: {result.error}",
                flush=True,
            )
            raise RuntimeError(f"Codex failed on {module.node_id}: {result.error}")
        _verbose(f"module {module.node_id}: attempt {attempt} returned successfully")
        if flag("module_continuation"):
            result = _continue_until_files_change(
                thread,
                result,
                source_before,
                output_dir=output_dir,
                module=module,
                attempt=attempt,
                model=model,
            )
        print(
            "[codex][usage] "
            + token_usage.format_row(
                f"module {module.node_id}",
                token_usage.scope(output_dir, phase="module", module_id=module.node_id),
            ),
            flush=True,
        )
        return result
    print(
        f"[codex] ERROR: Codex failed on {module.node_id} after {max_attempts} attempts",
        flush=True,
    )
    raise RuntimeError(f"Codex failed on {module.node_id} after {max_attempts} attempts")


def implement_modules(
    requirements_dir: Path,
    output_dir: Path,
    modules: list[RequirementModule],
    skills_dir: Path,
    include_frontend_spec: bool = True,
    include_telemetry: bool = True,
) -> None:
    model = os.environ.get("MODEL", "").strip() or None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    wall_started = time.time()
    print(f"[codex] model: {model if model else '<unset>'}", flush=True)
    print(f"[codex] api key: {'set' if api_key else 'unset'}", flush=True)
    progress_file = output_dir / ".arc" / "completed_modules.json"

    completed_ids = _load_completed(progress_file)
    pending = [module for module in modules if module.node_id not in completed_ids]
    print(
        f"[codex] Resuming with {len(completed_ids)}/{len(modules)} completed; "
        f"{len(pending)} pending module(s).",
        flush=True,
    )
    _verbose(f"completed ids: {completed_ids}")
    _verbose(f"pending module ids: {[m.node_id for m in pending]}")

    with Codex(config=_codex_config(CodexConfig)) as codex:
        if api_key:
            print("[codex] logging in with OPENAI_API_KEY", flush=True)
            codex.login_api_key(api_key)
        for module in pending:
            print(f"[codex] Implementing {module.index}/{module.total}: {module.node_id}", flush=True)
            visual_block = ""
            if flag("visual"):
                visual_block = build_visual_block(
                    module.subtree,
                    requirements_dir,
                    output_dir,
                    verbose=flag("verbose_log"),
                )
            start = time.monotonic()
            _run_module_with_retry(
                codex,
                output_dir,
                module,
                requirements_dir,
                completed_ids,
                skills_dir,
                model,
                include_frontend_spec=include_frontend_spec,
                include_telemetry=include_telemetry,
                visual_block=visual_block,
            )
            elapsed = time.monotonic() - start
            completed_ids.append(module.node_id)
            _save_completed(progress_file, completed_ids)
            # Keep the CLI's per-call rollout trail close to the artifact, so a
            # timeout mid-run still leaves an audit trail for the modules done.
            copied = _collect_codex_rollouts(output_dir, since=wall_started)
            if copied:
                _verbose(f"copied {copied} codex rollout file(s) into {CODEX_ROLLOUT_DIR}")
            print(
                f"[codex] Completed {module.node_id} in {elapsed:.1f}s "
                f"({len(completed_ids)}/{len(modules)} done)",
                flush=True,
            )


def _write_contract_index(requirements_dir: Path, output_dir: Path) -> None:
    """S5: transcribe the requirement tree into an assertable-contract index.

    Zero-token and advisory-only: the index is written into the artifact under
    `.codex/` and is NOT pushed into any module prompt, so a failure here can
    never break a run. Opt-in via ARCBENCH_CONTRACT_INDEX=1; the default (off)
    keeps the legacy behaviour byte-identical (lossless degradation).

    The module is imported here, not at the top of this file, so that a missing or
    incompatible contract_index.py degrades to this warning instead of killing the
    run before a single module is implemented.
    """
    try:
        route_root = str(Path(__file__).resolve().parent)
        if route_root not in sys.path:
            sys.path.insert(0, route_root)
        import contract_index

        index = contract_index.build_index(requirements_dir)
        md_path, _json_path = contract_index.write_index(index, output_dir)
    except Exception as exc:  # noqa: BLE001 - never fatal
        print(f"[codex] WARNING: contract index failed ({exc}); continuing", flush=True)
        return
    summary = index.get("summary") or {}
    print(
        f"[codex][contracts] indexed {summary.get('leaves', 0)} ATOMIC leaf(ies), "
        f"{summary.get('controls', 0)} role+name control(s), {summary.get('names', 0)} name(s), "
        f"{summary.get('seed_entities', 0)} seed entit(ies) -> {md_path}",
        flush=True,
    )


def _main() -> int:
    started = time.monotonic()
    wall_started = time.time()
    args = parse_args()
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    print(f"[codex] task type: {args.task_type}", flush=True)
    print(f"[codex] requirement dir: {requirements_dir}", flush=True)
    print(f"[codex] output dir: {output_dir}", flush=True)
    if not requirements_dir.is_dir():
        print(
            f"[codex] ERROR: requirement directory not found: {requirements_dir}",
            flush=True,
        )
        raise FileNotFoundError(f"Requirement directory not found: {requirements_dir}")

    agent_root = Path(__file__).resolve().parent
    copy_template_contents(agent_root / "template", output_dir)
    include_frontend_spec = flag("frontend_spec")
    include_telemetry = flag("telemetry")
    print(f"[codex] features: {feature_summary()}", flush=True)
    excluded_skills: set[str] = set()
    if not include_frontend_spec:
        excluded_skills.add(FRONTEND_SPEC_SKILL)
    if not include_telemetry:
        excluded_skills.update(TELEMETRY_SKILLS)
    skills_dir = copy_skills_to_output(agent_root / "skills", output_dir, exclude=excluded_skills)
    if flag("contract_index"):
        _write_contract_index(requirements_dir, output_dir)
    if not flag("contract_inline"):
        try:
            contract_path = _write_contract_file(output_dir, effort_level())
            print(
                f"[codex][contracts] externalised the route contract -> {contract_path}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to the inline checklist
            print(
                f"[codex] WARNING: could not externalise the route contract ({exc}); "
                "module prompts keep the inline checklist",
                flush=True,
            )
    modules = load_root_modules(requirements_dir)
    implement_modules(
        requirements_dir,
        output_dir,
        modules,
        skills_dir,
        include_frontend_spec=include_frontend_spec,
        include_telemetry=include_telemetry,
    )

    if flag("verify_gate"):
        report = _run_verification(requirements_dir, output_dir)
        gate = (report or {}).get("gate") or {}
        if gate.get("passed") is False:
            print(
                f"[codex][gate] HARD GATE FAILED with {len(gate.get('blocking') or [])} "
                "blocking defect(s); starting exactly one repair round",
                flush=True,
            )
            _repair_against_gate(
                requirements_dir,
                output_dir,
                skills_dir,
                report,
                include_frontend_spec=include_frontend_spec,
                include_telemetry=include_telemetry,
            )
            final = _run_verification(requirements_dir, output_dir)
            final_gate = (final or {}).get("gate") or {}
            if final_gate.get("passed") is True:
                print("[codex][gate] repair round cleared the gate", flush=True)
            else:
                print(
                    "[codex][gate] gate still failing after one repair round: "
                    f"{json.dumps(final_gate, ensure_ascii=False)}",
                    flush=True,
                )
        else:
            print(
                f"[codex][gate] gate verdict: {json.dumps(gate, ensure_ascii=False)}",
                flush=True,
            )
    else:
        print(
            "[codex] verification disabled "
            '(FEATURE_FLAGS["verify_gate"] = False)',
            flush=True,
        )

    copied = _collect_codex_rollouts(output_dir, since=wall_started)
    if copied:
        print(f"[codex][usage] copied {copied} codex rollout file(s) -> {CODEX_ROLLOUT_DIR}", flush=True)
    _report_token_usage(output_dir)
    print(
        f"[codex] finished {len(modules)} module(s) in {time.monotonic() - started:.1f}s; "
        f"output at {output_dir}",
        flush=True,
    )
    return 0


def _report_token_usage(output_dir: Path) -> None:
    """Print the per-phase token ledger summary (S0). Informational only."""
    try:
        records = token_usage.read_all(output_dir)
        if not records:
            print(
                "[codex][usage] no token records captured "
                f"(ledger: {token_usage.ledger_path(output_dir)})",
                flush=True,
            )
            return
        for phase in ("module", "module_continuation", "repair", "vision"):
            subset = [record for record in records if record.get("phase") == phase]
            if subset:
                print(
                    "[codex][usage] "
                    + token_usage.format_row(phase, token_usage.summarize(subset)),
                    flush=True,
                )
        print(
            "[codex][usage] "
            + token_usage.format_row("run total", token_usage.summarize(records)),
            flush=True,
        )
        print(f"[codex][usage] ledger: {token_usage.ledger_path(output_dir)}", flush=True)
    except Exception as exc:  # noqa: BLE001 - reporting must never break a run
        print(f"[codex] WARNING: token usage report failed: {exc}", flush=True)


def _run_verification(requirements_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Run the post-implementation verification (see verify/verify.py) and return its report.

    Verification itself never crashes the run: import or runtime failures
    degrade to a warning plus a minimal report. The report's `gate` field
    decides whether the hard gate triggers one repair round (see
    _repair_against_gate).
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from verify.verify import verify_application  # type: ignore[import-not-found]

        report = verify_application(
            requirements_dir,
            output_dir,
            contract_probes=flag("contract_probes"),
            install_browser=flag("verify_browser_install"),
            reconcile_database=flag("db_reconcile"),
        )
        print(
            f"[codex] verification report: {json.dumps(report, ensure_ascii=False)}",
            flush=True,
        )
        return report
    except Exception as exc:  # noqa: BLE001 - verification must never break the run
        print(
            f"[codex] WARNING: verification failed to run: {exc}",
            flush=True,
        )
        return {"error": str(exc)}


def _repair_against_gate(
    requirements_dir: Path,
    output_dir: Path,
    skills_dir: Path,
    report: dict[str, Any],
    include_frontend_spec: bool = True,
    include_telemetry: bool = True,
) -> bool:
    """Run exactly ONE repair round against a failing gate report.

    The gate's blocking defects (plus a few advisory hints) are fed back to a
    fresh Codex thread as the sole task; no retry is attempted, so the repair
    round costs at most one agent turn regardless of outcome.
    """
    gate = (report or {}).get("gate") or {}
    blocking = gate.get("blocking") or []
    advisory = gate.get("advisory") or []
    if not blocking:
        return False
    model = os.environ.get("MODEL", "").strip() or None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    bullets_b = "\n".join(f"- {line}" for line in blocking)
    bullets_a = "\n".join(f"- {line}" for line in advisory[:10]) if advisory else "(none)"
    prompt = textwrap.dedent(
        f"""
        The automated gate check on the generated application found the defects
        below. Fix ONLY these defects, in ONE pass, in the current project.
        Do not restructure the app, add unrelated features, or change behavior
        beyond what the defects require. For each defect make the minimal change,
        then report "defect -> file -> change".

        If a defect says an own test suite exited non-zero, the implementation is
        what must change: NEVER delete, skip, comment out, or weaken a test (or
        its assertions) to make the suite green, and never rewrite a test to
        assert something easier. If a test itself contradicts the requirement
        text, fix the implementation to satisfy the requirement and say so.

        BLOCKING DEFECTS:
        {bullets_b}

        ADVISORY (fix only if trivial):
        {bullets_a}

        Read arcbench-frontend-spec/SKILL.md before touching frontend
        interaction code. Do not start a long-running server. Finish with a
        short summary of the changes you made.
        """
    ).strip()
    print(
        f"[codex][gate] launching one repair round for {len(blocking)} blocking defect(s)",
        flush=True,
    )
    try:
        with Codex(config=_codex_config(CodexConfig)) as codex:
            if api_key:
                codex.login_api_key(api_key)
            thread = codex.thread_start(
                cwd=str(output_dir),
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
                model=model,
                developer_instructions=_developer_instructions(
                    skills_dir, include_frontend_spec, include_telemetry
                ),
            )
            result = _run_turn(
                thread,
                prompt,
                output_dir=output_dir,
                phase="repair",
                module_id="gate-repair",
                module_index=0,
                attempt=1,
                model=model,
            )
            if getattr(result, "error", None) is not None:
                print(f"[codex][gate] repair round errored: {result.error}", flush=True)
                return True
            print("[codex][gate] repair round finished", flush=True)
            print(
                "[codex][usage] "
                + token_usage.format_row(
                    "repair round",
                    token_usage.scope(output_dir, phase="repair", module_id="gate-repair"),
                ),
                flush=True,
            )
            return True
    except Exception as exc:  # noqa: BLE001 - repair must never break the run
        print(f"[codex][gate] repair round failed to run: {exc}", flush=True)
        return True


def main() -> int:
    report_retired_flag_env()
    try:
        return _main()
    except Exception:
        print("[codex] ERROR: fatal error", flush=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
