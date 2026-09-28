"""钓鱼状态机。"""
from enum import Enum


class FishingState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    WAITING_CAST = "waiting_cast"      # 等待手动/自动抛竿
    FISHING = "fishing"                # 等待咬钩
    BITE = "bite"                      # 检测到咬钩
    PULLING = "pulling"                # 收竿
    RECAST_WAIT = "recast_wait"        # 等待后重新抛竿
    DEPLETED = "depleted"              # 鱼群枯竭
    STOPPED = "stopped"
    ERROR = "error"
