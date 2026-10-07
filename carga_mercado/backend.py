"""Cliente del backend MathRisk. Todo pasa por su API (nada de SQL directo): así se respeta el
mapeo de entidades y no hay que seguir el esquema de tablas."""
from __future__ import annotations

import requests


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, base: str = "http://127.0.0.1:8080"):
        self.base = base.rstrip("/") + "/mantenedores"
        self.s = requests.Session()

    def listar(self, ruta: str, **params) -> list[dict]:
        r = self.s.get(f"{self.base}/{ruta}/list", params=params or None, timeout=120)
        if not r.ok:
            raise BackendError(f"GET {ruta}/list -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def crear(self, ruta: str, cuerpo) -> dict | list:
        r = self.s.post(f"{self.base}/{ruta}", json=cuerpo, timeout=120)
        if not r.ok:
            raise BackendError(f"POST {ruta} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def obtener_o_crear(self, lista: str, crear: str, coincide, cuerpo: dict) -> dict:
        for fila in self.listar(lista):
            if coincide(fila):
                return fila
        return self.crear(crear, cuerpo)
