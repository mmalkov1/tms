"""CVRPTW: OR-Tools. Окна клиентов, смены водителей, вес+объём, склад старт/финиш."""
from dataclasses import dataclass

from ortools.constraint_solver import pywrapcp, routing_enums_pb2


@dataclass
class Stop:
    order_id: int
    tw_from: int        # минуты от полуночи
    tw_to: int
    service_min: int
    weight: float
    volume: float
    kind: str = "delivery"   # delivery: везем со склада (-), pickup: подбираем (+)
    break_from: int | None = None   # v39: перерва точки (обід), минуты
    break_to: int | None = None


@dataclass
class Truck:
    vehicle_id: int
    max_weight: float
    max_volume: float
    shift_start: int    # минуты
    shift_end: int


def traffic_factor(minute: int, factors: dict[str, float] | None) -> float:
    """Коефіцієнт руху за київським часом виїзду з попередньої точки."""
    if not factors:
        return 1.0
    hour = (int(minute) // 60) % 24
    if 7 <= hour < 10:
        key = "07-10"
    elif 10 <= hour < 13:
        key = "10-13"
    elif 13 <= hour < 16:
        key = "13-16"
    elif 16 <= hour < 19:
        key = "16-19"
    else:
        key = "other"
    return float(factors.get(key, factors.get("other", 1.0)))


def travel_minutes(base_sec: int | float, departure_min: int,
                   factors: dict[str, float] | None = None) -> int:
    """Час перегону в хвилинах; без коефіцієнтів повністю зберігає старе округлення."""
    if not factors:
        return int(base_sec) // 60
    adjusted_sec = int(round(float(base_sec) * traffic_factor(departure_min, factors)))
    return adjusted_sec // 60


def _advance(t: int, stop: Stop, base_sec: int | float,
             factors: dict[str, float] | None) -> tuple[int, int]:
    """Повернути ETA/ETD однієї точки з очікуванням вікна та обідньої перерви."""
    t += travel_minutes(base_sec, t, factors)
    t = max(t, stop.tw_from)
    if (stop.break_from is not None and stop.break_to is not None
            and stop.break_from - stop.service_min < t < stop.break_to):
        t = stop.break_to
    return t, t + stop.service_min


def coefficient_duration_matrices(
    routes: list[list[int]],
    stops: list[Stop],
    trucks: list[Truck],
    durations: list[list[int]],
    factors: dict[str, float],
) -> list[list[list[int]]]:
    """Матриці для уточнювального проходу оптимізатора.

    Спочатку оцінюємо час виїзду з кожної точки за поточним рішенням. Потім
    масштабуємо кожен рядок OSRM-матриці відповідним часовим коефіцієнтом.
    Окремий рядок депо для кожної машини враховує її власний початок зміни.
    """
    departures: dict[int, int] = {}
    for v, seq in enumerate(routes):
        t, prev = trucks[v].shift_start, 0
        for stop_i in seq:
            stop = stops[stop_i]
            eta, etd = _advance(t, stop, durations[prev][stop_i + 1], factors)
            departures[stop_i + 1] = etd
            t, prev = etd, stop_i + 1

    matrices = []
    for tr in trucks:
        matrix = []
        for node, row in enumerate(durations):
            if node == 0:
                depart = tr.shift_start
            else:
                stop = stops[node - 1]
                depart = departures.get(
                    node, max(tr.shift_start, stop.tw_from) + stop.service_min)
            factor = traffic_factor(depart, factors)
            matrix.append([int(round(float(sec) * factor)) for sec in row])
        matrices.append(matrix)
    return matrices


def solve(
    stops: list[Stop],
    trucks: list[Truck],
    durations: list[list[int]],   # секунды, узел 0 = склад, далее stops по порядку
    time_limit_s: int = 15,
    allowed_vehicles: list[list[int]] | None = None,  # per stop: индексы машин (геозоны)
    zone_penalty_min: int | None = None,  # None = жесткие зоны; N = мягкие, штраф N мин за чужую
    span_cost: int = 0,                   # баланс: штраф за разброс длительности машин
    hard_allowed: list[list[int]] | None = None,  # ЖЕСТКО: только эти машины (возможности авто: забор/доставка)
    vehicle_durations: list[list[list[int]]] | None = None,  # v87: уточнені матриці по авто
) -> list[list[int]] | None:
    """Возвращает по каждой машине список индексов stops (0-based) в порядке объезда."""
    n = len(stops) + 1  # + депо
    k = len(trucks)
    mgr = pywrapcp.RoutingIndexManager(n, k, 0)
    routing = pywrapcp.RoutingModel(mgr)

    # время: перегон (мин) + сервис в точке отправления
    def time_cb(fi, ti):
        f, t = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
        travel = durations[f][t] // 60
        service = stops[f - 1].service_min if f > 0 else 0
        return travel + service

    time_callbacks = []
    if vehicle_durations:
        for matrix in vehicle_durations:
            def vehicle_time_cb(fi, ti, matrix=matrix):
                f, t = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
                travel = matrix[f][t] // 60
                service = stops[f - 1].service_min if f > 0 else 0
                return travel + service
            time_callbacks.append(vehicle_time_cb)
        tcb_indices = [routing.RegisterTransitCallback(cb) for cb in time_callbacks]
    else:
        time_callbacks = [time_cb] * k
        tcb = routing.RegisterTransitCallback(time_cb)
        tcb_indices = [tcb] * k

    # Мягкие зоны: штраф уходит в СТОИМОСТЬ per-vehicle, но НЕ в размерность времени,
    # чтобы ETA оставались физическим временем без виртуальных минут.
    if allowed_vehicles and zone_penalty_min is not None:
        pen = int(zone_penalty_min)

        def make_cost(v):
            def cb(fi, ti):
                base = time_callbacks[v](fi, ti)
                f = mgr.IndexToNode(fi)
                if f > 0:
                    allowed = allowed_vehicles[f - 1]
                    if allowed is not None and v not in allowed:
                        return base + pen
                return base
            return cb

        for v in range(k):
            routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(make_cost(v)), v)
    else:
        if vehicle_durations:
            for v, cb_index in enumerate(tcb_indices):
                routing.SetArcCostEvaluatorOfVehicle(cb_index, v)
        else:
            routing.SetArcCostEvaluatorOfAllVehicles(tcb)

    horizon = 24 * 60
    if vehicle_durations:
        routing.AddDimensionWithVehicleTransits(tcb_indices, horizon, horizon, False, "Time")
    else:
        routing.AddDimension(tcb, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    if span_cost:
        # выравнивание машин: штраф за (макс. длительность - мин. длительность)
        time_dim.SetGlobalSpanCostCoefficient(int(span_cost))

    for i, s in enumerate(stops):
        idx = mgr.NodeToIndex(i + 1)
        time_dim.CumulVar(idx).SetRange(s.tw_from, max(s.tw_from, s.tw_to))
        # v39: перерва (обід) — приезд так, чтобы визит закончился до перерыва
        # или начался после нее: запрещаем прибытие в (bf - service, bt)
        if s.break_from is not None and s.break_to is not None and s.break_to > s.break_from:
            lo = s.break_from - s.service_min + 1
            hi = s.break_to - 1
            if hi >= lo:
                time_dim.CumulVar(idx).RemoveInterval(lo, hi)

    for v, tr in enumerate(trucks):
        time_dim.CumulVar(routing.Start(v)).SetRange(tr.shift_start, tr.shift_end)
        time_dim.CumulVar(routing.End(v)).SetRange(tr.shift_start, tr.shift_end)
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.Start(v)))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.End(v)))

    # Модель загрузки: доставки едут со склада (на старте весь их вес в кузове,
    # на точке -w), заборы добавляются (+w). Две размерности на каждую метрику:
    #  main: знаковый ход груза, cumul = физический груз в кузове, [0, cap] на каждом узле
    #  D:    только доставки (-w), End==0 => Start == сумме доставок рейса
    #  связка Start(main) == Start(D) прибивает стартовую загрузку к реальной.
    def _add_load(name, get, caps):
        def signed_cb(fi):
            f = mgr.IndexToNode(fi)
            if f == 0:
                return 0
            s_ = stops[f - 1]
            w = int(get(s_) * 1000)
            return w if s_.kind == "pickup" else -w

        def deliv_cb(fi):
            f = mgr.IndexToNode(fi)
            if f == 0:
                return 0
            s_ = stops[f - 1]
            return 0 if s_.kind == "pickup" else -int(get(s_) * 1000)

        routing.AddDimensionWithVehicleCapacity(
            routing.RegisterUnaryTransitCallback(signed_cb), 0, caps, False, name)
        routing.AddDimensionWithVehicleCapacity(
            routing.RegisterUnaryTransitCallback(deliv_cb), 0, caps, False, name + "D")
        dm = routing.GetDimensionOrDie(name)
        dd = routing.GetDimensionOrDie(name + "D")
        for v in range(k):
            routing.solver().Add(dd.CumulVar(routing.End(v)) == 0)
            routing.solver().Add(dm.CumulVar(routing.Start(v)) == dd.CumulVar(routing.Start(v)))

    _add_load("Weight", lambda s_: s_.weight, [int(t.max_weight * 1000) for t in trucks])
    _add_load("Volume", lambda s_: s_.volume, [int(t.max_volume * 1000) for t in trucks])

    # Жесткие ограничения по машинам на точке:
    #   1) геозоны в жестком режиме (zone_penalty_min is None)
    #   2) возможности авто (can_pickup / can_delivery) — всегда жестко
    # Домен VehicleVar = пересечение. (-1) = "точка не обслужена" (уйдет в буфер
    # через дисжанкцию, а не сделает задачу неразрешимой).
    hard: list[set | None] = [None] * len(stops)
    if allowed_vehicles and zone_penalty_min is None:
        for i, a in enumerate(allowed_vehicles):
            if a is not None:
                hard[i] = set(int(x) for x in a)
    if hard_allowed:
        for i, a in enumerate(hard_allowed):
            if a is None:
                continue
            s = set(int(x) for x in a)
            hard[i] = s if hard[i] is None else (hard[i] & s)
    for i, h in enumerate(hard):
        if h is not None and len(h) < k:
            routing.VehicleVar(mgr.NodeToIndex(i + 1)).SetValues([-1] + sorted(h))

    # разрешаем «выбросить» заявку со штрафом — план соберётся даже если всё не влазит
    penalty = 10_000_000
    for i in range(1, n):
        routing.AddDisjunction([mgr.NodeToIndex(i)], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_s)

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None

    result = []
    for v in range(k):
        seq, idx = [], routing.Start(v)
        while not routing.IsEnd(idx):
            node = mgr.IndexToNode(idx)
            if node > 0:
                seq.append(node - 1)
            idx = sol.Value(routing.NextVar(idx))
        result.append(seq)
    return result


def eta_schedule(seq: list[Stop], durations, shift_start: int,
                 factors: dict[str, float] | None = None) -> list[tuple[int, int]]:
    """(ETA, ETD) в минутах для последовательности стопов. Узел 0 — склад."""
    out, t, prev = [], shift_start, 0
    for s in seq:
        node = s.order_id  # здесь order_id = индекс узла в матрице
        eta, etd = _advance(t, s, durations[prev][node], factors)
        out.append((eta, etd))
        t = etd
        prev = node
    return out
