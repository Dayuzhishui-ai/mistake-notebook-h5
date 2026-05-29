# 错题闯关大冒险 - H5/PWA 云端版

数学错题闯关游戏，部署到 Render 后可在手机上添加到桌面，作为 PWA 使用。

## 技术栈

- **后端**: FastAPI (async) + libsql_client
- **数据库**: Turso（云端 SQLite）/ 本地 SQLite（dev）
- **OCR**: 阿里云 DashScope Qwen-VL
- **前端**: 原生 HTML/CSS/JS + PWA（manifest + service worker）
- **部署**: Render.com 免费版

## 本地开发

```bash
cd "mistake notebook H5"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 不设环境变量 → 自动用本地 game.db 文件（开发模式）
.venv/bin/python server.py

# 浏览器打开 http://127.0.0.1:8765
```

## 环境变量（Render 部署必填）

| 变量 | 说明 |
|------|------|
| `TURSO_DATABASE_URL` | `libsql://xxx-xxx.turso.io` |
| `TURSO_AUTH_TOKEN`   | Turso Full Access token |
| `DASHSCOPE_API_KEY`  | 阿里云 DashScope API Key（`sk-xxx`） |
| `PORT`               | Render 自动注入，无需手动设置 |

未配置 `TURSO_*` 时自动降级为本地 SQLite。
未配置 `DASHSCOPE_API_KEY` 时 OCR 功能会报错（但其他功能正常）。

## 部署到 Render

1. 把整个项目 push 到 GitHub 私有仓库
2. Render Dashboard → New Web Service → 选你的仓库
3. Render 会自动识别 `render.yaml`
4. 在 Environment 标签页填上面 3 个环境变量
5. 等待 build（3-5 分钟）→ 拿到 `xxx.onrender.com` URL

## 手机安装 PWA

- **iOS Safari**：分享 → 添加到主屏幕
- **Android Chrome**：菜单 → 安装应用 / 添加到主屏幕

## 项目结构

```
mistake notebook H5/
├── server.py            # FastAPI 后端
├── index.html           # PWA 入口
├── style.css
├── manifest.json        # PWA 元信息
├── service-worker.js    # 离线缓存
├── logo.png, logo-192.png, logo-512.png
├── requirements.txt
├── render.yaml          # Render 部署蓝图
└── .gitignore
```
