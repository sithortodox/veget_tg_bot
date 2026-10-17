-- Инициализация схемы БД «ОВОЩНОЙ» (§13 ТЗ)
-- Накатывается docker-compose автоматически при первом старте postgres.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Пользователи
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    role VARCHAR(20) NOT NULL,              -- client | seller | courier | admin
    name VARCHAR(100),
    phone VARCHAR(20),
    address_text TEXT,
    address_geo GEOGRAPHY(Point, 4326),
    address_components JSONB,
    is_banned BOOLEAN DEFAULT false,
    banned_until TIMESTAMPTZ,
    ban_count INT DEFAULT 0,
    ban_reason TEXT,
    banned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Магазины
CREATE TABLE IF NOT EXISTS shops (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE,
    name VARCHAR(200) NOT NULL,
    address_text TEXT,
    address_geo GEOGRAPHY(Point, 4326),
    address_components JSONB,
    phone VARCHAR(20),
    payment_details TEXT,
    is_active BOOLEAN DEFAULT false,
    moderation_status VARCHAR(20) DEFAULT 'pending',
    avg_response_time INTERVAL,
    avg_assemble_time INTERVAL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Товары
CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT REFERENCES shops(id),
    name VARCHAR(200) NOT NULL,
    synonyms TEXT[],
    category VARCHAR(100),
    description TEXT,
    price NUMERIC(10,2) NOT NULL,
    unit VARCHAR(20) NOT NULL,              -- кг | шт | вилок | пучок | упаковка
    photo_file_id VARCHAR(300),
    is_active BOOLEAN DEFAULT true,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Заказы
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES users(id),
    status VARCHAR(30) DEFAULT 'draft',
    order_type VARCHAR(20) DEFAULT 'now',   -- now | preorder
    delivery_type VARCHAR(20),              -- pickup | courier
    delivery_address TEXT,
    delivery_geo GEOGRAPHY(Point, 4326),
    delivery_cost NUMERIC(10,2) DEFAULT 0,
    total_estimated NUMERIC(12,2),
    total_actual NUMERIC(12,2),
    payment_method VARCHAR(20),             -- cash | card | sbp
    payment_status VARCHAR(20),             -- pending | paid | refused
    paid_at TIMESTAMPTZ,
    assembled_at TIMESTAMPTZ,
    picked_up_at TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ,               -- для предзаказа
    reminder_sent_at TIMESTAMPTZ,
    seller_confirmed_at TIMESTAMPTZ,
    search_other_at TIMESTAMPTZ,
    courier_id BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Подзаказы
CREATE TABLE IF NOT EXISTS sub_orders (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT REFERENCES orders(id) ON DELETE CASCADE,
    shop_id BIGINT REFERENCES shops(id),
    status VARCHAR(30) DEFAULT 'pending',
    subtotal_estimated NUMERIC(12,2),
    subtotal_actual NUMERIC(12,2),
    assembled_at TIMESTAMPTZ,
    picked_up_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Позиции заказа
CREATE TABLE IF NOT EXISTS order_items (
    id BIGSERIAL PRIMARY KEY,
    sub_order_id BIGINT REFERENCES sub_orders(id) ON DELETE CASCADE,
    product_id BIGINT REFERENCES products(id),
    raw_name VARCHAR(200),
    qty NUMERIC(10,2),
    unit VARCHAR(20),
    price NUMERIC(10,2),
    actual_qty NUMERIC(10,2),
    actual_price NUMERIC(10,2),
    status VARCHAR(20),                     -- found | not_found | analog
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Логи статусов
CREATE TABLE IF NOT EXISTS order_status_log (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT REFERENCES orders(id),
    status VARCHAR(30),
    changed_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- История банов
CREATE TABLE IF NOT EXISTS ban_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    level INT NOT NULL,
    reason TEXT,
    banned_from TIMESTAMPTZ DEFAULT now(),
    banned_until TIMESTAMPTZ,
    order_id BIGINT REFERENCES orders(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Лог предзаказов
CREATE TABLE IF NOT EXISTS preorder_log (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT REFERENCES orders(id),
    event VARCHAR(50),                      -- reminder_sent | seller_confirmed | ...
    shop_id BIGINT REFERENCES shops(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Логи диалогов
CREATE TABLE IF NOT EXISTS message_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    direction VARCHAR(10),
    text TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Индексы (§13)
CREATE INDEX IF NOT EXISTS idx_shops_geo ON shops USING gist(address_geo);
CREATE INDEX IF NOT EXISTS idx_users_geo ON users USING gist(address_geo);
CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id, is_active);
CREATE INDEX IF NOT EXISTS idx_products_name ON products USING gin(name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_synonyms ON products USING gin(synonyms);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_scheduled ON orders(scheduled_at) WHERE order_type = 'preorder';
CREATE INDEX IF NOT EXISTS idx_sub_orders_shop ON sub_orders(shop_id, status);
