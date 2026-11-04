from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['monthly_branch','top_products','weekly_sales','quarterly_branches','low_stock','category_revenue']
    start: date
    end: date
    branch_id: int | None = Field(None, gt=0)
    category_id: int | None = Field(None, gt=0)
    chart: Literal['bar','line'] = 'bar'

    @model_validator(mode='after')
    def dates(self):
        if self.end < self.start or (self.end-self.start).days > 366:
            raise ValueError('El rango debe ser válido y de hasta 366 días.')
        return self


class PromptIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=5, max_length=2000)


class MLRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    demo: bool = False
    branch_id: int | None = Field(None, gt=0)
