"""vision 视觉识别层 (阶段四)

统一接口：VisionDetector.detect(frame) -> VisionResult
可替换底层实现 (OpenCV/分割模型) 而不修改上层 navigation。
"""
from vision.models import (
    CandidateSpot, Region, TerrainClass, VisionResult, WaterRegion,
)
from vision.water import WaterDetector
from vision.terrain import TerrainAnalyzer
from vision.detector import VisionDetector

__all__ = [
    "CandidateSpot", "Region", "TerrainClass", "VisionResult", "WaterRegion",
    "WaterDetector", "TerrainAnalyzer", "VisionDetector",
]
