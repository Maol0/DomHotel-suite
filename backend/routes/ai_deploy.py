# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — AI 一键部署路由

新机器装好 plugin 后,用户点 UI 上的"🚀 一键部署 4 个基础 agent"按钮,
本端点把 templates/{hotel-ai-*} 目录里的 PROFILE.md / SOUL.md / AGENTS.md / agent.json
写到 QWENPAW_WORKING_DIR/workspaces/{agent_id}/ 里,并通过 qwenpaw agent CLI 注册。

设计原则:
  - 完全幂等: 已存在的不动配置,缺的补上
  - 不删除任何用户已有数据 (memory/ chats/ skills/ 等子目录)
  - channels 全部 disabled (防止 token 被覆盖)
  - 部署后建议用户重启 QwenPaw (agent 注册需要 reload)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_build_active_model(provider_id, model_id):
    """
    安全的 active_model 构造:
      - 用户已有 provider + model → 直接构造 (即使 provider 是用户自加的也行)
      - provider 不存在或 model 不在 provider 里 → 返回 None (用全局默认模型)
      - 而不是直接 raise (会让整个 agent 部署失败)

    关键: 模型是用户自己的事, plugin 不强制绑定某个 provider。
    """
    if not provider_id or not model_id:
        return None
    try:
        return _build_active_model_config(provider_id, model_id)
    except Exception as e:
        # provider 不在 (用户机器上没装 aliyun-codingplan) 或 model 不存在
        # → 返回 None, 让 agent 用全局默认模型
        logger.info(
            f"[ai_deploy] active_model '{provider_id}/{model_id}' 在当前环境不可用, "
            f"agent 将使用全局默认模型 (用户在 console 里可自行配置)"
        )
        logger.debug(f"[ai_deploy] _safe_build_active_model 异常详情: {e}")
        return None


def _resolve_workspace_base() -> Path:
    """与 QwenPaw constant.py 的 WORKING_DIR 解析规则保持一致:
    1. QWENPAW_WORKING_DIR env → 用它
    2. ~/.copaw 存在 (legacy) → 用它
    3. 默认 → ~/.qwenpaw

    修复 v2.1.0 之前硬编码 /app/working 的问题 (在 8899 / 群晖 / 自定义部署下会 500)
    """
    explicit = os.environ.get("QWENPAW_WORKING_DIR") or os.environ.get("COPAW_WORKING_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve() / "workspaces"
    legacy = Path("~/.copaw").expanduser()
    if legacy.exists():
        return legacy.resolve() / "workspaces"
    return Path("~/.qwenpaw").expanduser().resolve() / "workspaces"


WORKSPACE_BASE = _resolve_workspace_base()

# plugin/templates/ 目录(本模块相对路径向上 3 级到 plugin 根)
_THIS_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = (_THIS_DIR.parent.parent / "templates").resolve()

# 4 个内置 agent 模板定义(必须与 HOTEL_AI_TEMPLATES 对应)
BUILTIN_AGENTS = [
    {
        "agent_id": "hotel-ai-frontdesk",
        "name": "酒店 AI - 前台小帮",
        "description": "酒店前台 AI 助手:接电话、查房态、改派工单、登记客人需求。",
    },
    {
        "agent_id": "hotel-ai-engineering",
        "name": "酒店 AI - 工程师傅",
        "description": "酒店工程 AI 助手:维修派单、设备状态跟踪、紧急维修响应。",
    },
    {
        "agent_id": "hotel-ai-housekeeping",
        "name": "酒店 AI - 客房管家",
        "description": "酒店客房 AI 助手:清洁派工、查脏房、跟踪房间清洁进度、申领耗材。",
    },
    {
        "agent_id": "hotel-ai-guest-service",
        "name": "酒店 AI - 客人服务",
        "description": "酒店客人 AI 助手:知识问答、需求接收、投诉处理。",
    },
]

PROMPT_FILES = ["PROFILE.md", "SOUL.md", "AGENTS.md", "agent.json"]


def _read_template_files(agent_id: str) -> Dict[str, str]:
    """读取 templates/{agent_id}/ 下所有 prompt 文件,返回 {filename: content}"""
    tmpl_dir = TEMPLATES_DIR / agent_id
    if not tmpl_dir.is_dir():
        raise FileNotFoundError(f"模板目录不存在: {tmpl_dir}")
    out: Dict[str, str] = {}
    for fn in PROMPT_FILES:
        p = tmpl_dir / fn
        if not p.is_file():
            logger.warning(f"[ai_deploy] 模板 {agent_id} 缺 {fn}, 跳过")
            continue
        out[fn] = p.read_text(encoding="utf-8")
    return out


def _merge_agent_json(existing: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并 agent.json:
      - id/name/description/language/approval_level 从 template 取
      - active_model 不强制覆盖: 用户已有的保留(避免把用户自己的模型覆盖掉)
                       只在用户没有设置时才用 template 里的(且 template 必须是有 provider 的)
      - channels 全部设 enabled=False (防 token 覆盖)
      - 其他字段(existing 已有)保留
      - 不丢任何现有字段(except channels.enabled)
    """
    merged = dict(existing)
    # 顶层标识符 (active_model 单独处理)
    for k in ["id", "name", "description", "language", "approval_level",
              "system_prompt_files", "template_id"]:
        if k in template:
            merged[k] = template[k]

    # active_model: 用户已有 → 保留; 没有才用 template 的
    # 这样用户在 console 里手动改的模型不会被 plugin 部署覆盖
    user_has_model = (
        existing.get("active_model", {}).get("provider_id")
        and existing.get("active_model", {}).get("model")
    )
    if not user_has_model and template.get("active_model"):
        merged["active_model"] = template["active_model"]
    # workspace_dir: 强制用运行时路径(模板里的可能是开发机绝对路径)
    merged["workspace_dir"] = str(WORKSPACE_BASE / template["id"])
    # channels 全部 disabled
    if "channels" in template:
        merged["channels"] = {
            k: {**v, "enabled": False}
            for k, v in template["channels"].items()
        }
    return merged


def _register_agent_via_api(agent_id: str, workspace: Path) -> Dict[str, Any]:
    """
    通过 QwenPaw 内部 SDK 注册 agent (而不是起子进程调 CLI)。

    复用 qwenpaw.cli.agents_cmd 里 create_cmd 的核心逻辑:
      - load_config / save_config / save_agent_config (QwenPaw 自己的注册表)
      - build_agent_template (按 template 初始化 agent.json)
      - _initialize_new_agent_workspace (写 AGENTS.md / PROFILE.md / SOUL.md)
      - _build_active_model_config (绑定 LLM)

    这样:
      1. config.agents.profiles 内存里立即更新, console 下次 list 就能看到
      2. 不依赖子进程, 不污染 cwd, 不需要把 qwenpaw CLI 装在 PATH
      3. 已存在走 "already exists" 视为成功, 不报错

    返回 ok=True 的两种情况:
      - 全新创建成功
      - 已存在 = 达成目标,不算失败
    """
    try:
        # 导入 QwenPaw 内部模块
        from qwenpaw.cli.agents_cmd import (
            _build_agent_workspace_dir,
            _build_active_model_config,
            _initialize_new_agent_workspace,
        )
        from qwenpaw.config import load_config, save_config
        from qwenpaw.config.config import (
            save_agent_config,
            AgentProfileConfig,
            AgentProfileRef,
            ChannelConfig,
            MCPConfig,
            HeartbeatConfig,
            ToolsConfig,
        )
        from qwenpaw.agents.templates import build_agent_template
        from qwenpaw.agents.utils.setup_utils import normalize_agent_language
    except ImportError as e:
        return {
            "ok": False,
            "msg": f"QwenPaw SDK import 失败: {e}",
        }

    try:
        # 读 agent.json 拿 name / description / active_model
        agent_json = workspace / "agent.json"
        if not agent_json.is_file():
            return {"ok": False, "msg": "agent.json 不存在"}
        data = json.loads(agent_json.read_text(encoding="utf-8"))

        config = load_config()
        existing_ids = set(config.agents.profiles.keys())

        new_id = data.get("id") or agent_id
        if new_id in existing_ids:
            # 已注册 = 达成目标
            existing_ref = config.agents.profiles[new_id]
            return {
                "ok": True,
                "already_exists": True,
                "agent_id": new_id,
                "workspace_dir": existing_ref.workspace_dir,
            }

        # 1) workspace 目录 (QwenPaw 自己解析, 不写死)
        resolved_workspace = Path(
            str(workspace)
        ).expanduser().resolve()
        resolved_workspace.mkdir(parents=True, exist_ok=True)

        # 2) 模板 + agent_config
        template = data.get("template_id", "default")
        try:
            template_result = build_agent_template(
                template,
                agent_id=new_id,
                workspace_dir=resolved_workspace,
                fallback_language=(
                    getattr(config.agents, "language", None) or "zh"
                ),
                name=data.get("name", new_id),
                description=data.get("description", ""),
                language=data.get("language"),
            )
        except Exception as e:
            # 模板失败 → 用最小 AgentProfileConfig 注册(只有 profile, 不写模板文档)
            agent_config = AgentProfileConfig(
                id=new_id,
                name=data.get("name", new_id),
                description=data.get("description", ""),
                workspace_dir=str(resolved_workspace),
                language=normalize_agent_language(
                    data.get("language")
                    or getattr(config.agents, "language", None)
                    or "zh"
                ),
                channels=ChannelConfig(),
                mcp=MCPConfig(),
                heartbeat=HeartbeatConfig(),
                tools=ToolsConfig(),
                active_model=_safe_build_active_model(
                    data.get("active_model", {}).get("provider_id"),
                    data.get("active_model", {}).get("model"),
                ),
            )
        else:
            agent_config = template_result.agent_config
            agent_config.active_model = _safe_build_active_model(
                data.get("active_model", {}).get("provider_id"),
                data.get("active_model", {}).get("model"),
            )

        # 3) 初始化 workspace (写 AGENTS.md / PROFILE.md / SOUL.md 模板)
        try:
            _initialize_new_agent_workspace(
                resolved_workspace,
                skill_names=list(
                    getattr(template_result, "initial_skill_names", [])
                ) if "template_result" in locals() else [],
                md_template_id=getattr(
                    template_result, "md_template_id", None
                ) if "template_result" in locals() else None,
            )
        except Exception:
            # 初始化失败不致命,我们自己已经写了文档
            pass

        # 4) 写 config + agent.json
        agent_ref = AgentProfileRef(
            id=new_id,
            workspace_dir=str(resolved_workspace),
            enabled=True,
        )
        config.agents.profiles[new_id] = agent_ref
        save_config(config)
        save_agent_config(new_id, agent_config)

        return {
            "ok": True,
            "agent_id": new_id,
            "workspace_dir": str(resolved_workspace),
        }

    except Exception as e:
        logger.exception(f"[ai_deploy] _register_agent_via_api {agent_id} 失败")
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _qwenpaw_cmd():
    """跟 workflow-studio 一致 — plugin 与主程序共享 venv, 直接用同一 Python 解释器"""
    py = os.environ.get("QWENPAW_PYTHON") or sys.executable
    return [py, "-m", "qwenpaw"]


def _register_agent_via_api(agent_id: str, workspace: Path) -> Dict[str, Any]:
    """
    通过子进程调 `qwenpaw agent create` 注册 agent (跟 workflow-studio 一样的做法)。

    优势 (相对之前的 SDK 直调):
      1. CLI 走的是 qwenpaw 完整校验流程, 包括 config.agents.profiles / agent.json /
         AGENTS.md / PROFILE.md / SOUL.md 全套
      2. 不会因为 SDK 内部某个 schema 字段不一致而报错
      3. 跨平台一致 (WSL/Docker/Windows native 同样的命令行)
      4. 显式传 QWENPAW_WORKING_DIR, 子进程不会用 ~/.qwenpaw 默认

    设计:
      - 先 `qwenpaw agent list --json` 查是否已存在, 存在 = 成功
      - 不存在则 `qwenpaw agent create --agent-id X --workspace-dir Y --template qa ...`
    """
    import subprocess

    try:
        agent_json_path = workspace / "agent.json"
        if not agent_json_path.is_file():
            return {"ok": False, "msg": f"agent.json 不存在: {agent_json_path}"}

        agent_data = json.loads(agent_json_path.read_text(encoding="utf-8"))
        new_id = agent_data.get("id") or agent_id

        # 探测 QWENPAW_WORKING_DIR — workflow-studio 顺序
        env_wd = os.environ.get("QWENPAW_WORKING_DIR", "")
        wd_candidates = [
            env_wd,
            str(Path.home() / ".copaw"),
            str(Path.home() / "copaw-data"),
            str(Path.home() / ".qwenpaw"),
        ]
        wd_candidates = [c for c in wd_candidates if c]
        workspace_root = None
        for cand in wd_candidates:
            cand_path = Path(cand)
            if (cand_path / "workspaces").is_dir():
                workspace_root = cand
                break
        if not workspace_root:
            workspace_root = str(Path.home() / ".copaw")  # fallback

        # 1) list 看 agent 是否已注册
        #    v2.1.5 修复: 不再用 httpx 调 http://127.0.0.1:8889 (硬编码端口,8899/其他机器全挂)
        #    改用 QwenPaw 内部 SDK 直接读 config.agents.profiles (主进程内,无需端口)
        existing_ids = set()
        try:
            from qwenpaw.config import load_config
            cfg = load_config()
            profiles = getattr(cfg.agents, "profiles", None) or {}
            if isinstance(profiles, dict):
                existing_ids = set(profiles.keys())
        except Exception as e:
            logger.warning(f"[ai_deploy] load_config 失败, 退而用 subprocess: {e}")
            try:
                list_cmd = _qwenpaw_cmd() + ["agent", "list"]
                list_env = os.environ.copy()
                list_env["QWENPAW_WORKING_DIR"] = workspace_root
                list_proc = subprocess.run(
                    list_cmd, capture_output=True, text=True, timeout=15, env=list_env
                )
                if list_proc.stdout:
                    list_data = json.loads(list_proc.stdout)
                    items = list_data.get("agents", list_data) if isinstance(list_data, dict) else list_data
                    if isinstance(items, list):
                        for it in items:
                            if isinstance(it, dict):
                                existing_ids.add(it.get("id") or it.get("agent_id"))
            except Exception:
                pass

        if new_id in existing_ids:
            return {
                "ok": True,
                "already_exists": True,
                "agent_id": new_id,
                "workspace_dir": str(workspace),
                "stdout": "",
                "stderr": "",
            }

        # 2) 准备 create 命令 — 优先用当前 server 的 active model, 而不是模板里的硬编码
        #    v2.1.5 修复: 不再用 httpx (硬编码端口), 改用 ProviderManager.get_active_model()
        provider_id = ""
        model_id = ""
        try:
            from qwenpaw.providers.manager import ProviderManager
            pm = ProviderManager.get_instance()
            global_active = pm.get_active_model()
            if global_active and getattr(global_active, "provider_id", "") and getattr(global_active, "model", ""):
                provider_id = global_active.provider_id
                model_id = global_active.model
                logger.info(f"[ai_deploy] 用全局 active model: {provider_id}/{model_id}")
        except Exception as e:
            logger.warning(f"[ai_deploy] ProviderManager.get_active_model() 失败, 用模板默认: {e}")

        # 如果 active model 拿不到, 才退到模板 (v2.1.5: 模板 active_model=None, 所以也是空)
        if not provider_id:
            provider_id = (agent_data.get("active_model") or {}).get("provider_id", "")
            model_id = (agent_data.get("active_model") or {}).get("model", "")
            # v2.1.5: 不再 fallback 到 aliyun-codingplan/qwen3.6-plus 硬编码
            # 用户装好 plugin 后, 在 console 自己给 agent 绑模型
            if not provider_id:
                logger.info(
                    f"[ai_deploy] 没有全局 active model, agent '{new_id}' 将以 active_model=None 创建, "
                    f"用户需在 console 里绑定 LLM"
                )
        template_id = agent_data.get("template_id") or "qa"

        create_cmd = _qwenpaw_cmd() + [
            "agent", "create",
            "--agent-id", new_id,
            "--name", agent_data.get("name") or new_id,
            "--description", agent_data.get("description") or "",
            "--workspace-dir", str(workspace),
            "--template", template_id,
            "--language", agent_data.get("language") or "zh",
        ]
        if provider_id:
            create_cmd += ["--provider-id", provider_id]
        if model_id:
            create_cmd += ["--model-id", model_id]

        # 3) 显式传 QWENPAW_WORKING_DIR
        create_env = os.environ.copy()
        create_env["QWENPAW_WORKING_DIR"] = workspace_root

        logger.info(f"[ai_deploy] 注册 agent: {' '.join(create_cmd[:6])}... (env QWENPAW_WORKING_DIR={workspace_root})")

        try:
            proc = subprocess.run(
                create_cmd, capture_output=True, text=True, timeout=30, env=create_env
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            # already exists = 达成目标, 不报错 (跟 workflow-studio 一致)
            if proc.returncode != 0 and "already exists" in (err + out).lower():
                logger.info(f"[ai_deploy] ✓ agent {new_id} 已存在, 视为成功")
                return {
                    "ok": True,
                    "already_exists": True,
                    "agent_id": new_id,
                    "workspace_dir": str(workspace),
                    "stdout": out[:500],
                    "stderr": err[:500],
                }
            if proc.returncode != 0:
                logger.error(f"[ai_deploy] agent create 失败 (exit {proc.returncode}): {err or out}")
                return {
                    "ok": False,
                    "msg": f"qwenpaw agent create exit {proc.returncode}: {err[:200] or out[:200]}",
                    "agent_id": new_id,
                    "workspace_dir": str(workspace),
                    "stdout": out[:500],
                    "stderr": err[:500],
                }
            logger.info(f"[ai_deploy] ✓ agent create 成功: {new_id}")
            return {
                "ok": True,
                "already_exists": False,
                "agent_id": new_id,
                "workspace_dir": str(workspace),
                "stdout": out[:500],
                "stderr": err[:500],
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": "qwenpaw agent create 超时 (30s)"}
        except Exception as e:
            return {"ok": False, "msg": f"subprocess 异常: {type(e).__name__}: {e}"}

    except Exception as e:
        logger.exception(f"[ai_deploy] _register_agent_via_api {agent_id} 失败")
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# 保留旧名以兼容调用方
def _register_agent_via_cli(agent_id: str, workspace: Path) -> Dict[str, Any]:
    """向后兼容: 现在 _register_agent_via_api 已经是 subprocess CLI 路线了。"""
    return _register_agent_via_api(agent_id, workspace)


@router.get("/ai/deploy/preview")
async def preview_deploy() -> Dict[str, Any]:
    """
    预览:列出 4 个 agent 当前的部署状态,告诉 UI 该建/补/跳。
    不写任何文件。
    """
    out: Dict[str, Any] = {"agents": [], "all_ready": True}
    for spec in BUILTIN_AGENTS:
        agent_id = spec["agent_id"]
        workspace = WORKSPACE_BASE / agent_id
        exists = workspace.is_dir()
        checks: Dict[str, bool] = {
            "workspace_exists": exists,
            "has_profile": (workspace / "PROFILE.md").is_file() if exists else False,
            "has_soul": (workspace / "SOUL.md").is_file() if exists else False,
            "has_agents_md": (workspace / "AGENTS.md").is_file() if exists else False,
            "has_agent_json": (workspace / "agent.json").is_file() if exists else False,
        }
        all_ok = all(checks.values())
        if not all_ok:
            out["all_ready"] = False
        action = "skip" if all_ok else ("create" if not exists else "fix")
        out["agents"].append({
            "agent_id": agent_id,
            "name": spec["name"],
            "description": spec["description"],
            "workspace": str(workspace),
            "checks": checks,
            "all_ready": all_ok,
            "action": action,
        })
    return out


def _deploy_one(
    spec: Dict[str, Any],
    agent_result: Dict[str, Any],
    workspace: Path,
    results: List[Dict[str, Any]],
) -> None:
    """部署单个 agent 的完整流程(跟 workflow-studio 一样的 7 步)。

    顺序 (这是关键, 别乱):
      1) workspace 目录 + 8 个核心子目录 (workflow-studio 经验 — 缺这些子目录, console 加载会失败)
      2) 调 `qwenpaw agent create` 注册 (CLI 自动写 agent.json + AGENTS.md/SOUL.md/PROFILE.md 骨架)
      3) 用 plugin 模板的 AGENTS.md/SOUL.md/PROFILE.md 覆盖 CLI 生成的 (我们有专门的酒店 AI 提示词)
      4) 删 BOOTSTRAP.md (避免强制 bootstrap 模式)
      5) patch agent.json (name / description / system_prompt_files / active_model 来自模板)

    任何一步出错都把信息塞到 agent_result, 不抛 (unhandled 异常由 deploy_all 的 try 包住)。
    """
    agent_id = spec["agent_id"]

    # 0) 读模板
    try:
        tmpl_files = _read_template_files(agent_id)
    except FileNotFoundError as e:
        agent_result["errors"].append(str(e))
        results.append(agent_result)
        return

    # 1) workspace + 8 个核心子目录
    workspace.mkdir(parents=True, exist_ok=True)
    for sub in ["skills", "memory", "sessions", "dialog",
                "tool_results", "file_store", "embedding_cache", "backup"]:
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    agent_result["actions"].append(f"workspace={workspace} (含 8 个核心子目录)")

    # 2) 写 agent.json — 如果已存在则合并(保留用户设置如 active_model),不覆盖
    target_json = workspace / "agent.json"
    if "agent.json" not in tmpl_files:
        agent_result["errors"].append("模板缺 agent.json")
    else:
        template_json = json.loads(tmpl_files["agent.json"])
        template_json["workspace_dir"] = str(workspace)
        if target_json.exists():
            # 已存在 → 合并: 模板字段做默认值,用户已改的字段保留
            try:
                existing = json.loads(target_json.read_text(encoding="utf-8"))
                # 用户设置优先: active_model / provider_id / llm_routing 等
                for key in ("active_model", "provider_id", "llm_routing",
                            "channels", "tools", "mcp", "language",
                            "approval_level", "plan", "coding_mode"):
                    if key in existing and existing[key] is not None:
                        template_json[key] = existing[key]
                agent_result["actions"].append("agent.json=merged(保留用户设置)")
            except Exception:
                agent_result["actions"].append("agent.json=merge_failed(用模板覆盖)")
        else:
            agent_result["actions"].append("agent.json=created")
        target_json.write_text(
            json.dumps(template_json, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    # 3) 注册 agent (走 subprocess `qwenpaw agent create` — 跟 workflow-studio 一致)
    cli_result = _register_agent_via_cli(agent_id, workspace)
    if cli_result.get("already_exists"):
        agent_result["actions"].append("registered(已存在,跳过)")
    elif cli_result.get("ok"):
        agent_result["actions"].append("registered")
    else:
        agent_result["errors"].append(
            f"register failed: {cli_result.get('msg') or cli_result.get('stderr','')}"
        )
        agent_result["cli_result"] = cli_result

    # 4) 写文档 (覆盖 CLI 生成的骨架, 让 AGENTS.md 等用我们的酒店 AI 提示词)
    for fn in ["PROFILE.md", "SOUL.md", "AGENTS.md"]:
        target = workspace / fn
        target.write_text(tmpl_files.get(fn, ""), encoding="utf-8")
        agent_result["actions"].append(f"{fn}=written(覆盖)")

    # 5) 删 BOOTSTRAP.md (避免强制 bootstrap 模式)
    bootstrap_path = workspace / "BOOTSTRAP.md"
    if bootstrap_path.exists():
        bootstrap_path.unlink()
        agent_result["actions"].append("BOOTSTRAP.md=deleted(跳过 BOOTSTRAP 模式)")

    agent_result["ok"] = True  # 不管 register 成功没, 文件都写好了
    results.append(agent_result)


@router.post("/ai/deploy")
async def deploy_all() -> Dict[str, Any]:
    """
    一键部署 4 个内置 agent:
      1. 创建 workspace 目录(不存在)
      2. 写 PROFILE.md / SOUL.md / AGENTS.md (缺就补,不覆盖用户已改的内容)
      3. 写 agent.json (合并模板 + 现有)
      4. 调 qwenpaw agent register (失败不致命)

    v2.1.1 修复: 单个 agent 异常不再让整个端点返回 500
    v2.1.1 修复: WORKSPACE_BASE 改用 QwenPaw 自己的解析规则
              (env QWENPAW_WORKING_DIR → ~/.copaw → ~/.qwenpaw),
              不再硬编码 /app/working

    部署后用户需要重启 QwenPaw 让 console 重新加载 agent 列表。
    """
    results: List[Dict[str, Any]] = []
    overall_ok = True

    for spec in BUILTIN_AGENTS:
        agent_id = spec["agent_id"]
        workspace = WORKSPACE_BASE / agent_id
        agent_result: Dict[str, Any] = {
            "agent_id": agent_id,
            "name": spec["name"],
            "ok": False,
            "actions": [],
            "errors": [],
        }
        try:
            _deploy_one(spec, agent_result, workspace, results)
        except Exception as e:
            # 单个 agent 任何 unhandled 异常 → 记录,不打断其他 3 个
            logger.exception(f"[ai_deploy] {agent_id} 部署异常")
            agent_result["errors"].append(f"unhandled: {type(e).__name__}: {e}")
            if agent_result not in results:
                results.append(agent_result)

    return {
        "ok": overall_ok,
        "agents": results,
        "next_step": "重启 QwenPaw 让 console 重新加载 agent 列表 (qwenpaw daemon restart)",
        "restart_required": True,
        "workspace_base": str(WORKSPACE_BASE),  # v2.1.1: 告诉前端实际写入位置
    }


# ─────────────────────────────────────────────────────────────
# 后台自动部署 — 让新装 plugin 的用户无需手动操作
# v2.1.3: 新装 plugin 时自动跑一次 ai_deploy, 4 个 agent 立刻可用
# ─────────────────────────────────────────────────────────────

_auto_deploy_started = False
_auto_deploy_lock_file = WORKSPACE_BASE / ".hotel-ai-auto-deploy.lock" if WORKSPACE_BASE else None


async def auto_deploy_on_plugin_load():
    """
    plugin 加载后自动跑一次 4-agent 部署。

    设计:
      - 用 .lock 文件防止重复跑 (热加载时不会重复)
      - 只在 server 启动后跑一次, daemon 模式
      - 失败不影响 plugin 加载 (try/except 包住)
      - 日志写 logger, 用户可以看 qwenpaw.log

    调用方: backend/__init__.py 里 app.register() 之后
    """
    global _auto_deploy_started
    if _auto_deploy_started:
        return
    _auto_deploy_started = True

    import asyncio

    # lock 文件: 写一个 marker 文件, 防止重复跑
    if _auto_deploy_lock_file and _auto_deploy_lock_file.exists():
        try:
            age = _auto_deploy_lock_file.stat().st_mtime
            import time
            # 1 小时内的 lock 视为"刚跑过", 跳过
            if (time.time() - age) < 3600:
                logger.info("[ai_deploy] auto-deploy 已在 1h 内跑过, 跳过")
                return
        except Exception:
            pass

    # 等 5 秒, 让 plugin 完全初始化后再跑 (避免 race condition)
    await asyncio.sleep(5)

    logger.info("[ai_deploy] ====== 自动部署 4 个 AI 助手 (plugin 首次加载) ======")
    try:
        result = await deploy_all()
        ok_count = sum(1 for a in result.get("agents", []) if a.get("ok"))
        total = len(result.get("agents", []))
        logger.info(f"[ai_deploy] 自动部署结果: {ok_count}/{total} 成功")

        # 写 lock 文件
        if _auto_deploy_lock_file:
            try:
                _auto_deploy_lock_file.parent.mkdir(parents=True, exist_ok=True)
                _auto_deploy_lock_file.write_text(
                    f"deployed_at={result.get('deployed_at','?')}\nok_count={ok_count}/{total}\n",
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(f"[ai_deploy] 写 lock 文件失败: {e}")

        # 如果全部 ok, 不再喊 restart_required (因为已经部署过了)
        # 但是 console 还是要 reload 才能看到新 agent — 这部分无法绕过
        if ok_count == total:
            logger.info(
                "[ai_deploy] ✓ 4 个 agent 已就绪. "
                "如 console 还没显示, 请运行: qwenpaw daemon restart"
            )
        else:
            logger.warning(
                f"[ai_deploy] ⚠ {total - ok_count} 个 agent 失败, "
                f"请打开 plugin → AI 助手 tab → 一键部署"
            )
    except Exception as e:
        logger.exception(f"[ai_deploy] auto_deploy 失败: {e}")


def schedule_auto_deploy():
    """
    调度后台自动部署任务 — 在 event loop 里跑。

    调用方: backend/__init__.py 里 plugin 加载完成后
    """
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(auto_deploy_on_plugin_load())
            logger.info("[ai_deploy] 后台自动部署任务已调度")
        else:
            logger.info("[ai_deploy] event loop 未运行, 跳过自动部署")
    except RuntimeError:
        logger.info("[ai_deploy] 无 event loop, 跳过自动部署")
