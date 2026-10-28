"""Optional phone push via ntfy.sh. Never raises: a notification problem must never affect trading."""
from __future__ import annotations

from typing import Optional

from . import net


def push(cfg: dict, title: str, body: str, priority: str = "default", tags: str = "chart_with_upwards_trend", post=net.get) -> bool:
    topic = ((cfg.get("notifications") or {}).get("ntfy_topic") or "").strip()
    if not topic:
        return False
    try:
        post("https://ntfy.sh/" + topic, headers={"Title": title.encode("ascii", "replace").decode()[:120], "Priority": priority, "Tags": tags},
             data=body.encode("utf-8")[:3500], method="POST", timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return False
