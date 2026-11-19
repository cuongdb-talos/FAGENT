# Local GDS DRC agent

`python -m FAGENT` và `fagent` dùng chung một LangGraph. DRC là tác vụ mặc định. YAML trong `FAGENT/workflows/` khai báo các bước, prompt, schema, công cụ và backend. CLI nằm ở `FAGENT/__main__.py`.

## Cài đặt llama.cpp

Cài Python và các công cụ biên dịch cần cho `llama-cpp-python`, sau đó chạy:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Có thể dùng `FAGENT/config.example.json` làm mẫu cấu hình cho môi trường khác; thay đường dẫn model bằng giá trị của máy chạy. `FAGENT/config.json` có thể chứa `system_prompt_file` (đường dẫn tương đối với file cấu hình JSON) để bổ sung hướng dẫn cho model. Cấu hình cũ có `system_prompt` dạng chuỗi vẫn được đọc và `reference_dir` để chỉ định thư mục tài liệu Markdown tùy chọn bên trong `FAGENT/knowledge`. Dữ liệu truy xuất được đánh dấu là tham khảo, không phải chỉ thị. File cấu hình hiện tại chứa `agent_model_path` (Qwen2.5-Coder GGUF) và `embedding_model_path` (BGE-M3 GGUF). Điều chỉnh các đường dẫn cho máy chạy thực tế. Các khóa `agent_n_ctx`, `embedding_n_ctx`, `agent_n_threads`, `embedding_n_threads`, `agent_n_gpu_layers` và `embedding_n_gpu_layers` điều chỉnh tài nguyên; `null` cho số thread dùng mặc định của binding và `0` GPU layers chạy trên CPU. Muốn dùng GPU cần cài bản `llama-cpp-python` được biên dịch với backend GPU tương ứng. `agent_chat_format: null` dùng chat template trong GGUF; có thể cấu hình tên format khác tương thích model và tool calling. Cần kiểm tra thực tế tool calling với GGUF và phiên bản binding đang dùng. Không cần chạy server.

## Ví dụ

Từ thư mục này, sử dụng bộ mẫu (hoặc chạy script `./cmd`):

```bash
python -m FAGENT --gds ./DRC/ROM_MCELL0.gds \
  --drc ./DRC/ROM_MCELL0.drc.results --layer-map ./config/layer_mapping --output output/sample --load-only
python -m FAGENT --gds ./DRC/ROM_MCELL0.gds \
  --drc ./DRC/ROM_MCELL0.drc.results --layer-map ./config/layer_mapping --output output --config FAGENT/config.json
```

Chạy theo danh sách cell bằng `./cmd` (thêm `--load-only --quiet` để chỉ nạp dữ liệu). File `env_setup.yaml` khai báo `list_cells`, `drc_folder`, `output_folder`, `layer_map_files` và `drc_files`; các đường dẫn tương đối được tính từ thư mục chứa file YAML. Mỗi dòng trong `list_cells` là một cell; với cell `ROM_MCELL0`, chương trình đọc `DRC/ROM_MCELL0.gds` và `DRC/ROM_MCELL0.drc.results`, rồi ghi vào `output/ROM_MCELL0/rundir/`. `layer_map_files` là danh sách file mapping có các cột layer, datatype, tên layer và purpose; nếu một cặp layer/datatype xuất hiện nhiều lần, file sau ghi đè file trước. `drc_files` nhận một đường dẫn hoặc danh sách đường dẫn tới Calibre rule deck, được lập chỉ mục cho RAG khi chạy agent. Có thể chọn file YAML khác bằng `python -m FAGENT --setup path/to/env_setup.yaml`.

Hệ thống hỗ trợ cả file Calibre ASCII results (`.drc.results`) và Calibre Summary Report (`.drc.summary`):
- **Calibre ASCII Results (`.results`):** Chứa từng rule, mô tả lỗi, số lượng vi phạm và danh sách marker hình học (polygon `p`, edge `e`) theo đơn vị GDS database unit (DBU).
- **Calibre DRC Summary (`.summary`):** Bóc tách số lỗi tổng cộng (`total_violations`), phân bố lỗi theo từng cell (`violations_by_cell`) và theo rule (`violations_by_rule`). Tuy nhiên, file summary **không chứa tọa độ marker** hay tỉ lệ DBU; vì vậy việc tự động định vị và sửa polygon yêu cầu file ASCII results.

Để tiếp tục một phiên bị gián đoạn tại bước review, dùng ID được in trong lời nhắc review và cùng các input/output ban đầu:

```bash
python -m FAGENT --gds <cell.gds> --drc <cell.drc.results> --layer-map <layer-map> --output <output> --config FAGENT/config.json --resume-thread <thread-id>
```

Checkpoint lưu định danh, phiên bản và digest của workflow, cấu hình và candidate. Resume yêu cầu cùng workflow, cấu hình, đường dẫn GDS, DRC và layer map. Checkpoint từ phiên bản cũ thiếu định danh workflow cần chạy lại từ input gốc trong một thư mục mới. Tránh chạy đồng thời hai tiến trình trên cùng rundir.

### Cấu trúc file đầu ra (Artifacts)

Các file được lưu trong `<output>/rundir/`. Loader ghi `gds.json` và `drc.json`. Dịch vụ layout dùng đường dẫn GDS gốc trong `gds.json` để mở bằng gdstk.

- `gds.json`: Database hình học GDS từ `ProcessGDS` theo đơn vị Database Units (DBU), bao gồm `structs`, polygon, path, text, SREF và AREF.
- `drc.json`: Dữ liệu vi phạm DRC chuẩn hóa từ kết quả Calibre hoặc summary report.
- `candidate.gds`: Layout ứng viên được lưu khi có thay đổi hình học. File GDS gốc tuyệt đối không bị ghi đè.
- `conversation.json`: Toàn bộ lịch sử trao đổi giữa Agent, llama.cpp, các tool call và phản hồi review của người dùng.
- `graph-checkpoints.sqlite3`: Checkpoint state của LangGraph trong rundir; không chứa model hoặc đối tượng GDS đang chạy.
- `result.json`: Trạng thái kết thúc vòng lặp agent (`status`), lý do dừng, phản hồi có cấu trúc, thông tin `validation` (`simulated`) và kết quả `review` (`decision`, `storage_consent`, `consent_source`).

### Quy đổi đơn vị (DBU Conversion)
Binary GDS và kết quả Calibre sử dụng đơn vị số nguyên Database Units (DBU, ví dụ: 1000 DBU = 1 $\mu m$). Trong khi đó, thư viện `gdstk` thao tác trên đơn vị User Units ($\mu m$, số thực). Khi đối chiếu marker từ Calibre sang tọa độ `gdstk`, tọa độ DBU phải được chia cho `dbu_per_user_unit`.

## Calibre và validation

DRC đọc báo cáo ban đầu, yêu cầu model đọc và sửa hình học, lưu candidate, chạy backend và hiển thị bằng chứng mới để duyệt. Backend `local` dùng Calibre Simulator khi có rule deck và dùng mock khi thiếu deck. Kết quả giả lập có `simulated: true` và trạng thái tổng thể `incomplete`. Backend `real` hiện trả về `unavailable` vì chưa có hợp đồng gọi công cụ và đọc kết quả thực tế.

Dùng `--workflow /path/to/asset-root/workflows/task.yaml` để chọn định nghĩa khác. Đặt prompt và schema trong các thư mục tương ứng dưới cùng asset root. YAML chỉ tham chiếu các thao tác đã đăng ký.

### Yêu cầu LVS

```bash
python -m FAGENT --task lvs --output output/lvs
```

Có thể truyền `--lvs-report` để ghi đường dẫn báo cáo. Phân tích báo cáo, sửa layout và chạy LVS thật đang ở trạng thái `unavailable`. Mock được bật trong `lvs.yaml` cho ra `pass` kèm `simulated: true`, với trạng thái tổng thể `incomplete`. LVS thật cần định danh công cụ, ví dụ báo cáo pass, mismatch và lỗi, layout, netlist tham chiếu, deck, lệnh gọi và tiêu chí đối chiếu.

## Interactive Review & Hybrid RAG

1. **Duyệt tương tác**
   - LangGraph hiển thị bằng chứng mới và tạm dừng để nhận phản hồi. Model phân loại phản hồi thành dừng, tiếp tục hoặc chưa rõ. Với DRC, yêu cầu tiếp tục quay lại bước sửa. EOF kết thúc phiên và ghi artifact.
   - Trả lời `y` ở lời nhắc lưu tri thức riêng là thao tác cấp quyền. Quyền được ghi cùng phản hồi nếu phiên kết thúc tại lượt duyệt đó.
2. **Truy xuất Hybrid RAG & Batched llama.cpp Embedding (`Agent_rag`):**
   - `FAGENT/config.json` cấu hình `embedding_model_path` và `embedding_batch_size` (mặc định `16`) cho model GGUF cục bộ.
   - Chỉ các bản ghi hợp lệ (`eligible`: tài liệu tham khảo `kind=reference`, quy tắc trích xuất từ `DRC/1P7M_6Ic_1TKMc_ALPA2.drc`, và các case đã qua human review) mới được tạo embedding theo lô (`embed_batch`) và đưa vào truy xuất. Dữ liệu legacy chưa duyệt trong `knowledge_db.json` được giữ nguyên nhưng bị loại khỏi kết quả truy xuất.
   - Cache vector trong `FAGENT/knowledge/rag.sqlite3` gắn với định danh model và tiền tố query/document; khi model embedding không khả dụng hoặc lỗi batch, hệ thống tự động fallback về truy xuất lexical + exact rule mà không làm hỏng cache. Định danh cache dùng đường dẫn, kích thước và `mtime_ns`: thay thế tệp mà giữ nguyên cả kích thước lẫn thời gian sửa có thể không được phát hiện. Chi tiết kiến trúc và sơ đồ Mermaid xem tại `.agent/AI_Workflow.md`.

## Kiểm thử

```bash
python -m pytest -q
```
