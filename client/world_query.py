"""世界方块查询接口。

用于动态站位合法性检查：
- 脚下是否可站立
- 身体/头顶是否无碰撞
- 视线是否能看到钓点
- 是否在禁区

阶段 1: 抽象接口。阶段 2+ 由客户端 Mod 实现方块查询。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class BlockInfo:
    x: int
    y: int
    z: int
    name: str = "air"
    is_solid: bool = False
    is_water: bool = False
    is_passable: bool = True


class WorldQuery(abc.ABC):
    @abc.abstractmethod
    def get_block(self, x: int, y: int, z: int) -> BlockInfo:
        ...

    def is_standable(self, x: float, y: float, z: float) -> bool:
        """玩家站在 (x,y,z) 是否合法：脚下实心 + 身体空气 + 头顶空气。"""
        bx, by, bz = int(x), int(y), int(z)
        foot = self.get_block(bx, by - 1, bz)
        body = self.get_block(bx, by, bz)
        head = self.get_block(bx, by + 1, bz)
        return foot.is_solid and body.is_passable and head.is_passable

    def has_line_of_sight(
        self,
        ox: float, oy: float, oz: float,
        tx: float, ty: float, tz: float,
    ) -> bool:
        """视线检测 (简单体素步进)。阶段 1 占位，阶段 2 实现。"""
        return True

    def count_obstacles(
        self,
        ox: float, oy: float, oz: float,
        tx: float, ty: float, tz: float,
    ) -> int:
        """投竿路径障碍数。阶段 1 占位。"""
        return 0


class DummyWorldQuery(WorldQuery):
    """无客户端时的占位实现，所有检查默认通过。"""

    def get_block(self, x: int, y: int, z: int) -> BlockInfo:
        return BlockInfo(x, y, z)
