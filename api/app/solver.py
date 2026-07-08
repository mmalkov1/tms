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


@dataclass
class Truck:
    vehicle_id: int
    max_weight: float
    max_volume: float
    shift_start: int    # минуты
    shift_end: int


def solve(
    stops: list[Stop],
    trucks: list[Truck],
    durations: list[list[int]],   # секунды, узел 0 = склад, далее stops по порядку
    time_limit_s: int = 15,
    allowed_vehicles: list[list[int]] | None = None,  # per stop: индексы машин (геозоны)
    zone_penalty_min: int | None = None,  # None = жесткие зоны; N = мягкие, штраф N мин за чужую
    span_cost: int = 0,                   # баланс: штраф за разброс длительности машин
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

    tcb = routing.RegisterTransitCallback(time_cb)

    # Мягкие зоны: штраф уходит в СТОИМОСТЬ per-vehicle, но НЕ в размерность времени,
    # чтобы ETA оставались физическим временем без виртуальных минут.
    if allowed_vehicles and zone_penalty_min is not None:
        pen = int(zone_penalty_min)

        def make_cost(v):
            def cb(fi, ti):
                base = time_cb(fi, ti)
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
        routing.SetArcCostEvaluatorOfAllVehicles(tcb)

    horizon = 24 * 60
    routing.AddDimension(tcb, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    if span_cost:
        # выравнивание машин: штраф за (макс. длительность - мин. длительность)
        time_dim.SetGlobalSpanCostCoefficient(int(span_cost))

    for i, s in enumerate(stops):
        idx = mgr.NodeToIndex(i + 1)
        time_dim.CumulVar(idx).SetRange(s.tw_from, max(s.tw_from, s.tw_to))

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

    # геозоны: точку могут обслуживать только машины разрешенных зон.
    # Домен VehicleVar: разрешенные машины + (-1) = "точка не обслужена" (уйдет в буфер
    # через дисжанкцию, а не сделает задачу неразрешимой).
    if allowed_vehicles and zone_penalty_min is None:
        for i, allowed in enumerate(allowed_vehicles):
            if allowed is not None and len(allowed) < k:
                routing.VehicleVar(mgr.NodeToIndex(i + 1)).SetValues([-1] + [int(a) for a in allowed])

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


def eta_schedule(seq: list[Stop], durations, shift_start: int) -> list[tuple[int, int]]:
    """(ETA, ETD) в минутах для последовательности стопов. Узел 0 — склад."""
    out, t, prev = [], shift_start, 0
    for s in seq:
        node = s.order_id  # здесь order_id = индекс узла в матрице
        t += durations[prev][node] // 60
        t = max(t, s.tw_from)
        out.append((t, t + s.service_min))
        t += s.service_min
        prev = node
    return out
