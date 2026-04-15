"""Abstraccion de conexion a DB para la app administrativa.

Soporta dos backends:
- local: SQLite local en ../agente-administrativo/db/cavalletto_admin.db
- cloud: Turso/libSQL via HTTP pipeline API (hrana-http v2, sin websockets
  ni asyncio — solo `requests`, funciona out-of-the-box en Windows y
  Streamlit Cloud sin compilacion nativa).

Seleccion via env var DB_BACKEND (default: auto) o presencia de TURSO_URL
en st.secrets cuando corre dentro de Streamlit.
"""

import os
import sqlite3
from base64 import b64decode, b64encode
from contextlib import contextmanager
from pathlib import Path

_BASE = Path(__file__).parent
DEFAULT_LOCAL_DB = (_BASE.parent / "agente-administrativo" / "db" / "cavalletto_admin.db").resolve()
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", str(DEFAULT_LOCAL_DB))


def _get_secret(key):
    """Lee un secret desde st.secrets si disponible, si no de env var."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def _resolve_backend():
    """Determina el backend efectivo segun env y secrets."""
    explicit = os.environ.get("DB_BACKEND")
    if explicit:
        return explicit
    if _get_secret("TURSO_URL") and _get_secret("TURSO_TOKEN"):
        return "cloud"
    return "local"


def _normalize_turso_url(url):
    """Acepta libsql:// o https:// y retorna base https sin trailing slash."""
    if not url:
        return url
    url = url.rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


@contextmanager
def get_db():
    """Context manager para conexion a la DB."""
    backend = _resolve_backend()
    if backend == "local":
        conn = sqlite3.connect(LOCAL_DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    elif backend == "cloud":
        url = _normalize_turso_url(_get_secret("TURSO_URL"))
        token = _get_secret("TURSO_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "DB_BACKEND=cloud pero faltan TURSO_URL/TURSO_TOKEN en secrets o env."
            )
        conn = _TursoHTTPConnection(url, token)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        raise ValueError(f"DB_BACKEND desconocido: {backend}")


# ---------------------------------------------------------------------------
# Turso HTTP pipeline client — minimal, sync, pure requests.
# ---------------------------------------------------------------------------

class _TursoHTTPConnection:
    """Conexion Turso via HTTP pipeline API (hrana-http v2).

    Agrupa statements en una transaccion logica: acumula SQL y lo envia en un
    pipeline cuando se llama commit(). PRAGMA foreign_keys=ON se envia al
    inicio. lastrowid del ultimo INSERT se captura por cada execute().
    """

    def __init__(self, url, token):
        import requests  # lazy
        self._requests = requests
        self._url = url + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._closed = False
        # Turso aplica cada statement autocommit si no abrimos transaccion.
        # Para simplicidad, cada execute() hace roundtrip (ok para la carga
        # esperada: pocos writes por turno del agente).

    def _pipeline(self, requests_list):
        r = self._requests.post(
            self._url,
            headers=self._headers,
            json={"requests": requests_list},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        # Validar que todas las responses sean ok
        for i, res in enumerate(results):
            if res.get("type") != "ok":
                err = res.get("error", {}) if isinstance(res, dict) else {}
                msg = err.get("message", str(res))
                raise RuntimeError(f"Turso error (req {i}): {msg}")
        return results

    def execute(self, sql, params=()):
        args = [_encode_param(p) for p in (params or [])]
        results = self._pipeline([
            {"type": "execute", "stmt": {"sql": sql, "args": args}},
            {"type": "close"},
        ])
        result = results[0]["response"]["result"]
        return _TursoCursor(result)

    def executescript(self, script):
        """Ejecuta multiples statements separados por ';' (best effort)."""
        statements = [s.strip() for s in script.split(";") if s.strip()]
        requests_list = [
            {"type": "execute", "stmt": {"sql": s}} for s in statements
        ]
        requests_list.append({"type": "close"})
        self._pipeline(requests_list)

    def commit(self):
        # Con autocommit no hay transaccion explicita; noop.
        pass

    def rollback(self):
        pass

    def close(self):
        self._closed = True


def _encode_param(value):
    """Convierte un valor Python al formato hrana-http arg."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"type": "blob", "base64": b64encode(raw).decode("ascii")}
    # str y demas
    return {"type": "text", "value": str(value)}


def _decode_value(cell):
    """Convierte celda hrana-http a valor Python."""
    t = cell.get("type")
    v = cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    if t == "float":
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    if t == "text":
        return v
    if t == "blob":
        b64 = cell.get("base64") or v
        if b64 is None:
            return None
        return b64decode(b64)
    return v


class _TursoCursor:
    """Cursor-like sobre el result set de un pipeline execute."""

    def __init__(self, result):
        self._cols = [c.get("name") for c in (result.get("cols") or [])]
        self._rows = [
            _DictRow(self._cols, tuple(_decode_value(c) for c in row))
            for row in (result.get("rows") or [])
        ]
        self._idx = 0
        lid = result.get("last_insert_rowid")
        self.lastrowid = int(lid) if lid not in (None, "") else None

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        r = self._rows[self._idx]
        self._idx += 1
        return r

    def fetchall(self):
        rest = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rest

    def __iter__(self):
        while self._idx < len(self._rows):
            yield self._rows[self._idx]
            self._idx += 1


class _DictRow:
    """Fila dict-compatible y acceso por indice/clave (imita sqlite3.Row)."""

    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals
        self._map = dict(zip(cols, vals))

    def keys(self):
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def get(self, key, default=None):
        return self._map.get(key, default)

    def __contains__(self, key):
        return key in self._map


def query(sql, params=(), fetchone=False):
    """Ejecuta SELECT y retorna dict(s)."""
    with get_db() as conn:
        cursor = conn.execute(sql, params)
        if fetchone:
            row = cursor.fetchone()
            return dict(row) if row else None
        return [dict(row) for row in cursor.fetchall()]


def execute(sql, params=()):
    """Ejecuta INSERT/UPDATE/DELETE. Retorna lastrowid."""
    with get_db() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid


def db_exists():
    """Verifica que la DB existe (para local) o que hay credenciales (cloud)."""
    backend = _resolve_backend()
    if backend == "local":
        return Path(LOCAL_DB_PATH).exists()
    if backend == "cloud":
        return bool(_get_secret("TURSO_URL") and _get_secret("TURSO_TOKEN"))
    return False


def db_info():
    """Info de la DB para mostrar al usuario."""
    backend = _resolve_backend()
    info = {"backend": backend}
    if backend == "local":
        info["path"] = LOCAL_DB_PATH
        info["exists"] = Path(LOCAL_DB_PATH).exists()
    elif backend == "cloud":
        url = _get_secret("TURSO_URL") or ""
        info["url"] = url.split("@")[0] if "@" in url else url
        info["exists"] = bool(url)
    return info


def fmt_money(amount):
    """Formatea cantidad como MXN."""
    if amount is None:
        return "$0.00"
    return f"${amount:,.2f}"


def current_period():
    """Retorna periodo actual YYYY-MM."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")


# Export alias backward compat
DB_BACKEND = _resolve_backend()
