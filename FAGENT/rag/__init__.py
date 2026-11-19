"""Package FAGENT.rag: Hệ thống Truy xuất Thông tin Tăng cường (RAG) và Tích lũy Tri thức Sửa lỗi DRC.

Trách nhiệm & Chức năng:
- Lập chỉ mục quy tắc từ DRC rule decks (SVRF) và tài liệu hướng dẫn kỹ thuật.
- Lưu trữ cơ sở dữ liệu tri thức trên SQLite và xuất snapshot JSON nguyên tử.
- Truy xuất tri thức kết hợp (Hybrid Retrieval) giữa khớp chính xác, từ khóa lexical và ngữ nghĩa semantic.
- Tự động trích xuất và học hỏi từ các trường hợp sửa lỗi thành công đã qua kiểm duyệt (Case Learner).
"""
