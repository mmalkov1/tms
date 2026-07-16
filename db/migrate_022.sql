-- v48: іменовані незалежні доступи до мобільного кабінету логіста
CREATE TABLE IF NOT EXISTS logist_tokens (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    token        TEXT NOT NULL UNIQUE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);
