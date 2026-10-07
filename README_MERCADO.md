# Carga de datos de mercado (`carga_mercado`)

Descarga datos reales y los entrega al backend MathRisk **por su API** (sin SQL directo), así se
respeta el mapeo de entidades y no depende del nombre de las tablas.

| Dato | Fuente | Destino |
|---|---|---|
| Tipo de cambio `USDPEN Currency` | BCRP, series `PD04639PD` (compra) y `PD04640PD` (venta) del *TC Sistema bancario SBS*; se guarda el **promedio simple**, como en `carga_bd_riesgos.py` | `tipoCambio` |
| Cierres diarios de acciones y fondos (ETF) | Yahoo Finance (API no oficial) | `vectorPrecio` + catálogo (`accion`, `fondo`, emisor, plaza, sector) |
| Curvas SBS (CCPSS, CCPEDS, CBCRS, …) | **Archivos** que se dejan en `entrada_curvas/` (export Excel/CSV del portal SBS o parquet del motor anterior) | `curvaReferenciaPuntos` + `curvaReferenciaValores` |
| Portafolios de Acciones, Fondos y Bonos | catálogo + último cierre | `portafolio` + `portafolioInstrumento` |

## Uso

```bash
pip install -r requirements-mercado.txt
python -m carga_mercado.run todo --dry-run      # descarga y cuenta, no escribe
python -m carga_mercado.run todo                # carga completa (3 años por defecto)
python -m carga_mercado.run tc --dias 10        # solo TC, últimos 10 días (corrida diaria)
python -m carga_mercado.run instrumentos --dias 10
python -m carga_mercado.run curvas --dry-run     # lee entrada_curvas/ y cuenta, no escribe
python -m carga_mercado.run curvas               # carga y mueve los archivos a procesados/ o con_error/
python -m unittest discover -s carga_mercado/tests -t .
```

Es **idempotente**: cada corrida agrega solo lo que falta (clave fecha + ISIN, o fecha + ticker).
Para la corrida diaria basta `--dias 10` (cubre feriados y fines de semana largos).

Programarla (cron, después del cierre de NY, ~18:00 hora Lima):
`0 18 * * 1-5  cd /ruta/ProcesosBatchPrometheus && python -m carga_mercado.run tc --dias 10 && python -m carga_mercado.run instrumentos --dias 10`

## Decisiones que conviene conocer

- **Ticker de TC**: el motor busca `origen + destino + " Currency"` → `USDPEN Currency`. Los TC viejos
  guardados como `USDPEN` (sin " Currency") **no los ve el motor**.
- **Precio**: cierre *split-adjusted* sin ajustar por dividendos (`numPrecioLimpio`). Solo sesiones
  cerradas: la barra del día en curso se descarta porque es un valor parcial.
- **El motor une precios solo por ISIN** (sin mirar fecha). Por eso no se cargan tickers que ya existen
  con historia distinta (AMZN, BankNY, CSCO, Boeing: plantilla de validación contra Excel 2002-2009).
- **ISIN**: todos pasan el dígito verificador (ISO 6166). Los marcados *por confirmar* en el log no
  pudieron contrastarse con una segunda fuente; validarlos contra CAVALI/Bloomberg.
- **Cantidades de los portafolios: ilustrativas.** Precios y series son reales; las cantidades no
  son posiciones de nadie (la nota del portafolio lo dice).
- **Bonos**: sin precio. El vector de precios SBS no tiene API pública y el motor de VaR excluye bonos.
- **Yahoo no es una fuente regulada**: sirve para desarrollo y demo; para producción conviene un
  proveedor con contrato y SLA.
- **Curvas SBS**: el portal está detrás de un WAF (Imperva) que pide un navegador real; no se automatiza
  ni se intenta saltar. Flujo: descargar el export de *Curva Soberana > Consulta histórica*, dejarlo en
  `entrada_curvas/` y correr `curvas` (idempotente: clave punto + fecha). El código de la curva sale de la
  columna "Tipo de Curva" o del nombre del archivo (`CCPSS_2025.xlsx`). `curvas` no entra en `todo`.
  Los scripts de `PrometheusModelos` (`curvas_to_postgres.py`, `curvas_service.obtener_curva`) no
  funcionan como están: el scraper usa un endpoint/parámetros que no existen y el espejo de GitHub
  llega solo hasta 2025-12-26. El SQL de `carga_bd_riesgos.py` apunta a tablas `fd_curvareferencia*`
  que no son las del backend (`t027_*`, `t028_*`): usar este comando.
- **No cubre aún**: superficie de volatilidad, índices de mercado, completar el TC del día *t* desde la SBS.
