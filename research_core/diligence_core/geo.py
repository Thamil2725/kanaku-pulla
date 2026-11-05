"""WGS84 ellipsoidal distance, not an address-to-fraud classifier."""
from geographiclib.geodesic import Geodesic

from .models import Coordinates


def distance_m(a: Coordinates, b: Coordinates) -> float:
    return round(Geodesic.WGS84.Inverse(a.lat, a.lon, b.lat, b.lon)["s12"], 2)


def compare_location(result, trusted):
    if result.get("status") != "located" or not trusted:
        return {"status": "unverified", "reason": "A located address and trusted reference coordinates are both required"}
    if result.get("partial_match") or result.get("precision") not in {"ROOFTOP", "GPS"}:
        return {"status": "unverified", "reason": "Approximate or partial match; no location discrepancy conclusion"}
    meters = distance_m(Coordinates.model_validate(result["coordinates"]), Coordinates.model_validate(trusted["coordinates"]))
    return {"status": "discrepancy" if meters > trusted.get("tolerance_m", 5000) else "consistent",
            "distance_m": meters, "method": "WGS84 ellipsoidal geodesic",
            "threshold_m": trusted.get("tolerance_m", 5000), "reference_source": trusted["source"],
            "reference_coordinates": trusted["coordinates"],
            "reference_observed_at": trusted["observed_at"],
            "meaning": "Address consistency only; not proof of fraud, ownership, or business activity"}
