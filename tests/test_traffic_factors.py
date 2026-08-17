import unittest

from app import solver


FACTORS = {
    "07-10": 1.59,
    "10-13": 1.43,
    "13-16": 1.68,
    "16-19": 1.10,
    "other": 1.52,
}


class TrafficFactorsTest(unittest.TestCase):
    def test_bucket_boundaries(self):
        self.assertEqual(solver.traffic_factor(7 * 60, FACTORS), 1.59)
        self.assertEqual(solver.traffic_factor(10 * 60, FACTORS), 1.43)
        self.assertEqual(solver.traffic_factor(13 * 60, FACTORS), 1.68)
        self.assertEqual(solver.traffic_factor(16 * 60, FACTORS), 1.10)
        self.assertEqual(solver.traffic_factor(19 * 60, FACTORS), 1.52)

    def test_standard_rounding_is_unchanged(self):
        self.assertEqual(solver.travel_minutes(599, 9 * 60), 9)
        self.assertEqual(solver.travel_minutes(600, 9 * 60), 10)

    def test_factor_changes_eta(self):
        stop = solver.Stop(1, 8 * 60, 18 * 60, 10, 1, 1)
        durations = [[0, 600], [600, 0]]
        standard = solver.eta_schedule([stop], durations, 9 * 60)
        adjusted = solver.eta_schedule([stop], durations, 9 * 60, FACTORS)
        self.assertEqual(standard, [(550, 560)])
        self.assertEqual(adjusted, [(555, 565)])

    def test_vehicle_depot_rows_use_own_shift(self):
        stops = [solver.Stop(1, 8 * 60, 18 * 60, 10, 1, 1)]
        trucks = [
            solver.Truck(1, 10, 10, 9 * 60, 18 * 60),
            solver.Truck(2, 10, 10, 10 * 60, 18 * 60),
        ]
        durations = [[0, 600], [600, 0]]
        matrices = solver.coefficient_duration_matrices(
            [[0], []], stops, trucks, durations, FACTORS)
        self.assertEqual(matrices[0][0][1], round(600 * 1.59))
        self.assertEqual(matrices[1][0][1], round(600 * 1.43))


if __name__ == "__main__":
    unittest.main()
