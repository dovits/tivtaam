from __future__ import annotations

from pydantic import BaseModel


class Product(BaseModel):
    sku: str
    name: str
    name_norm: str
    brand: str | None = None
    size_text: str | None = None
    last_price: float | None = None
    last_seen_at: str
    image_url: str | None = None
    raw_url: str | None = None
    is_available: int = 1


class Purchase(BaseModel):
    id: int | None = None
    order_id: str
    sku: str
    qty: float
    unit_price: float | None = None
    purchased_at: str


class GenericAlias(BaseModel):
    generic_name: str
    generic_name_norm: str
    preferred_sku: str
    confidence: float
    source: str  # 'user_confirmed' | 'history' | 'claude'
    last_confirmed_at: str
    times_used: int = 1
