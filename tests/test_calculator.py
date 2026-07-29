import unittest

from src.calculator import add, subtract


class CalculatorTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self) -> None:
        self.assertEqual(subtract(7, 4), 3)

    def test_allows_operands_equal_to_100(self) -> None:
        self.assertEqual(add(100, 0), 100)
        self.assertEqual(subtract(100, 1), 99)

    def test_rejects_operands_greater_than_100(self) -> None:
        for operation, operands in (
            (add, (101, 1)),
            (add, (1, 101)),
            (subtract, (101, 1)),
            (subtract, (1, 101)),
        ):
            with self.subTest(operation=operation.__name__, operands=operands):
                with self.assertRaisesRegex(ValueError, "must not exceed 100"):
                    operation(*operands)


if __name__ == "__main__":
    unittest.main()
