# Báo cáo trace DRC: task-1, task-2, task-3

Nguồn: `output/ROM_MCELL0/trace.log`, `conversation.json`, `checklist.json`. Checklist được đọc lúc 09:37 ngày 26-11-2026 (ICT). Trace tiếp tục sau các task này, nên trạng thái dưới đây là kết quả hiện có, chưa phải kết quả cuối toàn bộ run.

## Cấu trúc Input Prompt

Mỗi model request gồm **system prompt** và **user prompt**.

- **System prompt** đặt vai trò DRC layout, hướng dẫn dùng layer map và polygon IDs chính xác, chọn `fix_area` cho minimum area và `fix_cover` cho enclosure/coverage, giữ ngoại lệ trong rule, không tự suy ra geometry thiếu, và trả JSON/tool call theo schema. Prompt đưa goal “Fix all DRC errors about M1 and CT layer but ignore error about density” cùng constraint “Minimize width where possible”.
- **User prompt** là JSON theo task. Các key cấp cao và key lồng nhau thực tế trong trace được thể hiện dưới đây. Giá trị geometry/evidence được rút gọn, nhưng tên key được giữ lại.

Ví dụ rút gọn của task-3:

```json
{
  "task_id": "task-3",
  "cell": "ROM_MCELL0",
  "unit": "um",
  "grid": 0.001,
  "violation": {
    "rule": "CT_R_11",
    "description": ["..."],
    "rule_text": "...",
    "marker_bbox": [0.065, 0.07, 0.118, 0.16],
    "marker_metrics": {"w": 0.053, "h": 0.09, "area": 0.00477},
    "marker_points": [[0.065, 0.07], "..."],
    "evidence": {
      "status": "available",
      "reason": "",
      "deck_file": "...",
      "line_start": 2680,
      "line_end": 2683,
      "mapping_diagnostics": {"missing_sources": [], "ambiguous_layers": {}, "...": "..."}
    },
    "coverage": "..."
  },
  "layer_map": {"...": "..."},
  "layout": {
    "search_region": "...",
    "complete": true,
    "candidate_digest": "...",
    "polygons": [{
      "id": "ROM_MCELL0:poly:9",
      "layer": "CT",
      "lpp": "...",
      "points": "...",
      "bbox": "...",
      "editable": true,
      "is_violating": true,
      "w": 0.053,
      "h": 0.09,
      "area": 0.00477
    }],
    "layer_status_in_search_region": "...",
    "references_in_region": "...",
    "paths_in_region": "...",
    "reason": ""
  },
  "task_history": []
}
```

Trong `layout.polygons[]`, mỗi polygon có các key `id`, `layer`, `lpp`, `points`, `bbox`, `editable`, `is_violating`, `w`, `h`, `area`. Các key mô tả vùng search và layer status được giữ trong `layout` như ví dụ. Prompt đầy đủ nằm trong `conversation.json`.

## task-1 — GT_A_13

**Trạng thái checklist**: `pass`. Lý do ghi nhận là diện tích polygon đã tăng đủ.

### 1. Input Prompt

Requests 1–3 cùng xử lý marker `GT_A_13`. Input nêu minimum area `0.038 um²`, marker bbox `[-0.09, 0.21, 0.31, 0.27]`, layout, layer map và grid `0.001 um`.

### 2. Model respond

- Request 1 gọi propose cho polygon `ROM_MCELL0:poly:5`, minimum area `0.038`.
- Request 2 gọi apply proposal `task-1-area-p1`.
- Request 3 trả review JSON với `decision=completed`, `status=verified`, xác nhận diện tích polygon đã đạt minimum.

### 3. Input tool

- `fix_area(task_id="task-1", action="propose", polygon_id="ROM_MCELL0:poly:5", minimum_area=0.038)`
- `fix_area(task_id="task-1", action="apply", proposal_id="task-1-area-p1")`

### 4. Output tool

- Propose: diện tích ban đầu `0.024 um²`, thiếu `0.014 um²`. Đề xuất tăng cạnh y từ `0.27` lên `0.305`, đạt dự kiến `0.038 um²`.
- Apply trả `status=applied`, candidate digest `406b82ec…`.
- Verify sau apply đưa task vào trạng thái pass.

### 5. Model review

Request 3 trả `verified`, xác nhận diện tích tăng đủ. Đây là review cục bộ của task, không phải review tổng thể toàn run.

## task-2 — CT_EN_7a_7b

**Trạng thái checklist**: `unresolved`. Lý do là “Repeated proposal call without a geometry change”.

### 1. Input Prompt

Requests 4–5 xử lý marker `CT_EN_7a_7b`, với CT polygon `ROM_MCELL0:poly:9`, M1 polygon `ROM_MCELL0:poly:10`, và minimum enclosure `0`.

### 2. Model respond

Cả hai request đều gọi lại cùng thao tác propose với cùng polygon IDs và enclosure. Model không chuyển sang apply hay tạo thay đổi geometry.

### 3. Input tool

Cả hai lần gọi:
`fix_cover(task_id="task-2", action="propose", inner_id="ROM_MCELL0:poly:9", groups=[["ROM_MCELL0:poly:10"]], minimum_enclosure=0)`

### 4. Output tool

Mỗi lần tool trả `status=pass`, measured distance `0.135 um`, không có violation và không có proposal. Workflow đánh dấu unresolved vì model lặp propose mà không có geometry change.

### 5. Model review

Trace không có response review riêng cho task-2. Tool đo pass, nhưng checklist vẫn unresolved theo cơ chế phát hiện gọi propose lặp. Không ghi nhận model review pass.

## task-3 — CT_R_11

**Trạng thái checklist**: `pass`. Lý do ghi nhận là fix đã apply và marker được xử lý.

### 1. Input Prompt

Requests 6–8 xử lý rule CT_R_11. Rule yêu cầu CT được phủ bởi M1 và (AA hoặc GT), ngoại trừ INST/EXCLU. Marker bbox `[0.065, 0.07, 0.118, 0.16]`; prompt chứa polygon IDs/layers và evidence.

### 2. Model respond

- Request 6 đề xuất coverage cho CT `poly:9`, với hai nhóm điều kiện M1 `poly:8` và AA `poly:3`.
- Request 7 yêu cầu apply proposal `task-3-cover-p2`.
- Request 8 trả review `completed/verified`, cho biết marker đã resolved.

### 3. Input tool

- `fix_cover(task_id="task-3", action="propose", inner_id="ROM_MCELL0:poly:9", groups=[["ROM_MCELL0:poly:8"], ["ROM_MCELL0:poly:3"]], minimum_enclosure=0)`
- `fix_cover(task_id="task-3", action="apply", proposal_id="task-3-cover-p2")`

### 4. Output tool

- Propose: coverage ban đầu fail ở nhóm đầu, shortage `0.053 um`. Proposal `task-3-cover-p2` mở rộng M1 `poly:8` từ `x=0.118` sang `x=0.065`; local check pass.
- Apply trả `status=applied`, candidate digest `d85f0140…`.
- Verify sau apply đưa task vào trạng thái pass.

### 5. Model review

Request 8 trả `decision=completed`, `status=verified`, lý do fix đã apply và marker resolved. Đây là review cục bộ của task; trace chưa ghi review cuối toàn run.

## Tổng hợp

| Task | Rule | Checklist | Tool outcome | Model review |
|---|---|---|---|---|
| task-1 | GT_A_13 | pass | Tăng diện tích polygon và apply thành công | verified |
| task-2 | CT_EN_7a_7b | unresolved | Đo pass nhưng model lặp propose, không đổi geometry | Không có review riêng |
| task-3 | CT_R_11 | pass | Mở rộng M1 và apply thành công | verified |
