"""Cliente del backend MathRisk. Todo pasa por su API (nada de SQL directo): así se respeta el
mapeo de entidades y no hay que seguir el esquema de tablas."""
from __future__ import annotations

import os

import requests


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, base: str = "http://127.0.0.1:8080"):
        self.raiz = base.rstrip("/")
        self.base = self.raiz + "/mantenedores"
        self.s = requests.Session()
        self._iniciar_sesion()

    @staticmethod
    def _cargar_env() -> None:
        """Lee API_USER/API_PASSWORD de un archivo .env (fuera de git) si no vienen ya del entorno."""
        for ruta in (os.path.join(os.getcwd(), ".env"), os.path.join(os.path.dirname(__file__), "..", ".env")):
            if os.path.exists(ruta):
                for linea in open(ruta, encoding="utf-8"):
                    k, _, v = linea.strip().partition("=")
                    if k and v and k not in os.environ:
                        os.environ[k] = v
                return

    def _iniciar_sesion(self) -> None:
        """El backend exige JWT. Credenciales por variables de entorno API_USER / API_PASSWORD (nunca en el codigo)."""
        self._cargar_env()
        usuario, clave = os.environ.get("API_USER"), os.environ.get("API_PASSWORD")
        if not usuario or not clave:
            raise BackendError("Defina las variables de entorno API_USER y API_PASSWORD (usuario con permiso de escritura "
                               "en mantenedores, p. ej. rol 'Analista de riesgos').")
        r = self.s.post(f"{self.raiz}/auth/login", json={"username": usuario, "password": clave}, timeout=120)
        if not r.ok:
            raise BackendError(f"Login rechazado ({r.status_code}): {r.json().get('message', r.text[:150])}")
        cuerpo = r.json()
        if cuerpo["usuario"].get("debeCambiarClave"):
            raise BackendError("El usuario del proceso debe cambiar su clave temporal antes de usarse (ingrese una vez a la app).")
        self.s.headers["Authorization"] = "Bearer " + cuerpo["token"]

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
