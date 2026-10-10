"""Carga diaria de datos de mercado reales hacia el backend MathRisk.

    python -m carga_mercado.run todo          [--dias 1095] [--dry-run] [--api URL]
    python -m carga_mercado.run instrumentos  # catálogo + precios de acciones y fondos (Yahoo Finance)
    python -m carga_mercado.run tc            # tipo de cambio USDPEN (BCRP, promedio SBS compra/venta)
    python -m carga_mercado.run portafolios   # portafolios de Acciones, Fondos y Bonos
    python -m carga_mercado.run bcrp          # rendimiento diario del bono soberano a 10 años (BCRP, S/ y US$)

Es idempotente: cada corrida solo agrega lo que falta (clave: fecha + instrumento / fecha + ticker).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import fuentes
import pandas as pd

from .curvas import cargar_df, cmd_curvas
from .backend import Backend, BackendError
from .instrumentos import ACCIONES, BONOS_CANTIDAD, FONDOS
from .isin import isin_valido

log = logging.getLogger("carga_mercado")

FUENTE_TC = "WEB"                  # la misma de los tipos de cambio ya cargados
FUENTE_PRECIOS = "YAH"             # codFuenteDatos de vector_precio
TICKER_USDPEN = "USDPEN Currency"  # el motor busca origen + destino + " Currency"
HILOS = 6


def _con_reintentos(fn, cuerpo, intentos: int = 3):
    for i in range(intentos):
        try:
            return fn(cuerpo)
        except Exception:
            if i == intentos - 1:
                raise


def _plaza(meta: dict) -> tuple[str, str]:
    """Plaza según la bolsa que declara Yahoo (así no se asume dónde cotiza cada ticker)."""
    nombre = (meta.get("fullExchangeName") or meta.get("exchangeName") or "").lower()
    if "nasdaq" in nombre:
        return "NASDAQ", "NASDAQ Stock Market"
    if "arca" in nombre:
        return "ARCA", "NYSE Arca"
    return "NYSE", "New York Stock Exchange"


class Contexto:
    """Catálogos de referencia ya existentes y creación de los que faltan."""

    def __init__(self, api: Backend):
        self.api = api
        self.moneda_usd = next(m for m in api.listar("moneda") if m["codMoneda"] == "USD")
        self.tipo = {t["codTipoInstrumento"]: t for t in api.listar("tipoInstrumento")}
        self.tipo_accion = api.listar("tipoAccion")[0]
        self.tipo_fondo = {f["desTipoFondo"]: f for f in api.listar("tipoFondo")}
        self.fuente = api.obtener_o_crear(
            "fuenteInformacion", "crearFuenteInformacion",
            lambda f: f["codFuenteInformacion"] == FUENTE_PRECIOS,
            {"codFuenteInformacion": FUENTE_PRECIOS,
             "desFuenteInformacion": "Yahoo Finance (cierres diarios, API no oficial)"})
        self.acciones = {a["codISIN"]: a for a in api.listar("accion")}
        self.fondos = {f["codISIN"]: f for f in api.listar("fondo")}

    def plaza(self, meta: dict) -> dict:
        cod, nombre = _plaza(meta)
        return self.api.obtener_o_crear("plaza", "crearPlaza", lambda p: p["codPlaza"] == cod,
                                        {"codPlaza": cod, "desPlaza": nombre})

    def emisor(self, cod: str, nombre: str, id_pais: int, gestora: bool) -> dict:
        peruano = id_pais == 1
        return self.api.obtener_o_crear(
            "emisor", "crearEmisor", lambda e: e["codEmisor"] == cod,
            {"codEmisor": cod, "nomEmisor": nombre, "codTipoEmisor": "SA" if gestora else "CO",
             "detalle": " ", "ambito": "NA" if peruano else "EX", "codBloomberg": "",
             "codFuente": "BV" if peruano else None, "idPais": id_pais})

    def sector(self, cod: str, nombre: str) -> dict:
        return self.api.obtener_o_crear("tipoSector", "crearTipoSector",
                                        lambda s: s["codTiposector"] == cod,
                                        {"codTiposector": cod, "descripcionTiposector": nombre})

    def asegurar_accion(self, t, isin, nombre, emisor, sector, meta) -> dict:
        if isin in self.acciones:
            return self.acciones[isin]
        fila = self.api.crear("crearAccion", {
            "codISIN": isin, "codTicker": t, "desNemonico": nombre, "flgCargaAutom": True, "flgVar": True,
            "idPlaza": self.plaza(meta)["idPlaza"], "idTipoAccion": self.tipo_accion["idTipoAccion"],
            "idFuenteInformacion": self.fuente["idFuenteInformacion"],
            "idEmisor": self.emisor(*emisor, gestora=False)["idEmisor"],
            "idMoneda": self.moneda_usd["idMoneda"],
            "idTipoInstrumento": self.tipo["TI001"]["idTipoInstrumento"],
            "idTipoSector": self.sector(*sector)["idTipoSector"]})
        self.acciones[isin] = fila
        return fila

    def asegurar_fondo(self, t, isin, nombre, gestora, tipo_fondo, meta) -> dict:
        if isin in self.fondos:
            return self.fondos[isin]
        fila = self.api.crear("crearFondo", {
            "codISIN": isin, "codTicker": t, "desNemonico": nombre, "flgCargaAutom": True, "flgVar": True,
            "idPlaza": self.plaza(meta)["idPlaza"],
            "idEmisor": self.emisor(*gestora, gestora=True)["idEmisor"],
            "idMoneda": self.moneda_usd["idMoneda"],
            "idTipoFondo": self.tipo_fondo[tipo_fondo]["idTipoFondo"],
            "idFuenteInformacion": self.fuente["idFuenteInformacion"],
            "idTipoInstrumento": self.tipo["TI003"]["idTipoInstrumento"]})
        self.fondos[isin] = fila
        return fila


# ---------------------------------------------------------------------------------------------
# Instrumentos + precios
# ---------------------------------------------------------------------------------------------

def cmd_instrumentos(api: Backend, desde: date, hasta: date, dry: bool) -> int:
    fallos = 0
    ctx = None if dry else Contexto(api)
    existentes: dict[str, set[str]] = {}
    for fila in api.listar("vectorPrecio", desde=desde.isoformat(), hasta=hasta.isoformat()):
        existentes.setdefault(fila["codISIN"], set()).add(fila["fecProceso"])

    definiciones = [("accion", *a) for a in ACCIONES] + [("fondo", *f) for f in FONDOS]
    for tipo, t, isin, nombre, emisor, extra, confirmado, _ in definiciones:
        try:
            if not isin_valido(isin):
                raise ValueError(f"ISIN {isin} no pasa el dígito verificador")
            meta, serie = fuentes.yahoo_cierres(t, desde, hasta)
            if meta.get("currency") != "USD":
                raise ValueError(f"se esperaba USD y Yahoo informa {meta.get('currency')}")
            if not serie:
                raise ValueError("Yahoo no devolvió cierres en el rango")
            if not dry:
                if tipo == "accion":
                    ctx.asegurar_accion(t, isin, nombre, emisor, extra, meta)
                else:
                    ctx.asegurar_fondo(t, isin, nombre, emisor, extra, meta)
            ya = existentes.get(isin, set())
            nuevos = [{"fecProceso": f.isoformat(), "codISIN": isin, "nemonico": t, "numPrecioLimpio": c,
                       "codFuenteDatos": FUENTE_PRECIOS,
                       "idMoneda": None if dry else ctx.moneda_usd["idMoneda"]}
                      for f, c in serie if f.isoformat() not in ya]
            if not dry and nuevos:
                with ThreadPoolExecutor(HILOS) as pool:
                    list(pool.map(lambda b: _con_reintentos(lambda x: api.crear("crearVectorPrecio", x), b), nuevos))
            marca = "" if confirmado else "  [ISIN por confirmar]"
            log.info("%-5s %-13s %4d cierres (%s -> %s), %d nuevos%s", t, isin, len(serie),
                     serie[0][0], serie[-1][0], len(nuevos), marca)
        except (ValueError, fuentes.FuenteError, BackendError) as exc:
            fallos += 1
            log.error("%-5s OMITIDO: %s", t, exc)
    return fallos


# ---------------------------------------------------------------------------------------------
# Tipo de cambio
# ---------------------------------------------------------------------------------------------

def cmd_tc(api: Backend, desde: date, hasta: date, dry: bool) -> int:
    try:
        serie = fuentes.tc_promedio_usd_pen(desde, hasta)
    except fuentes.FuenteError as exc:
        log.error("TC BCRP: %s", exc)
        return 1
    ya = {f["fecProceso"] for f in api.listar("tipoCambio", desde=desde.isoformat(), hasta=hasta.isoformat())
          if f["desTicker"] == TICKER_USDPEN}
    nuevos = [{"fecProceso": f.isoformat(), "codFuenteDatos": FUENTE_TC, "desTicker": TICKER_USDPEN, "valor": v}
              for f, v in serie if f.isoformat() not in ya]
    if not dry and nuevos:
        with ThreadPoolExecutor(HILOS) as pool:
            list(pool.map(lambda b: _con_reintentos(lambda x: api.crear("crearTipoCambio", x), b), nuevos))
    log.info("%s: %d días (%s -> %s), %d nuevos", TICKER_USDPEN, len(serie),
             serie[0][0] if serie else "-", serie[-1][0] if serie else "-", len(nuevos))
    return 0


# ---------------------------------------------------------------------------------------------
# Tasas BCRP (bono soberano 10 años) -> curvas BCRP10S / BCRP10D, vértice de 3600 días
# ---------------------------------------------------------------------------------------------

SERIES_BCRP = {"BCRP10S": fuentes.BCRP_BONO10_PEN, "BCRP10D": fuentes.BCRP_BONO10_USD}
PLAZO_BONO10 = 3600


def cmd_bcrp(api: Backend, desde: date, hasta: date, dry: bool) -> int:
    fallos = 0
    for curva, codigo in SERIES_BCRP.items():
        try:
            serie = fuentes.bcrp_serie_diaria(codigo, desde, hasta)
        except fuentes.FuenteError as exc:
            log.error("%s (%s): %s", curva, codigo, exc)
            fallos += 1
            continue
        if not serie:
            log.warning("%s (%s): el BCRP no devolvió datos en la ventana", curva, codigo)
            continue
        df = pd.DataFrame({"fecha": [f for f, _ in serie], "plazo": PLAZO_BONO10,
                           "tasa": [v for _, v in serie], "curva": curva})
        cargar_df(api, df, dry)
    return fallos


# ---------------------------------------------------------------------------------------------
# Portafolios
# ---------------------------------------------------------------------------------------------

def _siguiente_codigo(existentes: list[dict]) -> str:
    numeros = [int(m.group(1)) for p in existentes
               if (m := re.fullmatch(r"PORTAFOLIO(\d+)", p.get("codPortafolio") or ""))]
    return f"PORTAFOLIO{(max(numeros) if numeros else 0) + 1:04d}"


def _actualizar_snapshot(api: Backend, ctx, pf: dict, actuales: list[dict], nuevas: list[dict], fecha_ref: date) -> None:
    """Las posiciones son una foto por fecha (el dashboard consulta una fecha exacta). Si el portafolio ya
    tiene la foto de `fecha_ref` no se toca; si no, se crea con las cantidades de su última foto y los
    precios de `nuevas` (cierre de esa fecha; null en bonos)."""
    ultima = max(p["fechaValor"] for p in actuales)
    if ultima >= fecha_ref.isoformat():
        log.info("%s: ya tiene la foto del %s, se deja como está", pf["descripcionPortafolio"], ultima)
        return
    precios = {p["codISIN"]: p["precio"] for p in nuevas}
    cuerpo = [{"codISIN": p["codISIN"], "codticker": p["codticker"], "cantidad": p["cantidad"],
               "precio": precios.get(p["codISIN"]), "fechaValor": fecha_ref.isoformat(),
               "idPortafolio": pf["idPortafolio"], "idTipoInstrumento": p["idTipoInstrumento"]}
              for p in actuales if p["fechaValor"] == ultima]
    api.crear("crearPortafolioInstrumentoMasivo", cuerpo)
    log.info("%s: nueva foto al %s con %d posiciones (cantidades de la foto del %s)",
             pf["descripcionPortafolio"], fecha_ref, len(cuerpo), ultima)


def cmd_portafolios(api: Backend, dry: bool) -> int:
    hoy = date.today()
    ultimo: dict[str, tuple[date, float]] = {}
    for _, t, *_ in [("a", *a) for a in ACCIONES] + [("f", *f) for f in FONDOS]:
        try:
            _, serie = fuentes.yahoo_cierres(t, hoy - timedelta(days=12), hoy)
            if serie:
                ultimo[t] = serie[-1]
        except fuentes.FuenteError as exc:
            log.error("%s sin precio reciente: %s", t, exc)

    ctx = None if dry else Contexto(api)
    usd = None if dry else ctx.moneda_usd
    pen = None if dry else next(m for m in api.listar("moneda") if m["codMoneda"] == "PEN")
    bono_ticker = {b["codISIN"]: b["ticker"] for b in api.listar("bono")}

    def posiciones_mercado(catalogo, codigo_tipo):
        out = []
        for t, isin, *_, monto in catalogo:
            if t not in ultimo:
                continue
            fecha, precio = ultimo[t]
            out.append({"codISIN": isin, "codticker": t, "cantidad": max(1, round(monto / precio)),
                        "precio": precio, "fechaValor": fecha.isoformat(), "_tipo": codigo_tipo})
        return out

    fecha_ref = max((f for f, _ in ultimo.values()), default=hoy)
    nota_mercado = (f"Posiciones ilustrativas (cantidades supuestas); precios de cierre reales "
                    f"de Yahoo Finance al {fecha_ref}.")
    definiciones = [
        ("Acciones USD - mercado real", "Renta Variable", usd, nota_mercado, posiciones_mercado(ACCIONES, "TI001")),
        ("Fondos USD - mercado real", "Fondos de Inversión", usd, nota_mercado, posiciones_mercado(FONDOS, "TI003")),
        ("Bonos soberanos PEN - mercado real", "Renta Fija", pen,
         "Posiciones ilustrativas (cantidades supuestas) sobre bonos soberanos del catálogo. Sin precio: "
         "el vector de precios SBS no tiene API pública y el motor de VaR aún no admite bonos.",
         [{"codISIN": i, "codticker": bono_ticker.get(i, i), "cantidad": q, "precio": None,
           "fechaValor": fecha_ref.isoformat(), "_tipo": "TI002"} for i, q in BONOS_CANTIDAD.items()]),
    ]

    existentes = api.listar("portafolio")
    todas = api.listar("portafolioInstrumento")
    con_posiciones = {p["idPortafolio"] for p in todas}
    for nombre, tipo_pf, moneda, nota, posiciones in definiciones:
        if not posiciones:
            log.error("%s: sin posiciones (¿faltan precios?)", nombre)
            continue
        if dry:
            log.info("%s: %d posiciones (dry-run)", nombre, len(posiciones))
            continue
        pf = next((p for p in existentes if p["descripcionPortafolio"] == nombre), None)
        if pf is None:
            pf = api.crear("crearPortafolio", {
                "codPortafolio": _siguiente_codigo(existentes), "descripcionPortafolio": nombre, "notas": nota,
                "tipoPortafolio": tipo_pf, "idMoneda": moneda["idMoneda"]})
            existentes.append(pf)
        if pf["idPortafolio"] in con_posiciones:
            _actualizar_snapshot(api, ctx, pf, [t for t in todas if t["idPortafolio"] == pf["idPortafolio"]],
                                 posiciones, fecha_ref)
            continue
        cuerpo = [{"codISIN": p["codISIN"], "codticker": p["codticker"], "cantidad": p["cantidad"],
                   "precio": p["precio"], "fechaValor": p["fechaValor"], "idPortafolio": pf["idPortafolio"],
                   "idTipoInstrumento": ctx.tipo[p["_tipo"]]["idTipoInstrumento"]} for p in posiciones]
        api.crear("crearPortafolioInstrumentoMasivo", cuerpo)
        log.info("%s: portafolio %s con %d posiciones", nombre, pf["codPortafolio"], len(cuerpo))
    return 0


# ---------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("comando", choices=["todo", "instrumentos", "tc", "bcrp", "portafolios", "curvas"])
    ap.add_argument("--dias", type=int, default=1095, help="ventana histórica a cargar (por defecto 3 años)")
    ap.add_argument("--api", default="http://127.0.0.1:8080", help="URL base del backend")
    ap.add_argument("--carpeta-curvas", default="entrada_curvas", help="carpeta con los exports de curvas SBS (comando curvas)")
    ap.add_argument("--curvas-desde", help="solo cargar tasas desde esta fecha (AAAA-MM-DD); evita cargar historia que el motor no usa")
    ap.add_argument("--dry-run", action="store_true", help="descarga y cuenta, sin escribir en el backend")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    api = Backend(a.api)
    hasta = date.today()
    desde = hasta - timedelta(days=a.dias)
    fallos = 0
    try:
        if a.comando in ("todo", "instrumentos"):
            fallos += cmd_instrumentos(api, desde, hasta, a.dry_run)
        if a.comando in ("todo", "tc"):
            fallos += cmd_tc(api, desde, hasta, a.dry_run)
        if a.comando in ("todo", "bcrp"):
            fallos += cmd_bcrp(api, desde, hasta, a.dry_run)
        if a.comando == "curvas":     # fuera de "todo": depende de archivos que se bajan a mano de la SBS
            fallos += cmd_curvas(api, Path(a.carpeta_curvas), date.fromisoformat(a.curvas_desde) if a.curvas_desde else None, a.dry_run)
        if a.comando in ("todo", "portafolios"):
            fallos += cmd_portafolios(api, a.dry_run)
    except BackendError as exc:
        log.error("Backend: %s", exc)
        return 2
    log.info("Terminado%s: %d elementos con error.", " (dry-run)" if a.dry_run else "", fallos)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
