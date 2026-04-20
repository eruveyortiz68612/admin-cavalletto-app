"""Tools para el agente administrativo de Cavalletto (Anthropic tool use).

Expone ANTHROPIC_TOOLS (schemas) y TOOL_HANDLERS (funciones).
Todos los handlers devuelven un dict serializable. Nunca lanzan excepciones:
en caso de error retornan {"error": "mensaje"}.
"""

from datetime import date, datetime, timedelta

from db import query, execute, get_db, fmt_money, current_period


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _err(msg):
    return {"error": str(msg)}


def _ok(**data):
    data.setdefault("ok", True)
    return data


def _valid_mes(mes):
    try:
        datetime.strptime(mes, "%Y-%m")
        return True
    except (ValueError, TypeError):
        return False


def _valid_fecha(fecha):
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Cobranza
# ---------------------------------------------------------------------------

def registrar_familia(**kw):
    try:
        nombre = kw.get("nombre_familia", "").strip()
        contacto = kw.get("contacto_principal", "").strip()
        if not nombre or not contacto:
            return _err("nombre_familia y contacto_principal son obligatorios")
        fid = execute(
            """INSERT INTO familias (nombre_familia, contacto_principal, telefono,
                                     email, direccion, notas, fecha_registro, activo)
               VALUES (?, ?, ?, ?, ?, ?, date('now'), 1)""",
            (nombre, contacto, kw.get("telefono", ""), kw.get("email", ""),
             kw.get("direccion", ""), kw.get("notas", "")),
        )
        return _ok(familia_id=fid, mensaje=f"Familia '{nombre}' registrada con ID {fid}")
    except Exception as e:
        return _err(e)


def listar_familias(**_):
    try:
        rows = query(
            """SELECT id, nombre_familia, contacto_principal, telefono, email, direccion
               FROM familias WHERE activo = 1 ORDER BY nombre_familia"""
        )
        return _ok(familias=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def registrar_alumno(**kw):
    try:
        nombre = kw.get("nombre", "").strip()
        familia_id = kw.get("familia_id")
        if not nombre or not familia_id:
            return _err("nombre y familia_id son obligatorios")
        fam = query("SELECT id FROM familias WHERE id = ? AND activo = 1",
                    (familia_id,), fetchone=True)
        if not fam:
            return _err(f"Familia ID {familia_id} no existe o inactiva")
        aid = execute(
            """INSERT INTO alumnos (nombre, familia_id, grupo, fecha_nacimiento,
                                    fecha_ingreso, estatus, beca_porcentaje, notas)
               VALUES (?, ?, ?, ?, date('now'), 'activo', 0, ?)""",
            (nombre, familia_id, kw.get("grupo", ""), kw.get("fecha_nacimiento"),
             kw.get("notas", "")),
        )
        return _ok(alumno_id=aid, mensaje=f"Alumno '{nombre}' registrado con ID {aid}")
    except Exception as e:
        return _err(e)


def listar_alumnos(**kw):
    try:
        sql = """SELECT a.id, a.nombre, a.familia_id, f.nombre_familia, a.grupo,
                        a.fecha_nacimiento, a.fecha_ingreso, a.beca_porcentaje, a.estatus
                 FROM alumnos a
                 JOIN familias f ON a.familia_id = f.id
                 WHERE a.estatus = 'activo'"""
        params = []
        if kw.get("grupo"):
            sql += " AND a.grupo = ?"
            params.append(kw["grupo"])
        sql += " ORDER BY a.nombre"
        rows = query(sql, params)
        return _ok(alumnos=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def aplicar_beca(**kw):
    try:
        alumno_id = kw.get("alumno_id")
        porcentaje = kw.get("porcentaje")
        if alumno_id is None or porcentaje is None:
            return _err("alumno_id y porcentaje son obligatorios")
        if not 0 <= porcentaje <= 100:
            return _err("porcentaje debe estar entre 0 y 100")
        alumno = query("SELECT id, nombre FROM alumnos WHERE id = ?",
                       (alumno_id,), fetchone=True)
        if not alumno:
            return _err(f"Alumno ID {alumno_id} no existe")
        execute("UPDATE alumnos SET beca_porcentaje = ? WHERE id = ?",
                (porcentaje, alumno_id))
        return _ok(mensaje=f"Beca de {porcentaje}% aplicada a {alumno['nombre']}")
    except Exception as e:
        return _err(e)


def actualizar_concepto(**kw):
    try:
        concepto_id = kw.get("concepto_id")
        monto = kw.get("monto")
        if concepto_id is None or monto is None:
            return _err("concepto_id y monto son obligatorios")
        c = query("SELECT id, nombre FROM conceptos_cobro WHERE id = ?",
                  (concepto_id,), fetchone=True)
        if not c:
            return _err(f"Concepto ID {concepto_id} no existe")
        execute("UPDATE conceptos_cobro SET monto_default = ? WHERE id = ?",
                (monto, concepto_id))
        return _ok(mensaje=f"Concepto '{c['nombre']}' actualizado a {fmt_money(monto)}")
    except Exception as e:
        return _err(e)


def listar_conceptos(**_):
    try:
        rows = query(
            """SELECT id, nombre, monto_default, periodicidad, activo
               FROM conceptos_cobro ORDER BY id"""
        )
        return _ok(conceptos=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def generar_cargos(**kw):
    try:
        mes = kw.get("mes")
        concepto_id = kw.get("concepto_id")
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        if concepto_id is None:
            return _err("concepto_id es obligatorio")
        concepto = query(
            "SELECT id, nombre, monto_default FROM conceptos_cobro WHERE id = ? AND activo = 1",
            (concepto_id,), fetchone=True,
        )
        if not concepto:
            return _err(f"Concepto ID {concepto_id} no existe o inactivo")
        alumnos = query(
            "SELECT id, nombre, beca_porcentaje FROM alumnos WHERE estatus = 'activo'"
        )
        anio, m = mes.split("-")
        fecha_venc = f"{anio}-{m}-10"
        generados = 0
        omitidos = 0
        for al in alumnos:
            existe = query(
                """SELECT id FROM cargos
                   WHERE alumno_id = ? AND concepto_id = ? AND mes_aplicable = ?""",
                (al["id"], concepto["id"], mes), fetchone=True,
            )
            if existe:
                omitidos += 1
                continue
            monto_base = concepto["monto_default"]
            descuento = monto_base * (al["beca_porcentaje"] / 100)
            monto_final = round(monto_base - descuento, 2)
            execute(
                """INSERT INTO cargos (alumno_id, concepto_id, monto, mes_aplicable,
                                       fecha_generacion, fecha_vencimiento, estatus)
                   VALUES (?, ?, ?, ?, date('now'), ?, 'pendiente')""",
                (al["id"], concepto["id"], monto_final, mes, fecha_venc),
            )
            generados += 1
        return _ok(
            mes=mes, concepto=concepto["nombre"],
            generados=generados, omitidos=omitidos,
            mensaje=f"Generados {generados} cargos, omitidos {omitidos} (ya existian)",
        )
    except Exception as e:
        return _err(e)


def listar_cargos(**kw):
    try:
        sql = """SELECT c.id, c.alumno_id, a.nombre AS alumno,
                        c.concepto_id, cc.nombre AS concepto,
                        c.monto, c.mes_aplicable, c.fecha_vencimiento, c.estatus
                 FROM cargos c
                 JOIN alumnos a ON c.alumno_id = a.id
                 JOIN conceptos_cobro cc ON c.concepto_id = cc.id
                 WHERE 1=1"""
        params = []
        if kw.get("mes"):
            sql += " AND c.mes_aplicable = ?"
            params.append(kw["mes"])
        if kw.get("alumno_id"):
            sql += " AND c.alumno_id = ?"
            params.append(kw["alumno_id"])
        if kw.get("estatus"):
            sql += " AND c.estatus = ?"
            params.append(kw["estatus"])
        sql += " ORDER BY c.mes_aplicable DESC, a.nombre"
        rows = query(sql, params)
        return _ok(cargos=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def registrar_pago(**kw):
    try:
        cargo_id = kw.get("cargo_id")
        monto = kw.get("monto")
        fecha = kw.get("fecha")
        metodo = kw.get("metodo_pago")
        if cargo_id is None or monto is None or not fecha or not metodo:
            return _err("cargo_id, monto, fecha y metodo_pago son obligatorios")
        if not _valid_fecha(fecha):
            return _err("fecha invalida, usa formato YYYY-MM-DD")
        metodos_validos = ("transferencia", "efectivo", "tarjeta", "deposito")
        if metodo not in metodos_validos:
            return _err(f"metodo_pago invalido. Opciones: {', '.join(metodos_validos)}")
        cargo = query(
            "SELECT id, monto, estatus FROM cargos WHERE id = ?",
            (cargo_id,), fetchone=True,
        )
        if not cargo:
            return _err(f"Cargo ID {cargo_id} no existe")
        if cargo["estatus"] == "pagado":
            return _err(f"El cargo {cargo_id} ya esta pagado")
        pid = execute(
            """INSERT INTO pagos (cargo_id, monto_pagado, fecha_pago, metodo_pago,
                                  referencia, notas, fecha_registro)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (cargo_id, monto, fecha, metodo,
             kw.get("referencia", ""), kw.get("notas", "")),
        )
        total_row = query(
            "SELECT COALESCE(SUM(monto_pagado), 0) AS total FROM pagos WHERE cargo_id = ?",
            (cargo_id,), fetchone=True,
        )
        total_pagado = total_row["total"]
        nuevo_estatus = "pagado" if total_pagado >= cargo["monto"] else "parcial"
        execute("UPDATE cargos SET estatus = ? WHERE id = ?",
                (nuevo_estatus, cargo_id))
        return _ok(
            pago_id=pid, total_pagado=total_pagado, monto_cargo=cargo["monto"],
            estatus=nuevo_estatus,
            mensaje=f"Pago de {fmt_money(monto)} registrado. Estatus: {nuevo_estatus}",
        )
    except Exception as e:
        return _err(e)


def listar_pagos(**kw):
    try:
        sql = """SELECT p.id, p.cargo_id, a.nombre AS alumno, cc.nombre AS concepto,
                        c.mes_aplicable, p.monto_pagado, p.fecha_pago,
                        p.metodo_pago, p.referencia, p.notas
                 FROM pagos p
                 JOIN cargos c ON p.cargo_id = c.id
                 JOIN alumnos a ON c.alumno_id = a.id
                 JOIN conceptos_cobro cc ON c.concepto_id = cc.id
                 WHERE 1=1"""
        params = []
        if kw.get("mes"):
            sql += " AND c.mes_aplicable = ?"
            params.append(kw["mes"])
        if kw.get("alumno_id"):
            sql += " AND c.alumno_id = ?"
            params.append(kw["alumno_id"])
        sql += " ORDER BY p.fecha_pago DESC"
        rows = query(sql, params)
        return _ok(pagos=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def listar_morosos(**_):
    try:
        fecha_limite = (date.today() - timedelta(days=30)).isoformat()
        rows = query(
            """SELECT c.id AS cargo_id, a.nombre AS alumno, f.nombre_familia,
                      f.telefono, f.email, cc.nombre AS concepto, c.monto,
                      c.mes_aplicable, c.fecha_vencimiento, c.estatus,
                      CAST(julianday('now') - julianday(c.fecha_vencimiento) AS INTEGER) AS dias_atraso
               FROM cargos c
               JOIN alumnos a ON c.alumno_id = a.id
               JOIN familias f ON a.familia_id = f.id
               JOIN conceptos_cobro cc ON c.concepto_id = cc.id
               WHERE c.estatus IN ('pendiente', 'parcial')
                 AND c.fecha_vencimiento < ?
               ORDER BY dias_atraso DESC""",
            (fecha_limite,),
        )
        total_monto = sum(r["monto"] for r in rows)
        return _ok(morosos=rows, total=len(rows), monto_total=total_monto)
    except Exception as e:
        return _err(e)


def estado_cuenta(**kw):
    try:
        familia_id = kw.get("familia_id")
        if familia_id is None:
            return _err("familia_id es obligatorio")
        familia = query(
            """SELECT id, nombre_familia, contacto_principal, telefono, email, direccion
               FROM familias WHERE id = ?""",
            (familia_id,), fetchone=True,
        )
        if not familia:
            return _err(f"Familia ID {familia_id} no existe")
        alumnos = query(
            """SELECT id, nombre, grupo, beca_porcentaje, estatus
               FROM alumnos WHERE familia_id = ?""",
            (familia_id,),
        )
        alumno_ids = [a["id"] for a in alumnos]
        cargos = []
        pagos = []
        if alumno_ids:
            placeholders = ",".join("?" * len(alumno_ids))
            cargos = query(
                f"""SELECT c.id, a.nombre AS alumno, cc.nombre AS concepto,
                           c.monto, c.mes_aplicable, c.fecha_vencimiento, c.estatus
                    FROM cargos c
                    JOIN alumnos a ON c.alumno_id = a.id
                    JOIN conceptos_cobro cc ON c.concepto_id = cc.id
                    WHERE c.alumno_id IN ({placeholders})
                    ORDER BY c.mes_aplicable DESC""",
                alumno_ids,
            )
            pagos = query(
                f"""SELECT p.id, p.cargo_id, a.nombre AS alumno, cc.nombre AS concepto,
                           p.monto_pagado, p.fecha_pago, p.metodo_pago
                    FROM pagos p
                    JOIN cargos c ON p.cargo_id = c.id
                    JOIN alumnos a ON c.alumno_id = a.id
                    JOIN conceptos_cobro cc ON c.concepto_id = cc.id
                    WHERE c.alumno_id IN ({placeholders})
                    ORDER BY p.fecha_pago DESC""",
                alumno_ids,
            )
        total_cargos = sum(c["monto"] for c in cargos)
        total_pagado = sum(p["monto_pagado"] for p in pagos)
        total_pendiente = total_cargos - total_pagado
        return _ok(
            familia=familia, alumnos=alumnos, cargos=cargos, pagos=pagos,
            total_cargos=total_cargos, total_pagado=total_pagado,
            total_pendiente=total_pendiente,
        )
    except Exception as e:
        return _err(e)


def resumen_cobranza(**kw):
    try:
        mes = kw.get("mes")
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        tot = query(
            """SELECT COUNT(*) AS total_cargos, COALESCE(SUM(monto), 0) AS facturado
               FROM cargos WHERE mes_aplicable = ?""",
            (mes,), fetchone=True,
        )
        cob = query(
            """SELECT COALESCE(SUM(p.monto_pagado), 0) AS cobrado
               FROM pagos p JOIN cargos c ON p.cargo_id = c.id
               WHERE c.mes_aplicable = ?""",
            (mes,), fetchone=True,
        )
        facturado = tot["facturado"]
        cobrado = cob["cobrado"]
        pendiente = facturado - cobrado
        pct = round((cobrado / facturado * 100), 1) if facturado > 0 else 0
        por_concepto = query(
            """SELECT cc.nombre AS concepto, COUNT(*) AS cargos,
                      COALESCE(SUM(c.monto), 0) AS total,
                      COALESCE(SUM(CASE WHEN c.estatus = 'pagado' THEN c.monto ELSE 0 END), 0) AS pagado
               FROM cargos c JOIN conceptos_cobro cc ON c.concepto_id = cc.id
               WHERE c.mes_aplicable = ?
               GROUP BY cc.nombre ORDER BY total DESC""",
            (mes,),
        )
        return _ok(
            mes=mes, total_cargos=tot["total_cargos"],
            facturado=facturado, cobrado=cobrado, pendiente=pendiente,
            porcentaje=pct, por_concepto=por_concepto,
        )
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Gastos
# ---------------------------------------------------------------------------

def registrar_gasto(**kw):
    try:
        monto = kw.get("monto")
        categoria_id = kw.get("categoria_id")
        descripcion = kw.get("descripcion", "").strip()
        fecha = kw.get("fecha")
        if monto is None or categoria_id is None or not descripcion or not fecha:
            return _err("monto, categoria_id, descripcion y fecha son obligatorios")
        if monto <= 0:
            return _err("el monto debe ser mayor a cero")
        if not _valid_fecha(fecha):
            return _err("fecha invalida, usa formato YYYY-MM-DD")
        cat = query(
            "SELECT id, nombre FROM categorias_gasto WHERE id = ? AND activo = 1",
            (categoria_id,), fetchone=True,
        )
        if not cat:
            return _err(f"Categoria ID {categoria_id} no existe o inactiva")
        prov_id = kw.get("proveedor_id")
        if prov_id:
            prov = query("SELECT id FROM proveedores WHERE id = ?",
                         (prov_id,), fetchone=True)
            if not prov:
                return _err(f"Proveedor ID {prov_id} no existe")
        gid = execute(
            """INSERT INTO gastos (categoria_id, descripcion, monto, fecha,
                                   proveedor_id, comprobante_ref, registrado_por,
                                   notas, fecha_registro)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, datetime('now'))""",
            (categoria_id, descripcion, monto, fecha, prov_id, kw.get("notas", "")),
        )
        return _ok(gasto_id=gid,
                   mensaje=f"Gasto {fmt_money(monto)} registrado en {cat['nombre']}")
    except Exception as e:
        return _err(e)


def listar_gastos(**kw):
    try:
        sql = """SELECT g.id, g.descripcion, g.monto, c.nombre AS categoria,
                        g.fecha, COALESCE(p.nombre, '') AS proveedor, g.notas
                 FROM gastos g
                 JOIN categorias_gasto c ON c.id = g.categoria_id
                 LEFT JOIN proveedores p ON p.id = g.proveedor_id
                 WHERE 1=1"""
        params = []
        if kw.get("mes"):
            sql += " AND strftime('%Y-%m', g.fecha) = ?"
            params.append(kw["mes"])
        if kw.get("categoria_id"):
            sql += " AND g.categoria_id = ?"
            params.append(kw["categoria_id"])
        sql += " ORDER BY g.fecha DESC"
        rows = query(sql, params)
        total = sum(r["monto"] for r in rows)
        return _ok(gastos=rows, total_registros=len(rows), total_monto=total)
    except Exception as e:
        return _err(e)


def gastos_por_categoria(**kw):
    try:
        mes = kw.get("mes") or current_period()
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        rows = query(
            """SELECT c.id, c.nombre, COALESCE(c.presupuesto_mensual, 0) AS presupuesto,
                      COALESCE(SUM(g.monto), 0) AS gastado
               FROM categorias_gasto c
               LEFT JOIN gastos g ON g.categoria_id = c.id
                    AND strftime('%Y-%m', g.fecha) = ?
               WHERE c.activo = 1
               GROUP BY c.id ORDER BY c.nombre""",
            (mes,),
        )
        data = []
        for r in rows:
            presupuesto = r["presupuesto"] or 0
            gastado = r["gastado"]
            restante = presupuesto - gastado
            pct = round((gastado / presupuesto * 100), 1) if presupuesto > 0 else 0
            data.append({
                "categoria_id": r["id"], "categoria": r["nombre"],
                "presupuesto": presupuesto, "gastado": gastado,
                "restante": restante, "porcentaje": pct,
            })
        return _ok(mes=mes, categorias=data)
    except Exception as e:
        return _err(e)


def registrar_proveedor(**kw):
    try:
        nombre = kw.get("nombre", "").strip()
        if not nombre:
            return _err("nombre es obligatorio")
        pid = execute(
            """INSERT INTO proveedores (nombre, contacto, telefono, rfc, notas)
               VALUES (?, ?, ?, ?, ?)""",
            (nombre, kw.get("contacto", ""), kw.get("telefono", ""),
             kw.get("rfc", ""), kw.get("notas", "")),
        )
        return _ok(proveedor_id=pid, mensaje=f"Proveedor '{nombre}' registrado con ID {pid}")
    except Exception as e:
        return _err(e)


def listar_proveedores(**_):
    try:
        rows = query("SELECT id, nombre, contacto, telefono, rfc, notas FROM proveedores ORDER BY nombre")
        return _ok(proveedores=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def listar_categorias_gasto(**_):
    try:
        rows = query(
            """SELECT id, nombre, COALESCE(presupuesto_mensual, 0) AS presupuesto_mensual, activo
               FROM categorias_gasto ORDER BY nombre"""
        )
        return _ok(categorias=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def actualizar_presupuesto_categoria(**kw):
    try:
        categoria_id = kw.get("categoria_id")
        monto = kw.get("monto")
        if categoria_id is None or monto is None:
            return _err("categoria_id y monto son obligatorios")
        if monto < 0:
            return _err("el monto no puede ser negativo")
        cat = query("SELECT id, nombre FROM categorias_gasto WHERE id = ?",
                    (categoria_id,), fetchone=True)
        if not cat:
            return _err(f"Categoria ID {categoria_id} no existe")
        execute("UPDATE categorias_gasto SET presupuesto_mensual = ? WHERE id = ?",
                (monto, categoria_id))
        return _ok(mensaje=f"Presupuesto de '{cat['nombre']}' actualizado a {fmt_money(monto)}")
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------

_CATEGORIAS_INV = ["didactico", "limpieza", "oficina", "alimentos", "botiquin", "mobiliario"]


def registrar_item(**kw):
    try:
        nombre = kw.get("nombre", "").strip()
        categoria = kw.get("categoria", "").strip()
        if not nombre or not categoria:
            return _err("nombre y categoria son obligatorios")
        if categoria not in _CATEGORIAS_INV:
            return _err(f"categoria invalida. Opciones: {', '.join(_CATEGORIAS_INV)}")
        iid = execute(
            """INSERT INTO items_inventario
               (nombre, categoria, unidad_medida, stock_actual, stock_minimo, ubicacion, notas)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (nombre, categoria, kw.get("unidad_medida", "pieza"),
             kw.get("stock_minimo", 0), kw.get("ubicacion", ""), kw.get("notas", "")),
        )
        return _ok(item_id=iid, mensaje=f"Item '{nombre}' registrado con ID {iid}")
    except Exception as e:
        return _err(e)


def listar_items(**kw):
    try:
        sql = """SELECT id, nombre, categoria, stock_actual, stock_minimo,
                        unidad_medida, ubicacion, notas
                 FROM items_inventario WHERE 1=1"""
        params = []
        if kw.get("categoria"):
            sql += " AND categoria = ?"
            params.append(kw["categoria"])
        sql += " ORDER BY categoria, nombre"
        rows = query(sql, params)
        return _ok(items=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def entrada_inventario(**kw):
    try:
        item_id = kw.get("item_id")
        cantidad = kw.get("cantidad")
        if item_id is None or cantidad is None:
            return _err("item_id y cantidad son obligatorios")
        if cantidad <= 0:
            return _err("la cantidad debe ser mayor a 0")
        item = query(
            "SELECT id, nombre, stock_actual FROM items_inventario WHERE id = ?",
            (item_id,), fetchone=True,
        )
        if not item:
            return _err(f"Item ID {item_id} no existe")
        nuevo = item["stock_actual"] + cantidad
        with get_db() as conn:
            conn.execute(
                "UPDATE items_inventario SET stock_actual = ? WHERE id = ?",
                (nuevo, item_id),
            )
            conn.execute(
                """INSERT INTO movimientos_inventario
                   (item_id, tipo, cantidad, fecha, motivo, registrado_por, fecha_registro)
                   VALUES (?, 'entrada', ?, date('now'), ?, NULL, datetime('now'))""",
                (item_id, cantidad, kw.get("motivo", "")),
            )
        return _ok(
            item_id=item_id, stock_anterior=item["stock_actual"], stock_nuevo=nuevo,
            mensaje=f"Entrada de {cantidad} '{item['nombre']}' ({item['stock_actual']} -> {nuevo})",
        )
    except Exception as e:
        return _err(e)


def salida_inventario(**kw):
    try:
        item_id = kw.get("item_id")
        cantidad = kw.get("cantidad")
        if item_id is None or cantidad is None:
            return _err("item_id y cantidad son obligatorios")
        if cantidad <= 0:
            return _err("la cantidad debe ser mayor a 0")
        item = query(
            "SELECT id, nombre, stock_actual FROM items_inventario WHERE id = ?",
            (item_id,), fetchone=True,
        )
        if not item:
            return _err(f"Item ID {item_id} no existe")
        if item["stock_actual"] < cantidad:
            return _err(
                f"Stock insuficiente. Disponible: {item['stock_actual']}, solicitado: {cantidad}"
            )
        nuevo = item["stock_actual"] - cantidad
        with get_db() as conn:
            conn.execute(
                "UPDATE items_inventario SET stock_actual = ? WHERE id = ?",
                (nuevo, item_id),
            )
            conn.execute(
                """INSERT INTO movimientos_inventario
                   (item_id, tipo, cantidad, fecha, motivo, registrado_por, fecha_registro)
                   VALUES (?, 'salida', ?, date('now'), ?, NULL, datetime('now'))""",
                (item_id, cantidad, kw.get("motivo", "")),
            )
        return _ok(
            item_id=item_id, stock_anterior=item["stock_actual"], stock_nuevo=nuevo,
            mensaje=f"Salida de {cantidad} '{item['nombre']}' ({item['stock_actual']} -> {nuevo})",
        )
    except Exception as e:
        return _err(e)


def items_bajo_stock(**_):
    try:
        rows = query(
            """SELECT id, nombre, categoria, stock_actual, stock_minimo,
                      unidad_medida, ubicacion
               FROM items_inventario
               WHERE stock_actual < stock_minimo
               ORDER BY categoria, nombre"""
        )
        return _ok(items=rows, total=len(rows))
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Nomina
# ---------------------------------------------------------------------------

_TIPOS_CONTRATO = ("nomina", "honorarios", "eventual")


def registrar_empleado(**kw):
    try:
        nombre = kw.get("nombre", "").strip()
        puesto = kw.get("puesto", "").strip()
        salario = kw.get("salario_mensual")
        if not nombre or not puesto or salario is None:
            return _err("nombre, puesto y salario_mensual son obligatorios")
        tipo = kw.get("tipo_contrato", "nomina")
        if tipo not in _TIPOS_CONTRATO:
            return _err(f"tipo_contrato invalido. Opciones: {', '.join(_TIPOS_CONTRATO)}")
        fecha = kw.get("fecha_ingreso") or datetime.now().strftime("%Y-%m-%d")
        if not _valid_fecha(fecha):
            return _err("fecha_ingreso invalida, usa formato YYYY-MM-DD")
        eid = execute(
            """INSERT INTO empleados (nombre, puesto, salario_mensual, tipo_contrato,
                                      fecha_ingreso, estatus, notas)
               VALUES (?, ?, ?, ?, ?, 'activo', ?)""",
            (nombre, puesto, salario, tipo, fecha, kw.get("notas", "")),
        )
        return _ok(empleado_id=eid,
                   mensaje=f"Empleado '{nombre}' ({puesto}) registrado con ID {eid}")
    except Exception as e:
        return _err(e)


def listar_empleados(**_):
    try:
        rows = query(
            """SELECT id, nombre, puesto, tipo_contrato, salario_mensual,
                      fecha_ingreso, estatus, notas
               FROM empleados WHERE estatus = 'activo' ORDER BY nombre"""
        )
        return _ok(empleados=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def registrar_pago_nomina(**kw):
    try:
        empleado_id = kw.get("empleado_id")
        monto = kw.get("monto")
        periodo = kw.get("periodo")
        fecha = kw.get("fecha")
        if empleado_id is None or monto is None or not periodo or not fecha:
            return _err("empleado_id, monto, periodo y fecha son obligatorios")
        if not _valid_mes(periodo):
            return _err("periodo invalido, usa formato YYYY-MM")
        if not _valid_fecha(fecha):
            return _err("fecha invalida, usa formato YYYY-MM-DD")
        emp = query("SELECT id, nombre, estatus FROM empleados WHERE id = ?",
                    (empleado_id,), fetchone=True)
        if not emp:
            return _err(f"Empleado ID {empleado_id} no existe")
        metodo = kw.get("metodo_pago", "transferencia")
        pid = execute(
            """INSERT INTO pagos_nomina (empleado_id, periodo, monto, fecha_pago,
                                          metodo_pago, notas, fecha_registro)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (empleado_id, periodo, monto, fecha, metodo, kw.get("notas", "")),
        )
        return _ok(pago_id=pid,
                   mensaje=f"Pago nomina de {fmt_money(monto)} a {emp['nombre']} ({periodo})")
    except Exception as e:
        return _err(e)


def pendientes_nomina(**kw):
    try:
        periodo = kw.get("periodo") or current_period()
        if not _valid_mes(periodo):
            return _err("periodo invalido, usa formato YYYY-MM")
        rows = query(
            """SELECT e.id, e.nombre, e.puesto, e.salario_mensual
               FROM empleados e
               WHERE e.estatus = 'activo'
                 AND e.id NOT IN (SELECT empleado_id FROM pagos_nomina WHERE periodo = ?)
               ORDER BY e.nombre""",
            (periodo,),
        )
        total = sum(r["salario_mensual"] for r in rows)
        return _ok(periodo=periodo, pendientes=rows, total=len(rows), monto_total=total)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Presupuesto / Reportes financieros
# ---------------------------------------------------------------------------

def _ingresos_reales(mes):
    r = query(
        """SELECT COALESCE(SUM(p.monto_pagado), 0) AS total
           FROM pagos p JOIN cargos c ON c.id = p.cargo_id
           WHERE c.mes_aplicable = ?""",
        (mes,), fetchone=True,
    )
    return r["total"] if r else 0


def _egresos_reales(mes):
    g = query(
        """SELECT COALESCE(SUM(monto), 0) AS total FROM gastos
           WHERE strftime('%Y-%m', fecha) = ?""",
        (mes,), fetchone=True,
    )["total"]
    n = query(
        "SELECT COALESCE(SUM(monto), 0) AS total FROM pagos_nomina WHERE periodo = ?",
        (mes,), fetchone=True,
    )["total"]
    return g, n, g + n


def resumen_financiero(**kw):
    try:
        mes = kw.get("mes") or current_period()
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        presup = query("SELECT * FROM presupuesto_mensual WHERE mes = ?",
                       (mes,), fetchone=True)
        ing_proy = presup["ingresos_proyectados"] if presup else 0
        egr_proy = presup["egresos_proyectados"] if presup else 0
        ing_real = _ingresos_reales(mes)
        gastos_op, nomina, egr_real = _egresos_reales(mes)
        return _ok(
            mes=mes,
            ingresos_proyectados=ing_proy, ingresos_reales=ing_real,
            egresos_proyectados=egr_proy, egresos_reales=egr_real,
            gastos_operativos=gastos_op, nomina=nomina,
            utilidad_proyectada=ing_proy - egr_proy,
            utilidad_real=ing_real - egr_real,
        )
    except Exception as e:
        return _err(e)


def alertas_financieras(**kw):
    try:
        mes = kw.get("mes") or current_period()
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        presup = query("SELECT * FROM presupuesto_mensual WHERE mes = ?",
                       (mes,), fetchone=True)
        alertas = []
        if presup and presup["egresos_proyectados"] > 0:
            _, _, egr_real = _egresos_reales(mes)
            pct = egr_real / presup["egresos_proyectados"] * 100
            if pct > 80:
                alertas.append({
                    "tipo": "EGRESOS",
                    "mensaje": f"Egresos al {pct:.0f}% del presupuesto",
                    "actual": egr_real, "presupuesto": presup["egresos_proyectados"],
                })
        if presup and presup["ingresos_proyectados"] > 0:
            ing_real = _ingresos_reales(mes)
            pct = ing_real / presup["ingresos_proyectados"] * 100
            if pct < 70:
                alertas.append({
                    "tipo": "INGRESOS",
                    "mensaje": f"Ingresos solo al {pct:.0f}% de lo proyectado",
                    "actual": ing_real, "presupuesto": presup["ingresos_proyectados"],
                })
        cats = query(
            """SELECT cg.id, cg.nombre, COALESCE(cg.presupuesto_mensual, 0) AS presupuesto,
                      COALESCE(SUM(g.monto), 0) AS gastado
               FROM categorias_gasto cg
               LEFT JOIN gastos g ON g.categoria_id = cg.id
                    AND strftime('%Y-%m', g.fecha) = ?
               WHERE cg.activo = 1 GROUP BY cg.id""",
            (mes,),
        )
        for c in cats:
            if c["presupuesto"] > 0 and c["gastado"] > c["presupuesto"]:
                exceso = c["gastado"] - c["presupuesto"]
                alertas.append({
                    "tipo": "CATEGORIA",
                    "mensaje": f"{c['nombre']} excede presupuesto (+{fmt_money(exceso)})",
                    "actual": c["gastado"], "presupuesto": c["presupuesto"],
                })
        return _ok(mes=mes, alertas=alertas, total=len(alertas))
    except Exception as e:
        return _err(e)


def proyectar_mes(**kw):
    try:
        mes = kw.get("mes") or current_period()
        if not _valid_mes(mes):
            return _err("mes invalido, usa formato YYYY-MM")
        alumnos = query(
            "SELECT COUNT(*) AS total FROM alumnos WHERE estatus = 'activo'",
            fetchone=True,
        )["total"]
        avg = query(
            """SELECT COALESCE(AVG(monto_default), 0) AS promedio
               FROM conceptos_cobro WHERE periodicidad = 'mensual' AND activo = 1""",
            fetchone=True,
        )["promedio"]
        ing_proy = alumnos * avg
        presup_cat = query(
            "SELECT COALESCE(SUM(presupuesto_mensual), 0) AS total FROM categorias_gasto WHERE activo = 1",
            fetchone=True,
        )["total"]
        nomina = query(
            "SELECT COALESCE(SUM(salario_mensual), 0) AS total FROM empleados WHERE estatus = 'activo'",
            fetchone=True,
        )["total"]
        egr_proy = presup_cat + nomina
        return _ok(
            mes=mes, alumnos_activos=alumnos, colegiatura_promedio=avg,
            ingresos_proyectados=ing_proy,
            presupuesto_categorias=presup_cat, nomina_mensual=nomina,
            egresos_proyectados=egr_proy, utilidad_estimada=ing_proy - egr_proy,
        )
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Reportes (solo signalizan; la generacion real ocurre en doc_generator.py)
# ---------------------------------------------------------------------------

def generar_reporte_mensual_excel(**kw):
    mes = kw.get("mes") or current_period()
    if not _valid_mes(mes):
        return _err("mes invalido, usa formato YYYY-MM")
    return _ok(
        tipo="excel", mes=mes,
        filename=f"reporte_mensual_{mes}.xlsx",
        reporte="mensual",
        mensaje=f"Reporte mensual {mes} generado. Descargalo en el panel lateral.",
    )


def generar_reporte_contador_excel(**kw):
    mes = kw.get("mes") or current_period()
    if not _valid_mes(mes):
        return _err("mes invalido, usa formato YYYY-MM")
    return _ok(
        tipo="excel", mes=mes,
        filename=f"reporte_contador_{mes}.xlsx",
        reporte="contador",
        mensaje=f"Reporte para contador {mes} generado. Descargalo en el panel lateral.",
    )


def generar_recibo_pdf(**kw):
    try:
        pago_id = kw.get("pago_id")
        if pago_id is None:
            return _err("pago_id es obligatorio")
        pago = query(
            """SELECT p.id FROM pagos p WHERE p.id = ?""",
            (pago_id,), fetchone=True,
        )
        if not pago:
            return _err(f"Pago ID {pago_id} no existe")
        return _ok(
            tipo="pdf", pago_id=pago_id,
            filename=f"recibo_{pago_id}.pdf",
            reporte="recibo",
            mensaje=f"Recibo del pago {pago_id} generado. Descargalo en el panel lateral.",
        )
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Fotos de inventario
# ---------------------------------------------------------------------------

def actualizar_foto_item(**kw):
    """Guarda foto (bytes) de un item. Llamado desde app.py (no expuesto a Claude)."""
    try:
        item_id = kw.get("item_id")
        foto_bytes = kw.get("foto_bytes")
        foto_mime = kw.get("foto_mime") or "image/jpeg"
        if item_id is None or not foto_bytes:
            return _err("item_id y foto_bytes son obligatorios")
        row = query("SELECT id, nombre FROM items_inventario WHERE id = ?",
                    (item_id,), fetchone=True)
        if not row:
            return _err(f"Item ID {item_id} no existe")
        execute(
            "UPDATE items_inventario SET foto_blob = ?, foto_mime = ? WHERE id = ?",
            (foto_bytes, foto_mime, item_id),
        )
        return _ok(item_id=item_id, nombre=row["nombre"],
                   mensaje=f"Foto guardada para item '{row['nombre']}' ({len(foto_bytes)} bytes)")
    except Exception as e:
        return _err(e)


def obtener_foto_item(**kw):
    """Recupera la foto de un item. Expuesto a Claude."""
    try:
        item_id = kw.get("item_id")
        if item_id is None:
            return _err("item_id es obligatorio")
        row = query(
            "SELECT id, nombre, foto_blob, foto_mime FROM items_inventario WHERE id = ?",
            (item_id,), fetchone=True,
        )
        if not row:
            return _err(f"Item ID {item_id} no existe")
        if not row.get("foto_blob"):
            return _ok(item_id=item_id, nombre=row["nombre"], tiene_foto=False,
                       mensaje=f"Item '{row['nombre']}' no tiene foto.")
        # Marcamos con tipo=foto para que la UI la renderice; bytes no se
        # devuelven al LLM (solo metadata), la UI los consulta aparte.
        return _ok(
            tipo="foto",
            item_id=item_id,
            nombre=row["nombre"],
            mime=row.get("foto_mime") or "image/jpeg",
            tiene_foto=True,
            mensaje=f"Foto de '{row['nombre']}' disponible en el panel.",
        )
    except Exception as e:
        return _err(e)


def _foto_bytes(item_id):
    """Helper para la UI: bytes crudos de la foto, o None."""
    row = query(
        "SELECT foto_blob, foto_mime FROM items_inventario WHERE id = ?",
        (item_id,), fetchone=True,
    )
    if not row or not row.get("foto_blob"):
        return None
    return row["foto_blob"], row.get("foto_mime") or "image/jpeg"


# ---------------------------------------------------------------------------
# Facturas
# ---------------------------------------------------------------------------

def subir_factura(**kw):
    """Guarda archivo de factura. Llamado desde app.py."""
    try:
        nombre = kw.get("nombre_archivo", "").strip()
        mime = kw.get("mime", "").strip()
        archivo_bytes = kw.get("archivo_bytes")
        notas = kw.get("notas", "")
        if not nombre or not mime or not archivo_bytes:
            return _err("nombre_archivo, mime y archivo_bytes son obligatorios")
        fid = execute(
            """INSERT INTO facturas (nombre_archivo, mime, archivo_blob, notas)
               VALUES (?, ?, ?, ?)""",
            (nombre, mime, archivo_bytes, notas),
        )
        return _ok(factura_id=fid, nombre=nombre,
                   mensaje=f"Factura '{nombre}' subida con ID {fid}")
    except Exception as e:
        return _err(e)


def listar_facturas(**kw):
    try:
        estatus = kw.get("estatus")
        base = ("SELECT id, nombre_archivo, mime, fecha_subida, estatus, "
                "gasto_id, monto_extraido, fecha_extraida, rfc_emisor, notas "
                "FROM facturas")
        if estatus:
            rows = query(base + " WHERE estatus = ? ORDER BY fecha_subida DESC",
                         (estatus,))
        else:
            rows = query(base + " ORDER BY fecha_subida DESC")
        return _ok(facturas=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def obtener_factura(**kw):
    """Metadata de una factura (no devuelve BLOB al LLM)."""
    try:
        fid = kw.get("factura_id")
        if fid is None:
            return _err("factura_id es obligatorio")
        row = query(
            """SELECT id, nombre_archivo, mime, fecha_subida, estatus,
                      gasto_id, monto_extraido, fecha_extraida, rfc_emisor,
                      uuid_fiscal, notas
               FROM facturas WHERE id = ?""",
            (fid,), fetchone=True,
        )
        if not row:
            return _err(f"Factura ID {fid} no existe")
        return _ok(factura=row)
    except Exception as e:
        return _err(e)


def _factura_bytes(factura_id):
    """Helper UI: bytes crudos + mime de una factura."""
    row = query(
        "SELECT archivo_blob, mime FROM facturas WHERE id = ?",
        (factura_id,), fetchone=True,
    )
    if not row or not row.get("archivo_blob"):
        return None
    return row["archivo_blob"], row["mime"]


def procesar_factura(**kw):
    """Registra gasto desde factura y marca la factura como procesada."""
    try:
        fid = kw.get("factura_id")
        monto = kw.get("monto")
        fecha = kw.get("fecha")
        categoria_id = kw.get("categoria_id")
        descripcion = kw.get("descripcion", "").strip() or "Factura"
        rfc_emisor = kw.get("rfc_emisor")
        uuid_fiscal = kw.get("uuid_fiscal")
        proveedor_id = kw.get("proveedor_id")

        if fid is None:
            return _err("factura_id es obligatorio")
        if monto is None or not isinstance(monto, (int, float)) or monto <= 0:
            return _err("monto debe ser numero positivo")
        if not _valid_fecha(fecha):
            return _err("fecha invalida, usa YYYY-MM-DD")
        if categoria_id is None:
            return _err("categoria_id es obligatorio")

        fact = query("SELECT id, nombre_archivo, estatus FROM facturas WHERE id = ?",
                     (fid,), fetchone=True)
        if not fact:
            return _err(f"Factura ID {fid} no existe")
        if fact["estatus"] == "procesada":
            return _err(f"Factura {fid} ya fue procesada (gasto relacionado)")

        cat = query("SELECT id, nombre FROM categorias_gasto WHERE id = ?",
                    (categoria_id,), fetchone=True)
        if not cat:
            return _err(f"Categoria ID {categoria_id} no existe")

        gasto_id = execute(
            """INSERT INTO gastos (categoria_id, descripcion, monto, fecha,
                                    proveedor_id, comprobante_ref, registrado_por)
               VALUES (?, ?, ?, ?, ?, ?, 'factura-ia')""",
            (categoria_id, descripcion, float(monto), fecha, proveedor_id,
             uuid_fiscal or fact["nombre_archivo"]),
        )
        execute(
            """UPDATE facturas
               SET estatus = 'procesada', gasto_id = ?, monto_extraido = ?,
                   fecha_extraida = ?, rfc_emisor = ?, uuid_fiscal = ?
               WHERE id = ?""",
            (gasto_id, float(monto), fecha, rfc_emisor, uuid_fiscal, fid),
        )
        return _ok(
            factura_id=fid, gasto_id=gasto_id, categoria=cat["nombre"],
            monto=float(monto), fecha=fecha,
            mensaje=(f"Factura {fid} procesada → gasto {gasto_id} en "
                     f"categoria '{cat['nombre']}' por {fmt_money(monto)}")
        )
    except Exception as e:
        return _err(e)


def rechazar_factura(**kw):
    try:
        fid = kw.get("factura_id")
        motivo = kw.get("motivo", "").strip()
        if fid is None:
            return _err("factura_id es obligatorio")
        if not motivo:
            return _err("motivo es obligatorio")
        row = query("SELECT id, estatus FROM facturas WHERE id = ?",
                    (fid,), fetchone=True)
        if not row:
            return _err(f"Factura ID {fid} no existe")
        execute(
            "UPDATE facturas SET estatus = 'rechazada', notas = ? WHERE id = ?",
            (motivo, fid),
        )
        return _ok(factura_id=fid, mensaje=f"Factura {fid} rechazada: {motivo}")
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Procesos Administrativos
# ---------------------------------------------------------------------------

import json as _json


def _init_procesos_table():
    """Crea la tabla de procesos si no existe."""
    execute("""
        CREATE TABLE IF NOT EXISTS procesos_administrativos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            objetivo TEXT NOT NULL,
            area TEXT NOT NULL,
            frecuencia TEXT NOT NULL,
            trigger_inicio TEXT,
            responsable_principal TEXT,
            pasos TEXT NOT NULL,
            kpis TEXT,
            excepciones TEXT,
            automatizaciones TEXT,
            documentos_asociados TEXT,
            estatus TEXT DEFAULT 'borrador',
            version INTEGER DEFAULT 1,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notas TEXT
        )
    """)


def crear_proceso(**kw):
    try:
        _init_procesos_table()
        nombre = kw.get("nombre", "").strip()
        objetivo = kw.get("objetivo", "").strip()
        area = kw.get("area", "").strip()
        frecuencia = kw.get("frecuencia", "").strip()
        if not all([nombre, objetivo, area, frecuencia]):
            return _err("nombre, objetivo, area y frecuencia son obligatorios")

        pasos = kw.get("pasos", [])
        if isinstance(pasos, str):
            pasos = _json.loads(pasos)

        kpis = kw.get("kpis", [])
        if isinstance(kpis, str):
            kpis = _json.loads(kpis)

        excepciones = kw.get("excepciones", [])
        if isinstance(excepciones, str):
            excepciones = _json.loads(excepciones)

        automatizaciones = kw.get("automatizaciones", [])
        if isinstance(automatizaciones, str):
            automatizaciones = _json.loads(automatizaciones)

        pid = execute(
            """INSERT INTO procesos_administrativos
               (nombre, objetivo, area, frecuencia, trigger_inicio,
                responsable_principal, pasos, kpis, excepciones,
                automatizaciones, notas)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nombre, objetivo, area, frecuencia,
             kw.get("trigger_inicio", ""),
             kw.get("responsable", ""),
             _json.dumps(pasos, ensure_ascii=False),
             _json.dumps(kpis, ensure_ascii=False),
             _json.dumps(excepciones, ensure_ascii=False),
             _json.dumps(automatizaciones, ensure_ascii=False),
             kw.get("notas", "")),
        )
        return _ok(proceso_id=pid, mensaje=f"Proceso '{nombre}' creado (ID {pid})", pasos_count=len(pasos))
    except Exception as e:
        return _err(e)


def listar_procesos(**kw):
    try:
        _init_procesos_table()
        sql = """SELECT id, nombre, area, frecuencia, responsable_principal,
                        estatus, version, fecha_creacion
                 FROM procesos_administrativos WHERE estatus != 'obsoleto'"""
        params = []
        if kw.get("area"):
            sql += " AND area = ?"
            params.append(kw["area"])
        if kw.get("estatus"):
            sql += " AND estatus = ?"
            params.append(kw["estatus"])
        sql += " ORDER BY area, nombre"
        rows = query(sql, params)
        return _ok(procesos=rows, total=len(rows))
    except Exception as e:
        return _err(e)


def ver_proceso(**kw):
    try:
        _init_procesos_table()
        pid = kw.get("proceso_id")
        if not pid:
            return _err("proceso_id es obligatorio")
        proc = query("SELECT * FROM procesos_administrativos WHERE id = ?",
                     (pid,), fetchone=True)
        if not proc:
            return _err(f"Proceso ID {pid} no encontrado")
        proc["pasos"] = _json.loads(proc["pasos"]) if proc["pasos"] else []
        proc["kpis"] = _json.loads(proc["kpis"]) if proc["kpis"] else []
        proc["excepciones"] = _json.loads(proc["excepciones"]) if proc["excepciones"] else []
        proc["automatizaciones"] = _json.loads(proc["automatizaciones"]) if proc["automatizaciones"] else []
        return _ok(proceso=proc)
    except Exception as e:
        return _err(e)


def editar_proceso(**kw):
    try:
        _init_procesos_table()
        pid = kw.get("proceso_id")
        if not pid:
            return _err("proceso_id es obligatorio")
        proc = query("SELECT id, version FROM procesos_administrativos WHERE id = ?",
                     (pid,), fetchone=True)
        if not proc:
            return _err(f"Proceso ID {pid} no encontrado")

        updates = []
        params = []
        for field in ["nombre", "objetivo", "area", "frecuencia", "trigger_inicio", "notas", "estatus"]:
            if kw.get(field) is not None:
                db_field = "responsable_principal" if field == "responsable" else field
                updates.append(f"{db_field} = ?")
                params.append(kw[field])
        if kw.get("responsable") is not None:
            updates.append("responsable_principal = ?")
            params.append(kw["responsable"])
        for json_field in ["pasos", "kpis", "excepciones", "automatizaciones"]:
            if kw.get(json_field) is not None:
                val = kw[json_field]
                if isinstance(val, (list, dict)):
                    val = _json.dumps(val, ensure_ascii=False)
                updates.append(f"{json_field} = ?")
                params.append(val)

        if not updates:
            return _err("No se especificaron campos a editar")

        updates.append("version = version + 1")
        updates.append("fecha_actualizacion = CURRENT_TIMESTAMP")
        params.append(pid)
        execute(f"UPDATE procesos_administrativos SET {', '.join(updates)} WHERE id = ?", params)
        return _ok(proceso_id=pid, mensaje=f"Proceso ID {pid} actualizado (v{proc['version'] + 1})")
    except Exception as e:
        return _err(e)


def activar_proceso(**kw):
    try:
        _init_procesos_table()
        pid = kw.get("proceso_id")
        estatus = kw.get("estatus", "activo")
        if not pid:
            return _err("proceso_id es obligatorio")
        if estatus not in ("borrador", "activo", "pausado", "obsoleto"):
            return _err("estatus debe ser: borrador, activo, pausado u obsoleto")
        execute(
            "UPDATE procesos_administrativos SET estatus = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?",
            (estatus, pid),
        )
        return _ok(proceso_id=pid, mensaje=f"Proceso ID {pid} marcado como '{estatus}'")
    except Exception as e:
        return _err(e)


def eliminar_proceso(**kw):
    try:
        _init_procesos_table()
        pid = kw.get("proceso_id")
        if not pid:
            return _err("proceso_id es obligatorio")
        execute(
            "UPDATE procesos_administrativos SET estatus = 'obsoleto', fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?",
            (pid,),
        )
        return _ok(proceso_id=pid, mensaje=f"Proceso ID {pid} eliminado (marcado obsoleto)")
    except Exception as e:
        return _err(e)


def exportar_proceso_md(**kw):
    try:
        _init_procesos_table()
        pid = kw.get("proceso_id")
        if not pid:
            return _err("proceso_id es obligatorio")
        proc = query("SELECT * FROM procesos_administrativos WHERE id = ?",
                     (pid,), fetchone=True)
        if not proc:
            return _err(f"Proceso ID {pid} no encontrado")

        pasos = _json.loads(proc["pasos"]) if proc["pasos"] else []
        kpis = _json.loads(proc["kpis"]) if proc["kpis"] else []
        excepciones = _json.loads(proc["excepciones"]) if proc["excepciones"] else []

        md = [f"# {proc['nombre']}", ""]
        md.append(f"**Area:** {proc['area']}  ")
        md.append(f"**Frecuencia:** {proc['frecuencia']}  ")
        md.append(f"**Responsable:** {proc['responsable_principal'] or 'Por asignar'}  ")
        md.append(f"**Estatus:** {proc['estatus']}  ")
        md.append(f"**Version:** {proc['version']}")
        md.extend(["", "## Objetivo", "", proc["objetivo"], ""])

        if proc.get("trigger_inicio"):
            md.extend(["## Trigger de inicio", "", proc["trigger_inicio"], ""])

        if pasos:
            md.extend(["## Pasos del proceso", ""])
            for i, p in enumerate(pasos, 1):
                accion = p.get("accion", p.get("nombre", "Sin nombre"))
                md.append(f"### Paso {i}: {accion}")
                md.append("")
                for k, label in [("responsable", "Responsable"), ("tiempo_estimado", "Tiempo estimado"),
                                 ("herramienta", "Herramienta"), ("entregable", "Entregable"),
                                 ("criterio_exito", "Criterio de exito")]:
                    if p.get(k):
                        md.append(f"- **{label}:** {p[k]}")
                md.append("")

        if kpis:
            md.extend(["## KPIs", "", "| Metrica | Meta | Medicion |", "|---|---|---|"])
            for k in kpis:
                if isinstance(k, dict):
                    md.append(f"| {k.get('nombre', '-')} | {k.get('meta', '-')} | {k.get('medicion', '-')} |")
                else:
                    md.append(f"| {k} | - | - |")
            md.append("")

        if excepciones:
            md.extend(["## Excepciones", ""])
            for e in excepciones:
                if isinstance(e, dict):
                    md.append(f"- **Si** {e.get('condicion', '-')} **entonces** {e.get('accion', '-')}")
                else:
                    md.append(f"- {e}")
            md.append("")

        return _ok(tipo="documento", contenido="\n".join(md), nombre=f"proceso_{proc['nombre'].lower().replace(' ', '_')}.md")
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# TOOL_HANDLERS registry
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    # Cobranza
    "registrar_familia": registrar_familia,
    "listar_familias": listar_familias,
    "registrar_alumno": registrar_alumno,
    "listar_alumnos": listar_alumnos,
    "aplicar_beca": aplicar_beca,
    "actualizar_concepto": actualizar_concepto,
    "listar_conceptos": listar_conceptos,
    "generar_cargos": generar_cargos,
    "listar_cargos": listar_cargos,
    "registrar_pago": registrar_pago,
    "listar_pagos": listar_pagos,
    "listar_morosos": listar_morosos,
    "estado_cuenta": estado_cuenta,
    "resumen_cobranza": resumen_cobranza,
    # Gastos
    "registrar_gasto": registrar_gasto,
    "listar_gastos": listar_gastos,
    "gastos_por_categoria": gastos_por_categoria,
    "registrar_proveedor": registrar_proveedor,
    "listar_proveedores": listar_proveedores,
    "listar_categorias_gasto": listar_categorias_gasto,
    "actualizar_presupuesto_categoria": actualizar_presupuesto_categoria,
    # Inventario
    "registrar_item": registrar_item,
    "listar_items": listar_items,
    "entrada_inventario": entrada_inventario,
    "salida_inventario": salida_inventario,
    "items_bajo_stock": items_bajo_stock,
    # Nomina
    "registrar_empleado": registrar_empleado,
    "listar_empleados": listar_empleados,
    "registrar_pago_nomina": registrar_pago_nomina,
    "pendientes_nomina": pendientes_nomina,
    # Presupuesto
    "resumen_financiero": resumen_financiero,
    "alertas_financieras": alertas_financieras,
    "proyectar_mes": proyectar_mes,
    # Reportes
    "generar_reporte_mensual_excel": generar_reporte_mensual_excel,
    "generar_reporte_contador_excel": generar_reporte_contador_excel,
    "generar_recibo_pdf": generar_recibo_pdf,
    # Fotos / Facturas (los que aceptan bytes se llaman desde la UI)
    "actualizar_foto_item": actualizar_foto_item,
    "obtener_foto_item": obtener_foto_item,
    "subir_factura": subir_factura,
    "listar_facturas": listar_facturas,
    "obtener_factura": obtener_factura,
    "procesar_factura": procesar_factura,
    "rechazar_factura": rechazar_factura,
    # Procesos Administrativos
    "crear_proceso": crear_proceso,
    "listar_procesos": listar_procesos,
    "ver_proceso": ver_proceso,
    "editar_proceso": editar_proceso,
    "activar_proceso": activar_proceso,
    "eliminar_proceso": eliminar_proceso,
    "exportar_proceso_md": exportar_proceso_md,
}


# ---------------------------------------------------------------------------
# ANTHROPIC_TOOLS schemas
# ---------------------------------------------------------------------------

ANTHROPIC_TOOLS = [
    # --- Cobranza ---
    {
        "name": "registrar_familia",
        "description": "Registra una nueva familia en el sistema. Devuelve el ID asignado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_familia": {"type": "string", "description": "Nombre de la familia (ej. 'Familia Pérez')."},
                "contacto_principal": {"type": "string", "description": "Nombre del contacto principal."},
                "telefono": {"type": "string", "description": "Telefono de contacto."},
                "email": {"type": "string", "description": "Correo electronico."},
                "direccion": {"type": "string", "description": "Direccion domiciliaria."},
                "notas": {"type": "string", "description": "Notas adicionales."},
            },
            "required": ["nombre_familia", "contacto_principal"],
        },
    },
    {
        "name": "listar_familias",
        "description": "Lista todas las familias activas con sus datos de contacto.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "registrar_alumno",
        "description": "Registra un nuevo alumno vinculado a una familia. Requiere familia existente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre completo del alumno."},
                "familia_id": {"type": "integer", "description": "ID de la familia a la que pertenece."},
                "grupo": {"type": "string", "description": "Grupo (maternal, kinder1, kinder2, etc.)."},
                "fecha_nacimiento": {"type": "string", "description": "Fecha de nacimiento YYYY-MM-DD."},
                "notas": {"type": "string", "description": "Notas adicionales."},
            },
            "required": ["nombre", "familia_id"],
        },
    },
    {
        "name": "listar_alumnos",
        "description": "Lista alumnos activos, opcionalmente filtrando por grupo. Incluye nombre de familia y % de beca.",
        "input_schema": {
            "type": "object",
            "properties": {
                "grupo": {"type": "string", "description": "Filtrar por grupo (opcional)."},
            },
        },
    },
    {
        "name": "aplicar_beca",
        "description": "Asigna un porcentaje de beca (0-100) a un alumno. Afecta cargos futuros.",
        "input_schema": {
            "type": "object",
            "properties": {
                "alumno_id": {"type": "integer", "description": "ID del alumno."},
                "porcentaje": {"type": "number", "description": "Porcentaje de beca entre 0 y 100."},
            },
            "required": ["alumno_id", "porcentaje"],
        },
    },
    {
        "name": "actualizar_concepto",
        "description": "Actualiza el monto default de un concepto de cobro (ej. colegiatura).",
        "input_schema": {
            "type": "object",
            "properties": {
                "concepto_id": {"type": "integer", "description": "ID del concepto."},
                "monto": {"type": "number", "description": "Nuevo monto default."},
            },
            "required": ["concepto_id", "monto"],
        },
    },
    {
        "name": "listar_conceptos",
        "description": "Lista todos los conceptos de cobro disponibles con sus montos default.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "generar_cargos",
        "description": "Genera cargos mensuales para todos los alumnos activos de un concepto. Aplica descuento de beca y omite duplicados.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes en formato YYYY-MM."},
                "concepto_id": {"type": "integer", "description": "ID del concepto a generar."},
            },
            "required": ["mes", "concepto_id"],
        },
    },
    {
        "name": "listar_cargos",
        "description": "Lista cargos con filtros opcionales por mes, alumno o estatus.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Filtrar por mes YYYY-MM."},
                "alumno_id": {"type": "integer", "description": "Filtrar por alumno."},
                "estatus": {"type": "string", "description": "Filtrar por estatus (pendiente/pagado/parcial)."},
            },
        },
    },
    {
        "name": "registrar_pago",
        "description": "Registra un pago aplicado a un cargo. Actualiza el estatus del cargo a 'pagado' o 'parcial' segun el total acumulado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cargo_id": {"type": "integer", "description": "ID del cargo."},
                "monto": {"type": "number", "description": "Monto pagado."},
                "fecha": {"type": "string", "description": "Fecha del pago YYYY-MM-DD."},
                "metodo_pago": {"type": "string", "description": "Metodo: transferencia, efectivo, tarjeta o deposito."},
                "referencia": {"type": "string", "description": "Referencia o folio de la operacion."},
                "notas": {"type": "string", "description": "Notas adicionales."},
            },
            "required": ["cargo_id", "monto", "fecha", "metodo_pago"],
        },
    },
    {
        "name": "listar_pagos",
        "description": "Lista pagos registrados con filtros opcionales por mes o alumno.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Filtrar por mes YYYY-MM."},
                "alumno_id": {"type": "integer", "description": "Filtrar por alumno."},
            },
        },
    },
    {
        "name": "listar_morosos",
        "description": "Lista familias con cargos pendientes o parciales con mas de 30 dias de atraso.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "estado_cuenta",
        "description": "Estado de cuenta completo de una familia: alumnos, cargos, pagos y totales.",
        "input_schema": {
            "type": "object",
            "properties": {
                "familia_id": {"type": "integer", "description": "ID de la familia."},
            },
            "required": ["familia_id"],
        },
    },
    {
        "name": "resumen_cobranza",
        "description": "Resumen de cobranza de un mes: facturado, cobrado, pendiente y % cobrado, con desglose por concepto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes en formato YYYY-MM."},
            },
            "required": ["mes"],
        },
    },
    # --- Gastos ---
    {
        "name": "registrar_gasto",
        "description": "Registra un gasto operativo con categoria, fecha y monto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": {"type": "number", "description": "Monto del gasto (mayor a 0)."},
                "categoria_id": {"type": "integer", "description": "ID de la categoria de gasto."},
                "descripcion": {"type": "string", "description": "Descripcion del gasto."},
                "fecha": {"type": "string", "description": "Fecha del gasto YYYY-MM-DD."},
                "proveedor_id": {"type": "integer", "description": "ID del proveedor (opcional)."},
                "notas": {"type": "string", "description": "Notas adicionales."},
            },
            "required": ["monto", "categoria_id", "descripcion", "fecha"],
        },
    },
    {
        "name": "listar_gastos",
        "description": "Lista gastos con filtros opcionales por mes y/o categoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM."},
                "categoria_id": {"type": "integer", "description": "ID de la categoria."},
            },
        },
    },
    {
        "name": "gastos_por_categoria",
        "description": "Para el mes indicado, devuelve cada categoria con su presupuesto, gastado, restante y % usado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM."},
            },
            "required": ["mes"],
        },
    },
    {
        "name": "registrar_proveedor",
        "description": "Registra un nuevo proveedor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del proveedor."},
                "contacto": {"type": "string", "description": "Persona de contacto."},
                "telefono": {"type": "string", "description": "Telefono."},
                "rfc": {"type": "string", "description": "RFC fiscal."},
                "notas": {"type": "string", "description": "Notas."},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "listar_proveedores",
        "description": "Lista todos los proveedores registrados.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "listar_categorias_gasto",
        "description": "Lista todas las categorias de gasto con su presupuesto mensual.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "actualizar_presupuesto_categoria",
        "description": "Actualiza el presupuesto mensual asignado a una categoria de gasto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria_id": {"type": "integer", "description": "ID de la categoria."},
                "monto": {"type": "number", "description": "Nuevo presupuesto mensual."},
            },
            "required": ["categoria_id", "monto"],
        },
    },
    # --- Inventario ---
    {
        "name": "registrar_item",
        "description": "Registra un articulo nuevo en inventario (stock inicial 0).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del articulo."},
                "categoria": {"type": "string",
                              "description": "Categoria: didactico, limpieza, oficina, alimentos, botiquin o mobiliario."},
                "unidad_medida": {"type": "string", "description": "Unidad (pieza, kg, litro, etc.)."},
                "stock_minimo": {"type": "integer", "description": "Stock minimo para alerta."},
                "ubicacion": {"type": "string", "description": "Ubicacion fisica."},
                "notas": {"type": "string", "description": "Notas."},
            },
            "required": ["nombre", "categoria"],
        },
    },
    {
        "name": "listar_items",
        "description": "Lista items del inventario, opcionalmente filtrando por categoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "description": "Filtrar por categoria."},
            },
        },
    },
    {
        "name": "entrada_inventario",
        "description": "Registra entrada de stock: suma cantidad al stock actual y crea un movimiento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID del articulo."},
                "cantidad": {"type": "integer", "description": "Cantidad a ingresar (mayor a 0)."},
                "motivo": {"type": "string", "description": "Motivo o referencia."},
            },
            "required": ["item_id", "cantidad"],
        },
    },
    {
        "name": "salida_inventario",
        "description": "Registra salida de stock: valida disponibilidad, resta y crea un movimiento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID del articulo."},
                "cantidad": {"type": "integer", "description": "Cantidad a retirar (mayor a 0)."},
                "motivo": {"type": "string", "description": "Motivo o referencia."},
            },
            "required": ["item_id", "cantidad"],
        },
    },
    {
        "name": "items_bajo_stock",
        "description": "Lista articulos cuyo stock actual esta por debajo del stock minimo.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # --- Nomina ---
    {
        "name": "registrar_empleado",
        "description": "Alta de empleado con puesto, salario y tipo de contrato.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre completo."},
                "puesto": {"type": "string", "description": "Puesto o cargo."},
                "salario_mensual": {"type": "number", "description": "Salario mensual."},
                "tipo_contrato": {"type": "string",
                                   "description": "Tipo: nomina, honorarios o eventual. Default: nomina."},
                "fecha_ingreso": {"type": "string", "description": "Fecha de ingreso YYYY-MM-DD."},
                "notas": {"type": "string", "description": "Notas."},
            },
            "required": ["nombre", "puesto", "salario_mensual"],
        },
    },
    {
        "name": "listar_empleados",
        "description": "Lista empleados activos con puesto, salario y fecha de ingreso.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "registrar_pago_nomina",
        "description": "Registra un pago de nomina a un empleado para un periodo (YYYY-MM).",
        "input_schema": {
            "type": "object",
            "properties": {
                "empleado_id": {"type": "integer", "description": "ID del empleado."},
                "monto": {"type": "number", "description": "Monto pagado."},
                "periodo": {"type": "string", "description": "Periodo YYYY-MM."},
                "fecha": {"type": "string", "description": "Fecha del pago YYYY-MM-DD."},
                "metodo_pago": {"type": "string", "description": "Metodo (transferencia, efectivo, etc.)."},
                "notas": {"type": "string", "description": "Notas."},
            },
            "required": ["empleado_id", "monto", "periodo", "fecha"],
        },
    },
    {
        "name": "pendientes_nomina",
        "description": "Empleados activos sin pago registrado en el periodo indicado (default: actual).",
        "input_schema": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "description": "Periodo YYYY-MM (opcional)."},
            },
        },
    },
    # --- Presupuesto ---
    {
        "name": "resumen_financiero",
        "description": "Resumen mensual: ingresos y egresos proyectados vs reales y utilidad (P&L).",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM (default: actual)."},
            },
        },
    },
    {
        "name": "alertas_financieras",
        "description": "Alertas del mes: egresos > 80% del presupuesto, ingresos < 70% proyectado, categorias sobre presupuesto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM (default: actual)."},
            },
        },
    },
    {
        "name": "proyectar_mes",
        "description": "Proyeccion financiera: ingresos basados en alumnos activos * colegiatura promedio y egresos por categorias + nomina.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM a proyectar."},
            },
        },
    },
    # --- Reportes ---
    {
        "name": "generar_reporte_mensual_excel",
        "description": "Genera reporte mensual en Excel (cobranza, gastos, nomina, P&L). Descargable desde el panel lateral.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM."},
            },
            "required": ["mes"],
        },
    },
    {
        "name": "generar_reporte_contador_excel",
        "description": "Genera reporte para contador en Excel con ingresos, egresos y nomina detallados del mes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mes YYYY-MM."},
            },
            "required": ["mes"],
        },
    },
    {
        "name": "generar_recibo_pdf",
        "description": "Genera recibo en PDF para un pago especifico. Descargable desde el panel lateral.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pago_id": {"type": "integer", "description": "ID del pago."},
            },
            "required": ["pago_id"],
        },
    },
    # --- Fotos de inventario ---
    {
        "name": "obtener_foto_item",
        "description": "Recupera la foto de un item de inventario. Si existe, se mostrara al usuario en el panel lateral.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID del item."},
            },
            "required": ["item_id"],
        },
    },
    # --- Facturas ---
    {
        "name": "listar_facturas",
        "description": "Lista facturas subidas. Filtra por estatus: 'pendiente', 'procesada', 'rechazada'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estatus": {
                    "type": "string",
                    "enum": ["pendiente", "procesada", "rechazada"],
                    "description": "Filtra por estatus (opcional).",
                },
            },
        },
    },
    {
        "name": "obtener_factura",
        "description": "Recupera metadata de una factura especifica por ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "factura_id": {"type": "integer", "description": "ID de la factura."},
            },
            "required": ["factura_id"],
        },
    },
    {
        "name": "procesar_factura",
        "description": (
            "Registra un gasto a partir de una factura ya subida y marca la "
            "factura como procesada. Usa esta tool DESPUES de leer el contenido "
            "del PDF/imagen de la factura y extraer monto, fecha, concepto y RFC."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "factura_id": {"type": "integer", "description": "ID de la factura a procesar."},
                "monto": {"type": "number", "description": "Monto total de la factura (MXN)."},
                "fecha": {"type": "string", "description": "Fecha de la factura en YYYY-MM-DD."},
                "categoria_id": {"type": "integer", "description": "ID de la categoria de gasto. Consulta listar_categorias_gasto si no sabes."},
                "descripcion": {"type": "string", "description": "Descripcion breve del gasto (ej. 'Papel higienico Costco')."},
                "rfc_emisor": {"type": "string", "description": "RFC del emisor (opcional)."},
                "uuid_fiscal": {"type": "string", "description": "UUID fiscal del CFDI (opcional)."},
                "proveedor_id": {"type": "integer", "description": "ID de proveedor si aplica (opcional)."},
            },
            "required": ["factura_id", "monto", "fecha", "categoria_id", "descripcion"],
        },
    },
    {
        "name": "rechazar_factura",
        "description": "Marca una factura como rechazada cuando no se pudo procesar. Requiere motivo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "factura_id": {"type": "integer"},
                "motivo": {"type": "string", "description": "Razon del rechazo."},
            },
            "required": ["factura_id", "motivo"],
        },
    },
    # --- Procesos Administrativos ---
    {
        "name": "crear_proceso",
        "description": (
            "Crea un nuevo proceso administrativo documentado. Usa despues de "
            "guiar al usuario paso a paso en el diseno del proceso. Guarda "
            "nombre, objetivo, area, frecuencia, pasos detallados, KPIs y excepciones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del proceso (ej. 'Cobranza Mensual', 'Inscripcion Nuevo Alumno')."},
                "objetivo": {"type": "string", "description": "Que resultado espera lograr este proceso."},
                "area": {
                    "type": "string",
                    "description": "Area del kinder: cobranza, gastos, inventario, nomina, inscripciones, comunicacion, operaciones.",
                },
                "frecuencia": {
                    "type": "string",
                    "description": "Con que frecuencia se ejecuta: diaria, semanal, quincenal, mensual, bimestral, semestral, anual, por-evento.",
                },
                "trigger_inicio": {"type": "string", "description": "Evento que dispara el proceso (ej. 'Dia 25 del mes anterior', 'Llega solicitud de inscripcion')."},
                "responsable": {"type": "string", "description": "Persona principal responsable del proceso."},
                "pasos": {
                    "type": "array",
                    "description": "Lista ordenada de pasos del proceso.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "accion": {"type": "string", "description": "Que se hace en este paso."},
                            "responsable": {"type": "string", "description": "Quien ejecuta este paso."},
                            "tiempo_estimado": {"type": "string", "description": "Cuanto tarda (ej. '30 min', '1 dia')."},
                            "herramienta": {"type": "string", "description": "Sistema o herramienta usada (ej. 'WhatsApp', 'agente-administrativo', 'Excel')."},
                            "entregable": {"type": "string", "description": "Que produce este paso."},
                            "criterio_exito": {"type": "string", "description": "Como sabes que se hizo bien."},
                        },
                        "required": ["accion"],
                    },
                },
                "kpis": {
                    "type": "array",
                    "description": "Metricas para evaluar si el proceso funciona.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre": {"type": "string"},
                            "meta": {"type": "string"},
                            "medicion": {"type": "string"},
                        },
                    },
                },
                "excepciones": {
                    "type": "array",
                    "description": "Reglas especiales o manejo de casos atipicos.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "condicion": {"type": "string", "description": "Cuando aplica la excepcion."},
                            "accion": {"type": "string", "description": "Que hacer en ese caso."},
                        },
                    },
                },
                "automatizaciones": {
                    "type": "array",
                    "description": "Scripts del agente vinculados a pasos del proceso.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "script": {"type": "string"},
                            "comando": {"type": "string"},
                        },
                    },
                },
                "notas": {"type": "string"},
            },
            "required": ["nombre", "objetivo", "area", "frecuencia", "pasos"],
        },
    },
    {
        "name": "listar_procesos",
        "description": "Lista los procesos administrativos registrados. Filtra opcionalmente por area o estatus.",
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "Filtrar por area (cobranza, gastos, inventario, nomina, inscripciones, comunicacion, operaciones)."},
                "estatus": {"type": "string", "description": "Filtrar por estatus (borrador, activo, pausado)."},
            },
            "required": [],
        },
    },
    {
        "name": "ver_proceso",
        "description": "Muestra el detalle completo de un proceso: pasos, KPIs, excepciones, automatizaciones.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proceso_id": {"type": "integer", "description": "ID del proceso a consultar."},
            },
            "required": ["proceso_id"],
        },
    },
    {
        "name": "editar_proceso",
        "description": "Edita campos de un proceso existente. Solo enviar los campos que cambian.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proceso_id": {"type": "integer", "description": "ID del proceso a editar."},
                "nombre": {"type": "string"},
                "objetivo": {"type": "string"},
                "area": {"type": "string"},
                "frecuencia": {"type": "string"},
                "trigger_inicio": {"type": "string"},
                "responsable": {"type": "string"},
                "pasos": {"type": "array", "items": {"type": "object"}},
                "kpis": {"type": "array", "items": {"type": "object"}},
                "excepciones": {"type": "array", "items": {"type": "object"}},
                "automatizaciones": {"type": "array", "items": {"type": "object"}},
                "notas": {"type": "string"},
                "estatus": {"type": "string"},
            },
            "required": ["proceso_id"],
        },
    },
    {
        "name": "activar_proceso",
        "description": "Cambia el estatus de un proceso (borrador, activo, pausado, obsoleto).",
        "input_schema": {
            "type": "object",
            "properties": {
                "proceso_id": {"type": "integer"},
                "estatus": {"type": "string", "description": "Nuevo estatus: borrador, activo, pausado, obsoleto."},
            },
            "required": ["proceso_id", "estatus"],
        },
    },
    {
        "name": "eliminar_proceso",
        "description": "Marca un proceso como obsoleto (eliminacion suave). Confirmar con el usuario antes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proceso_id": {"type": "integer"},
            },
            "required": ["proceso_id"],
        },
    },
    {
        "name": "exportar_proceso_md",
        "description": "Exporta un proceso como documento Markdown estructurado para compartir con el equipo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proceso_id": {"type": "integer"},
            },
            "required": ["proceso_id"],
        },
    },
]
