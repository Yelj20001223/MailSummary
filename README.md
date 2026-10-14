# MailSummary 多邮箱聚合摘要工具

本地运行的**多邮箱聚合阅读器**：把多个邮箱账号的邮件拉取到本地，统一分类、打标签、按公司聚合，并生成摘要（规则优先，可选接入 LLM）。**全程只读**，不做自动回复/发信，数据全部保存在本机。

## 功能特性

- **多账号聚合**：QQ 邮箱 / 163 / 企业微信邮箱（IMAP）、Gmail（API + OAuth2）、Outlook / Microsoft 365（Graph + OAuth2），统一入口浏览
- **文件夹视图**：收件箱 / 垃圾邮件 / 其他邮件（Outlook 焦点收件箱分区）/ 未读 / 加星 / 公司 / 摘要
- **智能分类**：规则优先的邮件分类（合同法务、财务发票、订单采购、人事招聘、客户往来、系统通知、验证码安全、市场广告、社交等），可选 LLM 增强摘要
- **公司归并**：按发件方域名自动聚合为「公司」卡片，可手动合并，删除账号后自动清理空壳公司
- **标签系统**：自定义标签、批量打标、按标签筛选
- **阅读体验**：HTML 正文沙箱渲染（默认拦截外链图片，防追踪）、纯文本/排版切换、附件元数据展示、列表加载更多
- **自动同步**：定时同步（默认 30 秒），新邮件自动出现在列表，无需手动刷新
- **桌面端**：PyInstaller + pywebview 打包，与网页端共用同一套后端

## 架构

```
backend/
├── app/
│   ├── api/         # FastAPI 路由
│   ├── core/        # 加密、日志、HTML 消毒等基础设施
│   ├── db/          # SQLAlchemy / SQLModel + 幂等迁移
│   ├── models/      # 数据模型（账号/邮件/分类/标签/公司/摘要）
│   ├── schemas/     # Pydantic 出入参
│   ├── services/    # 同步、分类、摘要、公司归并、OAuth 服务
│   │   └── providers/  # MailProvider 接口：IMAP / Gmail API / Microsoft Graph
│   └── static/      # 前端（原生 JS 单页，无构建依赖）
├── tests/           # pytest 单元测试
desktop.py           # 桌面端入口（pywebview 后台线程跑同一 ASGI 应用）
mail_summary.spec    # PyInstaller 打包配置
```

- 后端：Python 3.11+ / FastAPI / SQLModel / SQLite
- 前端：原生 JavaScript，无构建步骤
- 数据存储：`data/` 目录（数据库 + 加密密钥），打包后位于系统用户目录（`%APPDATA%\MailSummary`），与程序文件解耦

## 快速开始（源码运行）

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

cp ../.env.example ../.env      # 按需修改配置
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>（需先在本机启动程序；127.0.0.1 为本机地址，指向你正在运行程序的这台电脑），点击右上角「添加账号」配置邮箱。

### 桌面端（可选）

```bash
pip install -r requirements-desktop.txt
pyinstaller --noconfirm --distpath . mail_summary.spec
./MailSummary.exe
```

## 支持的邮箱接入方式

| 邮箱 | 推荐接入 | 降级 |
|---|---|---|
| QQ 邮箱 / 163 / 企业微信邮箱 | IMAP（授权码） | — |
| Gmail | Gmail API + OAuth2（historyId 增量） | IMAP + 应用专用密码 |
| Outlook / Microsoft 365 | Microsoft Graph + OAuth2（deltaLink 增量） | IMAP（租户不禁用基本认证时） |

- **IMAP 一律使用服务商生成的授权码/应用专用密码，不要使用登录密码**
- 网易系邮箱（163 等）IMAP 登录需发送 ID 命令（已内置），否则会触发 Unsafe Login
- Outlook 个人账号自 2024-09-16 起已禁用 IMAP 应用密码，需走 Graph OAuth2，且 Entra 应用为 Web 机密客户端时回调地址必须为 `http://localhost:8000/api/oauth/outlook/callback`（localhost 忽略端口）

## 配置（.env）

| 变量 | 说明 | 默认 |
|---|---|---|
| `APP_HOST` / `APP_PORT` | 监听地址 | `127.0.0.1` / `8000` |
| `DATA_DIR` | 数据目录（打包后忽略，固定用用户目录） | `data` |
| `SYNC_INTERVAL_SECONDS` | 定时同步间隔（秒），`0` 关闭 | `30` |
| `INITIAL_SYNC_DAYS` | 首次同步回看天数 | `30` |
| `FETCH_BATCH_SIZE` | 单批拉取数量 | `200` |
| `MAX_EML_BYTES` | 单封邮件 .eml 落盘大小上限 | `20971520` |
| `NOTIFY_ENABLED` | 新邮件通知（预留） | `false` |
| `APP_PUBLIC_BASE_URL` | OAuth 回调地址前缀 | `http://127.0.0.1:8000` |
| `LLM_ENABLED` / `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 可选 LLM 摘要（openai / ollama） | 关闭 |

## 测试

```bash
cd backend
pytest -q
```

## 隐私与安全

- **全程只读**：仅执行邮箱读操作（IMAP 只读模式 / 只读 OAuth 权限），不发送、不回复
- **加密存储**：密码/令牌经 Fernet 加密后落库，密钥文件 `.secret_key` 仅本机生成
- **数据本地化**：数据库、邮件原文、密钥均在本机用户目录，程序更新不触碰用户数据
- **正文隔离**：HTML 邮件经白名单消毒后放入 sandbox iframe 渲染，外链图片默认拦截
- 增量同步遵循各协议标准机制（IMAP UIDVALIDITY + UID、Gmail historyId、Graph deltaLink），避免服务商风控

## 免责声明

本项目仅用于个人学习与本地使用，不提供任何明示或暗示的保证。请自行评估并承担使用风险；接入邮箱服务时请遵守各服务商的使用条款。
