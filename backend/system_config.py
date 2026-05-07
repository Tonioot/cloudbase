from __future__ import annotations

from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import SystemConfig
import config as _cfg

_BASE_DOMAIN_KEY = "base_domain"
_BASE_SSL_CERT_KEY = "base_ssl_cert"
_BASE_SSL_KEY_KEY = "base_ssl_key"

_cache: Dict[str, str] = {}


def _normalize(value: str | None) -> str:
    return str(value or "").strip()


def get_base_domain_cached() -> str:
    value = _normalize(_cache.get(_BASE_DOMAIN_KEY))
    if value:
        return value
    return _cfg.get_base_domain()


def get_base_ssl_cert_cached() -> str:
    value = _normalize(_cache.get(_BASE_SSL_CERT_KEY))
    if value:
        return value
    return _cfg.get_base_ssl_cert()


def get_base_ssl_key_cached() -> str:
    value = _normalize(_cache.get(_BASE_SSL_KEY_KEY))
    if value:
        return value
    return _cfg.get_base_ssl_key()


async def load_cache(db: AsyncSession) -> None:
    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_([
                _BASE_DOMAIN_KEY,
                _BASE_SSL_CERT_KEY,
                _BASE_SSL_KEY_KEY,
            ])
        )
    )
    rows = result.scalars().all()
    for row in rows:
        _cache[row.key] = _normalize(row.value)


async def bootstrap_from_config_if_needed(db: AsyncSession) -> None:
    await load_cache(db)
    values = {
        _BASE_DOMAIN_KEY: _cfg.get_base_domain(),
        _BASE_SSL_CERT_KEY: _cfg.get_base_ssl_cert(),
        _BASE_SSL_KEY_KEY: _cfg.get_base_ssl_key(),
    }
    changed = False
    for key, value in values.items():
        if key not in _cache:
            db.add(SystemConfig(key=key, value=_normalize(value)))
            _cache[key] = _normalize(value)
            changed = True
    if changed:
        await db.commit()


async def set_base_settings(
    db: AsyncSession,
    *,
    base_domain: str,
    base_ssl_cert: str,
    base_ssl_key: str,
) -> None:
    values = {
        _BASE_DOMAIN_KEY: _normalize(base_domain),
        _BASE_SSL_CERT_KEY: _normalize(base_ssl_cert),
        _BASE_SSL_KEY_KEY: _normalize(base_ssl_key),
    }

    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_(list(values.keys()))
        )
    )
    existing = {row.key: row for row in result.scalars().all()}

    for key, value in values.items():
        if key in existing:
            existing[key].value = value
        else:
            db.add(SystemConfig(key=key, value=value))
        _cache[key] = value

    await db.commit()


async def get_base_settings(db: AsyncSession) -> dict:
    await load_cache(db)
    return {
        "base_domain": get_base_domain_cached(),
        "base_ssl_cert_path": get_base_ssl_cert_cached(),
        "base_ssl_key_path": get_base_ssl_key_cached(),
    }
