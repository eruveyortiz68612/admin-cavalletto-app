# Agente Administrativo Cavalletto — App Streamlit

App web para operar cobranza, gastos, inventario, nómina, reportes y
procesamiento de facturas con IA, sobre una DB compartida en la nube
(Turso/libSQL) o local (SQLite).

## Arquitectura

- **Frontend:** Streamlit (chat + sidebar con KPIs).
- **LLM:** Anthropic Claude Sonnet 4.5 con tool use (36+ tools).
- **DB:** SQLite local (desarrollo) o Turso (producción).
- **Archivos:** Fotos de inventario y facturas guardadas como BLOB en la DB.

## Setup en Streamlit Cloud + Turso (producción)

### 1. Crear la DB en Turso

```bash
# Instalar Turso CLI (una vez)
curl -sSfL https://get.tur.so/install.sh | bash

# Login y crear DB
turso auth login
turso db create cavalletto-admin

# Obtener credenciales
turso db show cavalletto-admin --url       # → copia como TURSO_URL
turso db tokens create cavalletto-admin    # → copia como TURSO_TOKEN
```

### 2. Migrar schema y datos iniciales

```bash
pip install -r requirements.txt

python migrate_to_turso.py \
  --url libsql://cavalletto-admin-<usuario>.turso.io \
  --token <TURSO_TOKEN> \
  --copy-from ../agente-administrativo/db/cavalletto_admin.db
```

Aplica `schema.sql`, añade columnas `foto_blob` + `foto_mime` a
`items_inventario` y crea la tabla `facturas`. Si pasas `--copy-from`, vuelca
catálogos (conceptos, categorías, empleados, familias…) desde la SQLite
local. Añade `--with-history` si quieres también copiar cargos/pagos/gastos.

### 3. Probar localmente contra Turso

Crea `.streamlit/secrets.toml` (ya está en `.gitignore`):

```toml
TURSO_URL = "libsql://cavalletto-admin-xxxxx.turso.io"
TURSO_TOKEN = "eyJhbG..."
```

Corre:
```bash
streamlit run app.py
```

Los KPIs del sidebar deben leerse desde Turso (no de la SQLite local).

### 4. Deploy a Streamlit Cloud

1. Push del folder `admin-cavalletto-app/` a un repo de GitHub.
2. En [share.streamlit.io](https://share.streamlit.io) → **New app** →
   selecciona el repo y la rama.
3. **Main file path:** `app.py`.
4. **Advanced settings → Secrets** → pega en formato TOML:
   ```toml
   TURSO_URL = "libsql://cavalletto-admin-xxxxx.turso.io"
   TURSO_TOKEN = "eyJhbG..."
   ```
5. Deploy. La URL pública se comparte con dueñas y directora. Cada una
   usa su propia API key de Anthropic (BYO, se pega en el sidebar).

## Modo local (sin Turso)

Sin `TURSO_URL` en secrets, la app usa la SQLite de
`../agente-administrativo/db/cavalletto_admin.db` automáticamente.

```bash
streamlit run app.py
```

Útil para desarrollo o uso offline single-user.

## Uso

1. Ingresa tu API key de Anthropic en el sidebar y pulsa **Verificar**.
2. El chat acepta lenguaje natural:
   - "Registra pago de Sofía por $5,000 por transferencia"
   - "¿Quién debe este mes?"
   - "Genera reporte mensual de 2026-04"
3. **Fotos de inventario:** panel "📸 Foto de item" → sube JPG/PNG
   y el ID del item.
4. **Facturas con IA:** panel "📄 Subir factura (IA)" → sube PDF o
   imagen, escribe "procésala" en el chat y Claude extrae monto, fecha,
   RFC y registra el gasto automáticamente.

## Costos

- **Turso:** tier gratis (500 DBs, 9 GB, 1B rows) — cubre años de operación.
- **Streamlit Cloud:** gratis para repos públicos.
- **Anthropic:** BYO key. Una factura procesada ≈ $0.01–0.03 USD.

## Estructura

```
admin-cavalletto-app/
├── app.py                    UI (chat + sidebar)
├── system_prompt.py          Prompt del agente
├── tools.py                  43 tools (36 Anthropic + 2 internos + 5 facturas/fotos)
├── db.py                     Abstracción local/cloud
├── kpis.py                   Queries para dashboard
├── doc_generator.py          Excel/PDF en memoria
├── migrate_to_turso.py       Script one-shot
├── requirements.txt
├── .streamlit/config.toml
├── .streamlit/secrets.toml.example
└── .gitignore
```
