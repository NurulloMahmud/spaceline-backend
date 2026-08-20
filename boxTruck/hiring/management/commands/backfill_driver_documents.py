from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from hiring.models import Driver, DriverCompany, DriverFile
from hiring.pdf_generation import fill_w9, generate_contract

W9_NAME = 'W-9 (Generated)'
CONTRACT_NAME = 'Contractor Agreement (Generated)'


class Command(BaseCommand):
    help = (
        "Backfill the generated W-9 and contractor agreement PDFs for "
        "drivers registered before document generation was added to every "
        "driver-creation path. Only fills in whichever of the two a driver "
        "is actually missing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be generated without creating any files.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        drivers = Driver.objects.select_related('company').all().order_by('id')

        generated = 0
        skipped_no_company_profile = 0
        skipped_no_template = 0

        for driver in drivers:
            existing_names = set(
                DriverFile.objects.filter(driver=driver, name__in=[W9_NAME, CONTRACT_NAME])
                .values_list('name', flat=True)
            )
            needs_w9 = W9_NAME not in existing_names
            needs_contract = CONTRACT_NAME not in existing_names
            if not needs_w9 and not needs_contract:
                continue

            driver_company = DriverCompany.objects.filter(driver=driver).first()
            if not driver_company:
                skipped_no_company_profile += 1
                self.stdout.write(self.style.WARNING(
                    f"driver {driver.id} ({driver.full_name}): no DriverCompany profile, skipping"
                ))
                continue

            if not driver.company or not driver.company.contract_template_text:
                skipped_no_template += 1
                self.stdout.write(self.style.WARNING(
                    f"driver {driver.id} ({driver.full_name}): company has no contract_template_text, skipping"
                ))
                continue

            needed = ' + '.join(
                n for n, flag in (('W-9', needs_w9), ('contract', needs_contract)) if flag
            )

            if dry_run:
                self.stdout.write(f"driver {driver.id} ({driver.full_name}): would generate {needed}")
                generated += 1
                continue

            with transaction.atomic():
                if needs_w9:
                    w9_bytes = fill_w9({
                        'company_name': driver_company.name or '',
                        'company_doing_business': driver_company.business_as or '',
                        'company_address': driver_company.address or '',
                        'company_city': driver_company.city or '',
                        'company_state': driver_company.state or '',
                        'company_zip': driver_company.zipcode or '',
                        'company_employer_id': driver_company.employer_id or '',
                        'company_type': driver_company.business_type or '',
                        'payee_code': driver.payee_code,
                        'fatca_reporting_code': driver.fatca_reporting_code,
                    }).getvalue()
                    w9_file = DriverFile(driver=driver, name=W9_NAME)
                    w9_file.document.save(f'w9_{driver.id}.pdf', ContentFile(w9_bytes), save=True)

                if needs_contract:
                    contractor_address = ', '.join(
                        part for part in [
                            driver_company.address, driver_company.city,
                            driver_company.state, driver_company.zipcode,
                        ] if part
                    )
                    today = timezone.now()
                    contract_bytes = generate_contract(driver.company, {
                        'effective_day': today.strftime('%d'),
                        'effective_month': today.strftime('%B'),
                        'effective_year': today.strftime('%y'),
                        'contractor_name': driver_company.name or '',
                        'contractor_address': contractor_address,
                        'contractor_email': driver.email or '',
                    }).getvalue()
                    contract_file = DriverFile(driver=driver, name=CONTRACT_NAME)
                    contract_file.document.save(f'contract_{driver.id}.pdf', ContentFile(contract_bytes), save=True)

            generated += 1
            self.stdout.write(self.style.SUCCESS(f"driver {driver.id} ({driver.full_name}): generated {needed}"))

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. generated={generated} "
            f"skipped_no_company_profile={skipped_no_company_profile} "
            f"skipped_no_template={skipped_no_template}"
        ))
