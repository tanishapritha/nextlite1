import argparse
import os
import sys
import requests
import urllib3

BASE_URL = os.getenv(
    "CRM_BASE_URL",
    "https://nextlite-voice-prod.indiasouthcentral.cloudapp.azure.com",
).rstrip("/")
TENANT_KEY = os.getenv("GLAZE_CRM_TENANT_KEY", "")
DATE = os.getenv("CRM_TEST_DATE", "2026-09-30")

SLOTS_URL = f"{BASE_URL}/api/v1/integrations/whatsapp/slots"
BOOK_URL = f"{BASE_URL}/api/v1/integrations/whatsapp/appointments/book"


def headers():
    return {
        "X-Tenant-Key": TENANT_KEY,
        "ngrok-skip-browser-warning": "true",
        "Content-Type": "application/json",
    }


def check_slots(verify: bool):
    print(f"\n[slots] verify_ssl={verify} date={DATE}")
    try:
        r = requests.get(
            SLOTS_URL,
            params={"date": DATE},
            headers=headers(),
            timeout=15,
            verify=verify,
        )
        print("HTTP:", r.status_code)
        print("Body:", r.text[:1000])
        try:
            data = r.json()
            print("availableSlots:", data.get("availableSlots"))
        except ValueError:
            pass
        return r
    except requests.exceptions.SSLError as e:
        print("TLS ERROR:", repr(e))
        return None
    except requests.RequestException as e:
        print("REQUEST ERROR:", repr(e))
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", action="store_true", help="also test one booking request")
    parser.add_argument("--time", default=os.getenv("CRM_TEST_TIME", "06:00 PM"))
    parser.add_argument("--name", default=os.getenv("CRM_TEST_NAME", "CRM Diagnostic"))
    args = parser.parse_args()

    if not TENANT_KEY:
        print("ERROR: GLAZE_CRM_TENANT_KEY is not set.")
        sys.exit(2)

    print("CRM:", BASE_URL)
    print("Tenant key: configured (value hidden)")
    print("Date:", DATE)

    verified = check_slots(True)

    if verified is None:
        print("\nVerified TLS request failed.")
        print("Trying the same request with certificate verification disabled...")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        insecure = check_slots(False)

        if insecure is not None and insecure.status_code == 200:
            print("\nDIAGNOSIS: CRM API is reachable and tenant authentication works,")
            print("but the CRM HTTPS certificate/hostname is invalid for this URL.")
            print("Fix the certificate/hostname on the CRM/Azure side.")
        elif insecure is not None and insecure.status_code in (401, 403):
            print("\nDIAGNOSIS: network/TLS works, but the CRM tenant key is rejected.")
        elif insecure is not None:
            print("\nDIAGNOSIS: CRM is reachable, but its API returned an error.")
        return

    if verified.status_code == 200:
        print("\nDIAGNOSIS: CRM slots endpoint works with normal TLS.")
    elif verified.status_code in (401, 403):
        print("\nDIAGNOSIS: CRM rejected the tenant key.")
    else:
        print("\nDIAGNOSIS: CRM endpoint is reachable but returned HTTP", verified.status_code)

    if args.book:
        payload = {
            "customerName": args.name,
            "customerPhone": "919999988888",
            "bookingDate": DATE,
            "bookingTime": args.time,
            "title": "WhatsApp Consultation",
            "age": "",
            "place": "",
        }
        print("\n[booking]")
        try:
            r = requests.post(
                BOOK_URL,
                headers=headers(),
                json=payload,
                timeout=20,
                verify=True,
            )
            print("HTTP:", r.status_code)
            print("Body:", r.text[:1500])
        except requests.RequestException as e:
            print("REQUEST ERROR:", repr(e))


if __name__ == "__main__":
    main()
