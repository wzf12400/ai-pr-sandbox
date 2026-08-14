import unittest

from src.calculator import add, mod, subtract


class CalculatorTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self) -> None:
        self.assertEqual(subtract(7, 4), 3)

    def test_mod(self) -> None:
        self.assertEqual(mod(7, 4), 3)

    def test_allows_operands_equal_to_50(self) -> None:
        self.assertEqual(add(50, 0), 50)
        self.assertEqual(subtract(50, 1), 49)
        self.assertEqual(add(-50, 0), -50)
        self.assertEqual(subtract(-50, 1), -51)

    def test_rejects_operands_greater_than_50(self) -> None:
        for operation, operands in (
            (add, (51, 1)),
            (add, (1, 51)),
            (add, (-51, 1)),
            (add, (1, -51)),
            (subtract, (51, 1)),
            (subtract, (1, 51)),
            (subtract, (-51, 1)),
            (subtract, (1, -51)),
        ):
            with self.subTest(operation=operation.__name__, operands=operands):
                with self.assertRaisesRegex(ValueError, "must not exceed 50"):
                    operation(*operands)


if __name__ == "__main__":
    unittest.main()
