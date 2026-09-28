"""navigation 导航层 (阶段五+)

阶段五：世界地图模型 (map.py)
阶段六：移动规划 (movement.py) — 路径→按键序列
阶段七：A*/2.5D 寻路 (pathfinding.py)
阶段八：自动寻找新钓点编排 (navigator.py)
"""
from navigation.map import (
    ExploredRegion, FishingSpot, Obstacle, PlayerPosition, WaterArea, WorldMap,
)
from navigation.movement import MoveAction, MoveActionType, MovementPlanner
from navigation.pathfinding import AStarPathfinder, PathNode, PathfindingGrid
from navigation.navigator import Navigator, NavigatorState, NavigationOutcome

__all__ = [
    "ExploredRegion", "FishingSpot", "Obstacle", "PlayerPosition",
    "WaterArea", "WorldMap",
    "MoveAction", "MoveActionType", "MovementPlanner",
    "PathNode", "PathfindingGrid", "AStarPathfinder",
    "Navigator", "NavigatorState", "NavigationOutcome",
]
