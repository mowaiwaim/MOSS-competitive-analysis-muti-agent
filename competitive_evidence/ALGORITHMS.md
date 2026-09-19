# CAPER 与 FIRM-ReAct：独立算法入口

这里提供两个可执行的 Python 模块。CAPER 在证据预算内选择可支持的比较结论，FIRM-ReAct 根据显式决策情形规划下一次取证。两者只使用标准库，不执行搜索、网页抓取或模型调用，目前尚未接入 MOSS 的在线工作流。

## 1. 从仓库根目录运行

```bash
python -X utf8 -m competitive_evidence.algorithms caper --input competitive_evidence/examples/caper_input.json --output caper_result.json
python -X utf8 -m competitive_evidence.algorithms firm-plan --input competitive_evidence/examples/firm_input.json --output firm_plan.json
python -X utf8 -m competitive_evidence.algorithms firm-observe --input observation.json --output firm_next.json
```

前两个输入文件是合成格式示例。第三个命令的 `observation.json` 需由调用方根据实际执行结果构建，不能把计划中的预期结果当成观察结果。

另有一个 A、B、C 产品月度成本选择的业务接口示例。它先声明候选均满足硬要求，以负月成本表示用户效用，再规划需要核查哪家产品的价格：

```bash
python -X utf8 -m competitive_evidence.algorithms firm-plan --input competitive_evidence/examples/firm_pricing_input.json --output firm_pricing_plan.json
```

这个业务样例使用虚构价格，仅演示输入、决策和取证接口，没有开展效果实验。`firm_input.json` 保留用于检查两条查询互补性的合成功能场景。

省略 `--output` 时，结果以 JSON 写入标准输出。输入接受 UTF-8 和 UTF-8 BOM，输出为 UTF-8，并保留中文。输入和输出不得指向同一文件，包括符号链接或硬链接。JSON 顶层必须是对象，输入和输出中的 NaN、Infinity 以及浮点溢出会被拒绝。校验、算法执行或结果写入失败时退出码非零，错误写入标准错误，已有输出文件保持原样。成功后通过同目录临时文件替换输出。

## 2. CAPER：以完整双边证据包为选择单位

CAPER 指 Comparability-Aware Paired Evidence Reranking。比较需要双方在同一口径下的证据，单独找到一方的资料还不能完成比较。因此算法先构建可支持比较的完整证据包，再按预算选择包。

对每个比较 `k`，先检查双方套餐身份、标准化口径、单位、有效时间及共同来源门槛。合格事实的引文必须出现在对应来源正文中，内容版本必须一致，四项上游核验标记须为 true。每个候选包由左右各一条合格事实，加上它们通过 `support_ids` 声明的递归支撑材料组成。缺少任一侧时返回 unknown，同一侧合格事实的值存在冲突时返回 conflict，算法不会按可信等级挑一条来消除冲突。

记已选唯一材料集合为 `S`，比较 `k` 的权重为 `w_k`。目标值为 `F(S) = Σ_k w_k · I(存在完整证据包 B_k ⊆ S)`。单边证据没有完成包时，不增加该比较的收益。“完整”以调用方声明的支撑依赖为准，算法不会自动发现被遗漏的推理前提。

每轮尝试加入一个完整包 `B`，计算所有由此完成的比较的总新增权重 `F(S ∪ B) - F(S)`，再除以新增唯一材料的 token 成本 `cost(B \ S)`。只在剩余预算内选择该比值最高的正收益包，并重复计算。新增材料可能顺带完成其他比较，这些收益也会计入。

来源按照明确的 `origin_id` 或规范化 URL 传递归并。材料身份由来源组、内容版本哈希及原文引文共同确定，共享的同一材料只计一次费用。同一材料声明不同 token 成本会被拒绝。原子事实仍分别保留，去重不会删除事实之间的值冲突。

这是带有证据互补性的包贪心规则，不保证全局最优，也没有宣称普通次模覆盖算法的近似保证。来源层级、标准化和 token 成本都由调用方提供，没有训练相关性权重或自动估计核验正确率。

### 输入和函数接口

函数入口：

```python
from competitive_evidence.caper import rank_comparisons

result = rank_comparisons(payload)
```

顶层字段为 `as_of`、`token_budget`、`sources`、`comparisons` 和 `evidence`。可选的 `max_bundle_combinations` 默认为 4096，用于限制累计左右包组合数。各侧精确重复闭包先去重，超过限额时报错，避免静默截断影响结论。合格事实的冲突检查在包去重与限额检查之前完成。

| 对象 | 关键字段 | 含义 |
|---|---|---|
| 来源 | `id`、`url`、`origin_id`、`content`、`version_hash`、`observed_at`、可选 `published_at`、`credibility_tier` | 保留原文、时间及转载链，可信层级为调用方提供的 0 至 3 整数 |
| 比较目标 | `id`、`left_competitor`、`right_competitor`、`left_offer_id`、`right_offer_id` | 明确比较双方及各自套餐或产品版本，不能仅凭同名 Pro 判断等价 |
| 比较口径 | `dimension`、`scope`、`unit`、`valid_at` | `scope` 使用双方一致的标准化条件，单位和目标有效时间必须明确 |
| 比较约束 | `weight`、`min_source_tier`、`max_age_days`、`require_published_at` | 分别控制比较价值及来源门槛，默认值为 1、2、365 天和 false |
| 原子事实 | `id`、`kind: fact`、`competitor`、`offer_id`、`dimension`、`scope`、`unit`、`normalized_value` | 每项只描述一个确定身份与口径的事实，已知值为有限数、非空字符串或布尔值，null 或 `value_status: unknown` 表示未知 |
| 支撑材料 | `id`、`kind: support`、`comparison_ids` | 支撑指定比较的辅助材料 |
| 共同证据字段 | `source_id`、`source_version_hash`、`quote`、`token_cost`、`review`、`valid_from`、`valid_until`、`support_ids` | 绑定来源版本、引文、预算成本和依赖的支撑材料 |

`review` 包含 `value_supported`、`scope_supported`、`normalization_verified` 和 `validity_verified` 四个布尔值。它们表示调用方完成的检查。算法可以检查字段、引用和版本关系，但不能从这些标记推导出独立的语义核验能力。未知事实不能作为已支持比较的见证，也不能用来判定该产品更差。数值 20 与 20.0 按同一值比较，布尔值和字符串仍保留自身类型。

输出包括 `selected_comparison_ids`、`selected_evidence_ids`、`selected_materials`、`token_used`、`trace`、`rejected_evidence` 和逐项 `comparisons`。逐项状态为 `supported`、`unknown` 或 `conflict`，`selected` 单独表示该项是否进入预算内的上下文。资料能够支持某项比较但预算不足或权重为零时，可以同时出现 `status: supported` 与 `selected: false`。已选项的 `witness` 给出左右事实及所需支撑证据，`trace` 记录每轮新增比较权重与成本。

`token_budget` 只约束 `selected_materials` 按声明成本计算的总量。材料携带引文、来源 URL 和版本引用，调用方应按实际送入模型的格式计入这些开销。完整诊断结果中的 trace、所有比较状态等不属于已计算的材料预算，不能把整个输出 JSON 当作已保证符合上下文预算的提示词。

## 3. FIRM-ReAct：反证条件与最小最大遗憾

FIRM 指 Falsification-Informed Regret Minimization。输入的每个情形描述一种仍与现有资料相容的情况。算法先找出哪些情况会使其他候选优于当前推荐，再估计查询这些条件能减少多少最坏决策损失。

记候选决策为 `d`，情形为 `s`，调用方给定的效用为 `U(d,s)`。在情形 `s` 中，选 `d` 的遗憾是 `r(d,s) = max_a U(a,s) - U(d,s)`，即相对该情形最优选择损失的效用。在当前情形集合 `S` 上，算法选择最小化 `max_s r(d,s)` 的决策，并将此最小值记为 `R(S)`。

对查询 `q` 的每种有效回答 `y`，用它保留的情形集合 `S_y` 重算遗憾。一阶查询收益为 `R(S) - max_y R(S_y)`。两阶前瞻允许针对第一步的不同回答，自适应选择可负担的第二个查询，比较最坏分支的剩余遗憾。第二步在各分支内先最小化剩余遗憾，相同时选费用更低的查询。

算法分别计算一阶与两阶计划的“遗憾减少量 / 最坏路径预计费用”。两阶费用为第一步费用加各回答分支中最大的第二步费用。它选取比值更高的计划，再在可负担的首查询中排序，每次只输出下一步动作。这个最多两步的规则不保证任意查询序列或收益费用比的全局最优。

以上收益以收到有效回答为条件。无结果、冲突或核验失败的概率没有被建模，因此输出的 `guaranteed_gain` 不能当作包含工具失败概率的期望收益。`decision.falsification_conditions` 会列出可能推翻当前推荐的情形、挑战候选、效用优势及建模来源。

### 输入和函数接口

函数入口：

```python
from competitive_evidence.firm import firm_plan, firm_observe

planned = firm_plan(payload)
# 后续规划使用调用方保存的最新状态。
replanned = firm_plan({"state": planned["state"]})
# 实际工具执行后，再提交完整观察对象。
updated = firm_observe(observation_payload)
```

首次规划输入为 `problem` 和 `config`。`problem.hard_constraints_prechecked` 必须为 true，表示调用方已确认所有候选在所有输入情形下都满足硬性要求。当前实现不支持某个候选只在部分情形可行的模型。

| `problem` 字段 | 内容 |
|---|---|
| `sources` | `id`、`uri`、`content`、`kind`，保存取证和建模所用材料 |
| `decisions` | 含 `id`、`label` 的候选决策 |
| `scenarios` | 含 `id`、`label`、`source_ids` 的显式情形 |
| `utility` | `values[scenario_id][decision_id]` 为有限数值，另有 `source_ids` 和 `description` |
| `queries` | `id`、`tool`、`args`、`scope`、`cost`、`source_ids` 和 `outcomes` |
| 查询的 `outcomes` | 每项含 `id`、`scenario_ids`、`source_ids`、`description`，每个查询的分支须互斥且覆盖全部输入情形 |

`config` 包含 `budget`、`max_calls`、`max_failures`、`regret_threshold`、`min_gain` 和 `lookahead`，默认依次为 10、8、2、0、0、2。它们控制成本、调用次数、累计失败次数、可接受遗憾、最低查询收益及前瞻深度。`lookahead` 仅可取 1 或 2。费用是调用方约定的统一单位，不能自动解释成美元、人民币或 token。

效用表必须覆盖每个情形下的每个候选，数值可为负。其来源至少包含用户要求、用户偏好或明确标记的合成测试材料。算法不会从网页自动生成用户效用。

返回对象包含 `state`、`decision`、`action`、`query_evaluations` 和 `boundaries`。先检查 `action.terminal`。非终止动作保存在 `state.pending_action`，含 `action_id`、`query_id`、`tool`、`args`、`scope`、`estimated_cost`。调用方必须保存最新状态，并把原始动作完整回传。对同一待执行状态重复 plan 会返回同一动作。

观察输入包含 `state`、`action`、必填的 `actual_cost`、`outcome` 和可选的新 `sources`。`outcome.status` 可取 `valid`、`no_result`、`conflict`、`scope_mismatch`、`unverified`、`error`。其中 `valid` 还需提供匹配的 `outcome_id`、`scope`、`source_ids` 以及 `verification: {verified: true, verifier: ..., method: ...}`。只有满足要求的观察才能排除不相容情形。无结果、未通过核验、范围不符或会使集合变空的观察会返回 `observation_result.accepted: false`，保留现有情形，并记录实际费用及失败。有效但没有排除任何情形的观察也计入累计失败。

以下片段展示完整回写格式。除 `planned` 外的变量需由实际取证和核验产生。`verified_outcome_id` 必须是此次真实查到的查询分支，`observed_scope` 必须反映来源中的实际口径，不能为了匹配目标而直接复制计划口径。只有确实查到且核验通过后，才可以提交 `valid`。

```python
from competitive_evidence.firm import firm_observe

if planned["action"]["terminal"]:
    raise ValueError("计划已终止，无待执行动作")

# 此处的来源、口径、分支和核验记录均由实际工具执行后提供。
observation_payload = {
    "state": planned["state"],
    "action": planned["action"],
    "actual_cost": actual_cost,
    "sources": [{
        "id": new_source_version_id,
        "uri": source_url,
        "content": source_text,
        "kind": "official_pricing",
    }],
    "outcome": {
        "status": "valid",
        "outcome_id": verified_outcome_id,
        "scope": observed_scope,
        "source_ids": [new_source_version_id],
        "verification": {
            "verified": True,
            "verifier": verifier_id,
            "method": review_method,
        },
    },
}
updated = firm_observe(observation_payload)
# 持久化 updated["state"]，检查 updated["action"]["terminal"] 后再执行下一步。
```

没有获得可核验结论时，回传相应的 `no_result`、`conflict`、`scope_mismatch`、`unverified` 或 `error`，并记录已经发生的 `actual_cost`。此时不得代填预期分支来缩小情形集合。

actual_cost 可以超过预计费用，账本会完整记录。新增来源的 ID 不得覆盖已有来源，内容变更时须使用新的版本 ID。observe 回传下一轮状态、推荐和动作，外加 `observation_result`。达到遗憾阈值、调用或失败上限、耗尽费用、没有候选或可负担查询、在前瞻范围内没有超过 `min_gain` 的收益时会停止，并保留剩余遗憾。

核验标记由外部工具或人工提供，状态对象也由调用方保管。状态检查可以拒绝重复或未计划提交，但它不构成签名或认证机制，不能防止调用方伪造整个状态历史。

## 4. 调用方仍需完成的工作

- 从真实资料中抽取产品身份、套餐、地区、币种、计费周期、有效时间及事实。年付均摊价格不能直接当作月付价格。
- 判断来源可信层级、同源关系、引文是否支持结论、单位换算与标准化是否正确，提供可靠的内容版本。
- 用目标模型的 tokenizer 计算证据预算，并为系统提示、对话和输出另留空间。
- 为 FIRM 明确候选决策、情形集合、效用表、查询可能结果及成本。遗漏情形或错误效用可能改变推荐结果，算法不自动补全这些建模假设。
- 执行所选工具，核对返回来源及范围，持久化实际成本和最新状态。观察没有到达时不能把预计收益记为实际进展。

来源正文的 SHA-256 只用于绑定内容版本。它不证明来源可信、事实为真，也不保证情形集合完备。

## 5. 源码、验证与接入状态

| 位置 | 用途 |
|---|---|
| `competitive_evidence/caper.py` | CAPER 的公开函数与选择逻辑 |
| `competitive_evidence/firm.py` | FIRM-ReAct 的规划、观察与状态逻辑 |
| `competitive_evidence/algorithms.py` | 本文的三个 CLI 命令 |
| `competitive_evidence/examples/` | 合成输入格式示例 |
| `tests/test_algorithms_cli.py` | CLI 编码、输入校验和失败保护测试 |

```bash
python -X utf8 -m unittest discover -s tests -p "test_algorithms_cli.py" -v
```

这些检查用于验证接口和规定行为。合成示例及功能单测没有测量真实任务的答案准确率、token 节省、延迟或商业收益，不能据此声称算法优于现有系统。

两个新模块提供独立入口，不会自动替换现有 `competitive_evidence.cli`、搜索排序器、LangGraph 或 direct-thinking 分支。接入在线 MOSS 仍需要工具适配、证据抽取、状态持久化及运行验证。
