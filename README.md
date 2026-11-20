# FAGENT

FAGENT chạy workflow DRC và LVS trên LangGraph. File setup chọn task và đầu vào. Các task dùng chung cấu hình môi trường, model, checkpoint và dịch vụ EDA.

## Cài đặt và chạy

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install .
python -m FAGENT --setup env_setup.yaml --config FAGENT/config.json
fagent --setup env_setup.yaml --config FAGENT/config.json --verbose
```

Dùng `FAGENT/config.example.json` làm mẫu cho cấu hình môi trường. Điền đường dẫn GGUF cho model agent và embedding, cấu hình tài nguyên và thư mục tri thức tương ứng với máy chạy. Đường dẫn setup được tính từ thư mục chứa YAML. Ví dụ DRC theo danh sách cell:

```yaml
output_folder: output
list_cells: list
layer_map_files: [config/layer_mapping]
tasks:
  - drc:
      drc_folder: DRC
      gds_folder: DRC
      drc_files: DRC/rules.drc
      prompt: Fix DRC errors.
```

Mỗi cell lấy `<cell>.gds` từ `gds_folder` và `<cell>.drc.results` từ `drc_folder`. Artifact nằm ở `output/<cell>/rundir/`. Với LVS, chọn `tasks: [{lvs: {}}]`; artifact nằm ở `output/lvs/rundir/`.

## Định nghĩa task

```text
FAGENT/tasks/<task>/
├── __init__.py
├── workflow.yaml
├── steps.py
├── tools.py
├── prompts/
└── schemas/
```

```yaml
task: inspect_layout
version: 1
inputs: [input_gds]
prompts: [prompts/agent.md]
schema: schemas/response.json
steps:
  - {id: read, action: 'steps:read_gds', next: inspect}
  - {id: inspect, action: agent, tools: ['tools:inspect_layer'], next: END}
```

Expose callable qua tên trong module, ví dụ `read_gds = LayoutSteps.run`. Step nhận `TaskContext` và trả về cập nhật state tương thích JSON. Tool nhận context và đối số khai báo trong schema của `ToolSpec` gắn trực tiếp vào hàm qua `my_tool.task_tool = ToolSpec(my_tool, description, parameters)`. Engine kiểm tra tên tool và JSON Schema trước khi gọi. Các module task là Python tin cậy của dự án. Action `review` dùng `continue` để chỉ bước chạy lại và `next` cho quyết định dừng.

## Trace, review và validation

Khi bật `--verbose`, stderr hiển thị thông tin điều hướng theo thứ tự thực thi:

```text
[Task drc] Starting workflow.
[Step read] Running workflow action steps:read_drc (node: read)
[Step read] Completed (node: read). Next node: repair.
```

Lỗi dùng tiền tố `[ERROR]`. Trace chỉ ghi định danh và trạng thái. `conversation.json`, `result.json` và checkpoint SQLite nằm trong rundir. Review có thể tiếp tục, dừng hoặc yêu cầu làm rõ. Chỉ lưu tri thức khi người dùng đồng ý ở lời nhắc riêng.

Backend DRC cục bộ chạy Calibre Simulator trên candidate và rule deck, rồi đối chiếu hai báo cáo mô phỏng. LVS hiện có placeholder và mock khi được cấu hình. Cả hai gắn `simulated: true` cho kết quả mô phỏng. Backend EDA thật chưa được tích hợp.

Chi tiết triển khai xem tại `.agent/AI_Workflow.md`.

```bash
python -m pytest -q tests
```
