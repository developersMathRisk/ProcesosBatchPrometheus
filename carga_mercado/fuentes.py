"""Descarga de series de mercado (BCRP y Yahoo Finance). Solo lectura: no toca la BD."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, time as hora, timedelta, timezone

import requests

UA = {"User-Agent": "Mozilla/5.0 (ProcesosBatchPrometheus)"}

BCRP_URL = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
# TC Sistema bancario SBS (S/ por US$). Verificados contra el título oficial que devuelve la propia API.
BCRP_TC_USD_COMPRA = "PD04639PD"
BCRP_TC_USD_VENTA = "PD04640PD"

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

_MESES = {"Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Ago": 8, "Set": 9, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12}


class FuenteError(RuntimeError):
    pass


def fecha_bcrp(texto: str) -> date:
    """'29.Set.26' -> date(2026, 9, 29)."""
    m = re.fullmatch(r"(\d{1,2})\.([A-Za-z]{3})\.(\d{2})", texto.strip())
    if not m or m.group(2).capitalize() not in _MESES:
        raise FuenteError(f"Fecha BCRP no reconocida: {texto!r}")
    return date(2000 + int(m.group(3)), _MESES[m.group(2).capitalize()], int(m.group(1)))


def _numero(txt) -> float | None:
    try:
        return float(txt)
    except (TypeError, ValueError):
        return None  # 'n.d.' u otro marcador de dato no disponible


def _get(url: str, params: dict | None = None, intentos: int = 4) -> requests.Response:
    ultimo = None
    for i in range(intentos):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise FuenteError(f"HTTP {r.status_code}")
            return r
        except (requests.RequestException, FuenteError) as exc:
            ultimo = exc
            time.sleep(1.5 * (i + 1))
    raise FuenteError(f"No se pudo leer {url}: {ultimo}")


def tc_promedio_usd_pen(desde: date, hasta: date) -> list[tuple[date, float]]:
    """TC USD/PEN promedio simple compra/venta (convención de carga_bd_riesgos.py), por día."""
    url = f"{BCRP_URL}/{BCRP_TC_USD_COMPRA}-{BCRP_TC_USD_VENTA}/json/{desde:%Y-%m-%d}/{hasta:%Y-%m-%d}"
    r = _get(url)
    try:
        datos = r.json()
    except ValueError as exc:
        raise FuenteError(f"BCRP no devolvió JSON (¿código de serie inválido?): {r.text[:120]}") from exc
    salida = []
    for p in datos.get("periods", []):
        compra, venta = (_numero(v) for v in p["values"][:2])
        if compra is None or venta is None:
            continue  # el BCRP publica el día con rezago; se completa en la corrida siguiente
        salida.append((fecha_bcrp(p["name"]), round((compra + venta) / 2, 6)))
    return salida


def yahoo_cierres(simbolo: str, desde: date, hasta: date) -> tuple[dict, list[tuple[date, float]]]:
    """Cierres diarios (split-adjusted, sin ajustar por dividendos) de sesiones ya cerradas.

    Devuelve (meta de Yahoo, [(fecha, cierre)]). Se descarta la barra del día en curso solo mientras el
    mercado sigue abierto (valor parcial); una vez terminada la sesión regular, esa barra ya es el cierre.
    """
    params = {
        "period1": int(datetime.combine(desde, hora.min, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime.combine(hasta + timedelta(days=1), hora.min, tzinfo=timezone.utc).timestamp()),
        "interval": "1d",
        "events": "div,splits",
    }
    r = _get(f"{YAHOO_URL}/{simbolo}", params)
    chart = r.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise FuenteError(f"Yahoo no devolvió datos para {simbolo}: {chart.get('error')}")
    res = chart["result"][0]
    meta = res["meta"]
    offset = timedelta(seconds=meta.get("gmtoffset", 0))
    hoy_mercado = (datetime.now(timezone.utc) + offset).date()
    fin_sesion = (meta.get("currentTradingPeriod") or {}).get("regular", {}).get("end")
    sesion_cerrada = fin_sesion is not None and datetime.now(timezone.utc).timestamp() >= fin_sesion
    cierres = res["indicators"]["quote"][0]["close"]
    salida = []
    for ts, c in zip(res.get("timestamp", []), cierres):
        if c is None:
            continue
        f = (datetime.fromtimestamp(ts, timezone.utc) + offset).date()
        if f > hoy_mercado or (f == hoy_mercado and not sesion_cerrada):
            continue
        salida.append((f, round(float(c), 4)))
    return meta, salida
