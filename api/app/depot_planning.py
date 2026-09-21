"""Internal TMS depots; deliberately independent of the 1C warehouse code."""
import math


SCHEMA_SQL = """
ALTER TABLE depots ALTER COLUMN lat DROP NOT NULL;
ALTER TABLE depots ALTER COLUMN lon DROP NOT NULL;
INSERT INTO depots (name, address, lat, lon)
SELECT 'Склад Тернопіль', 'Україна, Тернопільська обл., с. Біла, вул. Мазепи, 24Д', NULL, NULL
WHERE NOT EXISTS (SELECT 1 FROM depots WHERE name='Склад Тернопіль');
"""


def coordinates_ready(depot):
    return (depot is not None and depot['lat'] is not None and depot['lon'] is not None
            and math.isfinite(depot['lat']) and math.isfinite(depot['lon'])
            and -90 <= depot['lat'] <= 90 and -180 <= depot['lon'] <= 180)


def depot_matrix(matrix, depot_index, depot_count):
    """Project a shared OSRM matrix to [this depot, all orders].

    Every vehicle uses node 0 for its OWN depot, orders retain indices 1..N.
    Both the departure row and the return column must be replaced.
    """
    nodes = [depot_index, *range(depot_count, len(matrix))]
    return [[matrix[i][j] for j in nodes] for i in nodes]
