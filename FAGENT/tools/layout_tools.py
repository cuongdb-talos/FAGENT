"""Typed model-facing wrappers for layout operations."""

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


def build_layout_tools(service):
    return [
        StructuredTool.from_function(service.read_gds, name='read_gds', description='Read direct polygons in a cell, in GDS user units.', args_schema=ReadGDSInput),
        StructuredTool.from_function(service.replace_polygon, name='replace_polygon', description='Replace one direct polygon using GDS user units.', args_schema=ReplacePolygonInput),
        StructuredTool.from_function(service.save_gds, name='save_gds', description='Save the candidate GDS.', args_schema=SaveGDSInput),
    ]
