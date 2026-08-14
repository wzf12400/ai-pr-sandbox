"""Small calculator functions for workflow experiments."""

import builtins


def _validate_operand(value: float) -> None:
    if builtins.abs(value) > 50:
        raise ValueError("calculator operands must not exceed 50")


def add(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left + right


def subtract(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left - right


def abs(value: float) -> float:
    _validate_operand(value)
    return builtins.abs(value)
