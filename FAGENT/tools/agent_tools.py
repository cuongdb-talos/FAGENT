"""Module FAGENT.tools.agent_tools: Công cụ thao tác hình học Layout GDS và xác thực DRC dành cho Agent.

Trách nhiệm & Chức năng:
- Định nghĩa các Pydantic Schema kiểm tra kiểu dữ liệu đầu vào:
  + `ReadGDSInput`: Yêu cầu cell_name để đọc cấu trúc polygon.
  + `ReplacePolygonInput`: Yêu cầu cell_name, polygon_index và danh sách tọa độ points (GDS user units).
  + `SaveGDSInput`: Yêu cầu lưu layout candidate.
  + `ValidateInput`: Kích hoạt kiểm tra Calibre DRC trên layout candidate.
- Cung cấp hàm `build_tools()` tạo các `StructuredTool` (`read_gds`, `replace_polygon`, `save_gds`, `validate`)
  kết nối trực tiếp với các phương thức của `LayoutService`.
"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, StrictFloat, StrictInt


class ReadGDSInput(BaseModel):
    cell_name: str = Field(description='Name of the cell to inspect.')


class ReplacePolygonInput(BaseModel):
    cell_name: str = Field(description='Name of the cell to modify.')
    polygon_index: StrictInt = Field(description='Index of a direct polygon in the cell.')
    points: list[list[StrictFloat]] = Field(description='Polygon points in GDS user units.')


class SaveGDSInput(BaseModel):
    pass


class ValidateInput(BaseModel):
    pass


def build_tools(service):
    return [
        StructuredTool.from_function(
            service.read_gds,
            name='read_gds',
            description='Step 1 for DRC repair: Read direct polygons of a cell in GDS user units before editing.',
            args_schema=ReadGDSInput,
        ),
        StructuredTool.from_function(
            service.replace_polygon,
            name='replace_polygon',
            description='Step 2 for DRC repair: Replace one direct polygon in GDS user units (divide DBU coordinates by dbu_per_user_unit) to fix DRC violations. Appends the new polygon and returns new_index.',
            args_schema=ReplacePolygonInput,
        ),
        StructuredTool.from_function(
            service.save_gds,
            name='save_gds',
            description='Save the current modified candidate GDS to rundir/candidate.gds.',
            args_schema=SaveGDSInput,
        ),
        StructuredTool.from_function(
            service.validate,
            name='validate',
            description='Step 3 for DRC repair: Validate the modified candidate GDS against DRC rules. Call ONLY AFTER modifying polygons with replace_polygon.',
            args_schema=ValidateInput,
        ),
    ]
