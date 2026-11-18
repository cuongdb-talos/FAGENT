"""Typed wrapper for candidate DRC validation."""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel


class ValidateInput(BaseModel):
    pass


def build_validation_tools(service):
    return [StructuredTool.from_function(service.validate, name='validate', description='Validate the candidate and identify simulated results.', args_schema=ValidateInput)]
