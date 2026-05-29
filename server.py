"""
数学闯关游戏 - H5/Render 版后端
FastAPI + Turso (libSQL) + 阿里云 DashScope OCR
"""
import os
import re
import json
import uuid
import hashlib
import base64
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import libsql_client
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ==================== CONFIG ====================
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin888")

# 本地开发降级：若没有 Turso 配置，则使用本地 SQLite 文件
LOCAL_DB_FILE = Path(__file__).parent / "game.db"
USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

STATIC_DIR = Path(__file__).parent

# ==================== DATABASE ====================
_db_client: libsql_client.Client | None = None


def get_db() -> libsql_client.Client:
    """获取全局 libSQL 客户端。Turso 远程 或 本地文件 都用同一个 API。"""
    global _db_client
    if _db_client is None:
        if USE_TURSO:
            # libsql_client 0.3.1 默认用 WebSocket 连 libsql://，Turso 当前会返回 400。
            # 强制改用 HTTP 模式（已验证 /v2/pipeline 200 OK）。
            url = TURSO_URL
            if url.startswith("libsql://"):
                url = "https://" + url[len("libsql://"):]
            print(f"[Boot] Turso URL (HTTP mode): {url[:60]}...", flush=True)
            _db_client = libsql_client.create_client(
                url=url,
                auth_token=TURSO_TOKEN,
            )
        else:
            _db_client = libsql_client.create_client(
                url=f"file:{LOCAL_DB_FILE}",
            )
    return _db_client


def hash_password(pwd: str) -> str:
    salt = "mistake-notes-salt-2024"
    return hashlib.sha256((salt + pwd).encode()).hexdigest()


def rows_to_dicts(rs) -> list[dict]:
    """libsql_client.Row 不支持 dict()，按列名手动转换"""
    cols = rs.columns
    return [{c: row[c] for c in cols} for row in rs.rows]


async def init_db():
    db = get_db()
    # 用户表
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            score INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 题目表
    await db.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            display TEXT NOT NULL,
            answer INTEGER NOT NULL,
            type TEXT DEFAULT 'arithmetic',
            level INTEGER NOT NULL
        )
    """)
    # 已掌握表
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mastered (
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            mastered_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (user_id, question_id)
        )
    """)
    # 积分日志
    await db.execute("""
        CREATE TABLE IF NOT EXISTS score_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # Session 表（持久化登录态，替代原内存字典）
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 操作日志表
    await db.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 索引
    await db.execute("CREATE INDEX IF NOT EXISTS idx_questions_user ON questions(user_id, level)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_mastered_user ON mastered(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_user ON action_log(user_id, created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_time ON action_log(created_at)")

    # 旧库可能没有 is_admin 列，补加（忽略已存在的错误）
    try:
        await db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    except Exception:
        pass

    # 自动创建 admin 用户
    rs = await db.execute("SELECT id FROM users WHERE username='admin'")
    if not rs.rows:
        await db.execute(
            "INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?,?,?,1)",
            ["admin", hash_password(ADMIN_PASSWORD), "管理员"],
        )
        print(f"[Boot] Admin user created (password=ADMIN_PASSWORD env)", flush=True)
    else:
        # 确保 admin 拥有管理员权限
        await db.execute("UPDATE users SET is_admin=1 WHERE username='admin'")


# ==================== LOGGING ====================
async def log_action(user_id: int, username: str, action: str, detail: str = ""):
    """记录用户操作日志（失败不影响主流程）"""
    try:
        db = get_db()
        await db.execute(
            "INSERT INTO action_log (user_id, username, action, detail) VALUES (?,?,?,?)",
            [user_id, username, action, detail],
        )
    except Exception as e:
        print(f"[log_action] failed: {e}", flush=True)


# ==================== SESSION ====================
async def create_session(user_id: int) -> str:
    token = uuid.uuid4().hex
    db = get_db()
    await db.execute(
        "INSERT INTO sessions (token, user_id) VALUES (?, ?)",
        [token, user_id],
    )
    return token


async def get_user_id_from_request(request: Request) -> int | None:
    """从 Authorization header 或 cookie 提取 token → user_id"""
    token = None
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("token="):
                token = part[6:]
                break
    if not token:
        return None
    db = get_db()
    rs = await db.execute("SELECT user_id FROM sessions WHERE token=?", [token])
    if not rs.rows:
        return None
    return rs.rows[0]["user_id"]


async def require_user(request: Request) -> int:
    """FastAPI 依赖：要求登录"""
    user_id = await get_user_id_from_request(request)
    if not user_id:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "unauthorized"})
    return user_id


async def require_admin(request: Request) -> int:
    """FastAPI 依赖：要求管理员权限"""
    user_id = await get_user_id_from_request(request)
    if not user_id:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "未登录"})
    db = get_db()
    rs = await db.execute("SELECT is_admin FROM users WHERE id=?", [user_id])
    if not rs.rows or not rs.rows[0]["is_admin"]:
        raise HTTPException(status_code=403, detail={"ok": False, "error": "无管理员权限"})
    return user_id


# ==================== QUESTION PARSING ====================
def parse_expression(expr_str: str):
    """解析加减法算式，返回 {display, answer, type, level}"""
    expr = expr_str.replace(" ", "").rstrip("=")
    if not re.match(r"^[\d+\-()]+$", expr):
        return None
    try:
        answer = eval(expr)
    except Exception:
        return None

    numbers = [int(n) for n in re.findall(r"\d+", expr)]
    operators = re.findall(r"[+\-]", expr)
    max_num = max(numbers) if numbers else 0

    if any(n >= 100 for n in numbers):
        level = 5
    elif len(operators) >= 2:
        level = 4
    elif max_num <= 10:
        level = 1
    elif max_num <= 20:
        level = 2
    else:
        level = 3

    return {
        "display": expr.rstrip("=") + " = ?",
        "answer": int(answer),
        "type": "arithmetic",
        "level": level,
    }


# ==================== OCR (阿里云 DashScope Qwen-VL) ====================
async def ocr_image_with_qwen(image_b64: str, media_type: str = "image/jpeg") -> list[str]:
    """调用阿里云 DashScope Qwen-VL 识别图片中的数学算式"""
    if not DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY 未配置")

    prompt = """这张图片中有数学计算题（错题本/作业）。
请仔细识别所有的加减法算式，提取出来，每行一个。

要求：
1. 只提取加减法算式，格式为：数字+数字 或 数字-数字（可以多步，如 11-5+6）
2. 忽略文字说明、序号、圈圈、批注等
3. 不要计算答案，只提取算式本身（不含等号和答案）
4. 每行输出一个算式，例如：
   25+18
   34-17
   100+50-23
5. 如果看不清或不是数学算式，跳过
6. 只输出算式列表，不要任何其他文字"""

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    payload = {
        "model": "qwen-vl-max",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": f"data:{media_type};base64,{image_b64}"},
                        {"text": prompt},
                    ],
                }
            ]
        },
        "parameters": {"result_format": "message"},
    }
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise ValueError(f"DashScope API {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

    try:
        text = data["output"]["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(item.get("text", "") for item in text if isinstance(item, dict))
    except (KeyError, IndexError):
        raise ValueError(f"DashScope 响应格式异常: {data}")

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        line = re.sub(r"^\d+[\.\)、]\s*", "", line)
        line = line.strip()
        if re.search(r"\d", line) and re.search(r"[+\-]", line):
            clean = re.sub(r"[^0-9+\-]", "", line)
            if clean and re.search(r"\d", clean):
                lines.append(clean)
    return lines


# ==================== APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[Boot] Starting... USE_TURSO={USE_TURSO}, OCR={'on' if DASHSCOPE_API_KEY else 'off'}", flush=True)
    try:
        await init_db()
        print(f"[Boot] DB ready ({'Turso' if USE_TURSO else 'Local SQLite'})", flush=True)
    except Exception as e:
        import traceback
        print(f"[Boot] init_db FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        raise
    yield
    if _db_client:
        await _db_client.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== API ROUTES ====================
@app.get("/api/me")
async def api_me(request: Request):
    user_id = await get_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    db = get_db()
    rs = await db.execute(
        "SELECT id, username, display_name, score, is_admin FROM users WHERE id=?",
        [user_id],
    )
    if not rs.rows:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    u = rs.rows[0]
    return {
        "ok": True,
        "user": {
            "id": u["id"],
            "username": u["username"],
            "display_name": u["display_name"],
            "score": u["score"],
            "is_admin": bool(u["is_admin"]),
        },
    }


@app.get("/api/questions")
async def api_questions(request: Request, user_id: int = Depends(require_user)):
    db = get_db()
    qs = await db.execute(
        "SELECT id, display, answer, type, level FROM questions WHERE user_id=?",
        [user_id],
    )
    ms = await db.execute(
        "SELECT question_id FROM mastered WHERE user_id=?",
        [user_id],
    )
    return {
        "ok": True,
        "questions": rows_to_dicts(qs),
        "mastered_ids": [r["question_id"] for r in ms.rows],
    }


@app.post("/api/register")
async def api_register(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    display_name = (body.get("display_name") or "").strip() or username

    if not username or not password:
        return {"ok": False, "error": "用户名和密码不能为空"}
    if len(username) < 2 or len(password) < 3:
        return {"ok": False, "error": "用户名至少2位，密码至少3位"}

    db = get_db()
    rs = await db.execute("SELECT id FROM users WHERE username=?", [username])
    if rs.rows:
        return {"ok": False, "error": "用户名已存在"}

    await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?,?,?)",
        [username, hash_password(password), display_name],
    )
    rs = await db.execute("SELECT id FROM users WHERE username=?", [username])
    new_id = rs.rows[0]["id"]
    token = await create_session(new_id)
    await log_action(new_id, username, "注册", f"昵称: {display_name}")
    return {"ok": True, "token": token, "display_name": display_name}


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()

    db = get_db()
    rs = await db.execute(
        "SELECT id, display_name, score FROM users WHERE username=? AND password_hash=?",
        [username, hash_password(password)],
    )
    if not rs.rows:
        return {"ok": False, "error": "用户名或密码错误"}
    u = rs.rows[0]
    token = await create_session(u["id"])
    await log_action(u["id"], username, "登录")
    return {
        "ok": True,
        "token": token,
        "display_name": u["display_name"],
        "score": u["score"],
    }


@app.post("/api/logout")
async def api_logout(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        db = get_db()
        await db.execute("DELETE FROM sessions WHERE token=?", [token])
    return {"ok": True}


@app.post("/api/mark-correct")
async def api_mark_correct(request: Request, user_id: int = Depends(require_user)):
    body = await request.json()
    qid = body.get("question_id")
    if not qid:
        return {"ok": False, "error": "missing question_id"}
    db = get_db()
    await db.execute(
        "INSERT OR IGNORE INTO mastered (user_id, question_id) VALUES (?,?)",
        [user_id, qid],
    )
    return {"ok": True}


@app.post("/api/mark-wrong")
async def api_mark_wrong(user_id: int = Depends(require_user)):
    return {"ok": True}


@app.post("/api/add-question")
async def api_add_question(request: Request, user_id: int = Depends(require_user)):
    body = await request.json()
    expression = (body.get("expression") or "").strip()
    if not expression:
        return {"ok": False, "error": "empty"}
    parsed = parse_expression(expression)
    if not parsed:
        return {"ok": False, "error": "无法解析算式"}

    db = get_db()
    rs = await db.execute(
        "SELECT id FROM questions WHERE user_id=? AND display=?",
        [user_id, parsed["display"]],
    )
    if rs.rows:
        return {"ok": False, "duplicate": True, "existing": parsed["display"]}

    await db.execute(
        "INSERT INTO questions (user_id, display, answer, type, level) VALUES (?,?,?,?,?)",
        [user_id, parsed["display"], parsed["answer"], parsed["type"], parsed["level"]],
    )
    rs2 = await db.execute("SELECT username FROM users WHERE id=?", [user_id])
    uname = rs2.rows[0]["username"] if rs2.rows else str(user_id)
    await log_action(user_id, uname, "录入错题", parsed["display"])
    return {"ok": True, "added": parsed["display"]}


@app.post("/api/update-score")
async def api_update_score(request: Request, user_id: int = Depends(require_user)):
    body = await request.json()
    delta = int(body.get("delta", 0))
    reason = body.get("reason", "")

    db = get_db()
    await db.execute(
        "UPDATE users SET score = MAX(0, score + ?) WHERE id=?",
        [delta, user_id],
    )
    await db.execute(
        "INSERT INTO score_log (user_id, delta, reason) VALUES (?,?,?)",
        [user_id, delta, reason],
    )
    rs = await db.execute("SELECT score FROM users WHERE id=?", [user_id])
    rs2 = await db.execute("SELECT username FROM users WHERE id=?", [user_id])
    uname = rs2.rows[0]["username"] if rs2.rows else str(user_id)
    sign = "+" if delta >= 0 else ""
    await log_action(user_id, uname, "积分变化", f"{sign}{delta} ({reason})")
    return {"ok": True, "score": rs.rows[0]["score"]}


@app.post("/api/batch-add")
async def api_batch_add(request: Request, user_id: int = Depends(require_user)):
    body = await request.json()
    expressions = body.get("expressions") or []
    if not expressions:
        return {"ok": False, "error": "没有传入算式"}

    db = get_db()
    added, duplicates, errors = [], [], []
    for raw in expressions:
        raw = (raw or "").strip()
        if not raw:
            continue
        parsed = parse_expression(raw)
        if not parsed:
            errors.append(raw)
            continue
        rs = await db.execute(
            "SELECT id FROM questions WHERE user_id=? AND display=?",
            [user_id, parsed["display"]],
        )
        if rs.rows:
            duplicates.append(parsed["display"])
            continue
        await db.execute(
            "INSERT INTO questions (user_id, display, answer, type, level) VALUES (?,?,?,?,?)",
            [user_id, parsed["display"], parsed["answer"], parsed["type"], parsed["level"]],
        )
        added.append(parsed["display"])

    if added:
        rs2 = await db.execute("SELECT username FROM users WHERE id=?", [user_id])
        uname = rs2.rows[0]["username"] if rs2.rows else str(user_id)
        await log_action(user_id, uname, "批量录题", f"成功{len(added)}题 重复{len(duplicates)} 错误{len(errors)}")

    return {
        "ok": True,
        "added": added,
        "duplicates": duplicates,
        "errors": errors,
        "summary": f"成功添加 {len(added)} 题，重复 {len(duplicates)} 题，无法识别 {len(errors)} 条",
    }


@app.post("/api/ocr")
async def api_ocr(request: Request, user_id: int = Depends(require_user)):
    body = await request.json()
    image_b64 = body.get("image_b64", "")
    media_type = body.get("media_type", "image/jpeg")
    if not image_b64:
        return {"ok": False, "error": "没有图片数据"}
    if not DASHSCOPE_API_KEY:
        return {"ok": False, "error": "OCR 功能未启用（管理员未配置 API Key）"}
    try:
        expressions = await ocr_image_with_qwen(image_b64, media_type)
        db = get_db()
        rs2 = await db.execute("SELECT username FROM users WHERE id=?", [user_id])
        uname = rs2.rows[0]["username"] if rs2.rows else str(user_id)
        await log_action(user_id, uname, "AI识图", f"识别出 {len(expressions)} 道题")
        return {"ok": True, "expressions": expressions, "count": len(expressions)}
    except Exception as e:
        return {"ok": False, "error": f"OCR识别失败: {str(e)}"}


@app.post("/api/import-legacy")
async def api_import_legacy(user_id: int = Depends(require_user)):
    return {"ok": False, "error": "云端版本不支持导入本地文件，请使用拍照录题或手动输入"}


# ==================== ADMIN API ====================
@app.get("/api/admin/stats")
async def api_admin_stats(admin_id: int = Depends(require_admin)):
    db = get_db()
    users_rs = await db.execute("SELECT COUNT(*) as n FROM users WHERE is_admin=0")
    qs_rs = await db.execute("SELECT COUNT(*) as n FROM questions")
    mastered_rs = await db.execute("SELECT COUNT(*) as n FROM mastered")
    logs_rs = await db.execute("SELECT COUNT(*) as n FROM action_log")
    return {
        "ok": True,
        "stats": {
            "users": users_rs.rows[0]["n"],
            "questions": qs_rs.rows[0]["n"],
            "mastered": mastered_rs.rows[0]["n"],
            "log_entries": logs_rs.rows[0]["n"],
        }
    }


@app.get("/api/admin/users")
async def api_admin_users(admin_id: int = Depends(require_admin)):
    db = get_db()
    rs = await db.execute("""
        SELECT u.id, u.username, u.display_name, u.score, u.is_admin, u.created_at,
               COUNT(DISTINCT q.id) as question_count,
               COUNT(DISTINCT m.question_id) as mastered_count
        FROM users u
        LEFT JOIN questions q ON q.user_id = u.id
        LEFT JOIN mastered m ON m.user_id = u.id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    """)
    users = rows_to_dicts(rs)
    for user in users:
        lr = await db.execute(
            "SELECT created_at FROM action_log WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            [user["id"]]
        )
        user["last_active"] = lr.rows[0]["created_at"] if lr.rows else None
    return {"ok": True, "users": users}


@app.get("/api/admin/logs")
async def api_admin_logs(request: Request, admin_id: int = Depends(require_admin)):
    user_id = request.query_params.get("user_id")
    limit = int(request.query_params.get("limit", 200))
    db = get_db()
    if user_id:
        rs = await db.execute(
            "SELECT * FROM action_log WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            [int(user_id), limit]
        )
    else:
        rs = await db.execute(
            "SELECT * FROM action_log ORDER BY created_at DESC LIMIT ?",
            [limit]
        )
    return {"ok": True, "logs": rows_to_dicts(rs)}


@app.get("/admin")
async def serve_admin():
    return FileResponse(STATIC_DIR / "admin.html")


# ==================== STATIC FILES (放最后) ====================
@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ==================== ENTRY ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
