"""
PROTOTYPE referral models (built locally to demo the CARE 3.1 referral UI).
Not the official CARE 3.1 backend.
"""

from django.db import models

from care.emr.models.base import EMRBaseModel


class ReferralAvailability(EMRBaseModel):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, default="draft")
    is_public = models.BooleanField(default=False)
    referral_type = models.CharField(max_length=20, default="consultation")
    entity_type = models.CharField(max_length=40, default="facility")
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.CASCADE, null=True, blank=True
    )
    internal_organization = models.ForeignKey(
        "emr.FacilityOrganization", on_delete=models.CASCADE, null=True, blank=True
    )
    external_organization = models.ForeignKey(
        "emr.Organization", on_delete=models.CASCADE, null=True, blank=True
    )
    external_entity = models.JSONField(default=dict)
    # [{"user_id": "<external id>", "role": {code}}]
    care_team = models.JSONField(default=list)
    # [{"questionnaire": "<external id>", "questionnaire_type": "request"|"response"}]
    questionnaires = models.JSONField(default=list)


class Referral(EMRBaseModel):
    availability = models.ForeignKey(ReferralAvailability, on_delete=models.PROTECT)
    patient = models.ForeignKey("emr.Patient", on_delete=models.CASCADE)
    encounter = models.ForeignKey(
        "emr.Encounter", on_delete=models.SET_NULL, null=True, blank=True
    )
    source_facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals_sent",
    )
    title = models.CharField(max_length=255)
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, default="pending")
    assignees = models.JSONField(default=list)
    thread_id = models.UUIDField(null=True, blank=True)


class ReferralQuestionnaireResponse(EMRBaseModel):
    referral = models.ForeignKey(
        Referral, on_delete=models.CASCADE, related_name="responses"
    )
    questionnaire = models.ForeignKey("emr.Questionnaire", on_delete=models.PROTECT)
    questionnaire_type = models.CharField(max_length=20)
    status = models.CharField(max_length=20, default="completed")
    response = models.JSONField(default=dict)
    questionnaire_revision = models.IntegerField(default=1)
    questionnaire_snapshot = models.JSONField(default=dict)


class ReferralResource(EMRBaseModel):
    referral = models.ForeignKey(
        Referral, on_delete=models.CASCADE, related_name="resources"
    )
    resource_type = models.CharField(max_length=40)
    resource_id = models.CharField(max_length=64)
    attachment_type = models.CharField(max_length=20, default="source")
    metadata = models.JSONField(default=dict)


class ReferralNote(EMRBaseModel):
    referral = models.ForeignKey(
        Referral, on_delete=models.CASCADE, related_name="notes"
    )
    message = models.TextField()
    message_history = models.JSONField(default=dict)
