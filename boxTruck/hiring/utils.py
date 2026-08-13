DRIVER_FK_DISPLAY = {
    'status': lambda obj: obj.status.name if obj.status else None,
    'company': lambda obj: obj.company.name if obj.company else None,
    'manager': lambda obj: obj.manager.username if obj.manager else None,
    'referral_by': lambda obj: obj.referral_by.username if obj.referral_by else None,
}

VEHICLE_FK_DISPLAY = {
    'driver': lambda obj: obj.driver.full_name if obj.driver else None,
    'second_driver': lambda obj: obj.second_driver.full_name if obj.second_driver else None,
}

DRIVER_COMPANY_FK_DISPLAY = {
    'driver': lambda obj: obj.driver.full_name if obj.driver else None,
}


def build_change_description(old_instance, new_instance, updated_fields, fk_display):
    changes = []

    for field in updated_fields:
        if field in fk_display:
            old_value = fk_display[field](old_instance)
            new_value = fk_display[field](new_instance)
        else:
            old_value = getattr(old_instance, field, None)
            new_value = getattr(new_instance, field, None)

        if old_value != new_value:
            label = field.replace('_', ' ').title()
            changes.append(f"{label}: {old_value} -> {new_value}")
    return ' | '.join(changes) if changes else None
