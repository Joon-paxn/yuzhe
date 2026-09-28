"""test_map.py
阶段五世界地图模型测试。

用法：python test_map.py
"""
from __future__ import annotations

import os
import tempfile

from navigation import FishingSpot, PlayerPosition, WorldMap


def main() -> None:
    m = WorldMap(spot_dedup_distance=3.0, auto_save=False)

    print("=== 1. 钓点添加 + 去重 ===")
    id1 = m.add_spot(100.0, 64.0, -50.0, confidence=0.8, has_marker=True)
    id2 = m.add_spot(100.5, 64.0, -49.0, confidence=0.6)  # 距离<3 → 同一钓点
    id3 = m.add_spot(200.0, 64.0, -50.0, confidence=0.7)  # 远 → 新钓点
    assert id1 == id2, "去重应返回同一 id"
    assert id1 != id3, "远处应为新钓点"
    spot1 = m.get_spot(id1)
    assert spot1.confidence == 0.8 and spot1.has_marker, "合并应取较高置信度且保留 marker"
    print(f"  添加 3 个 (去重后 2 个)，spot1 conf={spot1.confidence} marker={spot1.has_marker} OK")

    print("\n=== 2. used / depleted / 候选 ===")
    m.mark_used(id1)
    m.mark_depleted(id1)
    assert m.get_spot(id1).depleted and m.get_spot(id1).used
    assert len(m.candidate_spots()) == 1, "枯竭的不算候选"
    assert len(m.unused_spots()) == 1, "id3 未用未枯竭"
    print(f"  candidate={len(m.candidate_spots())} unused={len(m.unused_spots())} OK")

    print("\n=== 3. nearest_candidate (有玩家位置) ===")
    m2 = WorldMap(spot_dedup_distance=3.0, auto_save=False)
    a = m2.add_spot(100, 64, -50, confidence=0.7)
    b = m2.add_spot(105, 64, -50, confidence=0.9)
    m2.update_player(PlayerPosition(x=104.0, y=64.0, z=-50.0, source="ocr"))
    near = m2.nearest_candidate()
    assert near is not None and near.id == b, "应选最近的 b"
    print(f"  玩家(104,-50) 最近钓点 = b(105,-50) OK")
    # max_distance 过滤
    far = m2.nearest_candidate(max_distance=0.5)
    assert far is None, "0.5 距离内无钓点"
    print("  max_distance 过滤 OK")

    print("\n=== 4. nearest_candidate (无玩家位置) ===")
    m3 = WorldMap(spot_dedup_distance=3.0, auto_save=False)
    m3.add_spot(0, 64, 0, confidence=0.5)
    m3.add_spot(10, 64, 10, confidence=0.9)
    near3 = m3.nearest_candidate()
    assert near3 is not None and near3.confidence == 0.9, "无位置时选高置信度"
    print(f"  无位置 → 选高置信度 {near3.confidence} OK")

    print("\n=== 5. PlayerPosition from_ocr + 距离 ===")
    p1 = PlayerPosition.from_ocr(125.4, 64.0, -37.8)
    p2 = PlayerPosition.from_ocr(125.4, 70.0, -40.8)
    assert p1.valid and p1.source == "ocr"
    d = p1.distance_xz(p2)
    assert abs(d - 3.0) < 0.01, f"XZ 距离应为 3.0, got {d}"
    print(f"  XZ 距离 {d:.2f} (Y 不计入) OK")

    print("\n=== 6. 已探索区域 ===")
    m4 = WorldMap(auto_save=False)
    m4.add_explored(100, 100, 200, 200)
    assert m4.is_explored(150, 150) and not m4.is_explored(50, 50)
    print("  探索区域 contains 判定 OK")

    print("\n=== 7. 持久化 round-trip ===")
    tmp = tempfile.mktemp(suffix=".json")
    try:
        m5 = WorldMap(auto_save=False, persistence_path=tmp)
        sid = m5.add_spot(50, 64, 50, confidence=0.9, has_marker=True)
        m5.mark_used(sid)
        m5.add_explored(40, 40, 60, 60)
        m5.add_water(45, 45, 55, 55)
        m5.add_obstacle(48, 64, 48, "stone")
        m5.update_player(PlayerPosition(x=50, y=64, z=50, source="ocr"))
        m5.save(tmp)
        loaded = WorldMap.load(tmp)
        assert len(loaded.spots) == 1
        s = loaded.get_spot(sid)
        assert s.used and s.confidence == 0.9 and s.has_marker
        assert loaded.player.x == 50 and loaded.player.source == "ocr"
        assert len(loaded.explored) == 1 and len(loaded.water_areas) == 1
        assert len(loaded.obstacles) == 1 and loaded.obstacles[0].kind == "stone"
        print(f"  save/load round-trip OK: {loaded.summary()}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print("\n=== 8. 同一钓点枯竭后不再候选 ===")
    m6 = WorldMap(auto_save=False)
    sid6 = m6.add_spot(0, 64, 0, confidence=0.9)
    m6.mark_depleted(sid6)
    assert m6.nearest_candidate() is None, "唯一钓点枯竭后无候选"
    print("  枯竭后无候选 OK (符合「同一钓点不重复无限使用」)")

    print("\nALL PHASE-5 MAP TESTS PASS")


if __name__ == "__main__":
    main()
