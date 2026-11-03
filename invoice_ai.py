import json
import os

from google import genai
from google.genai import errors, types


PROMPT = """Lee esta factura colombiana y extrae solo lo que sea legible. No deduzcas ni inventes campos. Para cualquier dato ausente o ilegible usa null y confianza 0. Devuelve JSON con: supplier, nit, invoice_number, invoice_date (YYYY-MM-DD), resolution, subtotal, tax, total, confidence (objeto con porcentaje 0-100 para cada campo), items (lista con name, quantity, unit_price, discount, tax, confidence). Montos como números decimales sin separadores. Si un dato es dudoso, baja su confianza. Incluye únicamente líneas de productos reales de la factura."""


def extract_invoice(content, extension, filename):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Configura GEMINI_API_KEY para activar la extracción gratuita con Gemini.")

    mime = "application/pdf" if extension == "pdf" else "image/png" if extension == "png" else "image/jpeg"
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            contents=[PROMPT, types.Part.from_bytes(data=content, mime_type=mime)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            raise RuntimeError("Gemini rechazó la clave. Revisa GEMINI_API_KEY y reinicia la aplicación.") from exc
        if code == 429:
            raise RuntimeError("Se alcanzó el límite gratuito de Gemini. Espera a que se restablezca e inténtalo de nuevo.") from exc
        if code == 400:
            raise RuntimeError("Gemini no aceptó el archivo. Verifica que sea un JPG, PNG o PDF válido y que no esté dañado.") from exc
        if code == 404:
            raise RuntimeError("El modelo de Gemini no está disponible. Usa GEMINI_MODEL=gemini-3.5-flash-lite o elimina la variable para usar el predeterminado.") from exc
        raise RuntimeError(f"Gemini devolvió un error (código {code or 'desconocido'}). Revisa la configuración e inténtalo de nuevo.") from exc
    except (TimeoutError, ConnectionError) as exc:
        raise RuntimeError("No se pudo conectar con Gemini. Revisa tu conexión a Internet e inténtalo de nuevo.") from exc

    try:
        data = json.loads(response.text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Gemini no devolvió datos legibles para esta factura.") from exc
    if not isinstance(data, dict):
        raise ValueError("No se pudieron reconocer los datos de la factura.")
    if not isinstance(data.get("items"), list):
        data["items"] = []
    if not isinstance(data.get("confidence"), dict):
        data["confidence"] = {}
    return data
