"""KPIs para el dashboard lateral del agente administrativo de Cavalletto.

Cada funcion es robusta ante datos vacios: retorna ceros/listas vacias
en vez de lanzar excepciones.
"""

from datetime import date, timedelta

from db import query, current_period


def _safe_query(sql, params=(), fetchone=False, default=None):
    try:
        return query(sql, params, fetchone=fetchone)
    except Exception:
        return default if default is not None else ([] if not fetchone else {})


def kpi_cobranza_mes(mes=None):
    """Facturado, cobrado y pendiente del mes."""
    mes = mes or current_period()
    try:
        fac = query(
            "SELECT COALESCE(SUM(monto), 0) AS total FROM cargos WHERE mes_aplicable = ?",
            (mes,), fetchone=True,
        )
        cob = query(
            """SELECT COALESCE(SUM(p.monto_pagado), 0) AS total
               FROM pagos p JOIN cargos c ON c.id = p.cargo_id
               WHERE c.mes_aplicable = ?""",
            (mes,), fetchone=True,
        )
        facturado = (fac or {}).get("total", 0) or 0
        cobrado = (cob or {}).get("total", 0) or 0
        pendiente = facturado - cobrado
        pct = round((cobrado / facturado * 100), 1) if facturado > 0 else 0.0
        return {
            "mes": mes,
            "facturado": facturado,
            "cobrado": cobrado,
            "pendiente": pendiente,
            "porcentaje": pct,
        }
    except Exception:
        return {"mes": mes, "facturado": 0, "cobrado": 0, "pendiente": 0, "porcentaje": 0.0}


def kpi_morosos_count():
    """Cantidad y monto de cargos morosos (> 30 dias)."""
    try:
        fecha_limite = (date.today() - timedelta(days=30)).isoformat()
        rows = query(
            """SELECT COUNT(*) AS count, COALESCE(SUM(monto), 0) AS total
               FROM cargos
               WHERE estatus IN ('pendiente', 'parcial')
                 AND fecha_vencimiento < ?""",
            (fecha_limite,), fetchone=True,
        )
        if not rows:
            return {"count": 0, "monto_total": 0}
        return {
            "count": rows.get("count", 0) or 0,
            "monto_total": rows.get("total", 0) or 0,
        }
    except Exception:
        return {"count": 0, "monto_total": 0}


def kpi_gastos_mes(mes=None):
    """Total gastado en el mes vs presupuesto total."""
    mes = mes or current_period()
    try:
        g = query(
            """SELECT COALESCE(SUM(monto), 0) AS total FROM gastos
               WHERE strftime('%Y-%m', fecha) = ?""",
            (mes,), fetchone=True,
        )
        p = query(
            """SELECT COALESCE(SUM(presupuesto_mensual), 0) AS total
               FROM categorias_gasto WHERE activo = 1""",
            fetchone=True,
        )
        gastado = (g or {}).get("total", 0) or 0
        presupuesto = (p or {}).get("total", 0) or 0
        pct = round((gastado / presupuesto * 100), 1) if presupuesto > 0 else 0.0
        return {
            "mes": mes,
            "gastado": gastado,
            "presupuesto_total": presupuesto,
            "porcentaje": pct,
        }
    except Exception:
        return {"mes": mes, "gastado": 0, "presupuesto_total": 0, "porcentaje": 0.0}


def kpi_stock_bajo():
    """Articulos bajo el stock minimo."""
    try:
        rows = query(
            """SELECT nombre FROM items_inventario
               WHERE stock_actual < stock_minimo
               ORDER BY categoria, nombre"""
        )
        nombres = [r["nombre"] for r in rows]
        return {"count": len(nombres), "items": nombres}
    except Exception:
        return {"count": 0, "items": []}


def _ingresos_reales(mes):
    try:
        r = query(
            """SELECT COALESCE(SUM(p.monto_pagado), 0) AS total
               FROM pagos p JOIN cargos c ON c.id = p.cargo_id
               WHERE c.mes_aplicable = ?""",
            (mes,), fetchone=True,
        )
        return (r or {}).get("total", 0) or 0
    except Exception:
        return 0


def _egresos_reales(mes):
    try:
        g = query(
            """SELECT COALESCE(SUM(monto), 0) AS total FROM gastos
               WHERE strftime('%Y-%m', fecha) = ?""",
            (mes,), fetchone=True,
        )
        n = query(
            "SELECT COALESCE(SUM(monto), 0) AS total FROM pagos_nomina WHERE periodo = ?",
            (mes,), fetchone=True,
        )
        return ((g or {}).get("total", 0) or 0) + ((n or {}).get("total", 0) or 0)
    except Exception:
        return 0


def kpi_alertas(mes=None):
    """Lista de alertas financieras activas del mes."""
    mes = mes or current_period()
    alertas = []
    try:
        presup = query(
            "SELECT * FROM presupuesto_mensual WHERE mes = ?",
            (mes,), fetchone=True,
        )
        if presup and (presup.get("egresos_proyectados") or 0) > 0:
            egr = _egresos_reales(mes)
            pct = egr / presup["egresos_proyectados"] * 100
            if pct > 80:
                alertas.append(f"Egresos al {pct:.0f}% del presupuesto mensual")
        if presup and (presup.get("ingresos_proyectados") or 0) > 0:
            ing = _ingresos_reales(mes)
            pct = ing / presup["ingresos_proyectados"] * 100
            if pct < 70:
                alertas.append(f"Ingresos solo al {pct:.0f}% de lo proyectado")
        cats = query(
            """SELECT cg.nombre, COALESCE(cg.presupuesto_mensual, 0) AS presupuesto,
                      COALESCE(SUM(g.monto), 0) AS gastado
               FROM categorias_gasto cg
               LEFT JOIN gastos g ON g.categoria_id = cg.id
                    AND strftime('%Y-%m', g.fecha) = ?
               WHERE cg.activo = 1
               GROUP BY cg.id""",
            (mes,),
        ) or []
        for c in cats:
            if (c.get("presupuesto") or 0) > 0 and (c.get("gastado") or 0) > c["presupuesto"]:
                alertas.append(f"Categoria '{c['nombre']}' excedio su presupuesto")

        morosos = kpi_morosos_count()
        if morosos["count"] > 0:
            alertas.append(f"{morosos['count']} cargos morosos (>30 dias)")

        stock = kpi_stock_bajo()
        if stock["count"] > 0:
            alertas.append(f"{stock['count']} articulos con stock bajo")
    except Exception:
        pass
    return {"alertas": alertas}


def kpi_utilidad_mes(mes=None):
    """Ingresos, egresos y utilidad del mes."""
    mes = mes or current_period()
    try:
        ingresos = _ingresos_reales(mes)
        egresos = _egresos_reales(mes)
        return {
            "mes": mes,
            "ingresos": ingresos,
            "egresos": egresos,
            "utilidad": ingresos - egresos,
        }
    except Exception:
        return {"mes": mes, "ingresos": 0, "egresos": 0, "utilidad": 0}
