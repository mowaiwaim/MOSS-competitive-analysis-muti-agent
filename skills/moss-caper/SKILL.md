---
name: moss-caper
description: 在 MOSS 仓库中构造可比较的双边证据，运行 CAPER 在 token 预算内选择完整证据包，并解释缺证、冲突及预算结果。适用于竞品功能、套餐或价格的证据选择。
---

# CAPER 比较证据选择

本 Skill 随 MOSS 仓库使用，复用 `competitive_evidence` 中的实现。它是独立算法的操作入口，尚未接入 MOSS 在线工作流。

## 读取协议并准备输入

先读取共享的[算法与输入协议](../../competitive_evidence/ALGORITHMS.md)，重点看 CAPER 部分。必要时参考[合成输入](../../competitive_evidence/examples/caper_input.json)，不要将样例数据当作真实竞品资料。

- 将任务写成明确的双边比较，分别记录厂商与套餐身份，统一地区、币种、计费周期、单位和有效时间。未知条件保留缺口，年付均摊价不能当作月付价。
- 从已有资料或获准使用的工具中抽取事实，保留原文、URL、同源关系、日期和内容版本。按协议生成来源哈希、事实和显式支撑依赖。
- 来源等级、标准化值及 `review` 标记须有实际检查依据。不要为了通过门槛把未完成的核验标成 true。算法能检查引文与版本关系，不能自动判断自然语言语义是否正确。
- 按实际送入模型的材料格式计算 `token_cost`，包括引文和引用元数据。另行预留提示词、对话及输出预算。

## 执行

以下命令从 MOSS 仓库根目录运行，`job.json` 是根据实际任务生成的输入：

```bash
python -X utf8 skills/moss-caper/scripts/run.py --input job.json --output caper-result.json
```

薄启动器根据自身文件位置查找仓库，调用共享的 `competitive_evidence.algorithms caper`。它不复制算法，也不发起网络或模型调用。仓库整体移动后仍可使用，不能只拷贝 Skill 文件夹作为独立安装包。输入和输出相对路径按当前工作目录解释，不能指向同一文件。

## 解释与交付

检查逐项 `comparisons`、`selected`、`witness`、`rejected_evidence` 和 `trace`。只有完整双边证据包才贡献比较收益，共享材料只计一次成本。`supported` 表示满足结构与上游核验门槛，`selected` 表示进入预算，两者含义不同。

交付同口径结论、绑定来源的证据及未解决项。缺证不代表产品较差，冲突需要继续核查，不能挑选有利数值。预算只约束声明成本下的 `selected_materials`，整个诊断 JSON 不在该预算保证内。贪心选择没有全局最优保证，合成示例与功能测试不构成真实效果指标。

实现见 [caper.py](../../competitive_evidence/caper.py)，完整 CLI 与字段说明继续以共享文档为准。
