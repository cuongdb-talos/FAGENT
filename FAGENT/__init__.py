"""Package FAGENT: Hệ thống FAGENT DRC Auto-Repair - Tự động sửa lỗi DRC cho Layout IC.

Package gốc chứa toàn bộ các phân hệ:
- agent: Điều phối luồng xử lý bằng LangGraph, quản lý state, prompts, review và runner.
- models: Adapter kết nối mô hình suy luận và embedding cục bộ (llama.cpp).
- rag: Hệ thống lưu trữ, lập chỉ mục và truy xuất tri thức sửa lỗi DRC kết hợp.
- tools: Bộ công cụ xử lý GDS/DRC (`gds_parser`, `drc`, `layout`, `loaders`) và các công cụ LangChain (`agent_tools`) đóng gói cho LLM.
"""
