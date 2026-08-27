from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.diagram import build_layout  # noqa: E402


class DiagramLayoutComponentTest(unittest.TestCase):
    def test_nodes_do_not_overlap_and_loop_routes_outside_lanes(self) -> None:
        participants = [
            {"id": "lane-a", "name": "申请人"},
            {"id": "lane-b", "name": "审批人"},
        ]
        nodes = [
            {"id": "start", "kind": "start", "participant": "lane-a"},
            {"id": "submit", "kind": "human", "participant": "lane-a"},
            {"id": "review", "kind": "human", "participant": "lane-b"},
            {"id": "finish", "kind": "end", "participant": "lane-b"},
        ]
        edges = [
            {"id": "e1", "from": "start", "to": "submit"},
            {"id": "e2", "from": "submit", "to": "review"},
            {"id": "e3", "from": "review", "to": "submit"},
            {"id": "e4", "from": "review", "to": "finish"},
        ]
        layout = build_layout(participants, nodes, edges, "start")

        boxes = list(layout.nodes.values())
        for index, left in enumerate(boxes):
            for right in boxes[index + 1 :]:
                overlap_x = left.x < right.right and right.x < left.right
                overlap_y = left.y < right.y + right.height and right.y < left.y + left.height
                self.assertFalse(overlap_x and overlap_y)

        lane_bottom = max(box.y + box.height for box in layout.lanes.values())
        loop_points = layout.edge_points["e3"]
        self.assertTrue(any(y > lane_bottom for _, y in loop_points))
        self.assertGreater(layout.width, 700)
        self.assertGreater(layout.height, lane_bottom)


if __name__ == "__main__":
    unittest.main()
