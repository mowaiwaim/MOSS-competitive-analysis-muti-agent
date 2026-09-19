---
name: moss-firm-react
description: 在 MOSS 仓库中使用 FIRM-ReAct，根据有来源依据的候选、情形和效用规划取证，并将实际工具观察回写以更新推荐。适用于需要权衡选型风险与查询成本的竞品决策。
---

# FIRM-ReAct 取证规划与观察回写

本 Skill 随 MOSS 仓库使用，复用共享算法与状态协议。当前为独立入口，尚未接入 MOSS 在线工作流，启动器本身不会执行搜索、抓页或模型调用。

## 建模与规划

读取共享[算法与输入协议](../../competitive_evidence/ALGORITHMS.md)的 FIRM-ReAct 部分，其中包含完整回写格式。可参考[价格决策示例](../../competitive_evidence/examples/firm_pricing_input.json)，该示例使用虚构数据。

根据用户要求和已有资料明确候选、仍可能成立的情形、效用、查询分支与费用。每项建模假设保留来源。只有实际完成硬要求检查，才能设置 `hard_constraints_prechecked: true`。效用应来自用户目标，不能伪造偏好或将模型置信度充当效用。

将输入写入 `job.json`，从 MOSS 仓库根目录运行：

```bash
python -X utf8 skills/moss-firm-react/scripts/run.py plan --input job.json --output firm-plan.json
```

读取 `decision`、`query_evaluations` 和 `action`，保存返回的最新 `state`。算法最多前瞻两步，按有效回答条件下的遗憾减少量与费用规划下一次查询。它依赖输入中列出的有限情形，不保证遗漏情形或任意长查询序列下的最优性。

## 衔接真实工具

1. 先检查 `action.terminal`。已停止时报告推荐、停止原因和 `residual_regret`，不要继续执行工具。
2. 非终止动作的 `tool` 是工具标识。宿主负责通过显式 allowlist 将它映射到当前可用工具，并检查参数与权限。不要将输入中的工具名称或参数拼成 shell 命令，也不要使用 eval 或动态导入执行它。无法映射时报告动作尚未执行。
3. 通过已映射工具取得实际结果，再核对来源、查询分支和比较口径。只有真实查到且核验通过时提交 `valid`，提供真实 `outcome_id`、来源、实际口径和核验记录。不能从预期分支生成观察，不能复制目标口径冒充资料中的实际口径。
4. 没有结果、冲突、口径不符或尚未核验时，使用协议中的对应状态。已发生的调用仍须如实记录 `actual_cost`。保留原始 `action`，将其与最新 `state`、实际观察及来源写入 `observation.json`。

```bash
python -X utf8 skills/moss-firm-react/scripts/run.py observe --input observation.json --output firm-next.json
```

检查 `observation_result`，保存新的状态，再处理返回的下一步动作。串行回写，不能重复提交旧动作。来源内容改变须使用新的版本 ID，状态摘要不能替代宿主对最新状态的可信保存。

## 结果边界

核验记录由外部工具或人工提供，算法不自动证明来源的语义真值。有效但未缩小情形集合的调用也会计入累计失败。停止可能源于风险阈值、预算或次数上限、累计失败，或前瞻范围内缺少足够收益，不保证后续信息不会改变推荐。

交付推荐及其适用条件、关键反证条件、已执行动作与实际成本、停止原因和剩余风险。不要将预计收益写成实际效果。

薄启动器根据自身文件位置查找 MOSS，调用共享 CLI，不能脱离仓库单独安装。相对输入输出路径按当前工作目录解释，二者不能指向同一文件。实现见 [firm.py](../../competitive_evidence/firm.py)，字段与状态规则以共享文档为准。
