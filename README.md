# MOSS多agent智能竞品分析系统——小莫

MOSS 团队项目。系统面向竞品分析场景，支持从竞品输入、公开资料采集、Agent 分析、质检复核、可视化展示到 PDF 报告导出的端到端流程。

## 核心功能

- 多 Agent 流程：采集 Agent、分析 Agent、质检 Agent、报告 Agent 协作完成竞品分析。
- 实时公开信息采集：主采集来源为火山联网搜索、Google Alerts RSS、AppArk 市场数据，并支持上传资料解析。
- 顶层 LangGraph 编排：使用 StateGraph 编排采集、分析、质检和撰写/报告节点，流程阶段图可视化且可追溯。
- DeepSeek -> 智谱 -> 豆包 深度报告：分析 Agent 内部优先 DeepSeek direct thinking，必要时使用内层 StateGraph ReAct 工具循环补证。
- 证据溯源：来源、证据分片、claims、引用映射和参考文献统一入库，报告中保留可点击来源链接。
- 可视化输出：生成评分矩阵、能力雷达图、API 成本对比、App 市场表现等模块。
- PDF 导出：网页版报告和 PDF 报告使用同一份报告内容生成。

## 技术栈

- 后端：Python Flask
- 端到端多 Agent 编排：LangGraph StateGraph + Orchestrator 节点执行器
- 深度报告生成：DeepSeek direct thinking 优先；内层 StateGraph ReAct 工具循环用于搜索、抓页、截图补证场景
- 模型：分析 API 顺序为 DeepSeek -> 智谱 -> 豆包；未配置时降级为本地规则/模板流程
- 数据库：SQLite
- 前端：HTML + CSS + JavaScript
- PDF：ReportLab + pypdf
- 数据采集：主采集使用火山联网搜索、Google Alerts RSS、AppArk；ReAct 辅助工具保留 DuckDuckGo 搜索、旧 Bing 搜索类、requests 抓页和 Playwright 截图能力

## 快速启动

```powershell
cd "path\to\MOSS-competitive-analysis"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

编辑 `.env`，填入自己的 API Key 和 RSS 地址后启动：

```powershell
python -m flask --app app run --host 127.0.0.1 --port 5016
```

浏览器访问：

```text
http://127.0.0.1:5016
```

## 环境变量

主要配置：

- `REACT_AGENT_PROVIDER=auto`
- `DEEPSEEK_API_KEY=your-deepseek-api-key`
- `DEEPSEEK_MODEL=deepseek-v4-pro`
- `DEEPSEEK_THINKING_TYPE=enabled`
- `ZHIPU_API_KEY=your-zhipu-api-key`
- `ZHIPU_MODEL=your-zhipu-model`
- `VOLC_SEARCH_API_KEY=your-volc-search-api-key`
- `GOOGLE_ALERTS_RSS_URL=your-google-alerts-rss-url`

## 目录结构

```text
.
├── app.py                    # Flask API 与页面入口
├── orchestrator.py           # 任务编排、采集、分析、质检和报告生成
├── react_report_agent.py     # LangGraph ReAct 深度报告 Agent
├── llm_provider.py           # 模型调用与 Prompt 逻辑
├── report_pdf.py             # PDF 报告渲染
├── collector.py              # 公开资料采集
├── rss_collector.py          # Google Alerts RSS 采集
├── appark_collector.py       # AppArk 市场数据采集
├── static/                   # 前端脚本和样式
├── templates/                # Flask 页面模板
├── docs/                     # 架构、部署和协议说明
├── tests/                    # 自动化测试
└── requirements.txt          # Python 依赖
```

## 运行测试

```powershell
python -m pytest tests\test_app.py -q
```

## 安全说明

- `.env`、数据库、日志、缓存、生成 PDF 和本地上传材料默认不提交。
- GitHub 仓库不包含 API Key、Cookie、Bearer Token 或本地任务数据。
- 如果误把密钥推送到公开仓库，请立即在对应平台作废并重新生成 Key。
