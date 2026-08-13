import threading

def async_generate_pdfs(statement_ids):
    from .models import Statement
    from billing.bot import send_telegram_message
    from .utils import generate_statement_pdf

    for statement_id in statement_ids:
        send_telegram_message(f"🌀 Starting PDF generation thread for statements: {statement_ids}")
        try:
            statement = Statement.objects.get(id=statement_id)
            generate_statement_pdf(statement)
            send_telegram_message(f"🌀 Starting PDF generation: {statement_id}")
        except Exception as e:
            error_message = f"🚨 *Error generating PDF* for statement *{statement_id.name}*:\n```{str(e)}```"
            send_telegram_message(error_message)
