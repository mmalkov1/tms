"""Endpoint warehouse selection: real API/rebuild with isolated DB and OSRM."""
import copy
import unittest
from contextlib import asynccontextmanager
from datetime import time
from unittest.mock import AsyncMock, patch

from test_multidepot import RUNTIME, DEPOTS

if RUNTIME:
    from test_multidepot import main, httpx


class EndpointPool:
    def __init__(self):
        self.depots = copy.deepcopy(DEPOTS)
        self.route = dict(id=17, vehicle_id=2, depot_id=1, eff_driver=2,
                          depart_time=time(9), return_time_manual=None,
                          start_kind='depot', finish_kind='depot',
                          start_lat=None, start_lon=None, finish_lat=None, finish_lon=None,
                          start_depot_id=None, finish_depot_id=None,
                          start_address=None, finish_address=None,
                          use_traffic_factors=False, traffic_factors=None, ss=time(9))
        self.writes = []
        self.commits = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy(self.route)
        try:
            yield self
        except Exception:
            self.route = before
            raise
        else:
            self.commits += 1

    async def fetchrow(self, sql, *args):
        if 'FROM depots' in sql:
            return self.depots.get(args[0])
        if 'FROM drivers' in sql:
            return dict(home_address='Дім', home_lat=48., home_lon=24.)
        if 'FROM routes r' in sql:
            if args[0] != self.route['id']:
                return None
            dep = self.depots[self.route['depot_id']]
            return {**self.route,
                    **{f'{short}_{coord}': self.route[f'{end}_{coord}']
                       if self.route[f'{end}_{coord}'] is not None else dep[coord]
                       for end, short in [('start', 's'), ('finish', 'f')]
                       for coord in ['lat', 'lon']}}
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if 'FROM route_stops' in sql:
            return [dict(order_id=1, lat=49.5, lon=26., tw_from=None,
                         break_from=None, break_to=None, service_min=15,
                         weight_kg=100, volume_m3=1, kind='delivery')]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        if 'UPDATE routes SET start_kind' in sql:
            fields = ['start_kind', 'start_address', 'start_lat', 'start_lon',
                      'finish_kind', 'finish_address', 'finish_lat', 'finish_lon']
            self.route.update(zip(fields, args[:8]))
            if args[8] is not None:
                self.route['depart_time'] = args[8]
            self.route.update(return_time_manual=args[9], start_depot_id=args[11], finish_depot_id=args[12])
        elif 'UPDATE routes SET geometry' in sql:
            self.route.update(geometry=args[0], total_km=args[1], return_time=args[5])
        elif 'UPDATE route_stops SET eta' not in sql:
            raise AssertionError(sql)


@unittest.skipUnless(RUNTIME, 'Install api/requirements.txt')
class StartFinishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = EndpointPool()
        self.pool_patch = patch.object(main, 'pool', self.pool)
        self.pool_patch.start()
        self.osrm = AsyncMock(return_value=('geometry', [600, 1200], 15.5))
        self.osrm_patch = patch.object(main.osrm, 'route_with_legs', self.osrm)
        self.osrm_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.osrm_patch.stop()
        self.pool_patch.stop()

    async def save(self, **body):
        return await self.client.patch('/api/routes/17/start-finish', json=body)

    async def test_distinct_warehouses_drive_geometry_and_times_without_changing_base(self):
        response = await self.save(start_depot_id=2, finish_depot_id=1,
                                   start_lat=0, start_lon=0, start_address='Spoof',
                                   depart_time='10:00', return_time_manual='16:00')
        self.assertEqual(response.status_code, 200, response.text)
        self.osrm.assert_awaited_once_with([(49., 25.), (49.5, 26.), (50., 30.)])
        r = self.pool.route
        self.assertEqual((r['start_address'], r['finish_address']), ('Тернопіль', 'Київ'))
        self.assertEqual((r['start_depot_id'], r['finish_depot_id']), (2, 1))
        self.assertEqual(r['depot_id'], 1)
        self.assertEqual(r['return_time'], time(10, 45))
        self.assertEqual(r['return_time_manual'], time(16))
        self.assertEqual(self.pool.commits, 1)

    async def test_both_endpoints_can_use_ternopil(self):
        response = await self.save(start_depot_id=2, finish_depot_id=2)
        self.assertEqual(response.status_code, 200)
        self.osrm.assert_awaited_once_with([(49., 25.), (49.5, 26.), (49., 25.)])

    async def test_legacy_client_without_ids_keeps_route_depot_fallback(self):
        response = await self.save()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.pool.route['start_lat'])
        self.assertIsNone(self.pool.route['start_depot_id'])
        self.osrm.assert_awaited_once_with([(50., 30.), (49.5, 26.), (50., 30.)])

    async def test_switch_to_home_custom_clears_saved_depot_selection(self):
        await self.save(start_depot_id=2, finish_depot_id=2)
        response = await self.save(start_kind='home', start_depot_id=2,
                                   finish_kind='custom', finish_depot_id=2,
                                   finish_address='СТО', finish_lat=47., finish_lon=23.)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.pool.route['start_depot_id'])
        self.assertIsNone(self.pool.route['finish_depot_id'])
        self.osrm.assert_awaited_with([(48., 24.), (49.5, 26.), (47., 23.)])

    async def test_invalid_or_pending_depot_does_not_write_either_endpoint(self):
        for depot_id in [999, 2]:
            with self.subTest(depot=depot_id):
                self.pool.depots[2]['lat'] = None
                response = await self.save(start_depot_id=1, finish_depot_id=depot_id)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.pool.writes, [])
        self.osrm.assert_not_awaited()

    async def test_failed_rebuild_rolls_back_endpoint_change(self):
        before = copy.deepcopy(self.pool.route)
        self.osrm.side_effect = RuntimeError('OSRM unavailable')
        with self.assertRaises(RuntimeError):
            await self.save(start_depot_id=2, finish_depot_id=1)
        self.assertEqual(self.pool.route, before)
        self.assertEqual(self.pool.commits, 0)

    async def test_missing_route_is_not_modified(self):
        response = await self.client.patch('/api/routes/999/start-finish', json={'start_depot_id': 2})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.pool.writes, [])
