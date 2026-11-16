"""Label stock settings used by the panel's browser label printing."""
import math

from django.http import JsonResponse

from .api_common import APIError, audit, endpoint, fields, membership_context, method, payload
from .views_terminal import DEVICE_ROLES

SENSORS = ("gap", "mark", "continuous")
DEFAULT_LABEL = {"width_mm": 50, "height_mm": 30, "gap_mm": 2, "sensor": "gap"}


def _number(value, name, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise APIError(f"{name} inválido")
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


def label_spec(data):
    fields(data, ("width_mm", "height_mm", "gap_mm", "sensor"), ("width_mm", "height_mm", "gap_mm", "sensor"))
    if data["sensor"] not in SENSORS:
        raise APIError("Tipo de sensor inválido")
    return {"width_mm": _number(data["width_mm"], "Largura", 15, 108),
            "height_mm": _number(data["height_mm"], "Altura", 10, 200),
            "gap_mm": 0 if data["sensor"] == "continuous" else _number(data["gap_mm"], "Espaço entre etiquetas", 0, 10),
            "sensor": data["sensor"]}


@endpoint
def label_settings(request):
    method(request, "GET", "PATCH")
    if request.method == "GET":
        member = membership_context(request)
        return JsonResponse({**DEFAULT_LABEL, **(member.tenant.label_settings or {})})
    member = membership_context(request, roles=DEVICE_ROLES, write=True)
    spec = label_spec(payload(request))
    member.tenant.label_settings = spec
    member.tenant.save(update_fields=["label_settings"])
    audit(member.tenant, member.user, "label_settings.updated", member.tenant, spec)
    return JsonResponse(spec)
