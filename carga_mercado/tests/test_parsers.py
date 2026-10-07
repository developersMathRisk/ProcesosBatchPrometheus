import unittest
from datetime import date

from carga_mercado.fuentes import FuenteError, fecha_bcrp
from carga_mercado.instrumentos import ACCIONES, BONOS_CANTIDAD, FONDOS
from carga_mercado.isin import isin_valido


class FechaBcrp(unittest.TestCase):
    def test_formato_oficial(self):
        self.assertEqual(fecha_bcrp("29.Set.26"), date(2026, 9, 29))
        self.assertEqual(fecha_bcrp("01.Oct.26"), date(2026, 10, 1))
        self.assertEqual(fecha_bcrp("5.Ene.24"), date(2024, 1, 5))

    def test_mes_desconocido(self):
        with self.assertRaises(FuenteError):
            fecha_bcrp("29.Xyz.26")


class Isin(unittest.TestCase):
    def test_validos_y_invalidos(self):
        self.assertTrue(isin_valido("US0378331005"))   # Apple
        self.assertTrue(isin_valido("BMG2519Y1084"))   # Credicorp
        self.assertFalse(isin_valido("US2253943057"))  # el ISIN de BAP que hay en el catálogo
        self.assertFalse(isin_valido("PEF001RF01"))    # largo incorrecto

    def test_todo_el_catalogo_pasa_el_checksum(self):
        for fila in ACCIONES + FONDOS:
            self.assertTrue(isin_valido(fila[1]), fila[0])
        for isin in BONOS_CANTIDAD:
            self.assertTrue(isin_valido(isin), isin)


if __name__ == "__main__":
    unittest.main()
