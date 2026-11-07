"""
PROTOTYPE referral API for the CARE 3.1 frontend (local demo only).
Shapes follow care_fe 3.1/base src/types/emr/referral/*.ts.
Authorization is simplified: any logged-in user can read/write.
"""

import uuid

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from care.emr.models.encounter import Encounter
from care.emr.models.organization import FacilityOrganization, Organization
from care.emr.models.patient import Patient
from care.emr.models.questionnaire import Questionnaire
from care.emr.models.referral import (
    Referral,
    ReferralAvailability,
    ReferralNote,
    ReferralQuestionnaireResponse,
    ReferralResource,
)
from care.emr.resources.base import model_from_cache
from care.emr.resources.questionnaire.spec import QuestionnaireReadSpec
from care.emr.resources.user.spec import UserSpec
from care.facility.models import Facility
from care.users.models import User

STATUSES = {"pending", "in_progress", "completed", "cancelled", "rejected"}


# ---------------------------------------------------------------- helpers
def _user(uid):
    return model_from_cache(UserSpec, id=uid) if uid else None


def _audit(obj):
    return {
        "created_date": obj.created_date.isoformat(),
        "modified_date": obj.modified_date.isoformat(),
        "created_by": _user(obj.created_by_id),
        "updated_by": _user(obj.updated_by_id),
    }


def _team_read(members):
    out = []
    for m in members or []:
        u = User.objects.filter(external_id=m.get("user_id")).first()
        if u:
            out.append({"role": m.get("role") or {}, "member": _user(u.id)})
    return out


def _team_write(members):
    clean = []
    for m in members or []:
        if not User.objects.filter(external_id=m.get("user_id")).exists():
            raise ValidationError({"care_team": f"Unknown user {m.get('user_id')}"})
        clean.append({"user_id": str(m["user_id"]), "role": m.get("role") or {}})
    return clean


def _ext(obj):
    return str(obj.external_id) if obj else None


def _paginate(request, items, serializer):
    try:
        limit = int(request.query_params.get("limit", 15))
        offset = int(request.query_params.get("offset", 0))
    except ValueError:
        limit, offset = 15, 0
    total = items.count() if hasattr(items, "count") else len(items)
    page = items[offset : offset + limit]
    return Response(
        {
            "count": total,
            "next": None,
            "previous": None,
            "results": [serializer(x) for x in page],
        }
    )


def _questionnaire_read(q):
    return QuestionnaireReadSpec.serialize(q).to_json()


def _availability_questionnaires(availability, qtype=None):
    out = []
    for m in availability.questionnaires or []:
        if qtype and m.get("questionnaire_type") != qtype:
            continue
        q = Questionnaire.objects.filter(external_id=m.get("questionnaire")).first()
        if q:
            out.append((q, m.get("questionnaire_type")))
    return out


def availability_read(a):
    return {
        "id": str(a.external_id),
        "name": a.name,
        "description": a.description,
        "status": a.status,
        "is_public": a.is_public,
        "referral_type": a.referral_type,
        "entity_type": a.entity_type,
        "facility": _ext(a.facility),
        "internal_organization": _ext(a.internal_organization),
        "external_organization": _ext(a.external_organization),
        "external_entity": a.external_entity or {},
        "care_team": _team_read(a.care_team),
        **_audit(a),
    }


def availability_retrieve(a):
    data = availability_read(a)
    data["questionnaires"] = [
        {
            "id": str(q.external_id),
            "slug": q.slug,
            "title": q.title,
            "status": q.status,
            "questionnaire_type": t,
        }
        for q, t in _availability_questionnaires(a)
    ]
    return data


def response_read(r):
    return {
        "id": str(r.external_id),
        "questionnaire": str(r.questionnaire.external_id),
        "status": r.status,
        "questionnaire_type": r.questionnaire_type,
        "questionnaire_response": r.response,
        "questionnaire_revision": r.questionnaire_revision,
        "questionnaire_snapshot": r.questionnaire_snapshot,
        **_audit(r),
    }


def referral_read(r):
    return {
        "id": str(r.external_id),
        "title": r.title,
        "note": r.note,
        "availability": str(r.availability.external_id),
        "availability_name": r.availability.name,
        "patient": str(r.patient.external_id),
        "patient_name": r.patient.name,
        "status": r.status,
        "assignees": _team_read(r.assignees),
        "created_date": r.created_date.isoformat(),
        "modified_date": r.modified_date.isoformat(),
    }


def referral_retrieve(r):
    data = referral_read(r)
    data.update(
        {
            "encounter": _ext(r.encounter),
            "thread": str(r.thread_id) if r.thread_id else None,
            "questionnaire_responses": [
                response_read(x)
                for x in r.responses.select_related("questionnaire").order_by(
                    "created_date"
                )
            ],
            "created_by": _user(r.created_by_id),
            "updated_by": _user(r.updated_by_id),
        }
    )
    return data


def note_read(n):
    return {
        "id": str(n.external_id),
        "message": n.message,
        "message_history": n.message_history or {"history": []},
        **_audit(n),
    }


def resource_read(x):
    return {
        "id": str(x.external_id),
        "referral": str(x.referral.external_id),
        "resource_type": x.resource_type,
        "resource_id": x.resource_id,
        "attachment_type": x.attachment_type,
        "metadata": x.metadata or {},
        "created_date": x.created_date.isoformat(),
        "modified_date": x.modified_date.isoformat(),
    }


def _entity_filter(entity_type, entity_id, prefix=""):
    if not entity_type or not entity_id:
        return Q()
    field = {
        "facility": "facility",
        "internal_organization": "internal_organization",
        "external_organization": "external_organization",
    }.get(entity_type)
    if not field:
        return Q(pk__in=[])
    return Q(**{f"{prefix}entity_type": entity_type}) & Q(
        **{f"{prefix}{field}__external_id": entity_id}
    )


# ---------------------------------------------------------- availability
class ReferralAvailabilityViewSet(ViewSet):
    """/referral_availability/ — destinations a referral can be sent to."""

    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def _qs(self, request, active_only=False):
        qs = ReferralAvailability.objects.filter(deleted=False).select_related(
            "facility", "internal_organization", "external_organization"
        )
        p = request.query_params
        qs = qs.filter(_entity_filter(p.get("entity_type"), p.get("entity_id")))
        if p.get("entity_type") and not p.get("entity_id"):
            qs = qs.filter(entity_type=p["entity_type"])
        if active_only:
            qs = qs.filter(status="active")
        elif p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("name"):
            qs = qs.filter(name__icontains=p["name"])
        if p.get("referral_type"):
            qs = qs.filter(referral_type=p["referral_type"])
        return qs.order_by("-name" if p.get("ordering") == "-name" else "name")

    def list(self, request):
        return _paginate(request, self._qs(request, active_only=True), availability_read)

    def retrieve(self, request, external_id=None):
        a = get_object_or_404(ReferralAvailability, external_id=external_id, deleted=False)
        return Response(availability_retrieve(a))

    def _apply(self, a, data, creating):
        for f in ("name", "description", "status", "is_public"):
            if f in data:
                setattr(a, f, data[f])
        if data.get("status") and data["status"] not in {"draft", "active", "inactive"}:
            raise ValidationError({"status": "Invalid status"})
        if creating:
            a.referral_type = data.get("referral_type", "consultation")
            a.entity_type = data.get("entity_type", "facility")
            if a.entity_type == "facility":
                a.facility = get_object_or_404(Facility, external_id=data.get("facility"))
            elif a.entity_type == "internal_organization":
                org = get_object_or_404(
                    FacilityOrganization, external_id=data.get("internal_organization")
                )
                a.internal_organization, a.facility = org, org.facility
            elif a.entity_type == "external_organization":
                a.external_organization = get_object_or_404(
                    Organization, external_id=data.get("external_organization")
                )
            else:
                raise ValidationError({"entity_type": "Invalid entity type"})
        if "care_team" in data:
            a.care_team = _team_write(data["care_team"])
        if "questionnaires" in data:
            qs = []
            for m in data["questionnaires"] or []:
                if not Questionnaire.objects.filter(external_id=m.get("questionnaire")).exists():
                    raise ValidationError({"questionnaires": "Unknown questionnaire"})
                qs.append(
                    {
                        "questionnaire": str(m["questionnaire"]),
                        "questionnaire_type": m.get("questionnaire_type", "request"),
                    }
                )
            a.questionnaires = qs
        if not a.name:
            raise ValidationError({"name": "Name is required"})

    def create(self, request):
        a = ReferralAvailability(created_by=request.user, updated_by=request.user)
        self._apply(a, request.data, True)
        a.save()
        return Response(availability_retrieve(a), status=http.HTTP_201_CREATED)

    def partial_update(self, request, external_id=None):
        a = get_object_or_404(ReferralAvailability, external_id=external_id, deleted=False)
        self._apply(a, request.data, False)
        a.updated_by = request.user
        a.save()
        return Response(availability_retrieve(a))

    def destroy(self, request, external_id=None):
        a = get_object_or_404(ReferralAvailability, external_id=external_id, deleted=False)
        a.deleted = True
        a.save(update_fields=["deleted"])
        return Response(status=http.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def questionnaires(self, request, external_id=None):
        a = get_object_or_404(ReferralAvailability, external_id=external_id, deleted=False)
        rows = _availability_questionnaires(a, request.query_params.get("questionnaire_type"))
        results = [{**_questionnaire_read(q), "questionnaire_type": t} for q, t in rows]
        return Response({"count": len(results), "results": results})


class ReferralAvailabilityConfigViewSet(ReferralAvailabilityViewSet):
    """/referral_availability_config/ — all statuses, for settings pages."""

    def list(self, request):
        return _paginate(request, self._qs(request), availability_read)


# ---------------------------------------------------------------- referral
class ReferralViewSet(ViewSet):
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def _get(self, external_id):
        return get_object_or_404(
            Referral.objects.select_related("availability", "patient", "encounter"),
            external_id=external_id,
            deleted=False,
        )

    def _qs(self, request):
        qs = Referral.objects.filter(deleted=False).select_related(
            "availability", "patient"
        )
        p = request.query_params
        if p.get("entity_type") and p.get("entity_id"):
            qs = qs.filter(
                _entity_filter(p["entity_type"], p["entity_id"], prefix="availability__")
            )
        if p.get("patient"):
            qs = qs.filter(patient__external_id=p["patient"])
        if p.get("encounter"):
            qs = qs.filter(encounter__external_id=p["encounter"])
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("assigned_to_me") in ("true", "True", "1"):
            qs = qs.filter(assignees__contains=[{"user_id": str(request.user.external_id)}])
        return qs.order_by(
            "created_date" if p.get("ordering") == "created_date" else "-created_date"
        )

    def list(self, request):
        return _paginate(request, self._qs(request), referral_read)

    def retrieve(self, request, external_id=None):
        return Response(referral_retrieve(self._get(external_id)))

    def create(self, request):
        d = request.data
        a = get_object_or_404(
            ReferralAvailability, external_id=d.get("availability"), deleted=False
        )
        if a.status != "active":
            raise ValidationError({"availability": "Destination is not active"})
        patient = get_object_or_404(Patient, external_id=d.get("patient"))
        encounter = None
        if d.get("encounter"):
            encounter = get_object_or_404(Encounter, external_id=d["encounter"])
        if not (d.get("title") or "").strip():
            raise ValidationError({"title": "Title is required"})
        q = get_object_or_404(Questionnaire, external_id=d.get("questionnaire"))
        r = Referral.objects.create(
            availability=a,
            patient=patient,
            encounter=encounter,
            source_facility=encounter.facility if encounter else None,
            title=d["title"].strip(),
            note=(d.get("note") or "").strip(),
            status="pending",
            assignees=a.care_team or [],
            created_by=request.user,
            updated_by=request.user,
        )
        ReferralQuestionnaireResponse.objects.create(
            referral=r,
            questionnaire=q,
            questionnaire_type="request",
            response=d.get("questionnaire_response") or {"results": []},
            questionnaire_revision=getattr(q, "version", 1) or 1,
            questionnaire_snapshot=_questionnaire_read(q),
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(referral_retrieve(r), status=http.HTTP_201_CREATED)

    def partial_update(self, request, external_id=None):
        r = self._get(external_id)
        d = request.data
        if "status" in d:
            if d["status"] not in STATUSES:
                raise ValidationError({"status": "Invalid status"})
            r.status = d["status"]
        for f in ("title", "note"):
            if f in d:
                setattr(r, f, d[f])
        if "assignees" in d:
            r.assignees = _team_write(d["assignees"])
        r.updated_by = request.user
        r.save()
        return Response(referral_retrieve(r))

    # ---- questionnaires & responses
    @action(detail=True, methods=["get"])
    def questionnaires(self, request, external_id=None):
        r = self._get(external_id)
        rows = _availability_questionnaires(
            r.availability, request.query_params.get("questionnaire_type")
        )
        results = [{**_questionnaire_read(q), "questionnaire_type": t} for q, t in rows]
        return Response({"count": len(results), "results": results})

    @action(detail=True, methods=["post"], url_path="questionnaire_responses")
    def add_response(self, request, external_id=None):
        r = self._get(external_id)
        q = get_object_or_404(Questionnaire, external_id=request.data.get("questionnaire"))
        mapping = {
            m.get("questionnaire"): m.get("questionnaire_type")
            for m in r.availability.questionnaires or []
        }
        qtype = mapping.get(str(q.external_id), "response")
        resp = ReferralQuestionnaireResponse.objects.create(
            referral=r,
            questionnaire=q,
            questionnaire_type=qtype,
            response=request.data.get("questionnaire_response") or {"results": []},
            questionnaire_revision=getattr(q, "version", 1) or 1,
            questionnaire_snapshot=_questionnaire_read(q),
            created_by=request.user,
            updated_by=request.user,
        )
        if r.status == "pending":
            r.status = "in_progress"
            r.save(update_fields=["status", "modified_date"])
        return Response(response_read(resp), status=http.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"questionnaire_responses/(?P<response_id>[^/.]+)",
    )
    def void_response(self, request, external_id=None, response_id=None):
        r = self._get(external_id)
        resp = get_object_or_404(r.responses, external_id=response_id)
        resp.status = "entered_in_error"
        resp.updated_by = request.user
        resp.save()
        return Response(status=http.HTTP_204_NO_CONTENT)

    # ---- conversation
    @action(detail=True, methods=["post"])
    def thread(self, request, external_id=None):
        r = self._get(external_id)
        if not r.thread_id:
            r.thread_id = uuid.uuid4()
            r.save(update_fields=["thread_id"])
        now = timezone.now().isoformat()
        return Response(
            {
                "id": str(r.thread_id),
                "title": request.data.get("title") or r.title,
                "thread_type": "referral",
                "created_by": _user(request.user.id),
                "updated_by": _user(request.user.id),
                "created_date": now,
                "modified_date": now,
            },
            status=http.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "post"])
    def notes(self, request, external_id=None):
        r = self._get(external_id)
        if request.method == "POST":
            msg = (request.data.get("message") or "").strip()
            if not msg:
                raise ValidationError({"message": "Message is required"})
            if not r.thread_id:
                r.thread_id = uuid.uuid4()
                r.save(update_fields=["thread_id"])
            n = ReferralNote.objects.create(
                referral=r,
                message=msg,
                message_history={"history": []},
                created_by=request.user,
                updated_by=request.user,
            )
            return Response(note_read(n), status=http.HTTP_201_CREATED)
        return _paginate(
            request, r.notes.filter(deleted=False).order_by("-created_date"), note_read
        )

    @action(
        detail=True,
        methods=["get", "put"],
        url_path=r"notes/(?P<message_id>[^/.]+)",
    )
    def note_detail(self, request, external_id=None, message_id=None):
        r = self._get(external_id)
        n = get_object_or_404(r.notes, external_id=message_id, deleted=False)
        if request.method == "PUT":
            msg = (request.data.get("message") or "").strip()
            if not msg:
                raise ValidationError({"message": "Message is required"})
            hist = (n.message_history or {}).get("history", [])
            hist.append(
                {
                    "message": n.message,
                    "created_by": {
                        "username": request.user.username,
                        "external_id": str(request.user.external_id),
                    },
                    "edited_at": timezone.now().isoformat(),
                    "created_at": n.created_date.isoformat(),
                }
            )
            n.message, n.message_history, n.updated_by = msg, {"history": hist}, request.user
            n.save()
        return Response(note_read(n))

    # ---- shared records
    @action(detail=True, methods=["get", "post"])
    def resources(self, request, external_id=None):
        r = self._get(external_id)
        if request.method == "POST":
            d = request.data
            if d.get("resource_type") not in RESOLVERS:
                raise ValidationError({"resource_type": "Unsupported resource type"})
            x = ReferralResource.objects.create(
                referral=r,
                resource_type=d["resource_type"],
                resource_id=str(d.get("resource_id")),
                attachment_type=d.get("attachment_type", "source"),
                metadata=d.get("metadata") or {},
                created_by=request.user,
                updated_by=request.user,
            )
            return Response(resource_read(x), status=http.HTTP_201_CREATED)
        qs = r.resources.filter(deleted=False).order_by("-created_date")
        p = request.query_params
        if p.get("attachment_type"):
            qs = qs.filter(attachment_type=p["attachment_type"])
        if p.get("resource_type"):
            qs = qs.filter(resource_type=p["resource_type"])
        return _paginate(request, qs, resource_read)

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"resources/(?P<resource_id>[^/.]+)",
    )
    def resource_delete(self, request, external_id=None, resource_id=None):
        r = self._get(external_id)
        x = get_object_or_404(r.resources, external_id=resource_id, deleted=False)
        x.deleted = True
        x.save(update_fields=["deleted"])
        return Response(status=http.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["get"],
        url_path=r"resources/(?P<resource_id>[^/.]+)/resolve",
    )
    def resource_resolve(self, request, external_id=None, resource_id=None):
        r = self._get(external_id)
        x = get_object_or_404(r.resources, external_id=resource_id, deleted=False)
        resolver = RESOLVERS.get(x.resource_type)
        data = resolver(x.resource_id) if resolver else None
        if data is None:
            return Response({"detail": "Resource not found"}, status=404)
        return Response({"resource_type": x.resource_type, "resource": data})


class ReferralAssignedViewSet(ReferralViewSet):
    """/referral_assigned/ — referrals where the current user is an assignee."""

    def list(self, request):
        qs = self._qs(request).filter(
            assignees__contains=[{"user_id": str(request.user.external_id)}]
        )
        return _paginate(request, qs, referral_read)


# --------------------------------------------------------- resource resolve
def _spec_resolver(model_path, spec_path):
    def resolve(resource_id):
        import importlib

        mod, cls = model_path.rsplit(".", 1)
        model = getattr(importlib.import_module(mod), cls)
        smod, scls = spec_path.rsplit(".", 1)
        spec = getattr(importlib.import_module(smod), scls)
        obj = model.objects.filter(external_id=resource_id).first()
        return spec.serialize(obj).to_json() if obj else None

    return resolve


def _referral_resolver(resource_id):
    r = Referral.objects.filter(external_id=resource_id, deleted=False).first()
    return referral_read(r) if r else None


RESOLVERS = {
    "condition": _spec_resolver(
        "care.emr.models.condition.Condition",
        "care.emr.resources.condition.spec.ConditionReadSpec",
    ),
    "questionnaire_response": _spec_resolver(
        "care.emr.models.questionnaire.QuestionnaireResponse",
        "care.emr.resources.questionnaire_response.spec.QuestionnaireResponseReadSpec",
    ),
    "diagnostic_report": _spec_resolver(
        "care.emr.models.diagnostic_report.DiagnosticReport",
        "care.emr.resources.diagnostic_report.spec.DiagnosticReportListSpec",
    ),
    "file_upload": _spec_resolver(
        "care.emr.models.file_upload.FileUpload",
        "care.emr.resources.file_upload.spec.FileUploadListSpec",
    ),
    "specimen": _spec_resolver(
        "care.emr.models.specimen.Specimen",
        "care.emr.resources.specimen.spec.SpecimenReadSpec",
    ),
    "referral": _referral_resolver,
}
