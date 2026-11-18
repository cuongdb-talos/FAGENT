# Local GDS DRC agent

Workflow GDS → Calibre ASCII DRC → llama.cpp → LangGraph tool calls → candidate GDS → validation → human review → Hybrid RAG. Các package `FAGENT/agent`, `domain`, `io`, `models`, `rag` và `tools` phân tách workflow khỏi xử lý CAD (điểm vào CLI ở `FAGENT/cli.py`). Các thư mục cũ `AGENT`, `RAG`, `TOOLS` và các thư mục memory cũ `knowledge/mandatory/`, `knowledge/references/` đã được dọn bỏ; chúng không cần thiết để chạy.

## Cài đặt llama.cpp

Cài Python và các công cụ biên dịch cần cho `llama-cpp-python`, sau đó chạy:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Có thể dùng `FAGENT/config.example.json` làm mẫu cấu hình cho môi trường khác; thay đường dẫn model bằng giá trị của máy chạy. `FAGENT/config.json` có thể chứa `system_prompt` để cấu hình hướng dẫn cho model và `reference_dir` để chỉ định thư mục tài liệu Markdown tùy chọn bên trong `FAGENT/knowledge`. Dữ liệu truy xuất được đánh dấu là tham khảo, không phải chỉ thị. File cấu hình hiện tại chứa `agent_model_path` (Qwen2.5-Coder GGUF) và `embedding_model_path` (BGE-M3 GGUF). Điều chỉnh các đường dẫn cho máy chạy thực tế. Các khóa `agent_n_ctx`, `embedding_n_ctx`, `agent_n_threads`, `embedding_n_threads`, `agent_n_gpu_layers` và `embedding_n_gpu_layers` điều chỉnh tài nguyên; `null` cho số thread dùng mặc định của binding và `0` GPU layers chạy trên CPU. Muốn dùng GPU cần cài bản `llama-cpp-python` được biên dịch với backend GPU tương ứng. `agent_chat_format: null` dùng chat template trong GGUF; có thể cấu hình tên format khác tương thích model và tool calling. Cần kiểm tra thực tế tool calling với GGUF và phiên bản binding đang dùng. Không cần chạy server.

## Ví dụ

Từ thư mục này, sử dụng bộ mẫu (hoặc chạy script `./cmd`):

```bash
python -m FAGENT --gds ./DRC/ROM_MCELL0.gds \
  --drc ./DRC/ROM_MCELL0.drc.results --output output/sample --load-only
python -m FAGENT --gds ./DRC/ROM_MCELL0.gds \
  --drc ./DRC/ROM_MCELL0.drc.results --output output --config FAGENT/config.json
```

Chạy theo danh sách cell bằng `./cmd` (thêm `--load-only --quiet` để chỉ nạp dữ liệu). File `env_setup.yaml` khai báo `list_cells`, `drc_folder`, `output_folder`, `layer_map_files` và `drc_files`; các đường dẫn tương đối được tính từ thư mục chứa file YAML. Mỗi dòng trong `list_cells` là một cell; với cell `ROM_MCELL0`, chương trình đọc `DRC/ROM_MCELL0.gds` và `DRC/ROM_MCELL0.drc.results`, rồi ghi vào `output/ROM_MCELL0/rundir/`. `layer_map_files` là danh sách file mapping có các cột layer, datatype, tên layer và purpose; nếu một cặp layer/datatype xuất hiện nhiều lần, file sau ghi đè file trước. `drc_files` nhận một đường dẫn hoặc danh sách đường dẫn tới Calibre rule deck, được lập chỉ mục cho RAG khi chạy agent. Có thể chọn file YAML khác bằng `python -m FAGENT --setup path/to/env_setup.yaml`.

Hệ thống hỗ trợ cả file Calibre ASCII results (`.drc.results`) và Calibre Summary Report (`.drc.summary`):
- **Calibre ASCII Results (`.results`):** Chứa từng rule, mô tả lỗi, số lượng vi phạm và danh sách marker hình học (polygon `p`, edge `e`) theo đơn vị GDS database unit (DBU).
- **Calibre DRC Summary (`.summary`):** Bóc tách số lỗi tổng cộng (`total_violations`), phân bố lỗi theo từng cell (`violations_by_cell`) và theo rule (`violations_by_rule`). Tuy nhiên, file summary **không chứa tọa độ marker** hay tỉ lệ DBU; vì vậy việc tự động định vị và sửa polygon yêu cầu file ASCII results.

Để tiếp tục một phiên bị gián đoạn tại bước review, dùng ID được in trong lời nhắc review và cùng các input/output ban đầu:

```bash
python -m FAGENT --gds <cell.gds> --drc <cell.drc.results> --output <output> --config FAGENT/config.json --resume-thread <thread-id>
```

Checkpoint lưu state và candidate đã được ghi vào file, không lưu đối tượng model trong bộ nhớ. Giữ nguyên file GDS, DRC và candidate khi tiếp tục. Tránh chạy đồng thời hai tiến trình trên cùng rundir.

### Cấu trúc file đầu ra (Artifacts)

Các file được lưu trong `<output>/rundir/`. Loader ghi `gds.json` và `drc.json`; caller và functions đọc các JSON này từ rundir. `gds.json` lưu đường dẫn GDS gốc để functions mở bằng gdstk; functions không phân tích trực tiếp báo cáo Calibre.

- `gds.json`: Database hình học GDS từ `ProcessGDS` theo đơn vị Database Units (DBU), bao gồm `structs`, polygon, path, text, SREF và AREF.
- `drc.json`: Dữ liệu vi phạm DRC chuẩn hóa từ kết quả Calibre hoặc summary report.
- `candidate.gds`: Layout ứng viên được lưu khi có thay đổi hình học. File GDS gốc tuyệt đối không bị ghi đè.
- `conversation.json`: Toàn bộ lịch sử trao đổi giữa Agent, llama.cpp, các tool call và phản hồi review của người dùng.
- `graph-checkpoints.sqlite3`: Checkpoint state của LangGraph trong rundir; không chứa model hoặc đối tượng GDS đang chạy.
- `result.json`: Trạng thái kết thúc vòng lặp agent (`status`), lý do dừng, phản hồi có cấu trúc, thông tin `validation` (`simulated`) và kết quả `review` (`authorized`, `completed`, `save_requested`).

### Quy đổi đơn vị (DBU Conversion)
Binary GDS và kết quả Calibre sử dụng đơn vị số nguyên Database Units (DBU, ví dụ: 1000 DBU = 1 $\mu m$). Trong khi đó, thư viện `gdstk` thao tác trên đơn vị User Units ($\mu m$, số thực). Khi đối chiếu marker từ Calibre sang tọa độ `gdstk`, tọa độ DBU phải được chia cho `dbu_per_user_unit`.

## Calibre và validation

`validate()` lưu candidate sau khi sửa, rồi gọi `run_calibre()`. Hiện tại hàm này **giả lập** kết quả `{"status": "good", "simulated": true}`; workflow không coi kết quả này là DRC pass và lưu nhãn giả lập (`validation_source: "simulated"`) trong `result.json` cũng như trong mọi bản ghi RAG được học. Không có lần chạy Calibre thực trong bước này và hệ thống không bao giờ coi kết quả giả lập là lần chạy Calibre thực (`real`).

### Yêu cầu LVS
- **DRC Clean $\neq$ LVS Pass:** Việc sửa đổi layout để hết lỗi DRC (spacing, enclosure, width) không đảm bảo giữ nguyên tính toàn vẹn của kết nối mạch (Connectivity) và sơ đồ nguyên lý (LVS - Layout Versus Schematic). Mọi candidate layout bắt buộc phải vượt qua kiểm định Calibre LVS và Electrical QA độc lập trước khi đưa vào compiler hoặc tape-out.

## Interactive Review & Hybrid RAG

1. **Cổng duyệt tương tác (`ReviewState`):**
   - LangGraph tạm dừng bằng interrupt sau khi agent hoàn tất chuỗi tool call (hoặc chạm `max_iterations`), terminal yêu cầu người dùng đánh giá (`Review task (EOF to finish): `).
   - Người dùng có thể nhập yêu cầu sửa tiếp để model agent gọi thêm tool (các lệnh gọi `replace_polygon` và `validate` sẽ tự động xóa quyền phê duyệt trước đó).
   - Tri thức chỉ được lưu vào RAG khi phản hồi của chính người dùng xác nhận rõ ràng **cả hai điều kiện**: công việc đã hoàn thành và yêu cầu lưu tri thức (ví dụ: `"The task is complete. Save the knowledge from this run."` hoặc `"Task đã hoàn thành. Hãy lưu kiến thức từ lần chạy này."`). Các câu phủ định, trích dẫn hoặc mơ hồ đều bị từ chối.
   - Khi gặp `EOF` hoặc ngắt (`Ctrl+C`), hệ thống vẫn lưu `conversation.json` và `result.json` nhưng không lưu vào kho tri thức.
2. **Truy xuất Hybrid RAG & Batched llama.cpp Embedding (`Agent_rag`):**
   - `FAGENT/config.json` cấu hình `embedding_model_path` và `embedding_batch_size` (mặc định `16`) cho model GGUF cục bộ.
   - Chỉ các bản ghi hợp lệ (`eligible`: tài liệu tham khảo `kind=reference`, quy tắc trích xuất từ `DRC/1P7M_6Ic_1TKMc_ALPA2.drc`, và các case đã qua human review) mới được tạo embedding theo lô (`embed_batch`) và đưa vào truy xuất. Dữ liệu legacy chưa duyệt trong `knowledge_db.json` được giữ nguyên nhưng bị loại khỏi kết quả truy xuất.
   - Cache vector trong `FAGENT/knowledge/rag.sqlite3` gắn với định danh model và tiền tố query/document; khi model embedding không khả dụng hoặc lỗi batch, hệ thống tự động fallback về truy xuất lexical + exact rule mà không làm hỏng cache. Định danh cache dùng đường dẫn, kích thước và `mtime_ns`: thay thế tệp mà giữ nguyên cả kích thước lẫn thời gian sửa có thể không được phát hiện. Chi tiết kiến trúc và sơ đồ Mermaid xem tại `.agent/AI_Workflow.md`.

## Kiểm thử

```bash
python -m pytest -q
```
