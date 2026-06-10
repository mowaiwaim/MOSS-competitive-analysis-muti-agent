# 第五版部署说明

## 本地运行

```powershell
cd "D:\ai驱动的竞品分析agent协作系统\competitive analysis\第五版"
python app.py
```

默认监听：

```text
http://127.0.0.1:5007
```

## 环境变量

基础可选项：

```powershell
$env:FLASK_SECRET_KEY="replace-with-random-secret"
```

豆包真实调用：

```powershell
$env:LLM_PROVIDER="doubao"
$env:DOUBAO_API_KEY="your-api-key"
$env:DOUBAO_ENDPOINT_ID="your-endpoint-id"
$env:DOUBAO_MODEL_NAME="Doubao-Seed-2.0-lite"
```

也可以在 `第五版/.env.local` 写入同名变量。本地文件已被 `.gitignore` 忽略，系统环境变量会覆盖 `.env.local`。

未设置 `LLM_PROVIDER` 时，如果存在 `DOUBAO_API_KEY` 会尝试豆包；否则使用 `mock` Provider。缺少接入点、网络失败或返回不符合 Schema 时会自动降级规则/模板流程。验证真实调用时，新建任务后查看日志：`model_provider=doubao` 且工具调用包含 `doubao_chat_completions` 才表示豆包参与了分析、质检或报告改写。

飞书问卷发布：

```powershell
$env:FEISHU_CLI_PATH="$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.16.0-win-x64\lark-cli.cmd"
$env:FEISHU_IDENTITY="user"
$env:FEISHU_DEFAULT_FOLDER_TOKEN=""
& $env:FEISHU_CLI_PATH auth status
```

`FEISHU_CLI_PATH` 不设置时会优先寻找本机 WinGet Node.js LTS 目录下的 `lark-cli.cmd`，再退回 PATH 中的 `lark-cli`。`FEISHU_DEFAULT_FOLDER_TOKEN` 可选；为空时使用当前授权用户的默认创建位置。飞书授权信息不得写入前端或日志。

## 数据库与文件

- 数据库：`第五版/data/app.db`
- 上传目录：`第五版/data/uploads/`
- 样例数据：`第五版/data/demo_dataset.json`
- 问卷设计：`questionnaire_designs`
- 问卷回答：`questionnaire_responses`
- 问卷发布记录：`questionnaire_publish_targets`

`.gitignore` 已排除数据库、上传目录、`.env*`、缓存和下载 zip。

## 演示建议

- 主流程默认不联网搜索；网络不稳定时使用缓存样例或上传资料，保证稳定。
- 现场证明真实能力时使用实时采集，并观察看板中的自动搜索状态和日志中的豆包调用状态。
- 演示用户研究能力时，先创建任务，再用右下角“用户调研”生成问卷链接；如需展示飞书链路，点击“生成飞书问卷”，打开返回的飞书链接。
- 如需展示豆包调用，先在本机设置环境变量；不要把 Key 写入页面、截图、文档或命令历史展示材料。

## 生产化待办

- 用生产 WSGI 服务替代 Flask 开发服务器。
- 为真实用户系统增加认证、授权和 CSRF 策略。
- 将 SQLite 替换为 PostgreSQL 或 MySQL。
- 把同步编排替换为队列或任务调度服务。
- 为采集和模型调用增加更细粒度的配额、重试、缓存和监控告警。
- 接入飞书机器人或开放平台应用，把问卷链接转发到指定群聊。
