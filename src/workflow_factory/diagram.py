from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET


BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
SVG_NS = "http://www.w3.org/2000/svg"

ET.register_namespace("bpmndi", BPMNDI_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("di", DI_NS)
ET.register_namespace("", SVG_NS)


def qname(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


@dataclass(frozen=True)
class DiagramLayout:
    width: float
    height: float
    lanes: dict[str, Box]
    nodes: dict[str, Box]
    edge_points: dict[str, list[tuple[float, float]]]


def _node_size(kind: str) -> tuple[float, float]:
    if kind in {"start", "end", "terminal"}:
        return 42, 42
    if kind in {"exclusive_gateway", "parallel_gateway", "choice", "parallel"}:
        return 58, 58
    return 156, 70


def _depths(nodes: list[dict], edges: list[dict], entry: str) -> dict[str, int]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])
    depths = {entry: 0}
    queue = deque([entry])
    while queue:
        source = queue.popleft()
        for target in adjacency[source]:
            candidate = depths[source] + 1
            if target not in depths or candidate < depths[target]:
                depths[target] = candidate
                queue.append(target)
    fallback = max(depths.values(), default=0) + 1
    for node in nodes:
        depths.setdefault(node["id"], fallback)
    return depths


def build_layout(
    participants: list[dict],
    nodes: list[dict],
    edges: list[dict],
    entry: str,
) -> DiagramLayout:
    if not participants:
        participants = [{"id": "lane-default", "name": "流程"}]
    lane_ids = [item["id"] for item in participants]
    default_lane = lane_ids[0]
    lane_index = {lane_id: index for index, lane_id in enumerate(lane_ids)}
    depths = _depths(nodes, edges, entry)

    def participant_ref(node: dict) -> str:
        return node.get("participant_ref") or node.get("participant") or default_lane

    buckets: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for node in nodes:
        lane_id = participant_ref(node)
        if lane_id not in lane_index:
            lane_id = default_lane
        buckets[(depths[node["id"]], lane_id)].append(node)

    title_height = 76
    lane_height = 138
    lane_label_width = 150
    cell_width = 184
    depth_gap = 48
    margin = 24
    max_depth = max(depths.values(), default=0)
    depth_x: dict[int, float] = {}
    cursor = margin + lane_label_width
    for depth in range(max_depth + 1):
        max_in_lane = max(
            (len(items) for (item_depth, _), items in buckets.items() if item_depth == depth),
            default=1,
        )
        depth_x[depth] = cursor
        cursor += max_in_lane * cell_width + depth_gap

    node_boxes: dict[str, Box] = {}
    for (depth, lane_id), items in buckets.items():
        for position, node in enumerate(items):
            width, height = _node_size(node.get("kind") or node.get("action", {}).get("kind", ""))
            lane_y = title_height + lane_index[lane_id] * lane_height
            x = depth_x[depth] + position * cell_width + (cell_width - width) / 2
            y = lane_y + (lane_height - height) / 2
            node_boxes[node["id"]] = Box(x, y, width, height)

    width = max(cursor + margin, 720)
    content_height = title_height + len(lane_ids) * lane_height
    has_back_edge = any(
        depths.get(edge["to"], 0) <= depths.get(edge["from"], 0) for edge in edges
    )
    height = content_height + (72 if has_back_edge else 24)
    lanes = {
        lane_id: Box(margin, title_height + index * lane_height, width - margin * 2, lane_height)
        for lane_id, index in lane_index.items()
    }

    edge_points: dict[str, list[tuple[float, float]]] = {}
    for edge in edges:
        source = node_boxes[edge["from"]]
        target = node_boxes[edge["to"]]
        if target.x > source.x:
            middle_x = (source.right + target.x) / 2
            points = [
                (source.right, source.center_y),
                (middle_x, source.center_y),
                (middle_x, target.center_y),
                (target.x, target.center_y),
            ]
        else:
            loop_y = content_height + 36
            points = [
                (source.right, source.center_y),
                (source.right + 24, source.center_y),
                (source.right + 24, loop_y),
                (target.x - 24, loop_y),
                (target.x - 24, target.center_y),
                (target.x, target.center_y),
            ]
        edge_points[edge["id"]] = points
    return DiagramLayout(width, height, lanes, node_boxes, edge_points)


def append_bpmn_di(
    definitions: ET.Element,
    process_id: str,
    participants: list[dict],
    nodes: list[dict],
    edges: list[dict],
    entry: str,
) -> None:
    layout = build_layout(participants, nodes, edges, entry)
    diagram = ET.SubElement(
        definitions,
        qname(BPMNDI_NS, "BPMNDiagram"),
        {"id": f"Diagram_{process_id}"},
    )
    plane = ET.SubElement(
        diagram,
        qname(BPMNDI_NS, "BPMNPlane"),
        {"id": f"Plane_{process_id}", "bpmnElement": process_id},
    )
    for participant in participants:
        box = layout.lanes[participant["id"]]
        shape = ET.SubElement(
            plane,
            qname(BPMNDI_NS, "BPMNShape"),
            {
                "id": f"Shape_{participant['id']}",
                "bpmnElement": participant["id"],
                "isHorizontal": "true",
            },
        )
        ET.SubElement(
            shape,
            qname(DC_NS, "Bounds"),
            {key: f"{value:.1f}" for key, value in vars(box).items()},
        )
    for node in nodes:
        box = layout.nodes[node["id"]]
        shape = ET.SubElement(
            plane,
            qname(BPMNDI_NS, "BPMNShape"),
            {"id": f"Shape_{node['id']}", "bpmnElement": node["id"]},
        )
        ET.SubElement(
            shape,
            qname(DC_NS, "Bounds"),
            {key: f"{value:.1f}" for key, value in vars(box).items()},
        )
    for edge in edges:
        edge_element = ET.SubElement(
            plane,
            qname(BPMNDI_NS, "BPMNEdge"),
            {"id": f"Edge_{edge['id']}", "bpmnElement": edge["id"]},
        )
        for x, y in layout.edge_points[edge["id"]]:
            ET.SubElement(
                edge_element,
                qname(DI_NS, "waypoint"),
                {"x": f"{x:.1f}", "y": f"{y:.1f}"},
            )


def _wrapped_lines(value: str, width: int = 12) -> list[str]:
    value = value.strip()
    if not value:
        return [""]
    return [value[index : index + width] for index in range(0, len(value), width)][:3]


def _edge_label(edge: dict) -> str:
    condition = edge.get("when", "")
    if condition.endswith("== true"):
        return "是"
    if condition.endswith("== false"):
        return "否"
    return condition


def render_graph_svg(graph: dict, workflow_ir: dict, output_path: Path) -> None:
    participants = workflow_ir["spec"].get("participants", [])
    nodes = []
    ir_nodes = {node["id"]: node for node in workflow_ir["spec"]["nodes"]}
    for node in graph["spec"]["nodes"]:
        nodes.append(
            {
                **node,
                "participant_ref": ir_nodes.get(node["id"], {}).get("participant_ref"),
            }
        )
    edges = graph["spec"]["edges"]
    layout = build_layout(participants, nodes, edges, graph["spec"]["entry"])

    root = ET.Element(
        qname(SVG_NS, "svg"),
        {
            "width": f"{layout.width:.0f}",
            "height": f"{layout.height:.0f}",
            "viewBox": f"0 0 {layout.width:.0f} {layout.height:.0f}",
            "role": "img",
            "aria-label": f"{workflow_ir['metadata']['id']} 整体工作流程图",
        },
    )
    style = ET.SubElement(root, qname(SVG_NS, "style"))
    style.text = """
      text { font-family: 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; fill: #172033; }
      .title { font-size: 22px; font-weight: 700; }
      .subtitle { font-size: 12px; fill: #667085; }
      .lane { fill: #f8fafc; stroke: #b8c2d1; stroke-width: 1.2; }
      .lane-alt { fill: #f1f5f9; }
      .lane-label { font-size: 14px; font-weight: 700; }
      .edge { fill: none; stroke: #667085; stroke-width: 1.8; }
      .edge-label { font-size: 12px; font-weight: 700; fill: #344054; }
      .task { fill: #e8f1ff; stroke: #2764b3; stroke-width: 1.8; }
      .human { fill: #fff4df; stroke: #b26a00; }
      .agent { fill: #eee8ff; stroke: #6941c6; }
      .gateway { fill: #fff7cc; stroke: #8a6d00; stroke-width: 1.8; }
      .event { fill: #e9fbef; stroke: #16803c; stroke-width: 2.2; }
      .node-label { font-size: 13px; font-weight: 600; text-anchor: middle; }
      .node-kind { font-size: 10px; fill: #667085; text-anchor: middle; }
    """
    ET.SubElement(root, qname(SVG_NS, "title")).text = (
        workflow_ir["metadata"].get("display_name") or workflow_ir["metadata"]["id"]
    )
    ET.SubElement(root, qname(SVG_NS, "desc")).text = (
        "由已编译 Agent Graph 生成的 BPMN 业务整体流程图"
    )
    defs = ET.SubElement(root, qname(SVG_NS, "defs"))
    marker = ET.SubElement(
        defs,
        qname(SVG_NS, "marker"),
        {
            "id": "arrow",
            "markerWidth": "9",
            "markerHeight": "7",
            "refX": "8",
            "refY": "3.5",
            "orient": "auto",
        },
    )
    ET.SubElement(
        marker,
        qname(SVG_NS, "polygon"),
        {"points": "0 0, 9 3.5, 0 7", "fill": "#667085"},
    )
    title = ET.SubElement(
        root,
        qname(SVG_NS, "text"),
        {"x": "24", "y": "31", "class": "title"},
    )
    title.text = workflow_ir["metadata"].get("display_name") or workflow_ir["metadata"]["id"]
    subtitle = ET.SubElement(
        root,
        qname(SVG_NS, "text"),
        {"x": "24", "y": "54", "class": "subtitle"},
    )
    subtitle.text = workflow_ir["spec"].get("intent") or "由可执行 Agent Graph 生成"

    for index, participant in enumerate(participants):
        box = layout.lanes[participant["id"]]
        lane_class = "lane lane-alt" if index % 2 else "lane"
        ET.SubElement(
            root,
            qname(SVG_NS, "rect"),
            {
                "x": str(box.x), "y": str(box.y), "width": str(box.width),
                "height": str(box.height), "class": lane_class,
            },
        )
        ET.SubElement(
            root,
            qname(SVG_NS, "text"),
            {"x": str(box.x + 16), "y": str(box.y + 28), "class": "lane-label"},
        ).text = participant.get("name", participant["id"])

    for edge in edges:
        points = layout.edge_points[edge["id"]]
        ET.SubElement(
            root,
            qname(SVG_NS, "polyline"),
            {
                "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in points),
                "class": "edge",
                "marker-end": "url(#arrow)",
            },
        )
        label = _edge_label(edge)
        if label:
            middle = points[len(points) // 2]
            ET.SubElement(
                root,
                qname(SVG_NS, "text"),
                {"x": str(middle[0] + 5), "y": str(middle[1] - 6), "class": "edge-label"},
            ).text = label

    kind_names = {
        "start": "开始",
        "terminal": "结束",
        "choice": "判断",
        "parallel": "并行",
        "human_gate": "人工任务",
        "agent_task": "Agent 任务",
        "tool_task": "Tool 任务",
        "script_task": "系统任务",
        "manual_task": "业务任务",
    }
    for node in nodes:
        box = layout.nodes[node["id"]]
        kind = node["action"]["kind"]
        if kind in {"start", "terminal"}:
            ET.SubElement(
                root,
                qname(SVG_NS, "circle"),
                {
                    "cx": str(box.x + box.width / 2), "cy": str(box.center_y),
                    "r": str(box.width / 2), "class": "event",
                },
            )
        elif kind in {"choice", "parallel"}:
            cx, cy = box.x + box.width / 2, box.center_y
            ET.SubElement(
                root,
                qname(SVG_NS, "polygon"),
                {
                    "points": (
                        f"{cx},{box.y} {box.right},{cy} "
                        f"{cx},{box.y + box.height} {box.x},{cy}"
                    ),
                    "class": "gateway",
                },
            )
        else:
            extra = " human" if kind == "human_gate" else " agent" if kind == "agent_task" else ""
            ET.SubElement(
                root,
                qname(SVG_NS, "rect"),
                {
                    "x": str(box.x), "y": str(box.y), "width": str(box.width),
                    "height": str(box.height), "rx": "10", "class": f"task{extra}",
                },
            )
        lines = _wrapped_lines(node.get("name", node["id"]))
        start_y = box.center_y - (len(lines) - 1) * 8
        for index, line in enumerate(lines):
            ET.SubElement(
                root,
                qname(SVG_NS, "text"),
                {
                    "x": str(box.x + box.width / 2),
                    "y": str(start_y + index * 17),
                    "class": "node-label",
                },
            ).text = line
        ET.SubElement(
            root,
            qname(SVG_NS, "text"),
            {
                "x": str(box.x + box.width / 2),
                "y": str(box.y + box.height + 15),
                "class": "node-kind",
            },
        ).text = kind_names.get(kind, kind)

    ET.indent(root, space="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
