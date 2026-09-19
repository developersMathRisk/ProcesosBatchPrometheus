#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carga_bd_riesgos.py
=====================================================================
Prepara e inserta en la base de datos (SQL Server) dos insumos:

  1) CURVAS  -> se cargan desde archivos Excel/CSV descargados MANUALMENTE
               del portal SBS Curva Soberana:
               https://www.sbs.gob.pe/app/pp/n_CurvaSoberana/CurvaSoberana/ConsultaHistorica
               Formato esperado de columnas:
                   Fecha de Proceso | Tipo de Curva | Plazo (DIAS) | Tasas (%)
               Cada archivo corresponde a UN tipo de curva; el tipo se
               indica con el flag --curva (o se infiere de la columna
               "Tipo de Curva").
               Destino: FD_CurvaReferenciaPuntos (+) FD_CurvaReferenciaValores

  2) TC      -> se descarga con el motor ya validado (descarga_curvas_tc.py):
               histórico BCRP + completado del día "t" desde la SBS.
               Se inserta el TC PROMEDIO (promedio simple compra/venta) por
               día y par de moneda.
               Destino: FD_TipoCambio

SALIDA
------
Por defecto genera un archivo .sql con los INSERT (revisable, portable).
Con --insertar además ejecuta la carga directa contra la BD vía pyodbc.

La estructura de tablas respetada es la del script DDL entregado
(FD_CurvaReferenciaPuntos, FD_CurvaReferenciaValores, FD_TipoCambio).

Nota sobre el DDL entregado: la tabla FD_CurvaReferenciaPuntos NO tiene
columna ID_Curva (está comentada como "NO VA"), por lo que el
ALTER ... FOREIGN KEY(ID_Curva) del script no aplica. Aquí se insertan
solo las columnas que la tabla realmente declara.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from typing import Optional

import pandas as pd

# El motor de TC (histórico BCRP + día SBS + promedio) ya validado.
try:
    import descarga_curvas_tc as motor
except ImportError:
    motor = None  # solo se necesita para el modo TC

log = logging.getLogger("carga_bd")


# ---------------------------------------------------------------------------
# Catálogo de curvas (código proveedor SBS -> nombre) y sus alias
# ---------------------------------------------------------------------------
# Los 9 códigos válidos del portal SBS Curva Soberana.
CURVAS: dict[str, str] = {
    "CCPSS":  "Curva Soberana Soles",
    "CCPVS":  "Curva Soberana Soles VAC",
    "CCPEDS": "Curva Cupón Cero Dólares Globales",
    "CCINFS": "Curva Cupón Cero de Inflación en Soles",
    "CCCLD":  "Curva Cupón Cero Libor",
    "CBCRS":  "Curva Banco Central de Reserva CDBCRP",
    "CBCRPS": "Curva Banco Central de Reserva CDBCRP NR",  # (NR: confirmar)
    "CSBCRD": "Curva Cupón Cero Dólares Sintética",
    "CCSDF":  "Curva Dólares Corto Plazo",
}

ID_CURVAS: dict[str, int] = {
    "CCCLD": 1,
    "CCPSS": 2,
    "CCPEDS": 3,
    "CCPVS": 4,
    "CBCRS": 5,
    "CCSDF": 6,
    "CSBCRD": 7,
    "CCINFS": 8,
}


def _norm(txt: str) -> str:
    """minúsculas, sin acentos, sin espacios extra — para matching robusto."""
    t = unicodedata.normalize("NFKD", str(txt))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


# nombre normalizado -> código, para inferir desde la columna "Tipo de Curva"
_NOMBRE_A_CODIGO = {_norm(v): k for k, v in CURVAS.items()}
# también permitimos que la columna traiga directamente el código
_NOMBRE_A_CODIGO.update({_norm(k): k for k in CURVAS})


class CargaError(RuntimeError):
    """Error controlado de lectura/validación/carga."""


# ---------------------------------------------------------------------------
# 1) LECTURA Y NORMALIZACIÓN DE ARCHIVOS DE CURVA
# ---------------------------------------------------------------------------

def leer_archivo_curva(ruta: str, curva: Optional[str] = None,
                       escala_tasa: float = 1.0) -> pd.DataFrame:
    """
    Lee un archivo Excel/CSV de curva (descarga SBS) y lo normaliza a:
        FecProceso (datetime) | CodCurvaProveedor (str) | NumPlazo (int) | NumTasa (float)

    `curva`: código de curva (uno de CURVAS). Si es None, se intenta inferir
    de la columna "Tipo de Curva" del archivo o del nombre del archivo.
    `escala_tasa`: factor multiplicativo para NumTasa (default 1.0 = tal cual,
    en %). Usa 0.01 si quieres almacenar la tasa en decimal.
    """
    if not os.path.exists(ruta):
        raise CargaError(f"No existe el archivo: {ruta}")

    ext = os.path.splitext(ruta)[1].lower()
    try:
        if ext in (".xlsx", ".xls", ".xlsm"):
            raw = _leer_excel_robusto(ruta)
        else:
            # CSV/TSV: probamos separador ; , y tab, y decimal , o .
            raw = _leer_csv_flexible(ruta)
    except Exception as exc:  # noqa: BLE001
        raise CargaError(f"No se pudo leer '{ruta}': {exc}") from exc

    if raw.empty:
        raise CargaError(f"El archivo '{ruta}' está vacío.")

    # Mapeo tolerante de columnas por nombre normalizado
    cols = {_norm(c): c for c in raw.columns}

    def _buscar(*claves):
        for k in claves:
            if k in cols:
                return cols[k]
        return None

    c_fecha = _buscar("fecha de proceso", "fecha proceso", "fecha", "fecproceso")
    c_plazo = _buscar("plazo (dias)", "plazo (dias)", "plazo(dias)", "plazo",
                      "periodo (dias)", "periodo(dias)", "periodo", "numplazo")
    c_tasa = _buscar("tasas (%)", "tasa (%)", "tasas(%)", "tasa", "tasas",
                     "numtasa")
    c_tipo = _buscar("tipo de curva", "tipocurva", "tipo curva", "curva")

    faltan = [nombre for nombre, col in
              [("Fecha de Proceso", c_fecha), ("Plazo (DIAS)", c_plazo),
               ("Tasas (%)", c_tasa)] if col is None]
    if faltan:
        raise CargaError(
            f"Al archivo '{os.path.basename(ruta)}' le faltan columnas: {faltan}. "
            f"Columnas encontradas: {list(raw.columns)}"
        )

    # Resolver el código de curva (flag > columna > nombre de archivo)
    codigo = _resolver_codigo(curva, raw, c_tipo, ruta)

    tasa = pd.to_numeric(
        raw[c_tasa].astype(str).str.replace(",", ".", regex=False),
        errors="coerce")
    if escala_tasa != 1.0:
        tasa = tasa * float(escala_tasa)

    df = pd.DataFrame({
        "FecProceso": pd.to_datetime(raw[c_fecha], errors="coerce", dayfirst=True),
        "NumPlazo": pd.to_numeric(raw[c_plazo], errors="coerce"),
        "NumTasa": tasa,
    })
    df["CodCurvaProveedor"] = codigo

    _validar_curva_archivo(df, codigo, ruta)

    df["NumPlazo"] = df["NumPlazo"].astype(int)
    df = df[["FecProceso", "CodCurvaProveedor", "NumPlazo", "NumTasa"]]
    df = df.drop_duplicates(subset=["FecProceso", "CodCurvaProveedor", "NumPlazo"])
    df = df.sort_values(["FecProceso", "NumPlazo"]).reset_index(drop=True)
    return df


def _leer_excel_robusto(ruta: str) -> pd.DataFrame:
    """Lee un .xlsx tolerando filas de título antes del encabezado real:
    detecta la fila que contiene 'Fecha' y ('Tasa' o 'Plazo/Periodo')."""
    directo = pd.read_excel(ruta)
    cols_norm = {_norm(c) for c in directo.columns}
    if any("fecha" in c for c in cols_norm) and any(
            ("tasa" in c or "plazo" in c or "periodo" in c) for c in cols_norm):
        return directo
    # Buscar la fila de encabezado dentro de las primeras filas
    crudo = pd.read_excel(ruta, header=None, nrows=15)
    for i in range(len(crudo)):
        fila = [_norm(x) for x in crudo.iloc[i].tolist()]
        if any("fecha" in c for c in fila) and any(
                ("tasa" in c or "plazo" in c or "periodo" in c) for c in fila):
            return pd.read_excel(ruta, header=i)
    return directo  # se deja al validador reportar columnas faltantes


def _leer_csv_flexible(ruta: str) -> pd.DataFrame:
    for sep in (";", ",", "\t"):
        try:
            df = pd.read_csv(ruta, sep=sep, encoding="utf-8-sig")
            if df.shape[1] >= 3:
                return df
        except Exception:  # noqa: BLE001
            continue
    # último intento: separador automático
    return pd.read_csv(ruta, sep=None, engine="python", encoding="utf-8-sig")


def _resolver_codigo(curva: Optional[str], raw: pd.DataFrame,
                     c_tipo: Optional[str], ruta: str) -> str:
    if curva:
        codigo = curva.upper().strip()
        if codigo not in CURVAS:
            raise CargaError(
                f"Curva '{curva}' no reconocida. Opciones: {list(CURVAS)}")
        # si el archivo trae tipo, avisamos si no concuerda
        if c_tipo is not None:
            vals = {_norm(x) for x in raw[c_tipo].dropna().unique()}
            inferidos = {_NOMBRE_A_CODIGO.get(v) for v in vals}
            inferidos.discard(None)
            if inferidos and codigo not in inferidos:
                log.warning("El archivo '%s' parece ser %s pero se forzó --curva %s.",
                            os.path.basename(ruta), inferidos, codigo)
        return codigo

    # Inferir desde la columna "Tipo de Curva"
    if c_tipo is None:
        raise CargaError(
            "No se indicó --curva y el archivo no tiene columna 'Tipo de Curva' "
            "para inferirlo. Indica el tipo con --curva.")
    vals = {_norm(x) for x in raw[c_tipo].dropna().unique()}
    inferidos = {_NOMBRE_A_CODIGO.get(v) for v in vals}
    inferidos.discard(None)
    if len(inferidos) == 1:
        return inferidos.pop()
    raise CargaError(
        f"No se pudo inferir un único tipo de curva del archivo (encontrado: "
        f"{vals}). Indícalo explícitamente con --curva.")


def _validar_curva_archivo(df: pd.DataFrame, codigo: str, ruta: str) -> None:
    n = len(df)
    n_fecha = int(df["FecProceso"].isna().sum())
    n_plazo = int(df["NumPlazo"].isna().sum())
    n_tasa = int(df["NumTasa"].isna().sum())
    if n_fecha:
        raise CargaError(f"{codigo}: {n_fecha}/{n} filas con fecha inválida.")
    if n_plazo:
        raise CargaError(f"{codigo}: {n_plazo}/{n} filas con plazo inválido.")
    if n_tasa:
        # NumTasa es NOT NULL en la BD: no se pueden insertar nulos.
        raise CargaError(
            f"{codigo}: {n_tasa}/{n} filas con tasa nula/no numérica. "
            "Revisa el archivo (la columna NumTasa es NOT NULL en la tabla).")
    log.info("Archivo %s validado: %s filas, %s fechas, plazos %s..%s",
             codigo, n, df["FecProceso"].nunique(),
             int(df["NumPlazo"].min()), int(df["NumPlazo"].max()))


# ---------------------------------------------------------------------------
# 2) GENERACIÓN DE SQL
# ---------------------------------------------------------------------------

def _sql_str(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def _sql_fecha(ts) -> str:
    return "'" + pd.Timestamp(ts).strftime("%Y-%m-%d") + "'"


def sql_curva(df: pd.DataFrame, codigo: str, esquema: str = "public") -> str:
    """Genera SQL PostgreSQL para maestro, puntos y valores de una curva."""
    if codigo not in ID_CURVAS:
        raise CargaError(f"No hay ID_CURVA configurado para '{codigo}'.")
    T_PUNTOS = f"{esquema}.fd_curvareferenciapuntos"
    T_VALORES = f"{esquema}.fd_curvareferenciavalores"
    out: list[str] = []
    nombre = CURVAS.get(codigo, codigo)

    out.append(f"-- ============================================================")
    out.append(f"-- Curva {codigo} — {nombre}")
    out.append(f"-- Filas: {len(df):,} | Fechas: {df['FecProceso'].nunique()} "
               f"| Plazos: {df['NumPlazo'].nunique()}")
    out.append(f"-- ============================================================")
    out.append("BEGIN;")
    out.append("-- Las tablas fm_curvareferencia y fd_curvareferenciapuntos son maestras.")
    out.append("-- Este script solo inserta valores usando los puntos existentes.")
    out.append("")

    # Valores: resuelve id_punto en el maestro existente.
    out.append(f"-- Valores (tasas) de la curva {codigo}")
    for _, r in df.iterrows():
        plazo = int(r["NumPlazo"])
        tasa = f"{float(r['NumTasa']):.8f}"
        out.append(
            f"INSERT INTO {T_VALORES} "
            f"(id_punto, fecproceso, numtasa, fecregistro, flgactivo) "
            f"SELECT p.id_punto, {_sql_fecha(r['FecProceso'])}, {tasa}, "
            f"CURRENT_TIMESTAMP, TRUE FROM {T_PUNTOS} p "
            f"WHERE p.codcurvaproveedor='{codigo}' AND p.numplazo={plazo} "
            f"AND NOT EXISTS (SELECT 1 FROM {T_VALORES} v "
            f"WHERE v.id_punto=p.id_punto "
            f"AND v.fecproceso={_sql_fecha(r['FecProceso'])});"
        )

    out.append("")
    out.append("COMMIT;")
    out.append(f"-- Curva {codigo}: {len(df)} valores procesados.")
    out.append("")
    return "\n".join(out)


# --- TC -> FD_TipoCambio ---------------------------------------------------

_FUENTE_A_COD = {
    "BCRP - BCRPData API": "WEB",
    "SBS (TC publicado del día)": "WEB",
    "Mixto (BCRP/SBS)": "WEB",
}


def sql_tc(df_tc: pd.DataFrame, esquema: str = "public",
           incluir_inversa: bool = False) -> str:
    """Genera SQL PostgreSQL para insertar TC promedio en fd_tipocambio.

    Usa únicamente las filas tipo_tc == 'promedio' del DataFrame del motor.
    CodMoneda1/CodMoneda2 salen del par (p. ej. USD/PEN -> 1=USD, 2=PEN).
    """
    T = f"{esquema}.fd_tipocambio"
    prom = df_tc[df_tc["tipo_tc"] == "promedio"].copy()
    if prom.empty:
        raise CargaError(
            "No hay filas de TC promedio para insertar. ¿El motor devolvió "
            "compra y venta para poder promediar?")

    out: list[str] = []
    out.append("-- ============================================================")
    out.append("-- Tipo de Cambio PROMEDIO (promedio simple compra/venta)")
    out.append(f"-- Filas: {len(prom):,} | Fechas: {prom['fecha'].nunique()}")
    out.append("-- ============================================================")
    out.append("BEGIN;")
    out.append("")

    for _, r in prom.iterrows():
        m1, m2 = str(r["par_moneda"]).split("/")  # 'USD/PEN'
        cod_fuente = _FUENTE_A_COD.get(r["fuente"], "WEB")
        ticker = f"{m1}{m2} Currency"
        valor = f"{float(r['valor']):.8f}"
        out.append(
            f"INSERT INTO {T} (fecproceso, codmoneda1, codmoneda2, "
            f"codfuentedatos, desticker, valor, flagactivo) VALUES "
            f"({_sql_fecha(r['fecha'])}, '{m1}', '{m2}', '{cod_fuente}', "
            f"'{ticker}', {valor}, TRUE);"
        )
        if incluir_inversa:
            inv = f"{1.0 / float(r['valor']):.8f}"
            out.append(
                f"INSERT INTO {T} (fecproceso, codmoneda1, codmoneda2, "
                f"codfuentedatos, desticker, valor, flagactivo) VALUES "
                f"({_sql_fecha(r['fecha'])}, '{m2}', '{m1}', '{cod_fuente}', "
                f"'{m2}{m1} Currency', {inv}, TRUE);"
            )

    out.append("")
    out.append("COMMIT TRANSACTION;")
    out.append(f"-- TC promedio: {len(prom)} filas cargadas.")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 2b) PROCESAMIENTO POR LOTE (carpeta con varios archivos de curva)
# ---------------------------------------------------------------------------

def _inferir_codigo_desde_nombre(ruta: str) -> Optional[str]:
    """Intenta deducir el código de curva a partir del nombre del archivo."""
    base = _norm(os.path.splitext(os.path.basename(ruta))[0])
    # 1) por código exacto contenido en el nombre (p. ej. 'CCPSS_2025')
    for cod in CURVAS:
        if _norm(cod) in base.split(" ") or _norm(cod) in base.replace("_", " ").split(" "):
            return cod
    # 2) por nombre de curva contenido en el nombre del archivo
    for nom_norm, cod in _NOMBRE_A_CODIGO.items():
        if nom_norm and nom_norm in base:
            return cod
    return None


def procesar_lote(carpeta: str, patron: str = "*",
                  esquema: str = "dbo") -> tuple[str, list[dict]]:
    """Procesa todos los archivos de curva de una carpeta y devuelve
    (sql_combinado, resumen). El tipo de cada archivo se infiere de la
    columna 'Tipo de Curva' y, si falta, del nombre del archivo."""
    import glob
    exts = (".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".txt")
    rutas = sorted(
        r for r in glob.glob(os.path.join(carpeta, patron))
        if os.path.splitext(r)[1].lower() in exts
    )
    if not rutas:
        raise CargaError(
            f"No se encontraron archivos de curva en '{carpeta}' "
            f"(patrón '{patron}').")

    bloques: list[str] = []
    resumen: list[dict] = []
    for ruta in rutas:
        nombre = os.path.basename(ruta)
        try:
            try:
                df = leer_archivo_curva(ruta, curva=None)   # inferir de columna
            except CargaError:
                cod = _inferir_codigo_desde_nombre(ruta)     # fallback: nombre
                if not cod:
                    raise
                df = leer_archivo_curva(ruta, curva=cod)
            codigo = df["CodCurvaProveedor"].iloc[0]
            bloques.append(sql_curva(df, codigo, esquema))
            resumen.append({"archivo": nombre, "curva": codigo,
                            "filas": len(df), "estado": "OK"})
            log.info("[LOTE] %s -> %s (%s filas)", nombre, codigo, len(df))
        except CargaError as exc:
            resumen.append({"archivo": nombre, "curva": "-",
                            "filas": 0, "estado": f"OMITIDO: {exc}"})
            log.warning("[LOTE] %s omitido: %s", nombre, exc)

    if not bloques:
        raise CargaError(
            "Ningún archivo de la carpeta pudo procesarse. Revisa el formato "
            "de columnas o indica el tipo por nombre de archivo.")

    encabezado = [
        "-- ####################################################################",
        "-- Carga por LOTE de curvas SBS",
        f"-- Carpeta: {carpeta}",
        f"-- Archivos procesados: {sum(1 for r in resumen if r['estado']=='OK')}"
        f" / {len(resumen)}",
        "-- ####################################################################",
        "",
    ]
    return "\n".join(encabezado) + "\n".join(bloques), resumen


# ---------------------------------------------------------------------------
# 3) INSERCIÓN DIRECTA (opcional, vía pyodbc)
# ---------------------------------------------------------------------------

def insertar_sql(sql: str, conn_str: str) -> None:
    """Ejecuta SQL PostgreSQL usando psycopg.

    `conn_str` debe ser una URL PostgreSQL.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise CargaError(
            "Falta 'psycopg' para la inserción directa "
            "(pip install 'psycopg[binary]').") from exc
    try:
        kwargs = {"conninfo": conn_str, "connect_timeout": 10}
        inicio = time.perf_counter()
        with psycopg.connect(**kwargs) as cn:
            with cn.cursor() as cur:
                cur.execute("SET statement_timeout = 30000")
                cur.execute(sql)
        log.info("Inserción directa en BD completada en %.2f s.",
                 time.perf_counter() - inicio)
    except Exception as exc:  # noqa: BLE001
        raise CargaError(f"Fallo en la inserción a BD: {exc}") from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Carga curvas (archivo manual) y TC promedio a la BD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # parser padre con opciones comunes (para que -v funcione tras el subcomando)
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="modo", required=True)

    # --- modo curva ---
    pc = sub.add_parser("curva", parents=[comun],
                        help="Ingiere un archivo de curva y genera SQL.")
    pc.add_argument("--archivo", required=True,
                    help="Ruta del Excel/CSV descargado de la SBS.")
    pc.add_argument("--curva", default=None,
                    help=f"Código de curva. Opciones: {list(CURVAS)}. "
                         "Si se omite, se infiere de la columna 'Tipo de Curva'.")
    pc.add_argument("--salida-sql", default=None,
                    help="Ruta del .sql a generar (default: junto al archivo).")
    pc.add_argument("--esquema", default="dbo")
    pc.add_argument("--insertar", action="store_true",
                    help="Además de generar el .sql, inserta en la BD (pyodbc).")
    pc.add_argument("--conn", default=None,
                    help="Cadena de conexión ODBC (requerida con --insertar).")

    # --- modo lote de curvas ---
    pl = sub.add_parser("curvas-lote", parents=[comun],
                        help="Procesa TODOS los archivos de curva de una carpeta.")
    pl.add_argument("--carpeta", required=True,
                    help="Carpeta con los archivos de curva (Excel/CSV).")
    pl.add_argument("--patron", default="*",
                    help="Patrón glob de archivos (default: '*').")
    pl.add_argument("--salida-sql", default=None,
                    help="Ruta del .sql combinado (default: en la carpeta).")
    pl.add_argument("--esquema", default="dbo")
    pl.add_argument("--insertar", action="store_true")
    pl.add_argument("--conn", default=None)

    # --- modo tc ---
    pt = sub.add_parser("tc", parents=[comun],
                        help="Descarga TC, calcula promedio y genera SQL.")
    pt.add_argument("--fecha-inicio", required=True)
    pt.add_argument("--fecha-fin", required=True)
    pt.add_argument("--serie", action="append", default=None,
                    help="Serie(s) TC del BCRP (default: interbancario C/V).")
    pt.add_argument("--salida-sql", default=None)
    pt.add_argument("--esquema", default="dbo")
    pt.add_argument("--incluir-inversa", action="store_true",
                    help="Genera también el par inverso (p. ej. PEN/USD).")
    pt.add_argument("--sin-completar-sbs", action="store_true")
    pt.add_argument("--insertar", action="store_true")
    pt.add_argument("--conn", default=None)

    # --- listar curvas ---
    sub.add_parser("listar-curvas", parents=[comun],
                   help="Muestra el catálogo de curvas.")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = construir_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    if args.modo == "listar-curvas":
        print(f"\nCurvas disponibles ({len(CURVAS)}):\n")
        print(f"  {'Código':<8} Nombre")
        print("  " + "-" * 55)
        for cod, nom in CURVAS.items():
            print(f"  {cod:<8} {nom}")
        print("\nDescarga manual desde: "
              "https://www.sbs.gob.pe/app/pp/n_CurvaSoberana/"
              "CurvaSoberana/ConsultaHistorica")
        return 0

    if args.insertar and not args.conn:
        log.error("--insertar requiere --conn con la cadena de conexión ODBC.")
        return 2

    # ---- modo CURVA ----
    if args.modo == "curva":
        try:
            df = leer_archivo_curva(args.archivo, args.curva)
            codigo = df["CodCurvaProveedor"].iloc[0]
            sql = sql_curva(df, codigo, args.esquema)
        except CargaError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        salida = args.salida_sql or (
            os.path.splitext(args.archivo)[0] + f"_{codigo}.sql")
        with open(salida, "w", encoding="utf-8") as fh:
            fh.write(sql)
        print(f"[OK] Curva {codigo}: {len(df):,} valores -> {salida}")
        if args.insertar:
            try:
                insertar_sql(sql, args.conn)
                print("[OK] Insertado en BD.")
            except CargaError as exc:
                print(f"[ERROR] Inserción: {exc}", file=sys.stderr)
                return 1
        return 0

    # ---- modo LOTE de curvas ----
    if args.modo == "curvas-lote":
        try:
            sql, resumen = procesar_lote(args.carpeta, args.patron, args.esquema)
        except CargaError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        salida = args.salida_sql or os.path.join(args.carpeta, "carga_curvas_lote.sql")
        with open(salida, "w", encoding="utf-8") as fh:
            fh.write(sql)
        print(f"\nResumen del lote:")
        print(f"  {'Archivo':<40} {'Curva':<8} {'Filas':>8}  Estado")
        print("  " + "-" * 78)
        for r in resumen:
            estado = r["estado"] if r["estado"] == "OK" else r["estado"][:30]
            print(f"  {r['archivo'][:40]:<40} {r['curva']:<8} "
                  f"{r['filas']:>8,}  {estado}")
        ok = sum(1 for r in resumen if r["estado"] == "OK")
        print(f"\n[OK] {ok}/{len(resumen)} archivos -> {salida}")
        if args.insertar:
            try:
                insertar_sql(sql, args.conn)
                print("[OK] Insertado en BD.")
            except CargaError as exc:
                print(f"[ERROR] Inserción: {exc}", file=sys.stderr)
                return 1
        return 0

    # ---- modo TC ----
    if args.modo == "tc":
        if motor is None:
            log.error("No se pudo importar descarga_curvas_tc.py "
                      "(debe estar junto a este script).")
            return 2
        try:
            fi = motor._parse_fecha(args.fecha_inicio)
            ff = motor._parse_fecha(args.fecha_fin)
            codigos = args.serie or motor.SERIES_TC_DEFAULT
            df_tc = motor.descargar_tc(
                codigos, fi, ff,
                completar_sbs=not args.sin_completar_sbs)
            sql = sql_tc(df_tc, args.esquema, args.incluir_inversa)
        except (motor.DescargaError, CargaError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        stamp = f"{fi:%Y%m%d}_{ff:%Y%m%d}"
        salida = args.salida_sql or f"tc_promedio_{stamp}.sql"
        with open(salida, "w", encoding="utf-8") as fh:
            fh.write(sql)
        n_prom = int((df_tc["tipo_tc"] == "promedio").sum())
        print(f"[OK] TC promedio: {n_prom:,} filas -> {salida}")
        if args.insertar:
            try:
                insertar_sql(sql, args.conn)
                print("[OK] Insertado en BD.")
            except CargaError as exc:
                print(f"[ERROR] Inserción: {exc}", file=sys.stderr)
                return 1
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
