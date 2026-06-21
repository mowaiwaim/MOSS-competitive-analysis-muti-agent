# MOSS多agent智能竞品分析系统——小莫部署说明

## 本地运行

```powershell
cd "path\to\MOSS-competitive-analysis"
python app.py
```

默认监听：

```text
http://127.0.0.1:5016
```

## 环境变量

基础可选项：

```powershell
$env:FLASK_SECRET_KEY="replace-with-random-secret"
```

深度分析模型调用顺序为 DeepSeek -> 智谱 -> 豆包。`REACT_AGENT_PROVIDER=auto` 会按此顺序尝试已配置的 provider；显式设置 `deepseek`、`zhipu` 或 `doubao` 时只使用该 provider。

```powershell
$env:REACT_AGENT_PROVIDER="auto"
$env:DEEPSEEK_API_KEY="your-deepseek-api-key"
$env:DEEPSEEK_MODEL="deepseek-v4-pro"
$env:ZHIPU_API_KEY="your-zhipu-api-key"
$env:ZHIPU_MODEL="your-zhipu-model"
$env:DOUBAO_API_KEY="your-doubao-api-key"
$env:DOUBAO_ENDPOINT_ID="your-endpoint-id"
$env:DOUBAO_MODEL_NAME="Doubao-Seed-2.0-lite"
```

也可以在项目根目录的 `.env.local` 写入同名变量。本地文件已被 `.gitignore` 忽略，系统环境变量会覆盖 `.env.local`。

常规结构化分析、搜索规划、问卷和访谈辅助仍可使用 `LLM_PROVIDER=doubao`；未配置时使用 `mock` Provider。顶层多 Agent 流程由 LangGraph StateGraph 编排，Orchestrator 承载节点业务逻辑。深度报告缺少模型 Key、网络失败或返回不符合 Schema 时会自动降级规则/模板流程。验证真实调用时，新建任务后查看日志中的 `workflow_engine=langgraph_stategraph`、`model_provider`、`preferred_order`、`deep_report_execution_mode` 和工具调用记录。

主采集来源：

```powershell
$env:VOLC_SEARCH_API_KEY="your-volc-search-api-key"
$env:GOOGLE_ALERTS_RSS_URL="your-google-alerts-rss-url"
```

火山联网搜索、Google Alerts RSS 和 AppArk 是报告主采集口径。ReAct 辅助工具中的 DuckDuckGo 搜索、旧 Bing 搜索类、requests 抓页和 Playwright 截图能力保留，用于深度报告补充和调试，不在本轮删除。

飞书问卷发布：

```powershell
$env:FEISHU_CLI_PATH="$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.16.0-win-x64\lark-cli.cmd"
$env:FEISHU_IDENTITY="user"
$env:FEISHU_DEFAULT_FOLDER_TOKEN=""
& $env:FEISHU_CLI_PATH auth status
```

`FEISHU_CLI_PATH` 不设置时会优先寻找本机 WinGet Node.js LTS 目录下的 `lark-cli.cmd`，再退回 PATH 中的 `lark-cli`。`FEISHU_DEFAULT_FOLDER_TOKEN` 可选；为空时使用当前授权用户的默认创建位置。飞书授权信息不得写入前端或日志。

## 数据库与文件

- 数据库：`data/app.db`
- 上传目录：`data/uploads/`
- 样例数据：`data/demo_dataset.json`
- 问卷设计：`questionnaire_designs`
- 问卷回答：`questionnaire_responses`
- 问卷发布记录：`questionnaire_publish_targets`

`.gitignore` 已排除数据库、上传目录、`.env*`、缓存和下载 zip。

## 演示建议

- 网络不稳定时使用缓存样例或上传资料，保证稳定。
- 现场证明真实能力时使用实时采集，并观察看板中的自动搜索状态、来源目录和模型调用状态。
- 演示用户研究能力时，先创建任务，再用右下角“用户调研”生成问卷链接；如需展示飞书链路，点击“生成飞书问卷”，打开返回的飞书链接。
- 如需展示模型调用，先在本机设置环境变量；不要把 Key 写入页面、截图、文档或命令历史展示材料。

## 生产化待办

- 用生产 WSGI 服务替代 Flask 开发服务器。
- 为真实用户系统增加认证、授权和 CSRF 策略。
- 将 SQLite 替换为 PostgreSQL 或 MySQL。
- 把同步编排替换为队列或任务调度服务。
- 为采集和模型调用增加更细粒度的配额、重试、缓存和监控告警。
- 接入飞书机器人或开放平台应用，把问卷链接转发到指定群聊。
