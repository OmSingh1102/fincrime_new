"""Mock identity-verification vendor payload — the "Details" behind a vendor
row. The names are text labels; nothing here calls a vendor."""


def details(case):
    vc = case["vendor_check"]
    ex = vc["extracted"]
    return {
        "vendor": vc["vendor"],
        "result": vc["result"],
        "checked_at": vc["checked_at"],
        "checks": [
            ("Document type", ex.get("id_type", "")),
            ("Document number", ex.get("id_number", "")),
            ("Name (OCR)", ex.get("name", "")),
            ("Date of birth (OCR)", ex.get("dob", "")),
            ("Nationality (OCR)", ex.get("nationality", "")),
            ("Expiry (OCR)", ex.get("expiry", "")),
            ("Face match", "Pass" if vc["result"] == "Approved" else "Not assessed"),
            ("Liveness", "Pass" if vc["result"] == "Approved" else "Not assessed"),
            ("Tamper check", "Pass" if vc["result"] == "Approved" else "Review"),
            ("Vendor reason", vc.get("vendor_reason") or "-"),
        ],
    }
