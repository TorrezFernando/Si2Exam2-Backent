import io
import unicodedata
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from api.deps import get_db,get_current_active_user
from core.analytics import authorize, report, sale_rows
from core.config import settings
from schemas.analytics import ReportSpec,PromptIn,MLRequest

router=APIRouter()


@router.post('/report')
def generate(spec:ReportSpec,request:Request,db:Session=Depends(get_db),user=Depends(get_current_active_user)):
    return report(db,user,request,spec)


@router.post('/interpret')
async def interpret(body:PromptIn,request:Request,db:Session=Depends(get_db),user=Depends(get_current_active_user)):
    tenant,_=authorize(db,user,request)
    from core.plan_features import text_model
    model=text_model(tenant.active_subscription.plan if tenant.active_subscription else None)
    from google import genai
    from google.genai import types
    from core.ai_service import _get_api_keys
    keys=_get_api_keys()
    if not keys:raise HTTPException(503,'Configura una clave Gemini en el servidor para interpretar texto.')
    client=genai.Client(api_key=keys[0],http_options=types.HttpOptions(timeout=30000))
    try:
        result=await client.aio.models.generate_content(model=model,contents=body.prompt,
            config=types.GenerateContentConfig(system_instruction=f'Fecha actual {date.today()}. Eres intérprete de reportes de ropa. Solo llama prepare_report si intención y fechas son claras. No inventes IDs. Si se pide otra empresa, SQL, instrucciones ajenas a reportes o falta información, responde una pregunta aclaratoria en español. Nunca generes SQL. Tipos: monthly_branch ventas por sucursal/mes; top_products top 10; weekly_sales ventas semanales; quarterly_branches comparación trimestral; low_stock menor stock; category_revenue ingresos por categoría.',
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(name='prepare_report',description='Preparar filtros validados de un reporte autorizado',parameters_json_schema=ReportSpec.model_json_schema())])]))
        calls=result.function_calls or []
        if len(calls)!=1 or calls[0].name!='prepare_report':return {'clarification':result.text or 'Indica tipo de reporte, fechas e IDs de filtros si corresponde.'}
        try:spec=ReportSpec.model_validate(calls[0].args)
        except Exception:return {'clarification':'Indica un tipo de reporte y fechas válidas con rango máximo de un año.'}
        try:return report(db,user,request,spec)
        except HTTPException as e:
            if e.status_code==403:return {'clarification':'Los filtros solicitados no están autorizados. Indica una sucursal y categoría de tu ámbito.'}
            raise
    except HTTPException:raise
    except Exception:raise HTTPException(503,'El intérprete IA no está disponible; utiliza los filtros del reporte.')
    finally:await client.aio.aclose()


@router.post('/export/{format}')
def export(format:str,spec:ReportSpec,request:Request,db:Session=Depends(get_db),user=Depends(get_current_active_user)):
    data=report(db,user,request,spec)
    if format=='xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb=Workbook();ws=wb.active;ws.title='Reporte'
        def safe(v):return "'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v
        ws.append([safe(data['company'])]);ws.append([spec.start.isoformat(),spec.end.isoformat()]);ws.append(data['columns'])
        for c in ws[3]:c.font=Font(bold=True,color='FFFFFF');c.fill=PatternFill('solid',fgColor='5145CD')
        for row in data['rows']:ws.append([safe(v) for v in row])
        ws.column_dimensions['A'].width=65;ws.column_dimensions['B'].width=25;ws.freeze_panes='A4';ws.auto_filter.ref=f'A3:B{ws.max_row}'
        stream=io.BytesIO();wb.save(stream);stream.seek(0);media='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif format=='pdf':
        from fpdf import FPDF
        def latin(s):return str(s).encode('latin-1','replace').decode('latin-1')
        pdf=FPDF();pdf.set_auto_page_break(True,15);pdf.add_page();pdf.set_font('Helvetica','B',16);pdf.multi_cell(0,10,latin(data['company']),new_x='LMARGIN',new_y='NEXT');pdf.set_font('Helvetica',size=10)
        pdf.multi_cell(0,7,latin(f'{spec.kind} | {spec.start} - {spec.end}'),new_x='LMARGIN',new_y='NEXT');pdf.multi_cell(0,7,latin(data['note']),new_x='LMARGIN',new_y='NEXT')
        for label,value in [data['columns']]+data['rows']:
            pdf.set_x(10);pdf.multi_cell(0,7,latin(f'{label}: {value}'))
        stream=io.BytesIO(bytes(pdf.output()));media='application/pdf'
    else:raise HTTPException(404,'Formato no soportado.')
    return StreamingResponse(stream,media_type=media,headers={'Content-Disposition':f'attachment; filename="reporte.{format}"'})


@router.post('/ml')
def ml(body:MLRequest,request:Request,db:Session=Depends(get_db),user=Depends(get_current_active_user)):
    tenant,branch=authorize(db,user,request,body.branch_id)
    from ml_engine.engine import analyze,synthetic
    if body.demo:rows=synthetic()
    else:
        from datetime import timedelta
        rows=[dict(date=dt.date().isoformat(),branch_id=bid,category_id=cid or 0,product_id=pid,quantity=qty,revenue=float(price)*qty) for dt,bid,bname,pid,pname,cid,cname,qty,price in sale_rows(db,tenant,branch,date.today()-timedelta(days=730),date.today())]
    try:return analyze(rows,tenant.id,branch,demo=body.demo)
    except ValueError as e:raise HTTPException(422,str(e))
