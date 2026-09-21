import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from app import geo  # noqa: E402


class NormalizePolygonTest(unittest.TestCase):
    def test_normalizes_numbers_and_removes_duplicate_closing_point(self):
        result = geo.normalize_polygon([
            ["50.45000004", "30.52000004"],
            [50.46, 30.52],
            [50.45, 30.54],
            [50.45000004, 30.52000004],
        ])
        self.assertEqual(result, [
            [50.45, 30.52],
            [50.46, 30.52],
            [50.45, 30.54],
        ])

    def test_removes_consecutive_duplicates(self):
        result = geo.normalize_polygon([
            [50.45, 30.52], [50.45, 30.52],
            [50.46, 30.52], [50.45, 30.54],
        ])
        self.assertEqual(len(result), 3)

    def test_rejects_too_few_distinct_vertices(self):
        with self.assertRaisesRegex(ValueError, "3 різні"):
            geo.normalize_polygon([[50.45, 30.52], [50.46, 30.52], [50.45, 30.52]])

    def test_rejects_zero_area_polygon(self):
        with self.assertRaisesRegex(ValueError, "ненульову площу"):
            geo.normalize_polygon([[50.45, 30.52], [50.46, 30.53], [50.47, 30.54]])

    def test_rejects_invalid_coordinate_range(self):
        with self.assertRaisesRegex(ValueError, "діапазоном"):
            geo.normalize_polygon([[91, 30.52], [50.46, 30.52], [50.45, 30.54]])

    def test_rejects_non_finite_coordinate(self):
        with self.assertRaisesRegex(ValueError, "скінченними"):
            geo.normalize_polygon([[math.inf, 30.52], [50.46, 30.52], [50.45, 30.54]])


if __name__ == "__main__":
    unittest.main()
