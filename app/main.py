from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import WriteLockedError, get_db, init_db, read_snapshot, write_transaction
from app.services import (
    execute_judgment,
    list_judgments,
    list_partitions,
    list_policies,
    list_reports,
    list_runs,
    list_scan_jobs,
    run_expire_scan,
    run_reconcile,
)
from app.services.reconcile import CountDriftError, build_snapshot

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-retain-session")

app = FastAPI(title="cc-retain-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("retain_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "retain_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("retain_session")
    return resp


@app.get("/api/meta")
def api_meta(request: Request):
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "lab_code": Config.LAB_CODE,
        "skeleton": "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
    }


@app.get("/api/policies")
def api_policies(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_policies(conn, active_only=True)}


@app.get("/api/partitions")
def api_partitions(request: Request, domain_code: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_partitions(conn, domain_code=domain_code)}


@app.get("/api/scan-jobs")
def api_scan_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_scan_jobs(conn)}


@app.post("/api/scan-jobs")
async def api_create_scan(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    try:
        with write_transaction() as conn:
            job = run_expire_scan(conn, as_of)
    except WriteLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@app.get("/api/judgments")
def api_judgments(request: Request, status: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_judgments(conn, status=status)}


@app.post("/api/judgments/{judgment_id}/execute")
def api_execute(request: Request, judgment_id: int):
    require_user(request)
    try:
        # 裁撤落笔与对账落库共用同一把写锁：两名值班并发时只放一个进去。
        with write_transaction() as conn:
            run = execute_judgment(conn, judgment_id)
    except WriteLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "run": run}


@app.get("/api/runs")
def api_runs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_runs(conn)}


@app.get("/api/reconcile-reports")
def api_reports(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_reports(conn)}


@app.get("/api/reconcile/snapshot")
def api_reconcile_snapshot(request: Request):
    """读页瞬间冻结：卡片 / 执行列表 / 分区详情三块同一读点同源返回。"""
    require_user(request)
    with read_snapshot() as (conn, snapshot_at):
        return {"ok": True, "snapshot": build_snapshot(conn, snapshot_at)}


@app.post("/api/reconcile-reports")
async def api_reconcile(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    try:
        # 对账落库与裁撤执行共用写锁；报告数字在同一写事务读点内计数并复核。
        with write_transaction() as conn:
            report = run_reconcile(conn, as_of)
    except WriteLockedError as exc:
        # 两名值班并发写：后来者快速失败，绝不排队双写。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CountDriftError as exc:
        # 混数即拦：账实对不上时整个事务已回滚，不产生脏报告。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "report": report}
