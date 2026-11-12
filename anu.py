import base64
import json
import random
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = ("https://i.instagram.com/api/v1/bloks/async_action/"
       "com.bloks.www.caa.ar.search.async/")
BLOKS_VERSION = ("5e47baf35c5a270b44c8906c8b99063564b30ef69779f3dee0b828bee2e4ef5b")
APP_USER_AGENT = ("Instagram 320.0.0.34.109 Android (33/13; 420dpi; "
                  "1080x2340; samsung; SM-A546B; a54x; exynos1380; "
                  "en_US; 465123678)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

DEADLINE_SECS = 60.0
ATTEMPTS = 2
PAUSE_BETWEEN = 3.0
READ_TIMEOUT = 20

def build_request(email):
    device = "android-" + "".join(random.choices("abcdef0123456789", k=16))
    family = str(uuid.uuid4())
    android = "android-" + "".join(random.choices("abcdef0123456789", k=16))
    waterfall = str(uuid.uuid4())

    params = (
        "{\"client_input_params\":{\"aac\":\"{\\\"aac_init_timestamp\\\":"
        + str(int(time.time()))
        + ",\\\"aacjid\\\":\\\"" + str(uuid.uuid4())
        + "\\\",\\\"aaccs\\\":\\\"" + secrets.token_urlsafe(32)
        + "\\\"}\",\"flash_call_permissions_status\":{\"READ_PHONE_STATE\":"
          "\"PERMANENTLY_DENIED\",\"READ_CALL_LOG\":\"DENIED\","
          "\"ANSWER_PHONE_CALLS\":\"DENIED\"},\"was_headers_prefill_available\":0,"
          "\"network_bssid\":null,\"sfdid\":\"\",\"fetched_email_token_list\":{},"
          "\"search_query\":\""
        + email
        + "\",\"auth_secure_device_id\":\"\",\"ig_oauth_token\":[],"
          "\"cloud_trust_token\":null,\"was_headers_prefill_used\":0,"
          "\"sso_accounts_auth_data\":[],\"encrypted_msisdn\":\"\","
          "\"device_network_info\":null,\"text_input_id\":\"akyuf0:61\","
          "\"zero_balance_state\":null,\"android_build_type\":\"release\","
          "\"accounts_list\":[],\"is_oauth_without_permission\":0,"
          "\"ig_android_qe_device_id\":\""
        + device
        + "\",\"gms_incoming_call_retriever_eligibility\":\"client_not_supported\","
          "\"search_screen_type\":\"email_or_username\",\"is_whatsapp_installed\":1,"
          "\"lois_settings\":{\"lois_token\":\"\"},\"ig_vetted_device_nonce\":null,"
          "\"headers_infra_flow_id\":\"\",\"fetched_email_list\":[]},"
          "\"server_params\":{\"event_request_id\":\""
        + str(uuid.uuid4())
        + "\",\"is_from_logged_out\":0,\"layered_homepage_experiment_group\":null,"
          "\"device_id\":\""
        + android
        + "\",\"login_surface\":\"login_home\",\"waterfall_id\":\""
        + waterfall
        + "\",\"INTERNAL__latency_qpl_instance_id\":6.3987980400102E13,"
          "\"is_platform_login\":0,\"context_data\":\"\","
          "\"login_entry_point\":\"logged_out\","
          "\"INTERNAL__latency_qpl_marker_id\":36707139,\"family_device_id\":\""
        + family
        + "\",\"offline_experiment_group\":\"caa_iteration_v3_perf_ig_4\","
          "\"access_flow_version\":\"pre_mt_behavior\","
          "\"is_from_logged_in_switcher\":0,\"qe_device_id\":\""
        + device + "\"}}"
    )

    payload = {
        "params": params,
        "bk_client_context": ("{\"bloks_version\":\"" + BLOKS_VERSION
                              + "\",\"styles_id\":\"instagram\"}"),
        "bloks_versioning_id": BLOKS_VERSION,
    }

    headers = {
        "User-Agent": APP_USER_AGENT,
        "accept-language": "en-IN, en-US",
        "x-bloks-version-id": BLOKS_VERSION,
        "x-fb-friendly-name": ("IgApi: bloks/async_action/"
                               "com.bloks.www.caa.ar.search.async/"),
        "x-ig-android-id": android,
        "x-ig-app-id": "567067343352427",
        "x-ig-app-locale": "en_IN",
        "x-ig-client-endpoint": "com.bloks.www.caa.ar.search",
        "x-ig-device-id": device,
        "x-ig-family-device-id": family,
        "x-ig-timezone-offset": str(int(datetime.now().astimezone()
                                        .utcoffset().total_seconds())),
        "x-mid": base64.urlsafe_b64encode(secrets.token_bytes(18))
                    .decode().rstrip("="),
        "x-pigeon-rawclienttime": str(time.time()),
        "x-pigeon-session-id": f"UFS-{uuid.uuid4()}-0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
    }
    return payload, headers


def send_request(payload, headers):
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=READ_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def classify(email, status_code, body):
    if status_code == 429:
        return "ig_error", "http_429"
    if status_code != 200:
        return "ig_error", f"http_{status_code}"
    try:
        data = json.loads(body)
    except Exception:
        return "ig_error", "non_json"
    if data.get("status") != "ok":
        return "ig_error", f"status_{data.get('status')}"

    low = body.lower()
    echoed = email.lower() in low
    has_screen = "embedded_payloads" in body
    size = len(body)

    if has_screen and size > 100000:
        return "ig_connected", f"len={size}"
    if (not echoed) and (not has_screen) and size < 100000:
        return "ig_not_connected", f"len={size}"
    return "ig_error", f"unexpected len={size}"


def ask_email(argv):
    for a in argv:
        if not a.startswith("--") and EMAIL_RE.fullmatch(a or ""):
            return a.strip().lower()
    while True:
        try:
            raw = input("\n Enter email: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n Cancelled.")
            sys.exit(1)
        if EMAIL_RE.fullmatch(raw):
            return raw
        print("   ❌ That does not look like a valid email. Try again.")


def run_recovery(email):
    deadline = time.time() + DEADLINE_SECS
    last, detail = "ig_error", "-"
    for i in range(1, ATTEMPTS + 1):
        payload, headers = build_request(email)
        try:
            status_code, body = send_request(payload, headers)
            last, detail = classify(email, status_code, body)
        except Exception as e:
            last, detail = "ig_error", type(e).__name__
        print(f"   attempt {i}/{ATTEMPTS}: {last} ({detail})", flush=True)
        if last != "ig_error":
            return last
        if time.time() > deadline:
            break
        if i < ATTEMPTS:
            time.sleep(PAUSE_BETWEEN)
    return last


def main():
    email = ask_email(sys.argv[1:])

    print("=" * 58)
    print(" INSTAGRAM RESET LINK SENDER")
    print(f" target: {email}")
    print("=" * 58)
    print("\n [1/1] Running the account-recovery flow (direct IP)...",
          flush=True)

    t0 = time.time()
    try:
        status = run_recovery(email)
    except KeyboardInterrupt:
        print("\n Cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"   ⚠️ temporary failure ({type(e).__name__})", flush=True)
        status = "ig_error"
    el = time.time() - t0

    print("-" * 58)
    if status == "ig_connected":
        print(f" ✅ Reset link sent to {email}  ({el:.1f}s)")
        print("    Open that inbox (check spam too) and click the link")
        print("    Instagram just mailed you to reset the password.")
        code = 0
    elif status == "ig_not_connected":
        print(f" ❌ No Instagram account is registered with {email}  ({el:.1f}s)")
        print("    Nothing was sent - there is no account to reset.")
        code = 2
    else:
        print(f" ⚠️ Instagram did not answer (blocked/busy)  ({el:.1f}s)")
        print("    Wait a minute and run it again - no link was sent.")
        code = 1
    print("-" * 58)
    sys.exit(code)


if __name__ == "__main__":
    main()
