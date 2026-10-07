# Carga de datos de mercado (`carga_mercado`)

Descarga datos reales y los entrega al backend MathRisk **por su API** (sin SQL directo), así se
respeta el mapeo de entidades y no depende del nombre de las tablas.

| Dato | Fuente | Destino |
|---|---|---|
| Tipo de cambio `USDPEN Currency` | BCRP, series `PD04639PD` (compra) y `PD04640PD` (venta) del *TC Sistema bancario SBS*; se guarda el **promedio simple**, como en `carga_bd_riesgos.py` | `tipoCambio` |
| Cierres diarios de acciones y fondos (ETF) | Yahoo Finance (API no oficial) | `vectorPrecio` + catálogo (`accion`, `fondo`, emisor, plaza, sector) |
| Portafolios de Acciones, Fondos y Bonos | catálogo + último cierre | `portafolio` + `portafolioInstrumento` |

## Uso

```bash
pip install -r requirements-mercado.txt
python -m carga_mercado.run todo --dry-run      # descarga y cuenta, no escribe
python -m carga_mercado.run todo                # carga completa (3 años por defecto)
python -m carga_mercado.run tc --dias 10        # solo TC, últimos 10 días (corrida diaria)
python -m carga_mercado.run instrumentos --dias 10
python -m unittest carga_mercado.tests.test_parsers
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
- **No cubre aún**: curvas soberanas SBS (descarga manual, ver `carga_bd_riesgos.py`), superficie de
  volatilidad, índices de mercado, completar el TC del día *t* desde la SBS.
