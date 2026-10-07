import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from carga_mercado.curvas import ErrorCurva, leer_archivo


def _csv(nombre: str, texto: str) -> Path:
    ruta = Path(tempfile.mkdtemp()) / nombre
    ruta.write_text(texto, encoding="utf-8")
    return ruta


class LeerArchivo(unittest.TestCase):
    def test_csv_estilo_sbs_con_coma_decimal(self):
        df = leer_archivo(_csv("x.csv", "Fecha de Proceso;Tipo de Curva;Plazo (DIAS);Tasas (%)\n"
                                        "2/01/2018;CBCRS;90;3,03378\n2/01/2018;CBCRS;180;3,00506\n"))
        self.assertEqual(list(df["curva"].unique()), ["CBCRS"])
        self.assertEqual(df["fecha"].iloc[0], date(2018, 1, 2))
        self.assertAlmostEqual(df["tasa"].iloc[0], 3.03378)

    def test_codigo_desde_nombre_de_archivo(self):
        df = leer_archivo(_csv("CCPSS_2024.csv", "Fecha;Plazo;Tasa\n02/01/2024;90;5,1\n"))
        self.assertEqual(df["curva"].iloc[0], "CCPSS")

    def test_parquet_del_motor_anterior_por_posicion(self):
        ruta = Path(tempfile.mkdtemp()) / "CCPEDS_2020.parquet"
        pd.DataFrame({"Sec.": [1], "Fecha de Proceso": [pd.Timestamp("2020-04-01")],
                      "Periodo (d\ufffdas)": [90], "Tasas (%)": [1.135]}).to_parquet(ruta)
        df = leer_archivo(ruta)
        self.assertEqual((df["curva"].iloc[0], int(df["plazo"].iloc[0])), ("CCPEDS", 90))

    def test_rechaza_curva_desconocida_y_tasas_en_escala_equivocada(self):
        with self.assertRaises(ErrorCurva):
            leer_archivo(_csv("ZZZ.csv", "Fecha;Plazo;Tasa\n02/01/2024;90;5\n"))
        with self.assertRaises(ErrorCurva):
            leer_archivo(_csv("CCPSS.csv", "Fecha;Plazo;Tasa\n02/01/2024;90;500\n"))

    def test_duplicados_se_descartan(self):
        df = leer_archivo(_csv("CCPSS.csv", "Fecha;Plazo;Tasa\n02/01/2024;90;5\n02/01/2024;90;5\n"))
        self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main()
