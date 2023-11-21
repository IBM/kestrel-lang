from enum import Enum
from typing import Union, List
from dataclasses import dataclass
from mashumaro.mixins.json import DataClassJSONMixin


class NumCompOp(str, Enum):
    """Numerical comparison operators (for int and float)"""

    EQ = "="
    NEQ = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


@dataclass
class IntComparison(DataClassJSONMixin):
    """Integer comparison expression"""

    field: str
    op: NumCompOp
    value: int


@dataclass
class FloatComparison(DataClassJSONMixin):
    """Floating point comparison expression"""

    field: str
    op: NumCompOp
    value: float


class StrCompOp(str, Enum):
    """String comparison operators"""

    EQ = "="
    NEQ = "!="
    LIKE = "LIKE"
    NLIKE = "NOT LIKE"
    MATCHES = "MATCHES"
    NMATCHES = "NOT MATCHES"


@dataclass
class StrComparison(DataClassJSONMixin):
    """String comparison expression"""

    field: str
    op: StrCompOp
    value: str


class ListOp(str, Enum):
    """List membership operator"""

    IN = "IN"
    NIN = "NOT IN"


@dataclass
class ListStrComparison(DataClassJSONMixin):
    """List of strings membership comparison expression"""

    field: str
    op: ListOp
    value: List[str]


@dataclass
class ListIntComparison(DataClassJSONMixin):
    """List of ints membership comparison expression"""

    field: str
    op: ListOp
    value: List[int]


@dataclass
class ListComparison(DataClassJSONMixin):
    """List membership comparison expression"""

    field: str
    op: ListOp
    value: Union[List[int], List[str]]


class ExpOp(str, Enum):
    """Boolean expression operator"""

    AND = "AND"
    OR = "OR"


@dataclass
class BoolExp(DataClassJSONMixin):
    """Boolean expression of comparisons"""

    lhs: Union[
        IntComparison, FloatComparison, StrComparison, ListComparison, "BoolExp"
    ]  # "Forward declaration"
    op: ExpOp
    rhs: Union[
        IntComparison, FloatComparison, StrComparison, ListComparison, "BoolExp"
    ]  # "Forward declaration"