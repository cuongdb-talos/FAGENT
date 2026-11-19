"""Package FAGENT.tools: Bộ công cụ xử lý GDS/DRC, nạp dữ liệu đầu vào và đóng gói LangChain tools cho Agent.

Trách nhiệm & Chức năng:
- Xử lý dữ liệu CAD/EDA: phân tích nhị phân GDSII (`gds_parser`), phân tích Calibre DRC (`drc`),
  nạp & chuẩn hóa đầu vào (`loaders`), và thao tác layout qua gdstk (`layout`).
- Cung cấp các Pydantic schema chặt chẽ và hàm `build_tools()` (`agent_tools`)
  cho phép LLM tương tác an toàn với layout GDS và dịch vụ kiểm tra Calibre DRC.
"""
