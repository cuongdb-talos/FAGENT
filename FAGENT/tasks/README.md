# Tạo task mới cho FAGENT

Dùng [sample_task](sample_task/) để chạy thử một workflow chuẩn hóa khoảng trắng.
Chạy các lệnh bên dưới từ thư mục gốc của repo, sau khi cài dependency theo
[README của dự án](../../README.md#cài-đặt-và-chạy).

## Chạy mẫu

```bash
sample_run_dir="$(mktemp -d)"
cat > "$sample_run_dir/setup.yaml" <<'YAML'
output_folder: output
tasks:
  - sample_task:
      text: "  hello    world  "
YAML
printf '{}\n' > "$sample_run_dir/config.json"
python -m FAGENT --setup "$sample_run_dir/setup.yaml" --config "$sample_run_dir/config.json" --verbose
cat "$sample_run_dir/output/sample_task/rundir/result.json"
```

Kết quả chính trong `result.json`:

```json
{
  "status": "complete",
  "step_outputs": {"summary": "hello world"}
}
```

Thư mục `rundir` cũng chứa `conversation.json` và checkpoint SQLite. Mẫu mặc
định chạy bằng Python, với cấu hình model rỗng.

## Sao chép và chỉnh sửa

```bash
cp -R FAGENT/tasks/sample_task FAGENT/tasks/my_task
grep -nE '^(task|inputs|nodes|steps|prompts|schema):' FAGENT/tasks/my_task/workflow.yaml
```

1. Đổi `task: sample_task` thành `task: my_task` trong `workflow.yaml` và đổi
   khóa dưới `tasks` trong file setup. Tên task phải khớp tên thư mục và theo
   mẫu `[a-z][a-z0-9_]*`. CLI tự tìm workflow theo tên này.
2. Khai báo các khóa cấu hình bắt buộc trong `inputs`. Đọc giá trị từ
   `context.inputs` và dữ liệu của bước trước từ `context.state`.
3. Viết logic trong `steps.py`, công khai callable ở cấp module và trỏ
   `action` tới `steps:<callable>`. Mỗi step nhận một `TaskContext` và trả
   về dict tương thích JSON. Bước đầu tiên trong danh sách là điểm vào.
   `next` trỏ tới ID bước kế tiếp hoặc `END`.
4. Nếu cần chuẩn bị đầu vào hoặc tạo nhiều lượt chạy, thêm
   `prepare: steps:prepare`. Callable nhận
   `(task_cfg, setup, base, output, config, verbose)` và trả về iterable
   gồm các bộ `(rundir, run_config, inputs)`. Dùng `base` để phân giải
   đường dẫn tương đối với file setup.

## Bật agent và review

- Đổi `normalize.next` thành `analyze` và bỏ chú thích khối `analyze` trong
  `workflow.yaml`. Cung cấp cấu hình model theo
  [config.example.json](../config.example.json).
- Trong `tools.py`, viết hàm nhận context trước các tham số của tool, rồi gán
  `my_tool.task_tool = ToolSpec(my_tool, description, parameters)` với schema
  tham số kiểu object. Khai báo tool trong
  `steps[].tools` của bước `agent`. Sửa prompt và schema phản hồi trong
  `prompts/` và `schemas/` theo dữ liệu của task mới.
- Để bật review tương tác, đổi `analyze.next` thành `human_review` và bỏ
  chú thích khối `human_review`. `continue` chỉ bước chạy lại và `next`
  chỉ đích khi kết thúc review. Nếu tùy chỉnh cách diễn giải phản hồi bằng
  model, cấu hình cả `review_prompt` và `review_schema`.

## Quy trình DRC Checklist và Định danh Polygon

Task DRC áp dụng kiến trúc quản lý sửa lỗi hướng checklist với các đặc điểm:

Node `read` đọc báo cáo DRC đầy đủ và truy xuất knowledge một lần, sau đó
tạo `drc_context` rút gọn chứa rule vi phạm, marker và tọa độ DBU, knowledge
và goal. Các rule sạch được lược khỏi context; báo cáo đầy đủ vẫn nằm trong
state để đối chiếu checklist. Marker ID là ID của vi phạm trong DRC report,
không mặc định là polygon ID trong GDS; agent cần dùng `read_gds` để kiểm tra.
Constraints chung nằm trong `FAGENT/knowledge/constraints.json`, là danh sách
chuỗi JSON; chúng được thêm vào goal của cả `plan` và `repair`. Có thể chỉ định
file khác bằng `constraints_file` trong config. Node `plan` tạo checklist;
node `repair` nhận checklist và gọi tool. `conversation.json` được cập nhật
sau mỗi phản hồi model và kết quả tool.

1. **Cách ly lượt chạy (Run Isolation):**
   - Mỗi cell được chạy trong một thư mục độc lập: `output/<cell>/runs/<run_id>/rundir/`.
   - Giữ nguyên toàn bộ báo cáo vi phạm DRC, thứ tự và số lượng vi phạm gốc (kể cả rule không có hình học).
2. **Định danh Polygon ổn định (`polygon_id`):**
   - Mỗi polygon trực tiếp có thể chỉnh sửa được gán ID ổn định dạng `<cell_name>:poly:<index>`.
   - ID được duy trì qua các phép chỉnh sửa bằng `replace_polygon(cell_name, polygon_id, points)` và bảo vệ toàn vẹn bằng SHA-256 digest của `candidate.gds`.
3. **Kiểm tra hình học Enclosure (`check_cover`):**
   - Sử dụng engine GEOS/Shapely để tính toán khoảng cách bao phủ chính xác giữa polygon inner và outer theo user units của GDS.
   - Báo cáo chi tiết các đoạn mép vi phạm và độ thiếu hụt (`shortage`). Trả về trạng thái `unknown` khi hình học phức tạp hoặc không xác định.
4. **Quản lý Checklist trạng thái vi phạm (Report-Driven):**
   - Danh sách vi phạm chuẩn được trích xuất trực tiếp từ báo cáo DRC theo đúng thứ tự gốc.
   - Gợi ý từ model được đối chiếu 1-1 với báo cáo; nếu model bỏ sót vi phạm nào, vi phạm đó tự động chuyển sang trạng thái `unresolved` với lý do chưa có kế hoạch sửa.
   - Checklist lưu trữ tại `checklist.json` trong rundir với các trạng thái: `pending`, `in_progress`, `pass`, `skipped`, `unresolved`.
   - Mục tiêu cấu hình (`goal`) cho phép tự động bỏ qua (skip) các vi phạm được loại trừ (ví dụ density).
   - Khi bất kỳ polygon nào bị thay đổi, các task đã `pass` phụ thuộc vào polygon đó sẽ tự động chuyển sang trạng thái `stale` (cần đánh giá lại).
   - Lỗi định dạng JSON hoặc ngoại lệ mô hình ở step plan vẫn kích hoạt fail-fast chứ không tự ý hoàn tất giả định.
5. **Tính chân thực của xác minh (Verification Truthfulness):**
   - Đánh giá `pass` trên checklist là nhận định hình học của agent, **không phải kết quả DRC Calibre chính thức**.
   - Báo cáo kết quả cuối cùng giữ trạng thái `incomplete` trừ khi có bằng chứng chạy DRC thực tế hỗ trợ.
6. **Truy vết Hội thoại Toàn diện (Complete Conversation Trace):**
   - **Đường dẫn artifact:** Đường dẫn của trace luôn được xác định động từ thư mục chạy được cấu hình (`<rundir>/conversation.json`), không hard-code tên cell hay run ID.
   - **Đặc tả Event Schema (Version 2):** Toàn bộ hội thoại và tương tác tool được lưu với định dạng `{"version": 2, "events": [...]}`. Mỗi event có các loại sau:
     - `model_request`: Lưu `request_id`, nguyên vẹn mảng `messages` (chứa toàn bộ context nhạy cảm như DRC context, checklist, prompt hệ thống), danh sách đặc tả công cụ `tools`, và `response_schema` thực tế được truyền cho client.
     - `model_response`: Lưu `request_id` tương ứng và `message` phản hồi của trợ lý (content và/hoặc `tool_calls`).
     - `model_error`: Lưu `request_id`, `error_type` và `error_message` khi gọi client gặp ngoại lệ.
     - `tool_result`: Lưu `message` kiểu tool (`role: "tool"`, `tool_call_id`, và `content` là chuỗi JSON kết quả hoặc thông báo lỗi `{"error": ...}`).
   - **Request ID:** Các yêu cầu model được gán `request_id` tăng dần đơn điệu (`"1"`, `"2"`, ...) duy nhất trong phạm vi run, được bảo toàn liên tục kể cả khi resume checkpoint.
   - **Ghi trực tiếp và liên tục (Live Persistence):** File `conversation.json` được ghi nguyên tử (atomic write qua file tạm) trước và ngay sau mỗi lần gọi model cũng như sau mỗi kết quả tool, giúp phục hồi và chẩn đoán lỗi kể cả khi tiến trình bị ngắt đột ngột. Runner ở bước `finish` tuyệt đối không ghi đè trace bằng mảng state `messages`.
   - **Giới hạn lượt chạy lịch sử (Historical Run Limitation):** Các lượt chạy trước đây chỉ lưu mảng tin nhắn kết thúc, không ghi lại các yêu cầu tạm thời (ephemeral requests), do đó không thể tái cấu trúc chính xác các yêu cầu model ban đầu từ dữ liệu lịch sử.

## Payload riêng cho agent

Khai báo `request_builder: model_payload:build_request` ở cấp workflow để
tạo request riêng mà giữ nguyên các node và route. Hàm task-local nhận
`(state, node_prompt, services, focused)` và trả về
`{'system': '...', 'user': {...}}`. `focused` là context marker sẵn có
cho DRC và có giá trị `None` cho task khác. Builder chọn trường gửi đi
và thể hiện dữ liệu thiếu bằng `null` hoặc lý do cụ thể.

Engine mã hóa user payload thành chuỗi JSON và nối các cặp assistant/tool
của mục đang xử lý sau hai message đầu. Tool, schema phản hồi và trace vẫn
do engine quản lý. Workflow chưa khai báo builder dùng request hiện tại.
