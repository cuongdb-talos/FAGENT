"""Module FAGENT.agent.state: Định nghĩa trạng thái tuần tự hóa (Serializable State) cho LangGraph.

Trách nhiệm & Chức năng:
- Cung cấp TypedDict `FagentState` lưu trữ toàn bộ dữ liệu trạng thái xuyên suốt các node trong đồ thị:
  + messages: Danh sách tin nhắn hội thoại và các tool call giữa Agent và LLM.
  + rundir: Đường dẫn thư mục làm việc chứa dữ liệu và kết quả.
  + candidate_path: Đường dẫn tới file layout candidate.gds đang được sửa đổi.
  + iteration: Đếm số vòng lặp tương tác để tránh lặp vô tận (giới hạn bởi max_iterations).
  + validation: Kết quả kiểm tra DRC sau khi sửa layout (chứa status, simulated, vi phạm).
  + result: Tổng hợp kết quả cuối cùng của phiên chạy.
  + review: Trạng thái và nội dung phê duyệt từ con người.
  + save_requested, review_finished: Cờ kiểm soát quyền lưu tri thức và kết thúc review.
"""
from typing import Any, TypedDict


class FagentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    rundir: str
    candidate_path: str
    iteration: int
    validation: dict[str, Any] | None
    result: dict[str, Any]
    review: dict[str, Any]
    save_requested: bool
    review_finished: bool
    inputs: dict[str, Any]
    response: dict[str, Any]
    repair_error: str
    inspected: list[str]
    identity: dict[str, Any]
    candidate_digest: str | None


def candidate_digest(path):
    from hashlib import sha256

    digest = sha256()
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
