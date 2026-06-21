# Agent 角色与协议

MOSS多agent智能竞品分析系统——小莫的核心协作通过顶层 LangGraph StateGraph 编排，并通过 SQLite 表和 JSON 字段表达状态，不依赖 Agent 间自由文本聊天。Orchestrator 作为节点执行器承载采集、分析、质检和报告的业务逻辑；新增问卷设计、问卷链接和访谈提纲仍归属“访谈/问卷整理 Agent”，并保留来源、Trace 和降级原因。

## 采集 Agent

输入：
- `task_id`
- 竞品列表、官网/URL
- 数据来源模式

输出：
- `sources`
- `evidence_chunks`
- `agent_runs`

规则：
- 实时采集前检查 `robots.txt`，设置超时、限频和 User-Agent。
- 只登记来源、原文片段、采集时间、可信度和降级原因。
- 用户只输入竞品名时也要自动搜索官网、产品页、价格页、公开评价和新闻/销量线索；用户 URL 只是补充。
- 外部站点失败时写日志并保留待补证状态，不能套用无关竞品资料。

## 访谈/问卷整理 Agent

输入：
- 上传 `.txt/.md/.csv/.json/.pdf`
- `source_id`
- 调研目标、目标用户画像、关注维度
- 当前任务中的行业和竞品列表

输出：
- `evidence_chunks`
- `claims`
- `questionnaire_designs`
- `questionnaire_responses`
- `questionnaire_publish_targets`
- `survey_analyses`
- `interview_analyses`
- `agent_runs`

规则：
- 访谈文本抽取用户场景、痛点、原文证据和不确定信息。
- 问卷 CSV/JSON 做脱敏检查、统计摘要和来源记录。
- 任务内生成问卷时，必须写入 `questionnaire_designs`，登记 `questionnaire_design` 来源，并返回 `/questionnaires/<design_id>` 本地链接。
- 发布飞书问卷时，必须先基于已入库问卷 JSON 做题型映射，再由后端发布器调用飞书 CLI；发布结果写入 `questionnaire_publish_targets`，并登记 `feishu_questionnaire` 来源。
- 问卷回答只保存脱敏回答，不要求手机号、邮箱、身份证号等敏感标识。
- 访谈提纲优先使用已配置模型；未配置或调用失败时降级为本地提纲模板，并把降级原因写入 Agent 日志。
- 生成的 claim 必须绑定上传来源，并默认标记为待人工复核。

## 分析 Agent

输入：
- `evidence_chunks`
- `sources`
- `qa_findings`

输出：
- `claims`
- `agent_runs`

规则：
- 先检索和注入 evidence，再生成结论。
- 深度分析模型顺序为 DeepSeek -> 智谱 -> 豆包；DeepSeek direct thinking 优先，必要时使用分析 Agent 内部的 StateGraph ReAct 工具循环补证；结构化辅助输出和 mock Provider 输出必须通过 Schema 校验。
- 接到打回后，必须补来源、降级为待确认，或删除无来源断言。

## 质检 Agent

输入：
- `claims`
- `sources`
- `evidence_chunks`

输出：
- `qa_findings`
- `agent_runs`

规则：
- 检查来源缺失、Schema 完整性、低置信度、时间敏感结论、重复结论和报告准入。
- 配置模型时，可调用模型进行二次复核；模型结果必须作为结构化质检意见记录，不得替代规则校验。
- 发现问题时写入严重级别、原因、打回对象和修复状态。
- 自动质检最多三轮；同类问题三次仍失败时把开放项更新为 `manual_pending`，报告继续生成并进入人工复核工作台。
- 复检通过后更新 `fix_status` 与 `recheck_result`。

## 报告 Agent

输入：
- 通过质检的 `claims`
- `sources`
- `qa_findings`

输出：
- `reports`
- `citation_map`
- `agent_runs`

规则：
- 只渲染至少绑定一个 `source_id` 的结论。
- 报告只展示用户勾选的模块；`source_id`、Trace 和证据分片保留在追溯数据里，不进入用户报告正文。
- 报告中区分事实、推断和不确定性；“建议与结论”模块本轮暂不展示，配置模型时只改写输入模块，不新增未输入模块。
- 报告生成前必须通过 `CompetitiveKnowledgeSchema`，覆盖功能树、定价模型、用户画像、SWOT、来源目录、方法论和图表数据。
- 保留从报告结论到来源、Agent、工具调用和质检记录的追溯路径。

## 人工复查协议

输入：
- `user_text`
- `selected_text`

意图识别：
- 包含“确认” -> `confirm_claim`
- 包含“质检/复检” -> `recheck_qa`
- 包含“来源/证据/搜索/补充” -> `supplement_source`
- 其他 -> `revise_claim`

输出：
- `manual_actions`
- 触发目标 Agent 的新一轮运行记录
- 新报告版本
