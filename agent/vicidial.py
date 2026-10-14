"""VICIdial integration: log dispositions / update leads via the non-agent API, and
push structured qualification results back to the dialer.
"""
from __future__ import annotations

import requests


class ViciDial:
    def __init__(self, config):
        self.config = config
        self.base = config.vicidial_url.rstrip("/") if config.vicidial_url else ""
        self.user = config.vicidial_user
        self.password = config.vicidial_pass
        self.source = config.vicidial_source

    def enabled(self) -> bool:
        return bool(self.base and self.user and self.password)

    def _call(self, params: dict) -> dict:
        params = {
            "user": self.user,
            "pass": self.password,
            "source": self.source,
            "output": "json",
            **params,
        }
        try:
            r = requests.get(f"{self.base}/vicidial/non_agent_api.php", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def update_disposition(self, lead_id: str, status: str, extra: dict | None = None) -> dict:
        """Update a lead's status (disposition) in VICIdial.

        status maps to a VICIdial status code, e.g. 'Q' qualified, 'NI' not interested,
        'DNC', 'INC' ineligible. Configure these in VICIdial's status categories.
        """
        params = {"function": "update_lead", "lead_id": lead_id, "status": status}
        if extra:
            fields = []
            for k, v in extra.items():
                if v is None or v == "":
                    continue
                params[k] = str(v)
                fields.append(k)
            if fields:
                params["update_fields"] = ",".join(fields)
        return self._call(params)

    def add_lead(self, phone: str, extra: dict | None = None) -> dict:
        params = {
            "function": "add_lead",
            "phone_number": phone,
            "status": "NEW",
            "list_id": extra.pop("list_id", "999") if extra else "999",
        }
        if extra:
            for k, v in extra.items():
                if v is not None and v != "":
                    params[k] = str(v)
        return self._call(params)

    def log_qualification(self, lead_id: str, disposition: str, facts: dict) -> dict:
        status_map = {
            "QUALIFIED": "Q",
            "NI": "NI",
            "AGE_UNDER": "INC",
            "AGE_OVER": "INC",
            "HEALTH_INELIGIBLE": "INC",
            "STATE_UNLICENSED": "INC",
            "IN_PROGRESS": "QC",
        }
        status = status_map.get(disposition, "QC")
        extra = {
            "age": facts.get("age"),
            "state": facts.get("state"),
            "callback_number": facts.get("callback_number"),
            "best_time": facts.get("best_time"),
        }
        return self.update_disposition(lead_id, status, extra)
