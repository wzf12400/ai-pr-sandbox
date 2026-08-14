"""Small calculator functions for workflow experiments."""


def _validate_operand(value: float) -> None:
    if abs(value) > 50:
        raise ValueError("calculator operands must not exceed 50")


def add(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left + right


def subtract(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left - right


def power(x: float, y: float) -> float:
    _validate_operand(x)
    _validate_operand(y)
    return x**y
