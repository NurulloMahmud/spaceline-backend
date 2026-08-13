import decimal
import json
import logging
import re
from datetime import datetime, date, time
from openai import OpenAI, APIError
from django.conf import settings
from django.db import connection

from .schema import get_schema_prompt

logger = logging.getLogger(__name__)

ALLOWED_SQL_PATTERN = re.compile(r'^\s*SELECT\b', re.IGNORECASE)
FORBIDDEN_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|EXEC|EXECUTE|GRANT|REVOKE)\b',
    re.IGNORECASE
)


def is_safe_sql(sql: str) -> bool:
    if not sql or not isinstance(sql, str):
        return False
    if not ALLOWED_SQL_PATTERN.match(sql.strip()):
        return False
    if FORBIDDEN_KEYWORDS.search(sql):
        return False
    return True


def serialize_value(v):
    """Safely serialize any DB value to a JSON-compatible type."""
    if v is None:
        return None
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def execute_sql(sql: str) -> tuple[list[dict], str | None]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            rows = []
            for row in cursor.fetchall():
                rows.append(dict(zip(columns, [serialize_value(v) for v in row])))
            return rows, None
    except Exception as e:
        logger.error(f"SQL execution error: {e}\nSQL: {sql}")
        return [], str(e)


def build_chart_from_result(chart_config: dict, rows: list[dict]) -> dict | None:
    if not chart_config or not rows:
        return None

    chart_type = chart_config.get('type')
    if not chart_type or chart_type == 'null':
        return None

    try:
        columns = list(rows[0].keys()) if rows else []
        label_col = columns[0] if columns else None
        value_cols = columns[1:] if len(columns) > 1 else []
        labels = [str(row.get(label_col, '')) for row in rows]
        datasets = []
        for col in value_cols:
            datasets.append({
                'label': col.replace('_', ' ').title(),
                'data': [row.get(col, 0) for row in rows]
            })

        return {
            'type': chart_type,
            'title': chart_config.get('title', ''),
            'labels': labels,
            'datasets': datasets
        }
    except Exception as e:
        logger.error(f"Chart build error: {e}")
        return None


def ask_ai_analyst(
    question: str,
    company_id: int,
    conversation_history: list[dict]
) -> dict:
    client = OpenAI(api_key=settings.AI_ACCESS_TOKEN)
    system_prompt = get_schema_prompt(company_id)
    today = date.today()
    system_prompt += f"\n\n=== CURRENT DATE ===\nToday is {today.strftime('%Y-%m-%d')}. Current year is {today.year}. Current month is {today.month}. ALWAYS use this date as reference. NEVER hardcode years from conversation history."
    messages = [{"role": "system", "content": system_prompt}]

    for msg in conversation_history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    messages.append({"role": "user", "content": question})
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2048,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages
        )

        raw_content = response.choices[0].message.content.strip()
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_content)
        if json_match:
            raw_content = json_match.group(1)

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}\nRaw content: {raw_content}")
            return {
                "answer": "⚠️ The AI returned an unreadable response. Please try again.",
                "sql": None,
                "query_result": None,
                "chart": None,
                "error": f"JSON decode error: {str(e)}"
            }
        sql = parsed.get("sql")
        answer = parsed.get("answer", "")
        chart_config = parsed.get("chart")
        query_result = None
        chart = None
        sql_error = None
        if sql and sql.strip().lower() != "null":
            if is_safe_sql(sql):
                rows, sql_error = execute_sql(sql)
                if not sql_error:
                    query_result = rows
                    chart = build_chart_from_result(chart_config, rows)
                else:
                    logger.error(f"SQL error: {sql_error}\nSQL: {sql}")
                    answer += f"\n\n⚠️ Query error: {sql_error}"
                    sql = None
            else:
                logger.warning(f"Unsafe SQL blocked: {sql}")
                answer = "⚠️ The query was blocked for security reasons. Please rephrase your question."
                sql = None

        return {
            "answer": answer,
            "sql": sql,
            "query_result": query_result,
            "chart": chart,
            "error": sql_error
        }

    except APIError as e:
        logger.error(f"OpenAI API error: {e}")
        return {
            "answer": "⚠️ AI service is temporarily unavailable. Please try again.",
            "sql": None,
            "query_result": None,
            "chart": None,
            "error": f"AI service error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error in ask_ai_analyst: {e}", exc_info=True)
        return {
            "answer": "⚠️ An unexpected error occurred. Please try again.",
            "sql": None,
            "query_result": None,
            "chart": None,
            "error": f"Unexpected error: {str(e)}"
        }
    