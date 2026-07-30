import unittest

from src.calculator import add, subtract


class CalculatorTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self) -> None:
        self.assertEqual(subtract(7, 4), 3)

    def test_allows_operands_equal_to_1000(self) -> None:
        self.assertEqual(add(1000, 0), 1000)
        self.assertEqual(subtract(1000, 1), 999)

    def test_rejects_operands_greater_than_1000(self) -> None:
        for operation, operands in (
            (add, (1001, 1)),
            (add, (1, 1001)),
            (subtract, (1001, 1)),
            (subtract, (1, 1001)),
        ):
            with self.subTest(operation=operation.__name__, operands=operands):
                with self.assertRaisesRegex(ValueError, "must not exceed 1000"):
                    operation(*operands)


if __name__ == "__main__":
    unittest.main()
