"""navigation/pathfinding.py
A* 2.5D 寻路 (阶段七)

职责：
- 在 3D 网格上寻路，考虑高度差 (2.5D：可上下台阶 1 格，超过则障碍)
- 纯数据层，禁止调用 pyautogui 或任何输入 API (分层约束)
- 节点含 x/y/z/walkable/cost/height_difference/obstacle
- 真实地形数据由后续阶段 (视觉/OCR) 填充到 PathfindingGrid，本阶段提供网格接口 + A* 实现

2.5D 规则：
- 水平移动 cost=1，对角线 cost=√2
- 上台阶 (height_diff=1) cost+0.5，需跳跃 (MovementPlanner 处理)
- 下台阶 (height_diff=-1) cost+0.2
- height_diff > 1 视为不可通行 (墙/高块)
- obstacle=True 的节点不可通行

架构允许未来升级为完整 3D 寻路 (游泳/飞行)，当前先满足地面寻路需求。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PathNode:
    """寻路网格节点"""
    x: int
    y: int                        # 高度 (方块)
    z: int
    walkable: bool = True
    cost: float = 1.0             # 进入此节点的代价
    height_difference: float = 0.0  # 相邻节点高度差 (运行时计算)
    obstacle: bool = False        # 是否障碍 (石头/墙/树)
    # A* 运行时字段
    g: float = 0.0                # 起点到本节点实际代价
    h: float = 0.0                # 本节点到终点启发代价
    f: float = 0.0                # g + h
    parent: Optional["PathNode"] = field(default=None, repr=False)

    def key(self) -> Tuple[int, int, int]:
        return (self.x, self.y, self.z)

    def __lt__(self, other: "PathNode") -> bool:
        # heapq 比较：f 相同时比 h (更接近终点优先)
        if self.f != other.f:
            return self.f < other.f
        return self.h < other.h


class PathfindingGrid:
    """
    寻路网格。用 dict 存储已知节点，未存储的坐标视为不可通行。
    真实地形数据后续由视觉/OCR 填充。
    """

    def __init__(self, grid_size: float = 1.0) -> None:
        self._nodes: Dict[Tuple[int, int, int], PathNode] = {}
        self.grid_size = grid_size

    def set_node(self, x: int, y: int, z: int, **kwargs) -> PathNode:
        """设置/更新一个节点"""
        key = (x, y, z)
        node = PathNode(x=x, y=y, z=z, **kwargs)
        self._nodes[key] = node
        return node

    def get_node(self, x: int, y: int, z: int) -> Optional[PathNode]:
        return self._nodes.get((x, y, z))

    def has_node(self, x: int, y: int, z: int) -> bool:
        return (x, y, z) in self._nodes

    def all_nodes(self) -> List[PathNode]:
        return list(self._nodes.values())

    def clear(self) -> None:
        self._nodes.clear()

    def get_neighbors(self, node: PathNode) -> List[PathNode]:
        """
        获取邻居节点 (8 方向水平 + 上下台阶)。
        2.5D：检查相邻 x/z 位置在 y-1 / y / y+1 高度的节点。
        """
        neighbors: List[PathNode] = []
        # 8 方向 (水平)
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dz == 0:
                    continue
                # 同高度
                n = self._try_neighbor(node, dx, 0, dz)
                if n is not None:
                    neighbors.append(n)
                # 上台阶 (y+1)
                n = self._try_neighbor(node, dx, 1, dz)
                if n is not None:
                    neighbors.append(n)
                # 下台阶 (y-1)
                n = self._try_neighbor(node, dx, -1, dz)
                if n is not None:
                    neighbors.append(n)
        return neighbors

    def _try_neighbor(self, node: PathNode, dx: int, dy: int, dz: int) -> Optional[PathNode]:
        """尝试获取一个邻居，应用 2.5D 规则。返回带 height_difference 的副本。"""
        nx, ny, nz = node.x + dx, node.y + dy, node.z + dz
        n = self._nodes.get((nx, ny, nz))
        if n is None or not n.walkable or n.obstacle:
            return None
        # 对角线移动时，要求两个正交邻居也通行 (防穿墙)
        if dx != 0 and dz != 0:
            n1 = self._nodes.get((node.x + dx, node.y, node.z))
            n2 = self._nodes.get((node.x, node.y, node.z + dz))
            if (n1 is None or not n1.walkable or n1.obstacle) and \
               (n2 is None or not n2.walkable or n2.obstacle):
                return None
        # height_difference
        n.height_difference = float(ny - node.y)
        # 上台阶超过 1 视为不可通行
        if n.height_difference > 1:
            return None
        return n


class AStarPathfinder:
    """A* 寻路器"""

    def __init__(self, grid: PathfindingGrid, max_steps: int = 500) -> None:
        self._grid = grid
        self._max_steps = max_steps

    def heuristic(self, a: PathNode, b: PathNode) -> float:
        """3D 欧几里得距离启发"""
        return math.sqrt(
            (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
        )

    def move_cost(self, current: PathNode, neighbor: PathNode) -> float:
        """移动代价：水平/对角 + 高度差"""
        dx = abs(neighbor.x - current.x)
        dz = abs(neighbor.z - current.z)
        # 对角线 √2，直线 1
        base = math.sqrt(2) if (dx > 0 and dz > 0) else 1.0
        # 高度差代价
        hd = neighbor.height_difference
        if hd > 0:
            base += 0.5 * hd      # 上台阶
        elif hd < 0:
            base += 0.2 * abs(hd)  # 下台阶
        return base * neighbor.cost

    def find_path(
        self, start: Tuple[int, int, int], goal: Tuple[int, int, int],
    ) -> List[PathNode]:
        """
        A* 寻路。
        Args:
            start: (x, y, z) 起点坐标
            goal: (x, y, z) 终点坐标
        Returns:
            路径节点列表 (含起点和终点)，失败返回空列表
        """
        start_node = self._grid.get_node(*start)
        goal_node = self._grid.get_node(*goal)
        if start_node is None or goal_node is None:
            return []
        if not start_node.walkable or not goal_node.walkable:
            return []

        start_node.g = 0.0
        start_node.h = self.heuristic(start_node, goal_node)
        start_node.f = start_node.h
        start_node.parent = None

        open_heap: List[PathNode] = [start_node]
        closed: set = set()
        steps = 0

        while open_heap:
            if steps >= self._max_steps:
                break
            steps += 1
            current = heapq.heappop(open_heap)
            if current.key() == goal_node.key():
                return self._reconstruct(current)
            closed.add(current.key())

            for neighbor in self._grid.get_neighbors(current):
                if neighbor.key() in closed:
                    continue
                tentative_g = current.g + self.move_cost(current, neighbor)
                in_open = any(n.key() == neighbor.key() for n in open_heap)
                if not in_open or tentative_g < neighbor.g:
                    neighbor.g = tentative_g
                    neighbor.h = self.heuristic(neighbor, goal_node)
                    neighbor.f = neighbor.g + neighbor.h
                    neighbor.parent = current
                    if not in_open:
                        heapq.heappush(open_heap, neighbor)

        return []

    def _reconstruct(self, node: PathNode) -> List[PathNode]:
        """从终点回溯路径"""
        path: List[PathNode] = []
        cur: Optional[PathNode] = node
        while cur is not None:
            path.append(cur)
            cur = cur.parent
        path.reverse()
        return path
