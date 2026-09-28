"""
ocr/models.py
-------------
OCR 数据模型。

OcrTextLine: 单行识别结果 (文本 + 置信度 + ROI 内坐标)
OcrResult:   单次识别的聚合结果 (所有行 + 拼接全文 + 是否成功)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class OcrTextLine:
    """单行识别结果"""

    text: str                                   # 识别文本
    confidence: float                           # 置信度 0~1
    bbox: Tuple[int, int, int, int]             # (x, y, w, h) ROI 内像素坐标

    def __repr__(self) -> str:
        return f"OcrTextLine(text={self.text!r}, conf={self.confidence:.2f}, bbox={self.bbox})"


@dataclass
class OcrResult:
    """
    单次 OCR 识别聚合结果。

    success=False 时 lines 为空，error 描述失败原因 (如引擎不可用 / 截图失败)。
    """

    lines: List[OcrTextLine] = field(default_factory=list)
    full_text: str = ""                         # 所有行文本拼接 (空格分隔)
    success: bool = False
    error: Optional[str] = None
    elapsed_ms: float = 0.0                     # 本次识别耗时 (毫秒)

    @property
    def available(self) -> bool:
        """是否识别成功且有文本"""
        return self.success and bool(self.lines)
