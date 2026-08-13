from django.core.mail import EmailMessage
from config import settings
from urllib.parse import quote
import boto3

def send_statement_email(driver_email, statement_id, start_date, end_date, driver_name):
    if not driver_email:
        return False
    view_url = f"{settings.BACKEND_URL}/api/payroll/statements/view-pdf/{statement_id}/"
    start_date_str = start_date.strftime("%m/%d/%Y")
    end_date_str = end_date.strftime("%m/%d/%Y")
    subject = f"Your Weekly Statement: {start_date_str} to {end_date_str}"
    body = f"""
            <p>Dear {driver_name},</p>

            <p>Your weekly statement for the period of {start_date_str} to {end_date_str} is ready.</p>

            <p>Please review the details carefully by clicking the link below:</p>
            <p><a href="{view_url}">Click here to see your statement</a></p>

            <p>If you have any questions or notice anything that needs clarification, feel free to reach out.</p>

            <p>Thank you for your service!</p>
        """

    email = EmailMessage(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [driver_email],
    )
    email.content_subtype = 'html'
    try:
        email.send(fail_silently=False)
        return True
    except Exception as e:
        print(f"Failed to send email to {driver_email}: {e}")
        return False

def generate_presigned_url(s3_key, expiration=3600):
    s3_client = boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )

    try:
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': s3_key},
            ExpiresIn=expiration
        )
    except Exception as e:
        print(f"Failed to generate presigned URL: {e}")
        return None
    return response
