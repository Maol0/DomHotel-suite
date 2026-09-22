"""DomHotel v2 单号序列生成器

格式：XQ-YYYYMMDD-NNNNNNNN / GD-YYYYMMDD-NNNNNNNN
使用 .seq.json 持久化每日序列，删单不复用。
"""

import json
import threading
from pathlib import Path
from typing import Dict

from . import data_layer

_SEQ_FILE = data_layer.DATA_DIR / ".seq.json"
_LOCK = threading.Lock()


def _load_seq() -> Dict[str, int]:
    with _LOCK:
        if not _SEQ_FILE.exists():
            return {}
        try:
            return json.loads(_SEQ_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _save_seq(data: Dict[str, int]) -> None:
    with _LOCK:
        _SEQ_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def next_seq_id(prefix: str, date_str: str, width: int = 8) -> str:
    """生成下一个序列号，返回 NN...NN（固定宽度）"""
    key = f"{prefix}:{date_str}"
    data = _load_seq()
    current = data.get(key, 0) + 1
    data[key] = current
    _save_seq(data)
    return str(current).zfill(width)


def new_request_id(date_str: str = "") -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = date_str or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    return f"XQ-{d}-{next_seq_id('XQ', d)}"


def new_ticket_id(date_str: str = "") -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = date_str or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    return f"GD-{d}-{next_seq_id('GD', d)}"
