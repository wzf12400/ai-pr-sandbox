"""Small calculator functions for workflow experiments."""


def _validate_operand(value: float) -> None:
    if value > 1000:
        raise ValueError("calculator operands must not exceed 1000")


def add(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left + right


def subtract(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left - right
