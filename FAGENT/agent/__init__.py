"""Package FAGENT.agent: Phân hệ điều phối Agent và Workflow sửa lỗi DRC bằng LangGraph.

Trách nhiệm & Chức năng:
- Xây dựng đồ thị trạng thái StateGraph kết nối LLM với các công cụ sửa layout.
- Quản lý trạng thái thực thi (state), prompt mẫu, dịch vụ phụ trợ và cơ chế human review.
- Điều khiển vòng lặp suy luận, checkpointing và lưu vết kết quả (artifacts).
"""
