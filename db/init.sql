-- TMS Культтовари Глобал — schema v1

CREATE TABLE depots (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,              -- 'Склад Киев'
    address     TEXT,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL
);

CREATE TABLE drivers (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT,
    shift_start TIME NOT NULL DEFAULT '08:00',
    shift_end   TIME NOT NULL DEFAULT '18:00',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE vehicles (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,            -- 'Sprinter AA1234BB'
    plate         TEXT,
    max_weight_kg NUMERIC(10,2) NOT NULL,
    max_volume_m3 NUMERIC(10,3) NOT NULL,
    is_hired      BOOLEAN NOT NULL DEFAULT FALSE,  -- наемная
    driver_id     INT REFERENCES drivers(id),      -- фиксированный водитель
    depot_id      INT NOT NULL REFERENCES depots(id) DEFAULT 1,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TYPE order_kind AS ENUM ('delivery', 'pickup');

CREATE TABLE orders (
    id            SERIAL PRIMARY KEY,
    plan_date     DATE NOT NULL,
    doc_number    TEXT,                     -- КГ000013347 (ключ синка с 1С)
    doc_ref       TEXT,                     -- полная «Ссылка»
    kind          order_kind NOT NULL,
    client        TEXT NOT NULL,
    address       TEXT,
    address_extra TEXT,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    tw_from       TIME,                     -- окно клиента «с»
    tw_to         TIME,                     -- «по»
    service_min   INT NOT NULL DEFAULT 15,  -- «разгрузка»
    weight_kg     NUMERIC(10,2) DEFAULT 0,
    volume_m3     NUMERIC(10,3) DEFAULT 0,
    status_1c     TEXT,
    depot_id      INT NOT NULL REFERENCES depots(id) DEFAULT 1,
    locked_route  INT,                      -- заблокирована за маршрутом (id)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_date, doc_number)
);
CREATE INDEX idx_orders_date ON orders(plan_date);

CREATE TABLE routes (
    id          SERIAL PRIMARY KEY,
    plan_date   DATE NOT NULL,
    vehicle_id  INT NOT NULL REFERENCES vehicles(id),
    driver_id   INT REFERENCES drivers(id),
    depot_id    INT NOT NULL REFERENCES depots(id) DEFAULT 1,
    color       TEXT,                       -- hex для карты
    total_km    NUMERIC(10,1),
    total_min   INT,
    load_weight NUMERIC(10,2),
    load_volume NUMERIC(10,3),
    status      TEXT NOT NULL DEFAULT 'draft',  -- draft|released|done
    geometry    TEXT,                       -- polyline OSRM
    depart_time TIME,                       -- выезд со склада
    return_time TIME,                       -- возврат на склад
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_routes_date ON routes(plan_date);

CREATE TABLE route_stops (
    id          SERIAL PRIMARY KEY,
    route_id    INT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    order_id    INT REFERENCES orders(id),
    seq         INT NOT NULL,               -- порядок на маршруте, 0 = выезд со склада
    eta         TIME,                       -- расчетное прибытие
    etd         TIME,                       -- расчетный отъезд
    UNIQUE (route_id, seq)
);

-- seed: склад + 3 своих машины (лимиты правь под реальные)
INSERT INTO depots (name, address, lat, lon) VALUES
 ('Склад Киев', 'Україна, м.Київ, вул.Молодогвардійська, буд.22',
  50.423507841149004, 30.450054761494783);

INSERT INTO drivers (name, shift_start, shift_end) VALUES
 ('Водій 1', '08:00', '18:00'),
 ('Водій 2', '08:00', '18:00'),
 ('Водій 3', '08:00', '18:00');

-- v48: іменовані незалежні доступи до мобільного кабінету логіста
CREATE TABLE IF NOT EXISTS logist_tokens (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    token        TEXT NOT NULL UNIQUE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

INSERT INTO vehicles (name, max_weight_kg, max_volume_m3, is_hired, driver_id) VALUES
 ('Авто 1', 1500, 12.0, FALSE, 1),
 ('Авто 2', 1500, 12.0, FALSE, 2),
 ('Авто 3', 1000,  9.0, FALSE, 3);
-- v3: геозоны
CREATE TABLE IF NOT EXISTS geozones (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    points  JSONB NOT NULL           -- [[lat,lon],...]
);
CREATE TABLE IF NOT EXISTS driver_zones (
    driver_id INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    zone_id   INT NOT NULL REFERENCES geozones(id) ON DELETE CASCADE,
    PRIMARY KEY (driver_id, zone_id)
);

INSERT INTO geozones (name, points) VALUES ('Правый берег', '[[50.37917346621653, 30.399500842917632], [50.37284615994315, 30.37818113833862], [50.37312713389094, 30.373237302437246], [50.371656193843314, 30.36932354587384], [50.36926113100865, 30.360638246262738], [50.35306841186386, 30.29885700454065], [50.33026923637665, 30.27340313168088], [50.31970238309197, 30.233372446351723], [50.341180371351136, 30.15852808600016], [50.35595309439299, 30.017432598087794], [50.33558628636768, 29.837360478777555], [50.22024136347872, 29.661189550217387], [50.323761489162244, 29.48658243233274], [50.47335706455043, 29.339619755665808], [50.71788455883305, 29.467028999082913], [50.8223504174931, 29.544872247891817], [51.07000290802198, 29.600041440254813], [51.05000759927172, 30.419527820002145], [50.67005888653924, 30.475175882865642], [50.61762036196105, 30.53037559967197], [50.54427151754749, 30.508095469918885], [50.53111464488839, 30.481054108533158], [50.50677673738671, 30.47918823046598], [50.50153429610117, 30.482302924384612], [50.49694678568685, 30.488506553336038], [50.489454210313944, 30.496658815375895], [50.48842700158207, 30.48056523757782], [50.48735988877753, 30.476451138536618], [50.48626533990805, 30.472680549434926], [50.480806828224786, 30.467196798403165], [50.45942755941167, 30.484656321109355], [50.45867331766442, 30.48597381820946], [50.45464045173893, 30.484888044307013], [50.44460704041808, 30.483403349983746], [50.4230057682833, 30.463962614530615], [50.41604752138152, 30.466392174645865], [50.406901485422495, 30.46710406185116], [50.396592209016724, 30.421509083435467], [50.393850340566956, 30.41579526559633], [50.391545613885675, 30.412313253454727], [50.38782074078792, 30.404994006980132], [50.384974976380406, 30.404994006980132], [50.384974976380406, 30.404994006980132], [50.384974976380406, 30.404994006980132], [50.384974976380406, 30.404994006980132], [50.382895271316826, 30.402934070456695], [50.37917346621653, 30.399500842917632]]') ON CONFLICT (name) DO UPDATE SET points=EXCLUDED.points;
INSERT INTO geozones (name, points) VALUES ('Левый берег', '[[50.29387457582088, 30.787181994864], [50.267547483650105, 30.556812426992906], [50.2677669362523, 30.520420215078843], [50.34215063776505, 30.492080387461556], [50.40276972203072, 30.522573657250405], [50.411728239565775, 30.530140673338565], [50.419710427506054, 30.546233044047312], [50.423456959948595, 30.56158579980651], [50.422103215755634, 30.56900553061203], [50.42424885983553, 30.575052082988144], [50.42714809897182, 30.5746867365741], [50.43501640421496, 30.570321702482843], [50.44297007437204, 30.56055239907117], [50.49389442360423, 30.536279611839404], [50.540745107244675, 30.53458808302537], [50.54481899513164, 30.509536179600673], [50.619303226029096, 30.530850710912546], [50.70765102365053, 30.43873600985671], [50.831642480683, 30.476961843505023], [50.750579828219166, 31.107647848490387], [50.30393385678976, 31.20551796682389], [50.29387457582088, 30.787181994864]]') ON CONFLICT (name) DO UPDATE SET points=EXCLUDED.points;
INSERT INTO geozones (name, points) VALUES ('Центр', '[[50.33532625534093, 29.840988944051084], [50.35645893023739, 30.02113472599041], [50.340320334044435, 30.16144282756168], [50.302622457206205, 30.27028312826878], [50.288800480412, 30.318682526591395], [50.27682911539116, 30.353367821579614], [50.264768984393925, 30.430786725918782], [50.26614796712238, 30.49356758911588], [50.26741268665766, 30.520490565338132], [50.34143233109889, 30.490944662841272], [50.39027075453364, 30.51707333715131], [50.4086330855394, 30.52479118664693], [50.419777725398056, 30.547651570240035], [50.42543842785364, 30.561799899328282], [50.423631758906936, 30.570187857326786], [50.4261426630825, 30.57460945198617], [50.43446323264986, 30.57004252901635], [50.44979702290877, 30.549254218728702], [50.47173476197032, 30.54130021999705], [50.492384080690606, 30.535759039119327], [50.505996841511205, 30.535047368165102], [50.51161630537598, 30.535077143935595], [50.51623159039885, 30.53603185211555], [50.52289922369463, 30.53606776739561], [50.530037264232746, 30.536835394300162], [50.535603688462935, 30.536686091477836], [50.540184021226715, 30.534994317448746], [50.54356526783462, 30.528826739045275], [50.542582536752256, 30.521972007721804], [50.54338996133881, 30.51673523102517], [50.54410938872982, 30.512314902056445], [50.54435262279644, 30.508200858874044], [50.54936808473855, 30.510610160798198], [50.55351579698589, 30.51130632432291], [50.56402089356585, 30.51434598661361], [50.55841024071777, 30.512944784824096], [50.56065119618324, 30.513603519294243], [50.56665950358657, 30.515264356024126], [50.57017145088209, 30.51652627819319], [50.57436324000274, 30.517685273138877], [50.577457466634314, 30.51805485446627], [50.57924474156076, 30.519453381670953], [50.58249105210876, 30.519177843983584], [50.585863932745866, 30.521017240894874], [50.58880016306325, 30.52165516905182], [50.59119411306478, 30.5232624566756], [50.59296249384861, 30.523368658627387], [50.594678860887285, 30.524120461734114], [50.59623292932013, 30.524827441419948], [50.59816639979921, 30.52480727217024], [50.600061232019904, 30.525643726718624], [50.6020061785712, 30.525686556676643], [50.602963773981614, 30.526181684036636], [50.60427841547385, 30.52570782309435], [50.60504496360169, 30.52693175250889], [50.60612095850779, 30.52686680218389], [50.60699255678416, 30.527381832816552], [50.60788639235838, 30.52786341129205], [50.608928540670036, 30.528241144855958], [50.60971890318894, 30.52902945853829], [50.61066386292436, 30.529784998244175], [50.6185167175572, 30.532086974323086], [50.669216368550074, 30.476264565578948], [51.04932113535017, 30.420003179637234], [51.06924596120626, 29.60155452980869], [50.81952924809294, 29.546872563622326], [50.71449090058639, 29.468299677092315], [50.61844424281845, 29.418994758824674], [50.471636047856514, 29.342281133238995], [50.32217294442734, 29.489462315915993], [50.21949227786454, 29.66319810752909], [50.33532625534093, 29.840988944051084]]') ON CONFLICT (name) DO UPDATE SET points=EXCLUDED.points;
-- v5: проекты планирования
CREATE TABLE IF NOT EXISTS projects (
    id         SERIAL PRIMARY KEY,
    plan_date  DATE NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_date ON projects(plan_date);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS project_id INT REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS project_id INT REFERENCES projects(id) ON DELETE CASCADE;

-- бэкфилл: по проекту на каждую дату с существующими заявками
INSERT INTO projects (plan_date, name)
SELECT DISTINCT o.plan_date, to_char(o.plan_date, 'DD-MM') || '_1'
FROM orders o
WHERE o.project_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.plan_date = o.plan_date);

UPDATE orders o SET project_id = p.id
FROM projects p WHERE o.project_id IS NULL AND p.plan_date = o.plan_date;

UPDATE routes r SET project_id = p.id
FROM projects p WHERE r.project_id IS NULL AND p.plan_date = r.plan_date;

-- уникальность заявки теперь в рамках проекта
ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_plan_date_doc_number_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_project_doc ON orders(project_id, doc_number);

CREATE TABLE IF NOT EXISTS client_service_stats (
    client_key  TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    visits      INT NOT NULL,
    median_min  NUMERIC(6,1) NOT NULL,
    p80_min     NUMERIC(6,1) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO client_service_stats VALUES ('НЕМЕЦКО-УКРАИНСКАЯ МЕЖКУЛЬТУРНАЯ ШКОЛА В Г.КИЕВ ЧОУЗ','Немецко-украинская межкультурная школа в г.Киев ЧОУЗ',467,11.5,22.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('АГРООБРАЗОВАНИЕ НМЦ ГУ','Агрообразование НМЦ ГУ',63,13.2,26.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ СІГМА. УКРАЇНА','ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ  СІГМА. УКРАЇНА',52,14.4,28.4,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ФРЕНДЛІ ВЕСТ ТОВ','ФРЕНДЛІ ВЕСТ ТОВ',34,21.3,63.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('МЕТРО КЕШ ЕНД КЕРІ УКРАЇНА ТОВ','Метро Кеш Енд Кері Україна ТОВ',26,18.0,28.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО','СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО',25,15.6,27.5,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('27 ООО','27 ООО',20,14.4,22.4,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЛД ІНВЕСТ ТОВ','ЛД ІНВЕСТ ТОВ',19,10.4,15.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЕПІЦЕНТР К ТОВ','ЕПІЦЕНТР К ТОВ',18,11.7,19.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЗІНЕКО ТОВ','Зінеко ТОВ',17,17.3,26.7,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('РОШЕН-ЗОДІАК ТОВ','РОШЕН-ЗОДІАК ТОВ',17,9.5,17.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('16','16',16,11.3,30.5,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЕХОКОР ТОВ','ЕХОКОР ТОВ',15,9.5,15.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('РОЗЕТКА УА ТОВ','Розетка УА ТОВ',15,10.5,28.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('АРТ ОФ КУКІНГ ТОВ','АРТ ОФ КУКІНГ ТОВ',15,15.7,56.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ШЕВЧЕНКО Т.Г. КНУ ИМ.','Шевченко Т.Г. КНУ им.',15,12.0,15.7,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ','ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ',14,8.8,11.5,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('УМС МАРИН ТОВ','УМС Марин ТОВ',13,11.2,20.3,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ГРИФФИН СЕРВИС ООО','Гриффин Сервис ООО',13,11.9,18.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('НОВАПЕЙ ТОВ','НоваПей ТОВ',12,11.7,16.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ','ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ',11,8.8,12.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ІНТЕРОКО ТОВ','ІНТЕРОКО ТОВ',11,12.5,16.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('САНІТА ПРОДАКТ ТОВ','САНІТА ПРОДАКТ ТОВ',9,14.6,32.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('МАКСБУД 2020 ТОВ','МАКСБУД 2020 ТОВ',9,16.2,22.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('РЫНОК КИЕВ','Рынок Киев',9,12.9,19.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('САМИЛОВА ЮЛІЯ ЮРІЇВНА ФОП','Самилова Юлія Юріївна ФОП',9,14.9,24.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ','ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ',8,23.3,27.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ФУДСЕРВИС МД ООО','Фудсервис МД ООО',8,12.0,32.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ТЕТА-ПРЕСТИЖ ЧП','ТЕТА-Престиж ЧП',7,9.2,9.8,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('УКРАИНСКИЕ ХИМИЧЕСКИЕ ТЕХНОЛОГИИ ООО','Украинские химические технологии ООО',7,12.9,13.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('АРИТЕЙЛ ООО','Аритейл ООО',7,14.2,47.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('КЛЕОН-ОЙЛ ТОВ','КЛЕОН-ОЙЛ ТОВ',7,17.6,26.3,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('АТМА ТОВ','АТМА ТОВ',7,10.8,27.7,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЗАРУДНИЦКАЯ Л.Ю. ФЛП','Зарудницкая Л.Ю. ФЛП',7,14.3,24.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ТЕМП-2000 ООО','ТЕМП-2000 ООО',7,15.8,18.3,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('КИЕВГОРВТОРРЕСУРСЫ ООО','Киевгорвторресурсы ООО',6,11.4,21.4,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО','АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО',6,7.6,8.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('УКРАЇНСЬКІ ХІМІЧНІ ТЕХНОЛОГІЇ ЛТД ТОВ','Українські хімічні технології ЛТД ТОВ',6,12.9,13.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ДУ ЦІТ МВС УКРАЇНИ','ДУ ЦІТ МВС УКРАЇНИ',5,17.0,33.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ДНІПРО-М ТОВ','ДНІПРО-М ТОВ',5,15.9,26.8,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ГАРАНТ СТАТУС ГРУП ТОВ','ГАРАНТ СТАТУС ГРУП ТОВ',5,11.2,17.6,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ПРЕМІУМ-МАРКЕТ','Преміум-маркет',5,9.8,14.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('НОВА ПОШТА ТОВ','Нова пошта ТОВ',5,13.2,14.9,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЭДВАНСИС ГРУПП ООО','Эдвансис Групп ООО',5,10.8,15.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ГАРАНТ СТАТУС ГРУП ЛТД ТОВ','ГАРАНТ СТАТУС ГРУП ЛТД ТОВ',5,10.9,75.0,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ЕКСПАНДІА ТОВ','Експандіа ТОВ',5,16.3,31.4,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('ООО ИНТАЙМ','ООО Интайм',5,11.3,19.2,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
INSERT INTO client_service_stats VALUES ('РIЗНИК Д.А.ФОП','РIЗНИК Д.А.ФОП',5,13.9,41.1,now()) ON CONFLICT (client_key) DO UPDATE SET visits=EXCLUDED.visits, median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now();
-- v6: нормативы простоя по клиент+адрес (xlsx, фильтр ночных баз). Заменяет v8-схему.
DROP TABLE IF EXISTS client_service_stats;
CREATE TABLE client_service_stats (
    client_key  TEXT NOT NULL,
    addr_key    TEXT NOT NULL,      -- '*' = сводный по клиенту
    client_name TEXT NOT NULL,
    address     TEXT,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    visits      INT NOT NULL,
    median_min  NUMERIC(6,1) NOT NULL,
    p80_min     NUMERIC(6,1) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_key, addr_key)
);
INSERT INTO client_service_stats VALUES ('АГРООБРАЗОВАНИЕ НМЦ ГУ','м київ вул смілянська буд 11','Агрообразование НМЦ ГУ','Україна, м.Київ, вул.Смілянська, буд.11',50.42357877777778,30.44976011111111,63,13.2,26.0,now());
INSERT INTO client_service_stats VALUES ('АГРООБРАЗОВАНИЕ НМЦ ГУ','*','Агрообразование НМЦ ГУ','',NULL,NULL,63,13.2,26.0,now());
INSERT INTO client_service_stats VALUES ('ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ СІГМА. УКРАЇНА','київ м київ просп повітрофлотський буд 66 нп№18','ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ  СІГМА. УКРАЇНА','Україна, Київ, м.Київ, просп.Повітрофлотський, буд.66 НП№18',50.41966671153846,30.452803942307693,52,14.4,28.4,now());
INSERT INTO client_service_stats VALUES ('ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ СІГМА. УКРАЇНА','*','ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ  СІГМА. УКРАЇНА','',NULL,NULL,52,14.4,28.4,now());
INSERT INTO client_service_stats VALUES ('ФРЕНДЛІ ВЕСТ ТОВ','*','ФРЕНДЛІ ВЕСТ ТОВ','',NULL,NULL,34,21.3,63.9,now());
INSERT INTO client_service_stats VALUES ('МЕТРО КЕШ ЕНД КЕРІ УКРАЇНА ТОВ','*','Метро Кеш Енд Кері Україна ТОВ','',NULL,NULL,26,18.0,28.1,now());
INSERT INTO client_service_stats VALUES ('СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО','київ м київ вул зрошувальна буд 4','СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО','Україна, Київ, м.Київ, вул.Зрошувальна, буд.4',50.43154952,30.67022728,25,15.6,27.5,now());
INSERT INTO client_service_stats VALUES ('СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО','*','СТАБІЛІЗЕЙШЕН СУППОРТ СЕРВІСЕЗ БФ БО','',NULL,NULL,25,15.6,27.5,now());
INSERT INTO client_service_stats VALUES ('ФРЕНДЛІ ВЕСТ ТОВ','київ м київ вул північна 48в','ФРЕНДЛІ ВЕСТ ТОВ','Україна, Київ, м.Київ, вул.Північна, 48В',50.52711120833334,30.513404416666663,24,38.3,75.1,now());
INSERT INTO client_service_stats VALUES ('АРИТЕЙЛ ООО','*','Аритейл ООО','',NULL,NULL,24,13.2,34.5,now());
INSERT INTO client_service_stats VALUES ('МЕТРО КЕШ ЕНД КЕРІ УКРАЇНА ТОВ','м київ просп григоренка петра буд 43','Метро Кеш Енд Кері Україна ТОВ','Україна, м.Київ, просп.Григоренка Петра, буд.43',50.39038334782608,30.639710130434786,23,15.6,28.1,now());
INSERT INTO client_service_stats VALUES ('27 ООО','м київ вул братиславська буд 11','27 ООО','Україна, м.Київ, вул.Братиславська, буд.11',50.4869035,30.6111395,20,14.4,22.4,now());
INSERT INTO client_service_stats VALUES ('27 ООО','*','27 ООО','',NULL,NULL,20,14.4,22.4,now());
INSERT INTO client_service_stats VALUES ('ЛД ІНВЕСТ ТОВ','київ м київ вул малевича казимира буд 86в','ЛД ІНВЕСТ ТОВ','Україна, Київ, м.Київ, вул.Малевича Казимира, буд.86В',50.41613557894737,30.517887052631576,19,10.4,15.9,now());
INSERT INTO client_service_stats VALUES ('ЛД ІНВЕСТ ТОВ','*','ЛД ІНВЕСТ ТОВ','',NULL,NULL,19,10.4,15.9,now());
INSERT INTO client_service_stats VALUES ('ЕПІЦЕНТР К ТОВ','*','ЕПІЦЕНТР К ТОВ','',NULL,NULL,18,11.7,19.0,now());
INSERT INTO client_service_stats VALUES ('ЗІНЕКО ТОВ','м київ вул кайсарова буд 1','Зінеко ТОВ','Україна, м.Київ, вул.Кайсарова, буд.1',50.39771817647059,30.479693294117652,17,17.3,26.7,now());
INSERT INTO client_service_stats VALUES ('РОШЕН-ЗОДІАК ТОВ','київ м київ вул пирогівський шлях буд 135','РОШЕН-ЗОДІАК ТОВ','Україна, Київ, м.Київ, вул.Пирогівський шлях, буд.135',50.352602235294114,30.543350411764703,17,9.5,17.0,now());
INSERT INTO client_service_stats VALUES ('ЗІНЕКО ТОВ','*','Зінеко ТОВ','',NULL,NULL,17,17.3,26.7,now());
INSERT INTO client_service_stats VALUES ('РОШЕН-ЗОДІАК ТОВ','*','РОШЕН-ЗОДІАК ТОВ','',NULL,NULL,17,9.5,17.0,now());
INSERT INTO client_service_stats VALUES ('АРИТЕЙЛ ООО','м київ вул данченка сергія буд 16','Аритейл ООО','Україна, м.Київ, вул.Данченка Сергія, буд.16',50.497319250000004,30.431080875,16,11.3,30.5,now());
INSERT INTO client_service_stats VALUES ('ЕХОКОР ТОВ','київ м київ вул бутлерова академіка буд 6','ЕХОКОР ТОВ','Україна, Київ, м.Київ, вул.Бутлерова академіка, буд.6',50.446940000000005,30.652321066666666,15,9.5,15.6,now());
INSERT INTO client_service_stats VALUES ('АРТ ОФ КУКІНГ ТОВ','київ м київ вул олександра олеся буд 7','АРТ ОФ КУКІНГ ТОВ','Україна, Київ, м.Київ, вул.Олександра Олеся, буд.7',50.496919733333336,30.423797200000003,15,15.7,56.0,now());
INSERT INTO client_service_stats VALUES ('ЕХОКОР ТОВ','*','ЕХОКОР ТОВ','',NULL,NULL,15,9.5,15.6,now());
INSERT INTO client_service_stats VALUES ('РОЗЕТКА УА ТОВ','*','Розетка УА ТОВ','',NULL,NULL,15,10.5,28.6,now());
INSERT INTO client_service_stats VALUES ('АРТ ОФ КУКІНГ ТОВ','*','АРТ ОФ КУКІНГ ТОВ','',NULL,NULL,15,15.7,56.0,now());
INSERT INTO client_service_stats VALUES ('ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ','київ м київ вул затишна буд 7б','ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ','Україна, Київ, м.Київ, вул.Затишна, буд.7Б',50.42532885714286,30.63118,14,8.8,11.5,now());
INSERT INTO client_service_stats VALUES ('ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ','*','ТУПКАЛО ОЛЕКСАНДР ЄВГЕНІЙОВИЧ ФІЗИЧНА ОСОБА ПІДПРИЄМЕЦЬ','',NULL,NULL,14,8.8,11.5,now());
INSERT INTO client_service_stats VALUES ('УМС МАРИН ТОВ','київ м київ вул електриків буд 26','УМС Марин ТОВ','Україна, Київ, м.Київ, вул.Електриків, буд.26',50.485948,30.52729576923077,13,11.2,20.3,now());
INSERT INTO client_service_stats VALUES ('УМС МАРИН ТОВ','*','УМС Марин ТОВ','',NULL,NULL,13,11.2,20.3,now());
INSERT INTO client_service_stats VALUES ('ГРИФФИН СЕРВИС ООО','*','Гриффин Сервис ООО','',NULL,NULL,13,11.9,18.0,now());
INSERT INTO client_service_stats VALUES ('НОВАПЕЙ ТОВ','*','НоваПей ТОВ','',NULL,NULL,12,11.7,16.6,now());
INSERT INTO client_service_stats VALUES ('ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ','київ м київ вул бориспільська буд 11','ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ','Україна, Київ, м.Київ, вул.Бориспільська, буд.11',50.43066590909091,30.675788818181818,11,8.8,12.9,now());
INSERT INTO client_service_stats VALUES ('ІНТЕРОКО ТОВ','київ м київ просп степана бандери буд 21б','ІНТЕРОКО ТОВ','Україна, Київ, м.Київ, просп.Степана Бандери, буд.21Б',50.49240381818181,30.493757454545456,11,12.5,16.6,now());
INSERT INTO client_service_stats VALUES ('ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ','*','ДАРНИЦЬКИЙ ЗАВОД ЗБК АТ','',NULL,NULL,11,8.8,12.9,now());
INSERT INTO client_service_stats VALUES ('ІНТЕРОКО ТОВ','*','ІНТЕРОКО ТОВ','',NULL,NULL,11,12.5,16.6,now());
INSERT INTO client_service_stats VALUES ('ЕПІЦЕНТР К ТОВ','м київ дорогакільцева буд 1б','ЕПІЦЕНТР К ТОВ','Україна, м.Київ, дорогаКільцева, буд.1Б',50.3765298,30.4465092,10,10.8,22.3,now());
INSERT INTO client_service_stats VALUES ('САМИЛОВА ЮЛІЯ ЮРІЇВНА ФОП','*','Самилова Юлія Юріївна ФОП','',NULL,NULL,10,16.1,24.0,now());
INSERT INTO client_service_stats VALUES ('САНІТА ПРОДАКТ ТОВ','київ м київ вул машиністівська буд 1','САНІТА ПРОДАКТ ТОВ','Україна, Київ, м.Київ, вул.Машиністівська, буд.1',50.44267855555555,30.67049588888889,9,14.6,32.9,now());
INSERT INTO client_service_stats VALUES ('МАКСБУД 2020 ТОВ','м київ вул героїв дніпра буд 69','МАКСБУД 2020 ТОВ','Україна, м.Київ, вул.Героїв Дніпра, буд.69',50.52398811111111,30.510371555555555,9,16.2,22.0,now());
INSERT INTO client_service_stats VALUES ('САНІТА ПРОДАКТ ТОВ','*','САНІТА ПРОДАКТ ТОВ','',NULL,NULL,9,14.6,32.9,now());
INSERT INTO client_service_stats VALUES ('МАКСБУД 2020 ТОВ','*','МАКСБУД 2020 ТОВ','',NULL,NULL,9,16.2,22.0,now());
INSERT INTO client_service_stats VALUES ('РЫНОК КИЕВ','*','Рынок Киев','',NULL,NULL,9,12.9,19.0,now());
INSERT INTO client_service_stats VALUES ('НОВАПЕЙ ТОВ','київ м київ ш столичне буд 103','НоваПей ТОВ','Україна, Київ, м.Київ, ш.Столичне, буд.103',50.340474625,30.55046025,8,12.4,17.9,now());
INSERT INTO client_service_stats VALUES ('РОЗЕТКА УА ТОВ','київ м київ просп повітрофлотський буд 56','Розетка УА ТОВ','Україна, Київ, м.Київ, просп.Повітрофлотський, буд.56',50.422730125,30.4565305,8,8.3,14.2,now());
INSERT INTO client_service_stats VALUES ('ФУДСЕРВИС МД ООО','м київ просп повітрофлотський буд 66','Фудсервис МД ООО','Україна, м.Київ, просп.Повітрофлотський, буд.66',50.419719,30.45317375,8,12.0,32.1,now());
INSERT INTO client_service_stats VALUES ('ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ','*','ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ','',NULL,NULL,8,23.3,27.0,now());
INSERT INTO client_service_stats VALUES ('ФУДСЕРВИС МД ООО','*','Фудсервис МД ООО','',NULL,NULL,8,12.0,32.1,now());
INSERT INTO client_service_stats VALUES ('ТЕТА-ПРЕСТИЖ ЧП','м київ просп степана бандери буд 8','ТЕТА-Престиж ЧП','Україна, м.Київ, просп.Степана Бандери, буд.8',50.48663714285714,30.491419,7,9.2,9.8,now());
INSERT INTO client_service_stats VALUES ('ЕПІЦЕНТР К ТОВ','м київ просп григоренка петра буд 40','ЕПІЦЕНТР К ТОВ','Україна, м.Київ, просп.Григоренка Петра, буд.40',50.38926585714285,30.636363285714285,7,12.3,18.3,now());
INSERT INTO client_service_stats VALUES ('УКРАИНСКИЕ ХИМИЧЕСКИЕ ТЕХНОЛОГИИ ООО','м київ вул довбуша олекси буд 37','Украинские химические технологии ООО','Україна, м.Київ, вул.Довбуша Олекси, буд.37',50.44743028571429,30.660783857142857,7,12.9,13.6,now());
INSERT INTO client_service_stats VALUES ('ЗАРУДНИЦКАЯ Л.Ю. ФЛП','м київ вул березняківська буд 29б','Зарудницкая Л.Ю. ФЛП','Україна, м.Київ, вул.Березняківська, буд.29Б',50.41982828571429,30.593613714285713,7,14.3,24.9,now());
INSERT INTO client_service_stats VALUES ('ТЕМП-2000 ООО','м київ вул козацька буд 122','ТЕМП-2000 ООО','Україна, м.Київ, вул.Козацька, буд.122',50.397872,30.492332285714287,7,15.8,18.3,now());
INSERT INTO client_service_stats VALUES ('РОЗЕТКА УА ТОВ','київ м київ дорогакільцева буд 1','Розетка УА ТОВ','Україна, Київ, м.Київ, дорогаКільцева, буд.1',50.37738342857143,30.446554142857142,7,22.0,32.1,now());
INSERT INTO client_service_stats VALUES ('ТЕТА-ПРЕСТИЖ ЧП','*','ТЕТА-Престиж ЧП','',NULL,NULL,7,9.2,9.8,now());
INSERT INTO client_service_stats VALUES ('УКРАИНСКИЕ ХИМИЧЕСКИЕ ТЕХНОЛОГИИ ООО','*','Украинские химические технологии ООО','',NULL,NULL,7,12.9,13.6,now());
INSERT INTO client_service_stats VALUES ('КЛЕОН-ОЙЛ ТОВ','*','КЛЕОН-ОЙЛ ТОВ','',NULL,NULL,7,17.6,26.3,now());
INSERT INTO client_service_stats VALUES ('АТМА ТОВ','*','АТМА ТОВ','',NULL,NULL,7,10.8,27.7,now());
INSERT INTO client_service_stats VALUES ('ЗАРУДНИЦКАЯ Л.Ю. ФЛП','*','Зарудницкая Л.Ю. ФЛП','',NULL,NULL,7,14.3,24.9,now());
INSERT INTO client_service_stats VALUES ('ТЕМП-2000 ООО','*','ТЕМП-2000 ООО','',NULL,NULL,7,15.8,18.3,now());
INSERT INTO client_service_stats VALUES ('КИЕВГОРВТОРРЕСУРСЫ ООО','м київ вул маланюка євгена буд 112','Киевгорвторресурсы ООО','Україна, м.Київ, вул.Маланюка Євгена, буд.112',50.4677625,30.58748833333333,6,11.4,21.4,now());
INSERT INTO client_service_stats VALUES ('АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО','м київ вул прирічна буд 27е','АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО','Україна, м.Київ, вул.Прирічна, буд.27Е',50.522543000000006,30.521435,6,7.6,8.1,now());
INSERT INTO client_service_stats VALUES ('ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ','київ м київ просп броварський буд 3в','ЛЕРУА МЕРЛЕН УКРАЇНА ТОВ','Україна, Київ, м.Київ, просп.Броварський, буд.3В',50.46787716666666,30.65518683333333,6,23.3,26.5,now());
INSERT INTO client_service_stats VALUES ('УКРАЇНСЬКІ ХІМІЧНІ ТЕХНОЛОГІЇ ЛТД ТОВ','м київ вул довбуша олекси буд 37','Українські хімічні технології ЛТД ТОВ','Україна, м.Київ, вул.Довбуша Олекси, буд.37',50.447607166666664,30.660927,6,12.9,13.9,now());
INSERT INTO client_service_stats VALUES ('АТМА ТОВ','київ м київ вул якутська буд 10','АТМА ТОВ','Україна, Київ, м.Київ, вул.Якутська, буд.10',50.41186866666667,30.415009333333334,6,9.8,27.7,now());
INSERT INTO client_service_stats VALUES ('ГРИФФИН СЕРВИС ООО','київ м київ вул полярна буд 20д','Гриффин Сервис ООО','Україна, Київ, м.Київ, вул.Полярна, буд.20Д',50.520501333333335,30.48192583333333,6,15.1,18.0,now());
INSERT INTO client_service_stats VALUES ('КИЕВГОРВТОРРЕСУРСЫ ООО','*','Киевгорвторресурсы ООО','',NULL,NULL,6,11.4,21.4,now());
INSERT INTO client_service_stats VALUES ('АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО','*','АПТЕКА ГОРМОНАЛЬНЫХ ПРЕПАРАТОВ ООО','',NULL,NULL,6,7.6,8.1,now());
INSERT INTO client_service_stats VALUES ('УКРАЇНСЬКІ ХІМІЧНІ ТЕХНОЛОГІЇ ЛТД ТОВ','*','Українські хімічні технології ЛТД ТОВ','',NULL,NULL,6,12.9,13.9,now());
INSERT INTO client_service_stats VALUES ('ВЮРТ-УКРАЇНА ТОВ','київ м київ вул зрошувальна буд 11','ВЮРТ-УКРАЇНА ТОВ','Україна, Київ, м.Київ, вул.Зрошувальна, буд.11',50.435269,30.6841568,5,13.9,22.6,now());
INSERT INTO client_service_stats VALUES ('ДУ ЦІТ МВС УКРАЇНИ','київ м київ вул сікевича володимира буд 28','ДУ ЦІТ МВС УКРАЇНИ','Україна, Київ, м.Київ, вул.Сікевича Володимира, буд.28',50.4231294,30.449982799999997,5,17.0,33.1,now());
INSERT INTO client_service_stats VALUES ('ДНІПРО-М ТОВ','київ м київ просп степана бандери буд 13','ДНІПРО-М ТОВ','Україна, Київ, м.Київ, просп.Степана Бандери, буд.13',50.4906148,30.487907999999997,5,15.9,26.8,now());
INSERT INTO client_service_stats VALUES ('ПРЕМІУМ-МАРКЕТ, ТОВ','м київ вул хвойки вікентія буд 10','Преміум-маркет, ТОВ','Україна, м.Київ, вул.Хвойки Вікентія, буд.10',50.4823202,30.48177,5,9.8,14.9,now());
INSERT INTO client_service_stats VALUES ('НОВА ПОШТА ТОВ','м київ вул калачівська буд 13','Нова пошта ТОВ','Україна, м.Київ, вул.Калачівська, буд.13',50.442789,30.6523054,5,13.2,14.9,now());
INSERT INTO client_service_stats VALUES ('ЭДВАНСИС ГРУПП ООО','м київ вул старосільська буд 1оф21','Эдвансис Групп ООО','Україна, м.Київ, вул.Старосільська, буд.1ОФ21',50.472794,30.594049000000002,5,10.8,15.0,now());
INSERT INTO client_service_stats VALUES ('ООО ИНТАЙМ','м київ вул смілянська буд 10а','ООО Интайм','Україна, м.Київ, вул.Смілянська, буд.10А',50.425072,30.446166599999998,5,11.3,19.2,now());
INSERT INTO client_service_stats VALUES ('РIЗНИК Д.А.ФОП','київ м київ вул гречка маршала буд 20д','РIЗНИК Д.А.ФОП','Україна, Київ, м.Київ, вул.Гречка маршала, буд.20Д',50.4886216,30.4095174,5,13.9,41.1,now());
INSERT INTO client_service_stats VALUES ('ВЮРТ-УКРАЇНА ТОВ','*','ВЮРТ-УКРАЇНА ТОВ','',NULL,NULL,5,13.9,22.6,now());
INSERT INTO client_service_stats VALUES ('ДУ ЦІТ МВС УКРАЇНИ','*','ДУ ЦІТ МВС УКРАЇНИ','',NULL,NULL,5,17.0,33.1,now());
INSERT INTO client_service_stats VALUES ('ДНІПРО-М ТОВ','*','ДНІПРО-М ТОВ','',NULL,NULL,5,15.9,26.8,now());
INSERT INTO client_service_stats VALUES ('ГАРАНТ СТАТУС ГРУП ТОВ','*','ГАРАНТ СТАТУС ГРУП ТОВ','',NULL,NULL,5,11.2,17.6,now());
INSERT INTO client_service_stats VALUES ('ПРЕМІУМ-МАРКЕТ, ТОВ','*','Преміум-маркет, ТОВ','',NULL,NULL,5,9.8,14.9,now());
INSERT INTO client_service_stats VALUES ('НОВА ПОШТА ТОВ','*','Нова пошта ТОВ','',NULL,NULL,5,13.2,14.9,now());
INSERT INTO client_service_stats VALUES ('ЭДВАНСИС ГРУПП ООО','*','Эдвансис Групп ООО','',NULL,NULL,5,10.8,15.0,now());
INSERT INTO client_service_stats VALUES ('ГАРАНТ СТАТУС ГРУП ЛТД ТОВ','*','ГАРАНТ СТАТУС ГРУП ЛТД ТОВ','',NULL,NULL,5,10.9,75.0,now());
INSERT INTO client_service_stats VALUES ('ЕКСПАНДІА ТОВ','*','Експандіа ТОВ','',NULL,NULL,5,16.3,31.4,now());
INSERT INTO client_service_stats VALUES ('ООО ИНТАЙМ','*','ООО Интайм','',NULL,NULL,5,11.3,19.2,now());
INSERT INTO client_service_stats VALUES ('РIЗНИК Д.А.ФОП','*','РIЗНИК Д.А.ФОП','',NULL,NULL,5,13.9,41.1,now());
INSERT INTO client_service_stats VALUES ('РИАЛТИ ЛТД ООО','м київ вул хвойки вікентія буд 21','Риалти ЛТД ООО','Україна, м.Київ, вул.Хвойки Вікентія, буд.21',50.484767500000004,30.488961,4,9.2,10.8,now());
INSERT INTO client_service_stats VALUES ('ШЕВЧЕНКО В.М. ФЛП','київ м київ вул бережанська буд 9','ШЕВЧЕНКО В.М. ФЛП','Україна, Київ, м.Київ, вул.Бережанська, буд.9',50.509814,30.46111275,4,12.2,25.7,now());
INSERT INTO client_service_stats VALUES ('ГРИФФИН СЕРВИС ООО','київ м київ вул вінстона черчелля буд 35','Гриффин Сервис ООО','Україна, Київ, м.Київ, вул.Вінстона  Черчелля, буд.35',50.45418725,30.6282865,4,11.2,15.2,now());
INSERT INTO client_service_stats VALUES ('ЛЕДМАРК КИЇВ ТОВ','київ м київ вул алматинська буд 6','ЛЕДМАРК КИЇВ ТОВ','Україна, Київ, м.Київ, вул.Алматинська, буд.6',50.434597499999995,30.6473655,4,21.9,31.3,now());
INSERT INTO client_service_stats VALUES ('ДЭВХ ПАО АК КИЕВВОДОКАНАЛ','м київ вул електротехнічна буд 16','ДЭВХ ПАО АК Киевводоканал','Україна, м.Київ, вул.Електротехнічна, буд.16',50.500048750000005,30.6154485,4,15.7,28.1,now());
INSERT INTO client_service_stats VALUES ('ЭКВИЛИБРИУМ ТРЕЙД ООО','м київ вул болсуновська буд 13-15','Эквилибриум трейд ООО','Україна, м.Київ, вул.Болсуновська, буд.13-15',50.4212355,30.553731,4,14.7,19.0,now());
INSERT INTO client_service_stats VALUES ('ИРБИС ТД ООО','м київ вул пирогівський шлях буд 34а','Ирбис ТД ООО','Україна, м.Київ, вул.Пирогівський шлях, буд.34А',50.37770475,30.5478625,4,10.2,10.5,now());
INSERT INTO client_service_stats VALUES ('ЛЕРУА МЕРЛЕН УКРАИНА ООО','київ м київ вул полярна буд 17а','Леруа Мерлен Украина ООО','Україна, Київ, м.Київ, вул.Полярна, буд.17А',50.51909675,30.46749725,4,9.3,11.5,now());
INSERT INTO client_service_stats VALUES ('КИЇВ ІНВЕСТ ГРУП ТОВ','київ м київ вул велика васильківська буд 72','КИЇВ ІНВЕСТ ГРУП ТОВ','Україна, Київ, м.Київ, вул.Велика Васильківська, буд.72',50.432083750000004,30.5153675,4,16.1,20.0,now());
INSERT INTO client_service_stats VALUES ('ЕКСПАНДІА ТОВ','київ м київ вул князів острозьких буд 32/2','Експандіа ТОВ','Україна, Київ, м.Київ, вул.Князів Острозьких, буд.32/2',50.43536,30.543632,4,13.8,31.4,now());
INSERT INTO client_service_stats VALUES ('АДІДАС УКРАЇНА ДП','київ м київ вул короленківська буд 4','Адідас Україна ДП','Україна, Київ, м.Київ, вул.Короленківська, буд.4',50.4323745,30.50608325,4,9.1,52.6,now());
INSERT INTO client_service_stats VALUES ('СТАР УКРАИНА ООО','м київ вул охтирська буд 7','Стар Украина ООО','Україна, м.Київ, вул.Охтирська, буд.7',50.397940750000004,30.4797465,4,12.9,22.0,now());
INSERT INTO client_service_stats VALUES ('ЮНІЛАБ ТОВ','м київ просп правди буд 88б','Юнілаб ТОВ','Україна, м.Київ, просп.Правди, буд.88Б',50.508639,30.41033275,4,7.7,23.4,now());
INSERT INTO client_service_stats VALUES ('САМИЛОВА ЮЛІЯ ЮРІЇВНА ФОП','київ м київ вул хрещатик буд 15/4','Самилова Юлія Юріївна ФОП','Україна, Київ, м.Київ, вул.Хрещатик, буд.15/4',50.4475975,30.52511675,4,12.9,17.6,now());
INSERT INTO client_service_stats VALUES ('ВІДІ АВТОСІТІ КІЛЬЦЕВА ТОВ','обл київська бучанський с софіївська борщагівка вул велика кільцева буд 60','ВІДІ АВТОСІТІ КІЛЬЦЕВА ТОВ','Україна, обл.Київська, Бучанський, с.Софіївська Борщагівка, вул.Велика Кільцева, буд.60',50.412494,30.38171525,4,11.6,14.3,now());
