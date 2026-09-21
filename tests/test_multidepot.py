"""Run with API runtime dependencies installed; no live DB/OSRM/1C required."""
import copy
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import date, time
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'api'))
from app import depot_planning

try:
    import asyncpg
    import ortools
    import httpx
except ImportError:
    RUNTIME = False
else:
    RUNTIME = True
    # StaticFiles in main is relative to the API container work directory.
    old_cwd = os.getcwd()
    try:
        os.chdir(ROOT / 'api')
        from app import main, solver, integration_1c
    finally:
        os.chdir(old_cwd)


# Synthetic coordinates and asymmetric travel; not the real Ternopil location.
DEPOTS = {
    1: dict(id=1, name='Київ', lat=50., lon=30.),
    2: dict(id=2, name='Тернопіль', lat=49., lon=25.),
}
# [Kyiv depot, Ternopil depot, Kyiv order, Ternopil order].
MATRIX = [[0, 30000, 600, 30000], [30000, 0, 30000, 1200],
          [900, 30000, 0, 30000], [30000, 1800, 30000, 0]]


class MatrixTests(unittest.TestCase):
    def test_own_departure_and_return_and_unchanged_order_edges(self):
        self.assertEqual(depot_planning.depot_matrix(MATRIX, 1, 2),
                         [[0, 30000, 1200], [30000, 0, 30000], [1800, 30000, 0]])

    def test_single_depot_identity(self):
        original = [[0, 5, 8], [7, 0, 3], [4, 2, 0]]
        self.assertEqual(depot_planning.depot_matrix(original, 0, 1), original)

    def test_pending_or_invalid_coordinates_not_ready(self):
        for lat, lon in [(None, None), (float('nan'), 25), (49, float('inf')), (91, 25)]:
            self.assertFalse(depot_planning.coordinates_ready(dict(lat=lat, lon=lon)))
        self.assertTrue(depot_planning.coordinates_ready(DEPOTS[2]))


@unittest.skipUnless(RUNTIME, 'Install api/requirements.txt for solver and API tests')
class SolverTests(unittest.TestCase):
    def setUp(self):
        self.stops = [solver.Stop(i, 540, 900, 10, 100, 1) for i in (1, 2)]
        self.trucks = [solver.Truck(i, 1000, 10, 540, 960) for i in (1, 2)]
        self.matrices = [depot_planning.depot_matrix(MATRIX, i, 2) for i in (0, 1)]

    def test_real_solver_distributes_to_own_depots(self):
        routes = solver.solve(self.stops, self.trucks, self.matrices[0], 1,
                              vehicle_durations=self.matrices, span_cost=10)
        self.assertEqual(routes, [[0], [1]])
        self.assertEqual(solver.eta_schedule([self.stops[1]], self.matrices[1], 540), [(560, 570)])
        self.assertEqual(570 + solver.travel_minutes(self.matrices[1][2][0], 570), 600)

    def test_traffic_uses_vehicle_base_without_double_scaling(self):
        factors = {'07-10': 2, '10-13': 3, 'other': 1}
        adjusted = solver.coefficient_duration_matrices(
            [[0], [1]], self.stops, self.trucks, self.matrices[0], factors,
            vehicle_base_durations=self.matrices)
        self.assertEqual(adjusted[0][0][1], 1200)
        self.assertEqual(adjusted[1][0][2], 2400)
        self.assertEqual(adjusted[1][2][0], 3600)
        self.assertEqual(self.matrices[1][2][0], 1800)
        self.assertEqual(solver.solve(self.stops, self.trucks, self.matrices[0], 1,
                                     vehicle_durations=adjusted), [[0], [1]])

    def test_hard_vehicle_restrictions_preserved(self):
        result = solver.solve(self.stops, self.trucks, self.matrices[0], 1,
                              hard_allowed=[[0], []], vehicle_durations=self.matrices)
        self.assertEqual(result, [[0], []])


class FakePool:
    def __init__(self):
        self.depots = copy.deepcopy(DEPOTS)
        self.vehicles = [dict(id=i, name=f'Car {i}', driver_id=i, depot_id=i,
                              max_weight_kg=1000, max_volume_m3=10, ss=time(9), se=time(16),
                              can_pickup=True, can_delivery=True) for i in (1, 2)]
        self.inserts, self.writes = [], []

    @asynccontextmanager
    async def acquire(self):
        yield self

    transaction = acquire

    async def fetchrow(self, sql, *args):
        if 'FROM depots' in sql:
            return self.depots.get(args[0])
        if 'FROM vehicles' in sql:
            return next((v for v in self.vehicles if v['id'] == args[0]), None)
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if 'FROM vehicles v' in sql:
            return self.vehicles
        if 'FROM orders WHERE project_id' in sql:
            return [dict(id=i, lat=50.1 if i == 1 else 49.1, lon=30.1 if i == 1 else 25.1,
                         tw_from=time(9), tw_to=time(15), service_min=10, weight_kg=100,
                         volume_m3=1, kind='delivery', break_from=None, break_to=None)
                    for i in (1, 2)]
        if any(x in sql for x in ('SELECT DISTINCT', 'vehicle_day_windows', 'SELECT color')):
            return []
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if 'INSERT INTO routes' in sql:
            self.inserts.append((sql, args))
            return 400 + len(self.inserts)
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        return 'OK'


@unittest.skipUnless(RUNTIME, 'Install api/requirements.txt for solver and API tests')
class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = FakePool()
        self.patch_pool = patch.object(main, 'pool', self.pool)
        self.patch_pool.start()
        self.addCleanup(self.patch_pool.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_plan_persists_own_depot_km_eta_geometry(self):
        for traffic in (False, True):
            self.pool.inserts.clear()
            geometry = AsyncMock(return_value='encoded-geometry')
            with patch.object(main.osrm, 'table', AsyncMock(return_value=(MATRIX, MATRIX))), \
                 patch.object(main.osrm, 'route_geometry', geometry), \
                 patch.object(main, '_load_traffic_factors', AsyncMock(return_value={'other': 2})):
                response = await self.client.post('/api/plan', params={
                    'project_id': 75, 'plan_date': '2026-09-22', 'time_limit': 1,
                    'use_traffic_factors': str(traffic).lower()})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(self.pool.inserts), 2)
            first, second = [args for sql, args in self.pool.inserts]
            self.assertEqual([first[-1], second[-1]], [1, 2])
            self.assertEqual([first[4], second[4]], [1.5, 3.0])
            self.assertEqual(second[9], time(10, 50) if traffic else time(10))
            ternopil_points = geometry.await_args_list[1].args[0]
            self.assertEqual(ternopil_points[0], (49., 25.))
            self.assertEqual(ternopil_points[-1], (49., 25.))
            self.assertEqual(response.json()['dropped_orders'], [])

    async def test_pending_depot_blocks_before_any_write(self):
        self.pool.depots[2]['lat'] = None
        response = await self.client.post('/api/plan', params={
            'project_id': 75, 'plan_date': '2026-09-22'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('координати', response.json()['detail'])
        self.assertEqual(self.pool.writes, [])
        self.assertEqual(self.pool.inserts, [])

    async def test_ternopil_only_selection_still_uses_ternopil_not_id_one(self):
        matrix = depot_planning.depot_matrix(MATRIX, 1, 2)
        table = AsyncMock(return_value=(matrix, matrix))
        with patch.object(main.osrm, 'table', table), \
             patch.object(main.osrm, 'route_geometry', AsyncMock(return_value='geometry')):
            response = await self.client.post('/api/plan', params={
                'project_id': 75, 'plan_date': '2026-09-22', 'time_limit': 1, 'vehicle_ids': '2'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(table.await_args.args[0][0], (49., 25.))
        self.assertEqual(len(self.pool.inserts), 1)
        self.assertEqual(self.pool.inserts[0][1][-1], 2)
        self.assertEqual(response.json()['dropped_orders'], [1])

    async def test_vehicle_swap_keeps_route_depot_and_warns(self):
        pool = AsyncMock()
        pool.fetchrow.side_effect = [
            dict(id=428, driver_id=1, eff_driver=1, depot_id=1),
            self.pool.vehicles[1], dict(w=0, v=0),
        ]
        with patch.object(main, 'pool', pool):
            response = await self.client.patch('/api/routes/428/vehicle', json={'vehicle_id': 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('Склад цього рейсу збережено', response.json()['warning'])
        self.assertNotIn('depot_id=', pool.execute.await_args.args[0])

    async def test_manual_route_uses_vehicle_depot(self):
        response = await self.client.post('/api/routes', json={
            'project_id': 75, 'plan_date': '2026-09-22', 'vehicle_id': 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.pool.inserts[0][1][-1], 2)

    async def test_routing_failure_does_not_delete_old_routes(self):
        with patch.object(main.osrm, 'table', AsyncMock(return_value=(MATRIX, MATRIX))), \
             patch.object(main.osrm, 'route_geometry', AsyncMock(side_effect=RuntimeError('OSRM unavailable'))):
            with self.assertRaises(RuntimeError):
                await self.client.post('/api/plan', params={
                    'project_id': 75, 'plan_date': '2026-09-22', 'time_limit': 1})
        self.assertFalse(any('DELETE FROM routes' in sql for sql, _ in self.pool.writes))
        self.assertEqual(self.pool.inserts, [])

    async def test_invalid_depot_assignment_writes_nothing(self):
        response = await self.client.patch('/api/vehicles/1', json={'depot_id': 999})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pool.writes, [])

    async def test_vehicle_assignment_does_not_rewrite_existing_routes(self):
        self.pool.vehicles[0].update(code_1c='car1', is_hired=False)
        response = await self.client.patch('/api/vehicles/1', json={'depot_id': 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.pool.writes), 1)
        sql, args = self.pool.writes[0]
        self.assertIn('UPDATE vehicles', sql)
        self.assertEqual(args[-1], 2)

    async def test_bad_coordinates_rejected(self):
        response = await self.client.put('/api/depots/2', json={'address': 'Біла', 'lat': 91, 'lon': 25})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pool.writes, [])


@unittest.skipUnless(RUNTIME, 'Install api/requirements.txt for export tests')
class ExportCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_depots_do_not_change_xml_or_project_warehouse(self):
        row = dict(id=428, plan_date=date(2026, 9, 22), depart_time=time(9), return_time=time(12),
                   return_time_manual=None, car_code='car1', driver_code='driver1',
                   vehicle_name='Car', driver_name='Driver', total_km=20, warehouse_code_1c='S#K000001',
                   depot_id=1, depot_name='Київ')
        rows = [row, {**row, 'id': 429, 'car_code': 'car2', 'driver_code': 'driver2'}]

        async def fetch(sql, *args):
            if 'FROM routes r' in sql:
                return rows
            if 'FROM route_stops' in sql:
                return [dict(seq=1, eta=time(10), etd=time(10, 15), order_id=7,
                             doc_number='ORDER7', weight_kg=100)]
            return []

        pool = AsyncMock()
        pool.fetch.side_effect = fetch
        with patch.object(integration_1c, 'pool', pool), \
             patch.object(integration_1c, '_check_key', AsyncMock(return_value=('project', 75))):
            before = (await integration_1c.export_trips('test', None, None)).body
            rows[1].update(depot_id=2, depot_name='Тернопіль')
            after = (await integration_1c.export_trips('test', None, None)).body
        self.assertEqual(before, after)
        root = ET.fromstring(after)
        self.assertEqual(root.findtext('ERROR'), '0')
        self.assertEqual(len(root.findall('TRIP')), 2)
        self.assertEqual([t.findtext('CODE_CAR') for t in root.findall('TRIP')], ['car1', 'car2'])
        for trip in root.findall('TRIP'):
            self.assertEqual([x.findtext('CODE') for x in trip.findall('ORDERS/ORDER')],
                             ['S#K000001', 'ORDER7', 'S#K000001'])


if __name__ == '__main__':
    unittest.main()
