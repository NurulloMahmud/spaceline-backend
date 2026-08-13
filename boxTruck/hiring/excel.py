import pandas as pd
from datetime import datetime
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from .models import Driver, DriverStatus
from users.models import Company

STATUS_MAP = {
    "Active":      "Active",
    "Terminated":  "Inactive",
    "Incomplete":  "Incomplete",
    "On Vacation": "On Vacation",
}


def _parse_date(value):
    if value is None or (isinstance(value, float)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date"):         
        return value.date()
    try:
        return datetime.strptime(str(value).strip(), "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def _str(value):
    if value is None:
        return None
    s = str(value).strip()
    return s if s and s.lower() != "nan" else None


class ImportDriversView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get("file")
        company_id = request.data.get("company_id")
        if not file:
            return Response(
                {"error": "No file provided. Send the xlsx as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not company_id:
            return Response(
                {"error": "No company_id provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            return Response(
                {"error": f"Company with id={company_id} does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            df = pd.read_excel(file, sheet_name="report", skiprows=4, dtype=str)
        except Exception as exc:
            return Response(
                {"error": f"Failed to parse Excel file: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        df = df.dropna(how="all")
        status_cache = {s.name: s for s in DriverStatus.objects.all()}
        drivers_to_create = []
        errors = []
        for idx, row in df.iterrows():
            excel_row = idx + 6
            first  = _str(row.get("First Name"))
            middle = _str(row.get("Middle Name"))
            last   = _str(row.get("Last Name"))

            name_parts = [p for p in [first, middle, last] if p]
            if not name_parts:
                errors.append({"row": excel_row, "error": "No name found, row skipped."})
                continue

            full_name = " ".join(name_parts)
            excel_status = _str(row.get("Status"))
            mapped_status_name = STATUS_MAP.get(excel_status)

            if not mapped_status_name:
                errors.append({
                    "row": excel_row,
                    "driver": full_name,
                    "error": f"Unknown status '{excel_status}', row skipped.",
                })
                continue

            driver_status = status_cache.get(mapped_status_name)
            if not driver_status:
                errors.append({
                    "row": excel_row,
                    "driver": full_name,
                    "error": (
                        f"DriverStatus '{mapped_status_name}' not found in the database. "
                        "Please seed the driver_statuses table first."
                    ),
                })
                continue

            drivers_to_create.append(
                Driver(
                    company=company,
                    full_name=full_name,
                    status=driver_status,
                    dob=_parse_date(row.get("DOB")),
                    driver_type=_str(row.get("Role")),
                    contract=_str(row.get("Contract")),
                    ssn=_str(row.get("SSN")),
                    fein=_str(row.get("FEIN")),
                    address=_str(row.get("Address")),
                    city=_str(row.get("City")),
                    state=_str(row.get("State")),
                    zip_code=_str(row.get("Zip Code")),
                    phone_number=_str(row.get("Phone")),
                    email=_str(row.get("Email")),
                    hired_date=_parse_date(row.get("Hired")),
                    terminated_date=_parse_date(row.get("Terminated")),
                    cdl_number=_str(row.get("CDL #")),
                    cdl_issued=_str(row.get("CDL Issued")),
                    cdl_class=_str(row.get("CDL Class")),
                    cdl_issue_date=_parse_date(row.get("CDL Issue Date")),
                    cdl_expiration=_parse_date(row.get("CDL Expiration")),
                    cdl_endorsement=_str(row.get("CDL Endorsement")),
                    unit_number="",
                )
            )

        created_count = 0
        if drivers_to_create:
            created = Driver.objects.bulk_create(drivers_to_create)
            created_count = len(created)

        return Response(
            {
                "imported": created_count,
                "skipped": len(errors),
                "errors": errors,
            },
            status=status.HTTP_201_CREATED,
        )
    