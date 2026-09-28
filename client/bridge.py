"""Python ↔ Minecraft Client 桥接接口。

职责：
- get_player_state() 获取实时坐标/朝向/状态
- goto(x,y,z) 让 Pathfinder 执行寻路
- stop_navigation() 紧急停止
- look_at(yaw, pitch) 平滑视角对准
- send_chat(msg) 发送聊天 (用于 Baritone 命令等)

阶段 1: 定义抽象接口。具体实现 (Baritone/自定义 Mod/OCR兜底) 在阶段 2+ 实现。
"""
from __future__ import annotations

import abc
from typing import Optional, Tuple

from .player_state import PlayerState


class ClientBridge(abc.ABC):
    """客户端桥接抽象。所有上层模块只依赖此接口。"""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """客户端是否连接/可用。"""

    @abc.abstractmethod
    def get_player_state(self) -> Optional[PlayerState]:
        """获取玩家实时状态。不可用返回 None。"""

    @abc.abstractmethod
    def goto(self, x: float, y: float, z: float) -> bool:
        """命令 Pathfinder 寻路到目标坐标。"""

    @abc.abstractmethod
    def stop_navigation(self) -> None:
        """立即停止所有寻路/移动。"""

    @abc.abstractmethod
    def look_at(self, yaw: float, pitch: float) -> bool:
        """平滑旋转视角到目标角度。"""

    @abc.abstractmethod
    def send_chat(self, message: str) -> bool:
        """发送聊天消息 (可用于 Baritone 命令)。"""

    def right_click(self) -> bool:
        """右键 (抛竿/收竿)。默认不实现，由具体桥接提供。"""
        return False

    def left_click(self) -> bool:
        return False


class OcrFallbackBridge(ClientBridge):
    """OCR HUD 坐标兜底桥接 (阶段 1 占位，阶段 2 实现)。

    当无法使用客户端模块时，通过 OCR 读取左下角坐标 HUD。
    导航仍由外部 Python 控制 (WASD)，性能较低。
    """

    def is_available(self) -> bool:
        return False

    def get_player_state(self) -> Optional[PlayerState]:
        return None

    def goto(self, x: float, y: float, z: float) -> bool:
        return False

    def stop_navigation(self) -> None:
        pass

    def look_at(self, yaw: float, pitch: float) -> bool:
        return False

    def send_chat(self, message: str) -> bool:
        return False
