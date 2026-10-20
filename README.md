# 高原铁路隧道科研—施工智能问答系统

面向隧道科研与施工资料的可追溯知识问答应用：将授权文档处理为有出处的知识块，构建“人—机—料—法—环”知识图谱，并结合检索证据调用 DeepSeek API 或本地模型生成回答。

本项目不训练或微调大模型。内置本地模式是证据摘录器，不是已训练的领域大模型。工程结论和风险建议必须结合原文、适用条件及专业人员复核。

## 功能

| 模块 | 当前能力 |
| --- | --- |
| 知识问答 | 多轮问答、知识块类别多选、证据约束生成、引用来源、问答证据图、报告导出 |
| 文档入库 | PDF、DOCX、TXT、MD、HTML、JSON、CSV、XLSX 解析；预览知识块；自定义块大小和重叠上限；原文留存；幂等上传 |
| 知识块处理 | 默认先清洗筛选，再按标题、段落和完整句子切块；保留页码、单位、否定及适用条件；提供完整正文策略 |
| 知识图谱 | 本体与规则抽取、实体/关系去重、多来源证据、候选审核发布、人工增删改、1–5 层探索、节点拖动与排斥碰撞 |
| 资料治理 | 文本、类型、时间筛选；查看完整块内容与处理报告；删除资料/块后同步清理证据；多库清理失败可重试 |
| 运行审计 | 问题、回答、来源片段、模型与知识版本追踪；风险复核、反馈和健康检查 |
| 设置与账号 | DeepSeek / OpenAI 兼容 API / Ollama / 内置摘录器；登录与会话、管理员账号管理、角色和密级访问控制 |

### 数据处理链路

上传原文 → 解析与质量检查 → 清洗筛选 → 结构化切块 → 保存原文和分块 → 抽取图谱候选 → 审核/发布 → 混合检索 → 基于证据回答与溯源。

- 预览与正式上传使用同一套预处理逻辑；预览不入库。
- 默认策略为 `key-v2`（关键正文）；`structural-v1` 保留完整正文。这是基于规则的原文筛选，不是大模型摘要或改写。
- 新文档入库后自动同步图谱；具备相应权限时可自动审核、发布高置信知识，其余保留在审核队列。
- 相同事实合并证据；不会因为上传新文档而自动恢复人工删除/驳回的知识。
- 图谱同步失败时文档和分块仍保留，可单独重试，不需要重复切分。
- 历史重切保留旧版本以支持问答追溯，当前检索只使用文档的当前处理版本。

## 技术栈

- Python 3.11+；FastAPI、Uvicorn、Pydantic、SQLAlchemy。
- SQLite 主库、原文库与分块库；文件原件存储在受管目录。
- PyMuPDF / pypdf、python-docx、openpyxl；RapidOCR 用于可选扫描 PDF 处理。
- BM25、哈希向量相似度与图谱证据联合检索及重排。当前不是专业嵌入模型或独立向量数据库。
- 原生 HTML / CSS / JavaScript 与 SVG 力导向图谱，无前端构建步骤。
- httpx 模型网关；PBKDF2 密码哈希、会话与 CSRF 校验、DPAPI / Fernet 密钥保护。
- pytest 测试；可选 Playwright 浏览器检查；Docker / Docker Compose。

## 快速开始

从仓库根目录运行。以下为 Windows PowerShell 示例：

```powershell
git clone https://github.com/QF12138/plateau-tunnel-qa.git
cd plateau-tunnel-qa
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
python -m uvicorn tunnel_qa.main:app --host 127.0.0.1 --port 8765
```

macOS / Linux 可用 `source .venv/bin/activate` 激活虚拟环境，用 `cp .env.example .env` 复制配置。

打开 <http://127.0.0.1:8765/>。开发模式接口文档为 <http://127.0.0.1:8765/api/docs>，健康检查为 `/healthz`。

仓库不含业务语料和已运行的数据库，第一次启动是空知识库。请在“文档入库”上传有权使用的文件，先预览再保存。无检索证据时不应期待生成具体工程答案。

默认示例仅用于本机开发，不能直接暴露到公网。已有 `.env` 不要覆盖，应逐项合并配置。

## 模型配置

在本机 `.env` 或系统环境变量中设置，不要提交真实 Key。

### DeepSeek API

```dotenv
TUNNEL_QA_MODEL_MODE=api
TUNNEL_QA_API_PROVIDER=deepseek
DEEPSEEK_API_KEY=<填写你自己的密钥>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

同时支持 `TUNNEL_QA_DEEPSEEK_API_KEY` 等前缀变量。API 请求会将问题和召回片段发送给所配置的模型服务，请先确认资料授权、密级和服务方的数据政策。

### 不调用外部模型

```dotenv
TUNNEL_QA_MODEL_MODE=local
TUNNEL_QA_LOCAL_PROVIDER=builtin
```

此模式只摘录已检索的原文，用于离线演示与回归测试。

### Ollama

先自行安装 Ollama 并准备所需模型，再配置：

```dotenv
TUNNEL_QA_MODEL_MODE=local
TUNNEL_QA_LOCAL_PROVIDER=ollama
TUNNEL_QA_LOCAL_BASE_URL=http://127.0.0.1:11434
TUNNEL_QA_LOCAL_MODEL=qwen2.5:7b
```

管理员也可在系统设置中切换模型。OpenAI 兼容 API 使用兼容提供方配置项；当前实现沿用 `deepseek_*` 字段保存其 URL、模型和 Key。页面保存的配置可能覆盖启动默认值，修改后应核对页面显示的生效设置。

## 登录与权限

启用登录保护时配置：

```dotenv
TUNNEL_QA_ENVIRONMENT=production
TUNNEL_QA_PUBLIC_ACCESS_ENABLED=true
TUNNEL_QA_PUBLIC_ACCESS_USERNAME=<自定义管理员账号>
TUNNEL_QA_PUBLIC_ACCESS_PASSWORD=<自行设置强密码>
```

- 首次启动用上述配置初始化管理员；之后账号密码由数据库管理，不能通过修改环境变量重置已有账号。
- 普通账号可由管理员在“账号管理”创建和启用。权限与密级由服务端校验，不相信浏览器自行声明的身份。
- 浏览器使用可撤销会话，API 保留 Basic 认证兼容；生产模式关闭 Swagger/ReDoc。
- 无登录保护时仅用于回环地址本地开发。不要把 `TUNNEL_QA_TRUST_IDENTITY_HEADERS` 打开后直接暴露给不可信用户。
- 在非 Windows 环境保存页面 API Key 前，应单独配置 `TUNNEL_QA_SECRETS_ENCRYPTION_KEY`（Fernet 密钥），不要与数据库备份一同公开。

详情：[身份与密钥保护](docs/身份与密钥保护.md)、[登录与账号管理](docs/登录与账号管理.md)。

## 数据与目录

默认运行时生成三个数据库：

| 文件 | 用途 |
| --- | --- |
| `data/tunnel_qa.db` | 文档元数据、图谱、证据、问答、审计、账号与设置 |
| `data/original_documents.db` | 原始文档内容归档 |
| `data/document_chunks.db` | 版本化分块内容 |
| `data/storage/` | 受管原文件 |

主库也保留业务所需的文本/分块记录；内容库是独立存储与一致性校验层，不应把三个 SQLite 文件视为天然跨库原子事务。

```text
tunnel_qa/
  api/          REST 接口
  auth/         登录、会话、账号
  core/         配置、权限、错误与追踪
  corpus/       批量语料处理及测试命令
  data/         解析、预处理、切块、内容存储
  knowledge/    本体、候选抽取、证据与图谱同步
  retrieval/    混合检索
  qa/           模型网关、问答与报告
  quality/      风险与评测
  settings/     系统配置及密钥保护
  web/          前端页面和静态资源
scripts/        备份、重切、图谱重建、公网启动工具
tests/          自动测试和浏览器检查脚本
test_data/      明确标记的合成测试语料
docs/           设计、运行与验证说明
```

真实语料、原始需求文件、账号数据库、环境密钥、备份、日志和测试产物均不随仓库发布。工程测试语料仅用于程序验证，不能作为实际施工规范。

## 测试与合成演示

```powershell
python -m pytest
python -m tunnel_qa.corpus.cli run-test-data --input test_data --output test_data/run_output
.\tests\public_process_checks.ps1
```

pytest 使用临时数据库和模型模拟器；合成演示默认使用内置本地摘录器，不调用真实 API。不应在真实业务库中导入测试数据。

浏览器检查函数位于 `tests/*_check.js`。需要额外准备 Playwright CLI，在项目根目录启动隔离页面：

```powershell
python -m uvicorn auth_ui_preview:app --app-dir tests --host 127.0.0.1 --port 8766
```

这些脚本需要对应隔离测试页面、测试会话和必要的前置状态，不是一个可直接执行所有脚本的统一 npm 测试套件。请勿对线上账号页运行会写入数据的测试。

2026-10-18 的验收记录为 361 项 pytest、37 项浏览器检查通过；这是当次记录，不代替克隆后在目标环境重新验证。

## 部署与备份

### Docker

先准备本机 `.env`。Docker Compose 的端口映射可能暴露给其他设备，启动前应启用登录保护、设置强密码，外网部署还需 HTTPS 入口。

```powershell
docker compose up --build -d
```

容器使用 8000 端口，并通过命名卷保存数据。不要使用 `docker compose down -v` 删除仍需保留的数据卷。系统当前以单应用进程运行；多 worker 需要另做共享锁、限流和任务调度。

### ngrok 演示

```powershell
.\scripts\start_public_demo.ps1 -Domain "<你的-ngrok-域名>"
.\scripts\stop_public_demo.ps1
```

需预先安装并认证 ngrok。域名可通过参数、环境变量 `TUNNEL_QA_PUBLIC_DOMAIN` 或已忽略的本机运行状态恢复。它只是将本机服务映射到公网，不是把后端部署到云服务器；电脑、后端、网络和隧道都必须保持运行。详情见 [公网部署说明](PUBLIC_DEPLOYMENT.md)。

### 三库备份

```powershell
python -m scripts.backup_databases --backup-to backups/my-backup
python -m scripts.backup_databases --verify backups/my-backup
python -m scripts.backup_databases --restore-from backups/my-backup --restore-to backups/my-recovery
```

备份/恢复目标必须是新目录。恢复不会自动切换线上服务；不要直接覆盖正在运行的数据库。详情见 [数据一致性与备份](docs/数据一致性与备份.md)。

## 已知边界

- 当前是可运行的工程原型，不是经过工程安全认证的自动决策系统；重要答案必须核对原文和适用范围。
- 图谱实体与关系主要依靠词典、别名及规则抽取，不保证覆盖所有专业概念；高置信发布也不等于事实已经过专家验证。
- 扫描图片、复杂表格和图形不能保证可靠解析。可疑页面会提示复核或隔离，不能把“导入成功”理解为每张图都已被理解。
- 原文筛选尽量保留技术正文和不确定内容，但不保证每个片段都是最优粒度；请通过预览和原文对照核查。
- 备份、并发写入、持续运行、开机启动及自动恢复仍需按实际部署要求配置。启动脚本不是常驻守护服务。
- 本仓库未授予开源许可证；对外分发或使用前请与权利人确认授权范围。
