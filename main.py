"""
API FastAPI para Gestor de Gastos (GG)
======================================

Esta API recibe datos desde Google Apps Script y los procesa,
interactuando con Google Sheets según sea necesario.

Deployment en Cloud Run:
    gcloud run deploy gg-parser \
        --source . \
        --platform managed \
        --region us-central1 \
        --allow-unauthenticated

Para desarrollo local:
    uvicorn main:app --reload --port 8080
"""

from fastapi import FastAPI, HTTPException, Request, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Any
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================================================
# CONFIGURACIÓN DE FASTAPI
# ============================================================================

app = FastAPI(
    title="GG - Gestor de Gastos API",
    description="API para procesar gastos y sincronizar con Google Sheets",
    version="1.0.0"
)

# Configurar CORS para permitir llamadas desde Apps Script
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, especifica los orígenes permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# SEGURIDAD - API KEY CONFIGURATION
# ============================================================================

# Definir el esquema de seguridad para Swagger UI
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def get_api_key(api_key: str = Security(api_key_header)) -> str:
    """
    Dependency para validar API key.
    
    Esta función será usada por FastAPI para validar el API key
    y también aparecerá en la documentación de Swagger (/docs).
    
    Args:
        api_key: El API key del header X-API-Key
        
    Returns:
        El API key validado
        
    Raises:
        HTTPException: Si el API key es inválido o falta
    """
    expected_key = os.environ.get("API_KEY")
    
    # Si no hay API_KEY configurado, permitir el request (modo desarrollo)
    if not expected_key:
        return "development-mode"
    
    # Validar que el header esté presente
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API Key. Include 'X-API-Key' header in your request."
        )
    
    # Validar que el API key sea correcto
    if api_key != expected_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key"
        )
    
    return api_key


# ============================================================================
# MODELOS PYDANTIC (VALIDACIÓN DE DATOS)
# ============================================================================


class EmailRequest(BaseModel):
    """Modelo para recibir correos desde Google Apps Script."""
    subject: str = Field(..., description="Asunto del correo")
    body: str = Field(..., description="Cuerpo del correo")
    date: str = Field(..., description="Fecha del correo en ISO format")
    
    class Config:
        json_schema_extra = {
            "example": {
                "subject": "Compra con Tarjeta de Crédito",
                "body": "Te informamos que se ha realizado una compra por $122.000 con Tarjeta de Crédito 3670 en SII 11001SANTIAGOCL el 05/02/2026 16:23.",
                "date": "2026-02-06T19:47:58.000Z"
            }
        }


class TransactionParsed(BaseModel):
    """Modelo para la transacción parseada del correo."""
    amount: float = Field(..., description="Monto de la transacción")
    card_type: Optional[str] = Field(None, description="Tipo de tarjeta (Crédito/Débito)")
    merchant: Optional[str] = Field(None, description="Comercio donde se realizó la compra")
    datetime: Optional[str] = Field(None, description="Fecha y hora de la transacción")


class ParseResponse(BaseModel):
    """Modelo para respuesta de procesamiento."""
    status: str
    transaction: Optional[TransactionParsed] = None
    message: Optional[str] = None


# ============================================================================
# CONFIGURACIÓN DE GOOGLE SHEETS
# ============================================================================

def get_sheets_service():
    """
    Crea y retorna un servicio de Google Sheets API.
    
    Usa Application Default Credentials (ADC) por defecto.
    Esto funciona automáticamente si has ejecutado:
        gcloud auth application-default login
    
    También funciona automáticamente en Cloud Run sin configuración adicional.
    
    Returns:
        Resource: Servicio de Google Sheets API
    """
    try:
        # Usar Application Default Credentials (ADC)
        # Esto busca credenciales en este orden:
        # 1. Variable GOOGLE_APPLICATION_CREDENTIALS (si apunta a archivo válido)
        # 2. gcloud auth application-default credentials
        # 3. Credentials del entorno (Cloud Run, Compute Engine, etc.)
        
        service = build('sheets', 'v4')
        return service
        
    except Exception as e:
        print(f"❌ Error al crear servicio de Sheets: {e}")
        print("💡 Asegúrate de haber ejecutado: gcloud auth application-default login")
        raise


def write_to_sheet(spreadsheet_id: str, range_name: str, values: List[List[Any]]):
    """
    Escribe datos a Google Sheets.
    
    Args:
        spreadsheet_id: ID de la hoja de cálculo
        range_name: Rango donde escribir (ej: 'Sheet1!A1:B2')
        values: Lista de listas con los valores a escribir
    
    Returns:
        dict: Respuesta de la API
    
    Ejemplo:
        write_to_sheet(
            'tu-spreadsheet-id',
            'Gastos!A2:D2',
            [['2024-01-01', 'Supermercado', 'Comida', 150.50]]
        )
    """
    service = get_sheets_service()
    
    body = {
        'values': values
    }
    
    result = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()
    
    return result


def read_from_sheet(spreadsheet_id: str, range_name: str) -> List[List[Any]]:
    """
    Lee datos desde Google Sheets.
    
    Args:
        spreadsheet_id: ID de la hoja de cálculo
        range_name: Rango a leer (ej: 'Sheet1!A1:D10')
    
    Returns:
        Lista de filas leídas
    """
    service = get_sheets_service()
    
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name
    ).execute()
    
    return result.get('values', [])


# ============================================================================
# ENDPOINTS DE LA API
# ============================================================================

@app.get("/")
async def root():
    """Endpoint de prueba."""
    return {
        "status": "ok",
        "message": "GG - Gestor de Gastos API está funcionando",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    """Health check para Cloud Run."""
    return {"status": "healthy"}


@app.post("/parse-email", response_model=ParseResponse)
async def parse_email(
    email: EmailRequest,
    api_key: str = Depends(get_api_key)
):
    """
    Procesa un correo bancario y extrae información de la transacción.
    
    **Seguridad**: Requiere API key válido en header 'X-API-Key'
    
    Extrae únicamente:
    - Monto de la transacción
    - Tipo de tarjeta (Crédito/Débito)
    - Comercio (merchant)
    - Fecha y hora de la transacción
    
    Args:
        email: Datos del correo (subject, body, date)
        api_key: API key validado (inyectado automáticamente)
    
    Returns:
        ParseResponse con los datos extraídos
    """
    try:
        # El API key ya fue validado por la dependency
        # Parsear el cuerpo del correo
        parsed_data = parse_banco_chile_email(email.body)
        
        # Si no se pudo extraer el monto, es inválido
        if parsed_data["amount"] == 0:
            return ParseResponse(
                status="error",
                message="No se pudo extraer información de transacción del correo"
            )
        
        # Crear el objeto validado con Pydantic
        transaction = TransactionParsed(**parsed_data)
        
        # ====================================================================
        # GUARDAR EN GOOGLE SHEETS
        # ====================================================================
        
        SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
        sheets_saved = False
        sheets_error = None
        sheets_details = None
        
        if not SPREADSHEET_ID:
            sheets_error = "SPREADSHEET_ID no configurado en variables de entorno"
        else:
            try:
                # Separar fecha y hora para mejor manejo en Sheets
                date_str = None
                time_str = None
                
                if transaction.datetime:
                    # El formato es: "05/02/2026 16:23"
                    parts = transaction.datetime.split(' ')
                    if len(parts) == 2:
                        date_str = parts[0]  # "05/02/2026"
                        time_str = parts[1]  # "16:23"
                
                # Preparar datos para insertar
                # Orden: Fecha | Hora | Comercio | Tipo Tarjeta | Monto
                row_data = [
                    date_str or '',          # Fecha separada
                    time_str or '',          # Hora separada
                    transaction.merchant or '',
                    transaction.card_type or '',
                    transaction.amount
                ]
                
                print(f"📊 Intentando guardar en Sheets: {SPREADSHEET_ID}")
                print(f"📝 Datos: {row_data}")
                
                # Nombre de la hoja (configurable via env, default: Sheet1)
                sheet_name = os.environ.get('SHEET_NAME', 'Sheet1')
                
                # Escribir a la hoja (se agrega como nueva fila)
                result = write_to_sheet(
                    SPREADSHEET_ID,
                    f'{sheet_name}!A:E',  # Ahora 5 columnas: Fecha, Hora, Comercio, Tipo, Monto
                    [row_data]
                )
                
                sheets_saved = True
                sheets_details = f"Agregada fila en rango: {result.get('updates', {}).get('updatedRange', 'N/A')}"
                print(f"✅ Guardado exitoso: {sheets_details}")
                
            except Exception as sheet_error:
                sheets_error = str(sheet_error)
                # No fallar toda la request si falla Sheets
                print(f"❌ Error al guardar en Sheets: {sheet_error}")
                import traceback
                traceback.print_exc()
        
        # Preparar mensaje de respuesta
        if sheets_saved:
            message = f"✅ Guardado en Google Sheets: {sheets_details}"
        elif sheets_error:
            message = f"⚠️ No guardado en Sheets: {sheets_error}"
        else:
            message = "⚠️ SPREADSHEET_ID no configurado"
        
        return ParseResponse(
            status="success",
            transaction=transaction,
            message=message
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/expenses")
async def get_expenses(
    limit: int = 100,
    offset: int = 0
):
    """
    Obtiene gastos desde Google Sheets.
    
    Args:
        limit: Número máximo de gastos a retornar
        offset: Número de gastos a saltar
    
    Returns:
        Lista de gastos
    """
    try:
        SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', 'tu-spreadsheet-id')
        
        # Calcular el rango a leer
        start_row = 2 + offset  # Asumiendo que la fila 1 es el header
        end_row = start_row + limit
        
        # Leer desde la hoja
        expenses = read_from_sheet(
            SPREADSHEET_ID,
            f'Gastos!A{start_row}:D{end_row}'
        )
        
        return {
            "status": "success",
            "count": len(expenses),
            "data": expenses
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# FUNCIONES AUXILIARES
# ============================================================================

def parse_banco_chile_email(body: str) -> dict:
    """
    Parsea correos del Banco de Chile para extraer información de transacciones.
    
    Soporta 3 tipos de transacciones:
    1. Compra con Tarjeta de Crédito
    2. Cargo en Cuenta (débito)
    3. Giro en Cajero
    
    Extrae:
    - amount: Monto de la transacción
    - card_type: Tipo (Crédito/Débito/Giro)
    - merchant: Comercio o "Cajero Automático"
    - datetime: Fecha y hora de la transacción
    
    Args:
        body: Cuerpo del correo
        
    Returns:
        Dict con los 4 campos parseados
    """
    import re
    
    result = {
        "amount": 0.0,
        "card_type": None,
        "merchant": None,
        "datetime": None
    }
    
    # 1. Extraer MONTO (ej: "$122.000", "$950", "$10.000")
    amount_match = re.search(r'\$[\d\.,]+', body)
    if amount_match:
        # Remover $ y puntos, convertir a float
        amount_str = amount_match.group().replace('$', '').replace('.', '').replace(',', '')
        try:
            result["amount"] = float(amount_str)
        except ValueError:
            result["amount"] = 0.0
    
    # 2. Detectar TIPO DE TRANSACCIÓN
    if 'giro' in body.lower() or 'cajero' in body.lower():
        # Es un giro en cajero
        result["card_type"] = "Giro"
        result["merchant"] = "Cajero Automático"
        
    elif 'cargo a cuenta' in body.lower():
        # Es débito (compra con cuenta)
        result["card_type"] = "Débito"
        # Extraer comercio para débito: "en SAN FRANCISCO el"
        merchant_match = re.search(r'en ([^\r\n]+?)\s+el\s+\d', body)
        if merchant_match:
            result["merchant"] = merchant_match.group(1).strip()
            
    elif 'tarjeta de crédito' in body.lower():
        # Es crédito
        result["card_type"] = "Crédito"
        # Extraer comercio para crédito: "en SII 11001SANTIAGOCL el"
        merchant_match = re.search(r'en ([^\r\n]+?)\s+el\s+\d', body)
        if merchant_match:
            result["merchant"] = merchant_match.group(1).strip()
    
    # Si no se detectó el tipo pero dice "Tarjeta de", intentar extraer
    if not result["card_type"]:
        card_type_match = re.search(r'Tarjeta de (Crédito|Débito)', body, re.IGNORECASE)
        if card_type_match:
            result["card_type"] = card_type_match.group(1).capitalize()
    
    # 3. Extraer FECHA Y HORA (formato: "el 05/02/2026 16:23")
    datetime_match = re.search(r'el (\d{2}/\d{2}/\d{4}) (\d{2}:\d{2})', body)
    if datetime_match:
        date_str = datetime_match.group(1)  # 05/02/2026
        time_str = datetime_match.group(2)  # 16:23
        result["datetime"] = f"{date_str} {time_str}"
    
    return result


# ============================================================================
# FUNCIONES DE UTILIDAD PARA GOOGLE SHEETS
# ============================================================================

def get_last_row(spreadsheet_id: str, sheet_name: str) -> int:
    """
    Obtiene el número de la última fila con datos en una hoja.
    
    Útil para insertar datos al final de la hoja.
    """
    service = get_sheets_service()
    
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f'{sheet_name}!A:A'
    ).execute()
    
    values = result.get('values', [])
    return len(values) + 1


def batch_update_sheet(spreadsheet_id: str, updates: List[dict]):
    """
    Realiza múltiples actualizaciones en una sola llamada.
    
    Args:
        spreadsheet_id: ID de la hoja
        updates: Lista de diccionarios con las actualizaciones
    
    Ejemplo:
        batch_update_sheet('id', [
            {'range': 'Sheet1!A1', 'values': [['Fecha', 'Monto']]},
            {'range': 'Sheet1!A2', 'values': [['2024-01-01', 100]]}
        ])
    """
    service = get_sheets_service()
    
    data = [
        {
            'range': update['range'],
            'values': update['values']
        }
        for update in updates
    ]
    
    body = {
        'valueInputOption': 'USER_ENTERED',
        'data': data
    }
    
    result = service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body
    ).execute()
    
    return result


# ============================================================================
# PUNTO DE ENTRADA PARA CLOUD RUN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    # Obtener el puerto desde la variable de entorno (Cloud Run usa PORT)
    port = int(os.environ.get("PORT", 8080))
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
