"""Curvas soberanas/de referencia (SBS) -> backend.

La SBS publica las curvas en su portal (Curva Soberana > Consulta histórica) detrás de un WAF que
exige un navegador real: una descarga automática no es confiable y no se intenta saltar el control.
El proceso batch por eso lee los archivos que se dejan en una carpeta de entrada (el export Excel/CSV
del portal, o el caché parquet del motor anterior) y los carga de forma idempotente. Los archivos
procesados pasan a `procesados/`; los que fallan pasan a `con_error/`.

Formatos aceptados (xlsx, csv, parquet), columnas por nombre o por posición:
    Fecha de Proceso | [Tipo de Curva] | Plazo (DIAS) | Tasas (%)
El código de la curva sale de la columna "Tipo de Curva" o del nombre del archivo (CCPSS_2024.csv).
"""
from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from .backend import Backend, BackendError

log = logging.getLogger("carga_mercado.curvas")

CURVAS = {
    "CCPSS": "Curva Soberana Soles",
    "CCPVS": "Curva Soberana Soles VAC",
    "CCPEDS": "Curva Cupón Cero Dólares Globales",
    "CCINFS": "Curva Cupón Cero de Inflación en Soles",
    "CCCLD": "Curva Cupón Cero Libor",
    "CBCRS": "Curva Banco Central de Reserva CDBCRP",
    "CSBCRD": "Curva Cupón Cero Dólares Sintética",
    "CCSDF": "Curva Dólares Corto Plazo",
    "CBCRPS": "Curva Banco Central de Reserva CDBCRP NR",
    # No son curvas SBS: rendimiento diario del bono soberano a 10 años publicado por el BCRP (comando bcrp)
    "BCRP10S": "BCRP - Rendimiento bono soberano 10 años (S/)",
    "BCRP10D": "BCRP - Rendimiento bono soberano 10 años (US$)",
}
HILOS = 6


class ErrorCurva(ValueError):
    pass


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def leer_archivo(ruta: Path) -> pd.DataFrame:
    """Normaliza a: fecha (date) | curva (str) | plazo (int) | tasa (float, en %)."""
    ext = ruta.suffix.lower()
    if ext == ".parquet":
        raw = pd.read_parquet(ruta)
    elif ext in (".xlsx", ".xls", ".xlsm"):
        raw = pd.read_excel(ruta)
        if not any("fecha" in _norm(c) for c in raw.columns):          # título antes del encabezado
            crudo = pd.read_excel(ruta, header=None, nrows=15)
            for i in range(len(crudo)):
                if any("fecha" in _norm(x) for x in crudo.iloc[i]):
                    raw = pd.read_excel(ruta, header=i)
                    break
    else:
        raw = None
        for sep in (";", ",", "\t"):
            try:
                tmp = pd.read_csv(ruta, sep=sep, encoding="utf-8-sig")
                if tmp.shape[1] >= 3:
                    raw = tmp
                    break
            except Exception:  # noqa: BLE001
                continue
        if raw is None:
            raise ErrorCurva("no se pudo leer como CSV")

    cols = {_norm(c): c for c in raw.columns}

    def col(*claves, pos=None):
        for k in claves:
            for n, c in cols.items():
                if n == k or n.startswith(k):
                    return c
        return raw.columns[pos] if pos is not None and pos < len(raw.columns) else None

    # El parquet del motor anterior trae: Sec. | Fecha de Proceso | Periodo (días) | Tasas (%)
    c_fecha = col("fecha de proceso", "fecha", pos=1 if ext == ".parquet" else 0)
    c_plazo = col("plazo", "periodo", pos=2)
    c_tasa = col("tasas", "tasa", pos=3)
    c_tipo = col("tipo de curva", "tipo curva")
    if None in (c_fecha, c_plazo, c_tasa):
        raise ErrorCurva(f"faltan columnas (encontradas: {list(raw.columns)})")

    codigo = None
    if c_tipo is not None:
        vals = {str(v).strip().upper() for v in raw[c_tipo].dropna().unique()} & set(CURVAS)
        if len(vals) == 1:
            codigo = vals.pop()
    if codigo is None:
        m = re.match(r"([A-Za-z]+)", ruta.stem)
        codigo = m.group(1).upper() if m else None
    if codigo not in CURVAS:
        raise ErrorCurva(f"curva '{codigo}' no reconocida (válidas: {', '.join(CURVAS)})")

    tasa = pd.to_numeric(raw[c_tasa].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    fecha = pd.to_datetime(raw[c_fecha], errors="coerce", dayfirst=True)
    plazo = pd.to_numeric(raw[c_plazo], errors="coerce")
    df = pd.DataFrame({"fecha": fecha.dt.date, "plazo": plazo, "tasa": tasa}).dropna()
    if df.empty:
        raise ErrorCurva("el archivo no tiene filas válidas")
    if (df["tasa"].abs() > 100).any():
        raise ErrorCurva("hay tasas fuera de rango (> 100 %): ¿unidades?")
    df["plazo"] = df["plazo"].astype(int)
    df["curva"] = codigo
    return df.drop_duplicates(["fecha", "curva", "plazo"]).sort_values(["fecha", "plazo"]).reset_index(drop=True)


def _con_reintentos(fn, arg, intentos: int = 3):
    for i in range(intentos):
        try:
            return fn(arg)
        except BackendError:
            if i == intentos - 1:
                raise


def cargar_df(api: Backend, df: pd.DataFrame, dry: bool) -> int:
    """Carga un DataFrame normalizado; devuelve cuántas tasas nuevas se crearon."""
    curva = df["curva"].iloc[0]
    puntos: dict[tuple[str, int], int] = {}
    existentes: set[tuple[int, str]] = set()
    if not dry:
        for p in api.listar("curvaReferenciaPuntos"):
            puntos[(p["codCurvaProveedor"], p["numPlazo"])] = p["idCurvaReferenciaPuntos"]
        for plazo in sorted(df["plazo"].unique()):
            if (curva, int(plazo)) not in puntos:
                cod = f"{curva}_{int(plazo):05d}"
                nuevo = api.crear("crearCurvaReferenciaPuntos", {
                    "codVertice": cod, "codCurvaProveedor": curva, "desVertice": f"{int(plazo)} dias",
                    "numPlazo": int(plazo), "codVerticeRef": cod})
                puntos[(curva, int(plazo))] = nuevo["idCurvaReferenciaPuntos"]
        existentes = {(f["idCurvaReferenciaPuntos"], f["fecProceso"]) for f in api.listar(
            "curvaReferenciaValores", desde=str(df["fecha"].min()), hasta=str(df["fecha"].max()))}

    nuevos = []
    for r in df.itertuples():
        pid = puntos.get((curva, int(r.plazo)))
        if dry or (pid, r.fecha.isoformat()) not in existentes:
            nuevos.append({"fecProceso": r.fecha.isoformat(), "numTasa": float(r.tasa),
                           "idCurvaReferenciaPuntos": pid, "desVertice": f"{int(r.plazo)} dias"})
    if not dry and nuevos:
        with ThreadPoolExecutor(HILOS) as pool:
            list(pool.map(lambda b: _con_reintentos(lambda x: api.crear("crearCurvaReferenciaValores", x), b), nuevos))
    log.info("%-7s %6d tasas (%s -> %s, %d fechas), %d nuevas", curva, len(df), df["fecha"].min(), df["fecha"].max(),
             df["fecha"].nunique(), len(nuevos))
    return len(nuevos)


def cmd_curvas(api: Backend, carpeta: Path, desde: date | None, dry: bool) -> int:
    """Procesa todos los archivos de `carpeta`. Devuelve la cantidad de archivos con error."""
    archivos = sorted(p for p in carpeta.glob("*") if p.suffix.lower() in (".xlsx", ".xls", ".xlsm", ".csv", ".parquet"))
    if not archivos:
        log.warning("No hay archivos de curvas en %s. Descargue el export de la SBS (Curva Soberana > Consulta "
                    "histórica) y déjelo ahí.", carpeta)
        return 0
    fallos = 0
    for ruta in archivos:
        try:
            df = leer_archivo(ruta)
            if desde:
                df = df[df["fecha"] >= desde]
            if df.empty:
                log.info("%s: sin fechas dentro de la ventana, se omite", ruta.name)
            else:
                cargar_df(api, df, dry)
            destino = "procesados"
        except (ErrorCurva, BackendError, OSError, ValueError) as exc:
            log.error("%s: %s", ruta.name, exc)
            fallos += 1
            destino = "con_error"
        if not dry:
            (carpeta / destino).mkdir(exist_ok=True)
            shutil.move(str(ruta), str(carpeta / destino / ruta.name))
    return fallos
