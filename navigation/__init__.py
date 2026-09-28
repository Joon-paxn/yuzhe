"""navigation 导航层 (阶段五+)

阶段五：世界地图模型 (map.py)
后续阶段：pathfinding.py (A*/2.5D 寻路)、navigator.py (移动控制)
"""
from navigation.map import (
    ExploredRegion, FishingSpot, Obstacle, PlayerPosition, WaterArea, WorldMap,
)

__all__ = [
    "ExploredRegion", "FishingSpot", "Obstacle", "PlayerPosition",
    "WaterArea", "WorldMap",
]
