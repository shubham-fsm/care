import enum

from care.security.permissions.constants import Permission, PermissionContext
from care.security.roles.role import (
    ADMIN_ROLE,
    ADMINISTRATOR,
    DOCTOR_ROLE,
    FACILITY_ADMIN_ROLE,
    NURSE_ROLE,
    STAFF_ROLE,
)

CLINICAL = [ADMIN_ROLE, DOCTOR_ROLE, NURSE_ROLE, FACILITY_ADMIN_ROLE]


class ReferralPermissions(enum.Enum):
    """PROTOTYPE referral permissions (local CARE 3.1 demo)."""

    can_create_referral = Permission(
        "Can create referral", "", PermissionContext.ENCOUNTER, CLINICAL
    )
    can_read_referral = Permission(
        "Can read referral",
        "",
        PermissionContext.ENCOUNTER,
        [*CLINICAL, STAFF_ROLE, ADMINISTRATOR],
    )
    can_write_referral = Permission(
        "Can update / respond to referral", "", PermissionContext.ENCOUNTER, CLINICAL
    )
    can_manage_referral_availability = Permission(
        "Can configure where referrals can be sent",
        "",
        PermissionContext.FACILITY,
        [ADMIN_ROLE, FACILITY_ADMIN_ROLE, ADMINISTRATOR],
    )
