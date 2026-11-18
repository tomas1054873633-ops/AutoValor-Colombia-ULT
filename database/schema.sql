PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    country TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    category TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brand_id, name)
);

CREATE TABLE IF NOT EXISTS vehicle_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
    model_year INTEGER NOT NULL CHECK(model_year BETWEEN 1950 AND 2100),
    generation TEXT,
    version_name TEXT NOT NULL,
    engine_label TEXT NOT NULL,
    displacement_cc INTEGER CHECK(displacement_cc IS NULL OR displacement_cc > 0),
    fuel TEXT,
    transmission TEXT NOT NULL,
    body_type TEXT,
    traction TEXT,
    horsepower REAL CHECK(horsepower IS NULL OR horsepower > 0),
    doors INTEGER CHECK(doors IS NULL OR doors > 0),
    passengers INTEGER CHECK(passengers IS NULL OR passengers > 0),
    image_url TEXT,
    image_source_url TEXT,
    technical_source_url TEXT,
    technical_source_name TEXT,
    technical_source_checked_at TEXT,
    canonical_key TEXT,
    verification_status TEXT NOT NULL DEFAULT 'pendiente' CHECK(verification_status IN ('pendiente','parcial','verificado','rechazado')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_id, model_year, version_name, engine_label, transmission)
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    domain TEXT,
    source_type TEXT NOT NULL DEFAULT 'anuncios_publicos',
    base_url TEXT,
    terms_url TEXT,
    usage_notes TEXT,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS price_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES vehicle_versions(id) ON DELETE RESTRICT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    price_cop INTEGER NOT NULL CHECK(price_cop > 0),
    price_type TEXT NOT NULL DEFAULT 'precio_solicitado' CHECK(price_type IN ('precio_solicitado','transaccion_documentada','guia_comercial','valor_fiscal','precio_nuevo','otro')),
    model_year INTEGER NOT NULL,
    mileage_km INTEGER CHECK(mileage_km IS NULL OR mileage_km >= 0),
    location TEXT,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'observado_no_verificado' CHECK(verification_status IN ('observado_no_verificado','parcial','verificado','expirado','rechazado')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(version_id, source_url, price_cop, observed_at)
);

CREATE TABLE IF NOT EXISTS valuation_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_models_brand ON models(brand_id, name);
CREATE INDEX IF NOT EXISTS idx_versions_model_year ON vehicle_versions(model_id, model_year);
CREATE INDEX IF NOT EXISTS idx_versions_filters ON vehicle_versions(model_id, model_year, engine_label, transmission);
CREATE INDEX IF NOT EXISTS idx_versions_engine_config ON vehicle_versions(model_id, model_year, engine_label, fuel, displacement_cc, transmission);
CREATE INDEX IF NOT EXISTS idx_versions_verification ON vehicle_versions(verification_status, model_year);
CREATE INDEX IF NOT EXISTS idx_prices_version_date ON price_references(version_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_prices_type_status ON price_references(price_type, verification_status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_prices_source ON price_references(source_id);

INSERT OR IGNORE INTO valuation_config(key, value, description) VALUES
('annual_km_low', '12000', 'Kilometraje anual inferior de referencia, km/año.'),
('annual_km_central', '15000', 'Kilometraje anual central de referencia, km/año.'),
('annual_km_high', '18000', 'Kilometraje anual superior de referencia, km/año.'),
('minimum_prices', '3', 'Mínimo de referencias recientes para una estimación preliminar.'),
('range_minimum_prices', '5', 'Mínimo de referencias recientes para presentar un rango orientativo.'),
('freshness_days', '90', 'Antigüedad máxima en días de las referencias incluidas en el cálculo.'),
('provisional_cop_per_10000km', '-250000', 'Ajuste provisional por cada 10.000 km cuando no se puede calibrar con comparables suficientes. No es un porcentaje oficial.'),
('max_adjustment_pct', '20', 'Límite técnico provisional del ajuste por kilometraje, como porcentaje del precio base.'),
('max_abs_slope_cop_per_km', '250', 'Límite técnico de pendiente empírica por km para evitar extrapolaciones extremas.')
;
