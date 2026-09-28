"""
ocr/
----
OCR 识别层 (阶段一基础框架)。

职责：
- 从 Minecraft 窗口指定 ROI 截图并识别文字
- 将识别结果解析为游戏状态 (咬钩 / 钓点枯竭 / XYZ 坐标 等)
- 以独立后台线程运行，不阻塞主循环与输入控制

模块：
- models.py    数据模型 (OcrTextLine / OcrResult)
- regions.py   OCR ROI 配置 (相对 Minecraft 窗口的归一化坐标)
- engine.py    OCR 引擎抽象 + RapidOCR 实现 + 降级实现 + 工厂
- parser.py    识别结果解析为游戏状态
- service.py   后台线程化 OCR 服务 (截图 + 识别 + 缓存最新结果)

设计原则：
- OCR 与具体业务逻辑解耦，只产出 OcrResult，由 parser 解释
- 引擎可插拔：未安装 RapidOCR 时自动降级为 NoOcrEngine，不影响现有钓鱼逻辑
- 阶段一不接入钓鱼状态机，仅提供框架与测试入口
"""

from __future__ import annotations

from .models import OcrTextLine, OcrResult
from .regions import OcrRoiConfig
from .engine import OcrEngineBase, RapidOcrEngine, NoOcrEngine, create_ocr_engine
from .parser import OcrParser, GameState
from .service import OcrService

__all__ = [
    "OcrTextLine",
    "OcrResult",
    "OcrRoiConfig",
    "OcrEngineBase",
    "RapidOcrEngine",
    "NoOcrEngine",
    "create_ocr_engine",
    "OcrParser",
    "GameState",
    "OcrService",
]
