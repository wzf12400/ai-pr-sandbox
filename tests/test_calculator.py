import unittest

from src.calculator import add, multiply, subtract


class CalculatorTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self) -> None:
        self.assertEqual(subtract(7, 4), 3)

    def test_multiply(self) -> None:
        self.assertEqual(multiply(2, 3), 6)
        self.assertEqual(multiply(-2, 3), -6)
        self.assertEqual(multiply(0, 8), 0)


if __name__ == "__main__":
    unittest.main()
