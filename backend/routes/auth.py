# -*- coding: utf-8 -*-
"""v2.1.15 权限系统路由 — setup / login / logout / whoami / role 管理 / 改密码

路由清单:
  POST /auth/setup        — 首次创建 super_admin (仅在 setup 未完成时可调)
  POST /auth/login        — 账号密码登录 (name 或 phone + password)
  POST /auth/logout       — 清 cookie
  GET  /auth/whoami       — 返回当前 session 详情 (公开,前端用)
  POST /admin/staff/{id}/role      — 改角色 (仅 super_admin)
  POST /admin/staff/{id}/password  — 改自己密码 (任意已登录) / 改任意人密码 (super_admin)
  GET  /admin/auth/log    — 看登录日志 (仅 super_admin)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Body, Request, Response

from .. import auth, data_layer
from ._helpers import now, new_id

logger = logging.getLogger(__name__)

router = APIRouter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def register_routes(app) -> None:
    """注册 auth 路由"""
    from qwenpaw.pawapp import get_ctx

    # ─────────────────────────────────────────
    # 1. 首次 setup
    # ─────────────────────────────────────────

    @router.get("/auth/setup-status")
    async def setup_status(force: str = ""):
        """公开:告诉前端是否需要 setup (是否已初始化 super_admin)
        v2.1.16-hotfix2: force=1 强制返回 initialized=false (前端可借此显示 setup 蒙层)
        """
        info = auth.get_setup_info()
        return {
            "ok": True,
            "initialized": auth.is_setup_done() if force != "1" else False,
            "initialized_at": (info or {}).get("initialized_at"),
            "hint": "已初始化" if auth.is_setup_done() else "首次访问需要创建超级管理员",
            "force_setup_supported": True,
        }

    # ─────────────────────────────────────────
    # v2.1.16-hotfix2: dev 端点 — 不重启就能修各种 setup 状态
    # ─────────────────────────────────────────

    @router.post("/auth/dev-reset-password")
    async def dev_reset_password(payload: dict = Body(default_factory=dict)):
        """dev 工具: 重置任意员工的密码 (按 name 或 phone)

        生产环境务必通过 admin/staff/{id}/password 走正常流程,
        这里只在 setup 异常/密码遗忘时救命用
        """
        name = (payload.get("name") or payload.get("phone") or "").strip()
        new_password = payload.get("new_password") or payload.get("password") or ""
        if not name:
            raise HTTPException(status_code=400, detail="name/phone 必填")
        if len(new_password) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")

        rows = auth.load_staff_rows()
        target = None
        for r in rows:
            if r.get("deleted"):
                continue
            if r.get("name") == name or r.get("phone") == name:
                target = r
                break
        if not target:
            raise HTTPException(status_code=404, detail=f"找不到员工: {name}")

        h = auth.hash_password(new_password)
        target["auth_salt"] = h["salt"]
        target["auth_password_hash"] = h["hash"]
        target["auth_failed_count"] = 0
        target["auth_locked_until"] = 0
        target["updated_at"] = now()
        auth.save_staff_rows(rows)
        auth.append_log("dev_reset_password", True, "dev", target["id"], f"重置 {name} 密码")
        logger.info("[dev] 重置密码: name=%s id=%s", name, target["id"])

        return {
            "ok": True,
            "msg": f"已重置 {name} 的密码",
            "staff_id": target["id"],
            "name": target["name"],
        }

    @router.post("/auth/dev-force-setup-reset")
    async def dev_force_setup_reset():
        """dev 工具: 清掉 auth_setup.json 的标记,让 setup 蒙层重新出现

        不动 staff 表 — 老的 super_admin 记录还在,只是 setup_done=false
        """
        # 通过 auth 模块提供的 mark_setup_undone (如无则 fallback 到文件)
        try:
            auth.mark_setup_undone()
            msg = "已通过 auth.mark_setup_undone() 重置"
        except Exception as exc:
            # fallback: 直接删 auth_setup.json (auth.py 启动时会自动重建空文件)
            import os
            auth_setup = auth.AUTH_SETUP_PATH
            if os.path.exists(auth_setup):
                os.remove(auth_setup)
                msg = f"已删除 {auth_setup}, 重启 plugin 后 setup_done=false"
            else:
                msg = f"{auth_setup} 不存在, 无需重置"
        auth.append_log("dev_force_setup_reset", True, "dev", "", "强制重置 setup_done")
        logger.info("[dev] 强制重置 setup_done")
        return {"ok": True, "msg": msg}

    @router.post("/auth/dev-seed-super-admin")
    async def dev_seed_super_admin(payload: dict = Body(default_factory=dict)):
        """dev 工具: 直接往 staff 表插入/升级一条 super_admin 记录 (绕过 setup_done 检查)

        如果 name 已存在 → 升级为 super_admin + 重置密码
        如果 name 不存在 → 新建 (部门=前台部)
        """
        name = (payload.get("name") or "").strip()
        password = payload.get("password") or ""
        if not name:
            raise HTTPException(status_code=400, detail="name 必填")
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")

        rows = auth.load_staff_rows()
        existing = next((r for r in rows if r.get("name") == name and not r.get("deleted")), None)
        h = auth.hash_password(password)

        if existing:
            existing["role"] = auth.ROLE_SUPER_ADMIN
            existing["auth_salt"] = h["salt"]
            existing["auth_password_hash"] = h["hash"]
            existing["auth_failed_count"] = 0
            existing["auth_locked_until"] = 0
            existing["updated_at"] = now()
            staff_id = existing["id"]
            action = "升级为 super_admin + 重置密码"
        else:
            depts = data_layer.load_table("departments")
            front_dept = next((d for d in depts if "前台" in d.get("name", "") and not d.get("deleted")), None)
            if not front_dept:
                front_dept = next((d for d in depts if not d.get("deleted")), None)
            staff_id = new_id("staff")
            new_staff = {
                "id": staff_id,
                "name": name,
                "phone": name,  # 用 name 当 phone 字段 (允许字符串)
                "department_id": front_dept["id"] if front_dept else "",
                "role": auth.ROLE_SUPER_ADMIN,
                "auth_salt": h["salt"],
                "auth_password_hash": h["hash"],
                "auth_failed_count": 0,
                "auth_locked_until": 0,
                "created_via": "dev_seed",
                "created_at": now(),
                "updated_at": now(),
                "deleted": False,
            }
            rows.append(new_staff)
            action = "新建 super_admin"

        auth.save_staff_rows(rows)
        auth.append_log("dev_seed_super_admin", True, "dev", staff_id, action)
        logger.info("[dev] %s: id=%s name=%s", action, staff_id, name)

        return {
            "ok": True,
            "msg": action,
            "staff_id": staff_id,
            "name": name,
            "role": auth.ROLE_SUPER_ADMIN,
        }

    @router.post("/auth/setup")
    async def setup_super_admin(
        request: Request,
        response: Response,
        payload: dict = Body(default_factory=dict),
    ):
        """首次创建 super_admin (仅 setup 未完成时可调)"""
        if auth.is_setup_done():
            raise HTTPException(status_code=410, detail={
                "ok": False,
                "reason": "已初始化过超级管理员,setup 接口关闭",
                "hint": "如需重置,删除 data_dir/auth_setup.json 并重启 plugin",
            })

        name = (payload.get("name") or "").strip()
        phone = (payload.get("phone") or "").strip()
        password = payload.get("password") or ""

        if not name:
            raise HTTPException(status_code=400, detail="姓名 必填")
        if not phone:
            raise HTTPException(status_code=400, detail="手机号 必填")
        # v2.1.15: phone 字段允许 (A) 11位手机号 (B) 任意字符串作登录账号 (如 domai)
        # 只要 ≥3 位, 不含特殊字符
        if len(phone) < 3:
            raise HTTPException(status_code=400, detail="手机号/账号 至少 3 位")
        if not re.match(r"^[A-Za-z0-9_-]+$", phone):
            raise HTTPException(status_code=400, detail="手机号/账号 只能含字母数字下划线短横线")
        # 如果看起来像手机号 (纯数字), 校验 11 位 1 开头
        if phone.isdigit() and len(phone) >= 11:
            if not re.match(r"^1[3-9]\d{9}$", phone):
                raise HTTPException(status_code=400, detail="11 位纯数字必须是 1[3-9] 开头")
        auth.validate_password_strength(password)

        rows = auth.load_staff_rows()
        # 检查重名/重手机号
        if any(s.get("name") == name and not s.get("deleted") for s in rows):
            raise HTTPException(status_code=409, detail=f"员工名 {name} 已存在")
        if any(s.get("phone") == phone and not s.get("deleted") for s in rows):
            raise HTTPException(status_code=409, detail=f"手机号 {phone} 已被登记")

        # 部门兜底: 前台部 (不存在则跳过 department_id,不会强校验)
        depts = data_layer.load_table("departments")
        front_dept = next((d for d in depts if "前台" in d.get("name", "") and not d.get("deleted")), None)
        if not front_dept:
            front_dept = next((d for d in depts if not d.get("deleted")), None)

        h = auth.hash_password(password)
        staff_id = new_id("staff")
        new_staff = {
            "id": staff_id,
            "name": name,
            "phone": phone,
            "department_id": front_dept["id"] if front_dept else "",
            "role": auth.ROLE_SUPER_ADMIN,
            "auth_salt": h["salt"],
            "auth_password_hash": h["hash"],
            "auth_failed_count": 0,
            "auth_locked_until": 0,
            "created_via": "seed",
            "created_at": now(),
            "updated_at": now(),
            "deleted": False,
        }
        rows.append(new_staff)
        auth.save_staff_rows(rows)

        # v2.1.15 友好迁移: setup 完成后, 启发式批量给老员工赋权限 role + 默认密码
        # 让 super_admin 登录后不用挨个分配, 老员工也能立刻用手机号+staff888ok 登录
        try:
            # HEURISTIC_MAP 在模块顶层
            HM = HEURISTIC_MAP
            DEFAULT_PASSWORD = "staff888ok"  # 老员工默认密码, 用户登录后可改
            summary = {auth.ROLE_MANAGER: 0, auth.ROLE_EMPLOYEE: 0, "unchanged": 0,
                       "passwords_set": 0}
            for r in rows:
                if r.get("id") == staff_id:  # 跳过自己 (super_admin)
                    continue
                if r.get("deleted"):
                    continue
                work_role = (r.get("role") or "") + " " + (r.get("name") or "")
                new_role = None
                for kw, rl in HM.items():
                    if kw in work_role:
                        new_role = rl
                        break
                if new_role:
                    r["role"] = new_role
                    summary[new_role] = summary.get(new_role, 0) + 1
                else:
                    summary["unchanged"] += 1
                # 给老员工设默认密码 (仅当还没密码时)
                if not r.get("auth_password_hash") or not r.get("auth_salt"):
                    h = auth.hash_password(DEFAULT_PASSWORD)
                    r["auth_salt"] = h["salt"]
                    r["auth_password_hash"] = h["hash"]
                    r["auth_failed_count"] = 0
                    r["auth_locked_until"] = 0
                    r["created_via"] = "migrated"
                    summary["passwords_set"] += 1
                r["updated_at"] = now()
            auth.save_staff_rows(rows)
            logger.info("[auth/setup] 智能默认完成: %s", summary)
        except Exception as exc:
            logger.warning("[auth/setup] 智能默认失败(不致命): %s", exc)

        auth.mark_setup_done(staff_id, name)
        auth.set_session(response, staff_id, auth.ROLE_SUPER_ADMIN)

        auth.append_log("setup", True, _client_ip(request), staff_id, "首次创建 super_admin")
        logger.info("[auth/setup] 超级管理员创建成功: id=%s name=%s", staff_id, name)

        return {
            "ok": True,
            "staff_id": staff_id,
            "name": name,
            "role": auth.ROLE_SUPER_ADMIN,
            "msg": "🎉 超级管理员创建成功!请妥善保管密码。",
            "defaults": {
                "staff_default_password": "staff888ok",
                "hint": "老员工可用手机号 + staff888ok 登录 (建议他们登录后立即改密码)",
            },
        }

    # ─────────────────────────────────────────
    # 2. 登录 / 登出 / whoami
    # ─────────────────────────────────────────

    @router.post("/auth/login")
    async def login(
        request: Request,
        response: Response,
        payload: dict = Body(default_factory=dict),
    ):
        """账号 (name 或 phone) + 密码 登录"""
        identifier = (payload.get("identifier") or payload.get("name") or payload.get("phone") or "").strip()
        password = payload.get("password") or ""
        if not identifier or not password:
            raise HTTPException(status_code=400, detail="账号 + 密码 必填")

        ip = _client_ip(request)
        # 限速
        fails = auth.recent_login_fails(ip)
        if fails >= auth.LOGIN_FAIL_MAX:
            auth.append_log("login", False, ip, identifier, "限速:1分钟超5次")
            raise HTTPException(status_code=429, detail={
                "ok": False,
                "reason": f"登录失败次数过多 ({fails} 次/分钟),请稍后再试",
            })

        staff = auth.find_staff_by_name_or_phone(identifier)
        if not staff:
            auth.append_log("login", False, ip, identifier, "账号不存在")
            raise HTTPException(status_code=401, detail="账号或密码错误")

        # v2.1.18-hotfix: 兼容 id / staff_id 两种字段名
        sid = staff.get("id") or staff.get("staff_id") or ""

        # 检查 locked
        if staff.get("auth_locked_until", 0) > 0:
            import time as _t
            remaining = int(staff.get("auth_locked_until", 0)) - int(_t.time())
            if remaining > 0:
                auth.append_log("login", False, ip, identifier, f"账号锁定,剩余 {remaining}s")
                raise HTTPException(status_code=429, detail={
                    "ok": False,
                    "reason": f"账号临时锁定,还剩 {remaining}s 解锁",
                })

        salt = staff.get("auth_salt") or ""
        expected = staff.get("auth_password_hash") or ""
        if not salt or not expected:
            auth.append_log("login", False, ip, identifier, "账号未设置密码")
            raise HTTPException(status_code=401, detail={
                "ok": False,
                "reason": "此账号未设置密码,请联系超级管理员",
            })

        if not auth.verify_password(password, salt, expected):
            # 计数 + 1,失败 5 次锁 5 分钟
            rows = auth.load_staff_rows()
            for r in rows:
                rid = r.get("id") or r.get("staff_id") or ""
                if rid == sid:
                    r["auth_failed_count"] = (r.get("auth_failed_count") or 0) + 1
                    if r["auth_failed_count"] >= auth.LOGIN_FAIL_MAX:
                        import time as _t
                        r["auth_locked_until"] = int(_t.time()) + 300  # 锁 5 分钟
                        r["auth_failed_count"] = 0
                    break
            auth.save_staff_rows(rows)
            auth.append_log("login", False, ip, identifier, "密码错误")
            raise HTTPException(status_code=401, detail="账号或密码错误")

        # 登录成功,清零失败计数 + 更新 last_login
        import time as _t
        rows = auth.load_staff_rows()
        for r in rows:
            rid = r.get("id") or r.get("staff_id") or ""
            if rid == sid:
                r["auth_failed_count"] = 0
                r["auth_locked_until"] = 0
                r["last_login_at"] = now()
                r["last_login_ip"] = ip
                break
        auth.save_staff_rows(rows)

        role = staff.get("role") or auth.ROLE_GUEST
        auth.set_session(response, sid, role)
        auth.append_log("login", True, ip, sid, f"role={role}")

        return {
            "ok": True,
            "staff_id": sid,
            "name": staff.get("name", ""),
            "role": role,
            "msg": f"欢迎回来,{staff.get('name', '')}",
        }

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response):
        sess = auth.get_session(request)
        auth.clear_session(response)
        if sess.get("user_id"):
            auth.append_log("logout", True, _client_ip(request), sess["user_id"], "")
        return {"ok": True, "msg": "已登出"}

    @router.get("/auth/whoami")
    async def whoami(request: Request):
        """返回当前 session 详情 (公开,前端用)"""
        sess = auth.get_session(request)
        return {
            "ok": True,
            "user_id": sess["user_id"],
            "name": sess["name"],
            "role": sess["role"],
            "is_super_admin": sess["is_super_admin"],
            "is_manager": sess["is_manager"],
            "is_employee": sess["is_employee"],
            "is_guest": sess["is_guest"],
            "logged_in": bool(sess["user_id"]),
            "setup_done": auth.is_setup_done(),
        }

    # ─────────────────────────────────────────
    # 3. 角色管理 (仅 super_admin)
    # ─────────────────────────────────────────

    @router.post("/admin/staff/{staff_id}/role")
    async def change_role(
        staff_id: str,
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_super_admin()),
    ):
        new_role = (payload.get("role") or "").strip()
        if new_role not in auth.ALL_ROLES:
            raise HTTPException(status_code=400, detail=f"role 必须 ∈ {sorted(auth.ALL_ROLES)}")

        # v2.1.18 修复: 先 load 一次拿 rows, 然后从 rows 里找 target (同一个 list 内的引用, 不是另起一份)
        rows = auth.load_staff_rows()
        target = None
        for r in rows:
            if r.get("id") == staff_id and not r.get("deleted"):
                target = r
                break
        if not target:
            raise HTTPException(status_code=404, detail=f"staff {staff_id} 不存在")

        old_role = target.get("role") or auth.ROLE_GUEST

        # 至少保留 1 个 super_admin
        if old_role == auth.ROLE_SUPER_ADMIN and new_role != auth.ROLE_SUPER_ADMIN:
            if auth.count_super_admins() <= 1:
                raise HTTPException(status_code=400, detail={
                    "ok": False,
                    "reason": "至少保留 1 个超级管理员,不能降级",
                    "hint": "先在 Admin Tab 把另一个员工升为 super_admin,再降级当前账号",
                })

        target["role"] = new_role
        target["updated_at"] = now()
        auth.save_staff_rows(rows)

        auth.append_log("change_role", True, "", sess["user_id"],
                        f"{target.get('name')}({staff_id}): {old_role} → {new_role}")

        return {
            "ok": True,
            "staff_id": staff_id,
            "name": target.get("name", ""),
            "old_role": old_role,
            "new_role": new_role,
            "msg": f"已更新 {target.get('name', '')} 的角色: {old_role} → {new_role}",
        }

    @router.post("/admin/staff/{staff_id}/password")
    async def change_password(
        staff_id: str,
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ):
        """改密码: 自己改自己 OR super_admin 改任意人"""
        target_id = staff_id
        # v2.1.18 修复: 先 load rows 再 find (避免 target 和 rows 是不同 list 实例导致保存丢失)
        rows = auth.load_staff_rows()
        target = None
        for r in rows:
            if r.get("id") == target_id and not r.get("deleted"):
                target = r
                break
        if not target:
            raise HTTPException(status_code=404, detail=f"staff {target_id} 不存在")

        is_self = (sess["user_id"] == target_id)
        is_super = sess["is_super_admin"]

        if not is_self and not is_super:
            raise HTTPException(status_code=403, detail="只能改自己密码,或 super_admin 改任意人")

        new_password = payload.get("new_password") or ""
        auth.validate_password_strength(new_password)

        # 自己改自己 → 必须验证旧密码
        if is_self and not is_super:
            old_password = payload.get("old_password") or ""
            if not target.get("auth_salt") or not target.get("auth_password_hash"):
                raise HTTPException(status_code=400, detail="账号未设置密码")
            if not auth.verify_password(old_password, target["auth_salt"], target["auth_password_hash"]):
                raise HTTPException(status_code=401, detail="旧密码错误")
        elif is_super and not is_self:
            # super_admin 改别人 → 不需要旧密码,但日志记录
            pass

        h = auth.hash_password(new_password)
        target["auth_salt"] = h["salt"]
        target["auth_password_hash"] = h["hash"]
        target["auth_failed_count"] = 0
        target["auth_locked_until"] = 0
        target["updated_at"] = now()
        auth.save_staff_rows(rows)

        auth.append_log("change_password", True, "", sess["user_id"],
                        f"{target.get('name')}({target_id}) {'自助' if is_self else '被 super_admin 强制'}改密")

        return {"ok": True, "msg": "密码已更新"}

    @router.get("/admin/auth/log")
    async def get_auth_log(
        limit: int = 50,
        sess: Dict[str, Any] = Depends(auth.require_super_admin()),
    ):
        """看登录/操作日志 (仅 super_admin)"""
        log_path = auth.LOG_FILE
        if not log_path.is_file():
            return {"ok": True, "rows": [], "total": 0}
        try:
            rows = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            rows = []
        # 最新在前
        rows = list(reversed(rows[-limit:]))
        return {"ok": True, "rows": rows, "total": len(rows)}

    # ─────────────────────────────────────────
    # 4. 角色列表 (前端 UI 用)
    # ─────────────────────────────────────────

    @router.get("/admin/auth/roles")
    async def list_roles():
        """公开:返回所有角色 + 描述 (前端角色下拉用)"""
        return {
            "ok": True,
            "roles": [
                {"value": auth.ROLE_SUPER_ADMIN, "label": "超级管理员", "desc": "唯一可改他人角色"},
                {"value": auth.ROLE_MANAGER, "label": "管理员(经理/总管)", "desc": "增删改业务数据 + 派单"},
                {"value": auth.ROLE_EMPLOYEE, "label": "员工", "desc": "只读 + 接受派单"},
                {"value": auth.ROLE_GUEST, "label": "游客/开发", "desc": "只读(纯网页开发模式默认)"},
            ],
        }

    # ─────────────────────────────────────────
    # 5. 智能默认 (heuristic) — 按工作角色批量赋权限角色
    #    适合 100% 默认分配场景, super_admin 后续可单独再调
    # ─────────────────────────────────────────

    # 启发式映射: 工作 role 关键词 → 权限 role (模块顶层, 全局可见)
    HM = {
        # manager: 经理/总管/主管/店长
        "经理": auth.ROLE_MANAGER,
        "总管": auth.ROLE_MANAGER,
        "主管": auth.ROLE_MANAGER,
        "店长": auth.ROLE_MANAGER,
        "经理助理": auth.ROLE_MANAGER,
        # employee: 前台/客房/工程/餐厅/PA/接待 等一线员工
        "前台": auth.ROLE_EMPLOYEE,
        "客房": auth.ROLE_EMPLOYEE,
        "工程": auth.ROLE_EMPLOYEE,
        "维修": auth.ROLE_EMPLOYEE,
        "餐厅": auth.ROLE_EMPLOYEE,
        "服务员": auth.ROLE_EMPLOYEE,
        "接待": auth.ROLE_EMPLOYEE,
        "管家": auth.ROLE_EMPLOYEE,
        "PA": auth.ROLE_EMPLOYEE,
    }
    # 模块顶层别名, 让 setup endpoint 通过 globals() 也能访问
    globals()['HEURISTIC_MAP'] = HM

    @router.post("/admin/auth/heuristic-assign")
    async def heuristic_assign(
        sess: Dict[str, Any] = Depends(auth.require_super_admin()),
    ):
        """按工作 role 启发式批量赋权限 role (一键默认)

        策略:
          - 包含 经理/总管/主管/店长 → manager
          - 包含 前台/客房/工程/维修/餐厅/服务员/接待/管家/PA → employee
          - 都没匹配到 (含空) → 保持当前 role (默认 guest)

        Returns:
          {ok, summary: {manager: N, employee: M, unchanged: K}, details: [...]}
        """
        rows = auth.load_staff_rows()
        summary = {auth.ROLE_MANAGER: 0, auth.ROLE_EMPLOYEE: 0, "unchanged": 0}
        details = []
        for r in rows:
            if r.get("deleted"):
                continue
            if r.get("role") == auth.ROLE_SUPER_ADMIN:
                # super_admin 永远是 super_admin, 跳过
                continue
            work_role = (r.get("role") or "") + " " + (r.get("name") or "")
            new_role = None
            for kw, role in HEURISTIC_MAP.items():
                if kw in work_role:
                    new_role = role
                    break
            if new_role is None:
                summary["unchanged"] += 1
                continue
            old = r.get("role") or auth.ROLE_GUEST
            if old == new_role:
                summary["unchanged"] += 1
                continue
            r["role"] = new_role
            r["updated_at"] = now()
            summary[new_role] += 1
            details.append({
                "name": r.get("name"),
                "work_role": r.get("role"),
                "old_role": old,
                "new_role": new_role,
            })
        auth.save_staff_rows(rows)
        auth.append_log("heuristic_assign", True, "", sess["user_id"],
                        f"manager+{summary[auth.ROLE_MANAGER]} employee+{summary[auth.ROLE_EMPLOYEE]}")
        return {"ok": True, "summary": summary, "details": details}

    @router.post("/admin/staff/assign-role-bulk")
    async def bulk_assign_role(
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_super_admin()),
    ):
        """批量赋角色: payload = {assignments: [{staff_id, role}, ...]}"""
        items = payload.get("assignments") or []
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=400, detail="assignments 必须是非空 list")
        rows = auth.load_staff_rows()
        applied = []
        skipped = []
        for it in items:
            sid = (it.get("staff_id") or "").strip()
            role = (it.get("role") or "").strip()
            if role not in auth.ALL_ROLES:
                skipped.append({"staff_id": sid, "reason": f"role {role} 不合法"})
                continue
            target = next((r for r in rows if r.get("id") == sid and not r.get("deleted")), None)
            if not target:
                skipped.append({"staff_id": sid, "reason": "不存在"})
                continue
            # 防锁死: 最后一个 super_admin 不能降级
            if target.get("role") == auth.ROLE_SUPER_ADMIN and role != auth.ROLE_SUPER_ADMIN:
                if auth.count_super_admins() <= 1:
                    skipped.append({"staff_id": sid, "reason": "至少保留 1 个 super_admin"})
                    continue
            target["role"] = role
            target["updated_at"] = now()
            applied.append({"staff_id": sid, "name": target.get("name"), "new_role": role})
        auth.save_staff_rows(rows)
        return {"ok": True, "applied": applied, "skipped": skipped}

    app.include_router(router)
    logger.info("[routes/auth] 已注册 8 个权限/角色管理路由")


# ═══════════════════════════════════════════════════════════
# v2.1.15 启发式映射 — 模块顶层 (setup endpoint 通过模块 globals 也能访问)
# 工作 role 关键词 → 权限 role
# ═══════════════════════════════════════════════════════════
HEURISTIC_MAP = {
    # manager: 经理/总管/主管/店长
    "经理": auth.ROLE_MANAGER,
    "总管": auth.ROLE_MANAGER,
    "主管": auth.ROLE_MANAGER,
    "店长": auth.ROLE_MANAGER,
    "经理助理": auth.ROLE_MANAGER,
    # employee: 前台/客房/工程/餐厅/PA/接待 等一线员工
    "前台": auth.ROLE_EMPLOYEE,
    "客房": auth.ROLE_EMPLOYEE,
    "工程": auth.ROLE_EMPLOYEE,
    "维修": auth.ROLE_EMPLOYEE,
    "餐厅": auth.ROLE_EMPLOYEE,
    "服务员": auth.ROLE_EMPLOYEE,
    "接待": auth.ROLE_EMPLOYEE,
    "管家": auth.ROLE_EMPLOYEE,
    "PA": auth.ROLE_EMPLOYEE,
}

import json  # for /admin/auth/log