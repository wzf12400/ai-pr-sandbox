"""Small calculator functions for workflow experiments."""


def _validate_operand(value: float) -> None:
    if value > 100:
        raise ValueError("calculator operands must not exceed 100")


def add(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left + right


def subtract(left: float, right: float) -> float:
    _validate_operand(left)
    _validate_operand(right)
    return left - right
