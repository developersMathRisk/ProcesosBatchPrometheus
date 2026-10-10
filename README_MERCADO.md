# Carga de datos de mercado (`carga_mercado`)

Descarga datos reales y los entrega al backend MathRisk **por su API** (sin SQL directo), así se
respeta el mapeo de entidades y no depende del nombre de las tablas.

| Dato | Fuente | Destino |
|---|---|---|
| Tipo de cambio `USDPEN Currency` | BCRP, series `PD04639PD` (compra) y `PD04640PD` (venta) del *TC Sistema bancario SBS*; se guarda el **promedio simple**, como en `carga_bd_riesgos.py` | `tipoCambio` |
| Cierres diarios de acciones y fondos (ETF) | Yahoo Finance (API no oficial) | `vectorPrecio` + catálogo (`accion`, `fondo`, emisor, plaza, sector) |
| Rendimiento diario del bono soberano a 10 años | BCRP, series `PD31893DD` (S/) y `PD31894DD` (US$); el BCRP no publica otros plazos a diario | `curvaReferenciaValores` como curvas `BCRP10S` / `BCRP10D` (vértice 3600 días) |
| Curvas SBS (CCPSS, CCPEDS, CBCRS, …) | **Archivos** que se dejan en `entrada_curvas/` (export Excel/CSV del portal SBS o parquet del motor anterior) | `curvaReferenciaPuntos` + `curvaReferenciaValores` |
| Portafolios de Acciones, Fondos y Bonos | catálogo + último cierre | `portafolio` + `portafolioInstrumento` |

## Uso

El backend exige sesión: defina `API_USER` y `API_PASSWORD` (usuario con permisos de escritura en mantenedores, p. ej. rol
*Analista de riesgos*, con su clave temporal ya cambiada) antes de ejecutar cualquier comando.

```bash
pip install -r requirements-mercado.txt
python -m carga_mercado.run todo --dry-run      # descarga y cuenta, no escribe
python -m carga_mercado.run todo                # carga completa (3 años por defecto)
python -m carga_mercado.run tc --dias 10        # solo TC, últimos 10 días (corrida diaria)
python -m carga_mercado.run bcrp --dias 10      # bono soberano 10 años S/ y US$ (BCRP)
python -m carga_mercado.run curvas-sbs          # el backend descarga las curvas del portal SBS (últimos 60 días)
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
- **Curvas SBS**: se descargan del endpoint de exportación del portal
  (`/app/pp/n_CurvaSoberana/ExportarListadoHistoricoCurvaSoberana`, POST {TipoCurva, FechaInicio, FechaFin}), el mismo
  que usa el botón "Exportar". Lo hace el backend (`POST /mantenedores/curvas/actualizar-sbs`): cada curva de
  `app.sbs.curvas` se vuelve a pedir en los últimos 60 días y se reemplazan las tasas. Lo disparan la carga diaria
  (`curvas-sbs`) y el botón *Actualizar desde SBS* (Factores de Riesgo > Tasas de interés). La página de consulta está
  detrás de un WAF (Imperva): si el portal pide verificación de navegador, la corrida se detiene sin reintentar y se
  vuelve a intentar al día siguiente; nunca se intenta saltar ese control. Los pedidos van espaciados (8 s).
  El comando `curvas` (archivos en `entrada_curvas/`) queda para cargas históricas puntuales.
- **No cubre aún**: superficie de volatilidad, índices de mercado, completar el TC del día *t* desde la SBS.
