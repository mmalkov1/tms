-- v81: облік пального ведеться по автомобілю (заміна водія не рве ланцюжок)
CREATE INDEX IF NOT EXISTS idx_transport_sheets_vehicle
    ON transport_sheets(vehicle_id, work_date DESC);
