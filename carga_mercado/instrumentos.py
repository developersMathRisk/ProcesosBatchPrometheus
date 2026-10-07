"""Instrumentos reales con serie de precios en Yahoo Finance.

`confirmado=True`: el ISIN coincide con una segunda fuente independiente (yfinance/BusinessInsider).
`confirmado=False`: solo pasó el dígito verificador; debe validarse contra CAVALI/Bloomberg.
Las cantidades de los portafolios son ILUSTRATIVAS (no son posiciones reales de nadie): se fijan por
monto objetivo en USD; los precios y las series sí son datos de mercado reales.
"""

# ticker, ISIN, nombre, emisor (cod, nombre, idPais), sector (cod, nombre), confirmado, monto objetivo USD
ACCIONES = [
    ("SCCO", "US84265V1052", "SOUTHERN COPPER",    ("SCCO", "Southern Copper Corporation", 3),          ("MIN", "Minería"),              False, 400_000),
    ("BVN",  "US2044481040", "BUENAVENTURA",       ("BVN",  "Compañía de Minas Buenaventura S.A.A.", 1), ("MIN", "Minería"),              False, 150_000),
    ("MSFT", "US5949181045", "MICROSOFT",          ("MSFT", "Microsoft Corporation", 3),                 ("TEC", "Tecnología"),           False, 300_000),
    ("AAPL", "US0378331005", "APPLE",              ("AAPL", "Apple Inc.", 3),                            ("TEC", "Tecnología"),           True,  250_000),
    ("JPM",  "US46625H1005", "JPMORGAN CHASE",     ("JPM",  "JPMorgan Chase & Co.", 3),                  ("FIN", "Financiero"),           False, 250_000),
    ("KO",   "US1912161007", "COCA-COLA",          ("KO",   "The Coca-Cola Company", 3),                 ("CON", "Consumo Masivo"),       False, 150_000),
    ("XOM",  "US30231G1022", "EXXON MOBIL",        ("XOM",  "Exxon Mobil Corporation", 3),               ("ENE", "Energía"),              False, 200_000),
]

# ticker, ISIN, nombre, gestora (cod, nombre, idPais), tipo de fondo, confirmado, monto objetivo USD
FONDOS = [
    ("SPY", "US78462F1030", "SPDR S&P 500 ETF",              ("SSGA", "State Street Global Advisors", 3), "Renta Variable", True,  500_000),
    ("EPU", "US4642898336", "ISHARES MSCI PERU ETF",         ("BLK",  "BlackRock Fund Advisors", 3),      "Renta Variable", False, 150_000),
    ("AGG", "US4642872265", "ISHARES CORE US AGGREGATE BOND", ("BLK",  "BlackRock Fund Advisors", 3),      "Renta Fija",     True,  400_000),
    ("EMB", "US4642882819", "ISHARES JPM USD EM BOND",       ("BLK",  "BlackRock Fund Advisors", 3),      "Renta Fija",     True,  250_000),
    ("TLT", "US4642874329", "ISHARES 20+ YEAR TREASURY",     ("BLK",  "BlackRock Fund Advisors", 3),      "Renta Fija",     True,  150_000),
    ("LQD", "US4642872422", "ISHARES IBOXX USD IG CORP",     ("BLK",  "BlackRock Fund Advisors", 3),      "Renta Fija",     True,  200_000),
]

# Bonos soberanos que ya existen en el catálogo (ISIN -> cantidad ilustrativa de bonos).
# No hay precio: el vector de precios SBS no tiene API pública y el motor de VaR aún excluye bonos.
BONOS_CANTIDAD = {
    "PEP01000C5D1": 1000, "PEP01000C5F6": 800, "PEP01000C5G4": 1500,
    "PEP01000C5H2": 600, "PEP01000C5I0": 1200, "PEP01000C5J8": 700,
}
