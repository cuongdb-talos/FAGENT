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
grep -nE '^(task|inputs|steps|prompts|schema):' FAGENT/tasks/my_task/workflow.yaml
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

1. **Cách ly lượt chạy (Run Isolation):**
   - Mỗi cell được chạy trong một thư mục độc lập: `output/<cell>/runs/<run_id>/rundir/`.
   - Giữ nguyên toàn bộ báo cáo vi phạm DRC, thứ tự và số lượng vi phạm gốc (kể cả rule không có hình học).
2. **Định danh Polygon ổn định (`polygon_id`):**
   - Mỗi polygon trực tiếp có thể chỉnh sửa được gán ID ổn định dạng `<cell_name>:poly:<index>`.
   - ID được duy trì qua các phép chỉnh sửa bằng `replace_polygon(cell_name, polygon_id, points)` và bảo vệ toàn vẹn bằng SHA-256 digest của `candidate.gds`.
3. **Kiểm tra hình học Enclosure (`check_cover`):**
   - Sử dụng engine GEOS/Shapely để tính toán khoảng cách bao phủ chính xác giữa polygon inner và outer theo user units của GDS.
   - Báo cáo chi tiết các đoạn mép vi phạm và độ thiếu hụt (`shortage`). Trả về trạng thái `unknown` khi hình học phức tạp hoặc không xác định.
4. **Quản lý Checklist trạng thái vi phạm:**
   - Checklist lưu trữ tại `checklist.json` trong rundir với các trạng thái: `pending`, `in_progress`, `pass`, `skipped`, `unresolved`.
   - Mục tiêu cấu hình (`goal`) cho phép tự động bỏ qua (skip) các vi phạm được loại trừ (ví dụ density).
   - Khi bất kỳ polygon nào bị thay đổi, các task đã `pass` phụ thuộc vào polygon đó sẽ tự động chuyển sang trạng thái `stale` (cần đánh giá lại).
5. **Tính chân thực của xác minh (Verification Truthfulness):**
   - Đánh giá `pass` trên checklist là nhận định hình học của agent, **không phải kết quả DRC Calibre chính thức**.
   - Báo cáo kết quả cuối cùng giữ trạng thái `incomplete` trừ khi có bằng chứng chạy DRC thực tế hỗ trợ.
