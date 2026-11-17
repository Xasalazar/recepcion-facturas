#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bandeja de recepcion: sube PDF y corre el mismo flujo que procesar.py."""
import os
import tempfile

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from procesar import (
    MUESTRAS,
    faltantes_config,
    leer_historial,
    procesar_factura,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
app.json.ensure_ascii = False
CARPETA_MUESTRAS = os.path.join(os.path.dirname(__file__), "facturas")


@app.get("/")
def inicio():
    return render_template(
        "index.html",
        faltantes=faltantes_config(),
        muestras=MUESTRAS,
    )


def _ejecutar(ruta: str, nombre: str) -> dict:
    try:
        return procesar_factura(ruta)
    except Exception as e:
        return {"factura": nombre, "estado": "error", "detalle": str(e), "pasos": []}


@app.get("/api/historial")
def api_historial():
    return jsonify(leer_historial())


@app.post("/api/procesar")
def api_procesar():
    archivo = request.files.get("factura")
    if archivo is None or not archivo.filename:
        return jsonify({"error": "Suba un PDF."}), 400
    if not archivo.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo se aceptan archivos PDF."}), 400

    nombre = secure_filename(archivo.filename) or "factura.pdf"
    with tempfile.TemporaryDirectory(prefix="factura-") as tmp:
        ruta = os.path.join(tmp, nombre)
        archivo.save(ruta)
        resultado = _ejecutar(ruta, nombre)
    return jsonify(resultado)


@app.post("/api/muestra/<nombre>")
def api_muestra(nombre):
    if nombre not in MUESTRAS:
        return jsonify({"error": "Factura de muestra no reconocida."}), 404
    ruta = os.path.join(CARPETA_MUESTRAS, nombre)
    if not os.path.isfile(ruta):
        return jsonify({"error": f"No esta {nombre} en facturas/."}), 404
    return jsonify(_ejecutar(ruta, nombre))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
