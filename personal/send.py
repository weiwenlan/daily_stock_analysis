"""Send rendered email artifacts and run a safe Gmail OAuth preflight.

Usage:
    python3 send.py <path-to-rendered.json> [subject-prefix]
    python3 send.py --check-auth --result-path <path>
    python3 send.py --failure-alert --run-date <date> --step <name>
        --error-file <path> [--artifact <path> ...]
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage


CRED_PATH = os.path.expanduser("~/.gmail-mcp/credentials.json")
KEYS_PATH = os.path.expanduser("~/.gmail-mcp/gcp-oauth.keys.json")
TO = os.environ.get("AUTOMATION_EMAIL_TO", "weiwenlan1999@gmail.com")
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class SenderError(RuntimeError):
    """A delivery or sender-boundary failure."""


class OAuthError(SenderError):
    """An OAuth refresh or preflight failure with a safe error type."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path, value):
    """Write JSON through a same-directory temporary file and replace."""
    destination = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(destination)) or "."
    basename = os.path.basename(destination)
    existing_mode = None
    try:
        existing_mode = stat.S_IMODE(os.stat(destination).st_mode)
    except FileNotFoundError:
        pass

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{basename}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            os.chmod(
                temporary_path,
                existing_mode if existing_mode is not None else 0o600,
            )
            json.dump(value, temporary, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _sanitize_text(value, secrets=()):
    """Remove known credential values, bearer headers, and control characters."""
    text = str(value)
    for secret in sorted(
        {secret for secret in secrets if isinstance(secret, str) and secret},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(\bbearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    return "".join(
        character
        if ord(character) >= 32 and ord(character) != 127
        else " "
        for character in text
    )


def _credential_secrets():
    """Collect credential values for in-memory redaction only."""
    secrets = set()
    for path in (CRED_PATH, KEYS_PATH):
        try:
            document = _read_json(path)
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        for section in (document, document.get("tokens"), document.get("installed")):
            if not isinstance(section, dict):
                continue
            for key in ("access_token", "refresh_token", "client_id", "client_secret"):
                value = section.get(key)
                if isinstance(value, str) and value:
                    secrets.add(value)
    return secrets


def _safe_oauth_error(exc, tokens=None, installed=None):
    """Format OAuth errors without echoing response bodies or credentials."""
    secrets = set(_credential_secrets())
    for document in (tokens or {}, installed or {}):
        if isinstance(document, dict):
            secrets.update(
                value
                for value in document.values()
                if isinstance(value, str) and value
            )

    if isinstance(exc, urllib.error.HTTPError):
        status = getattr(exc, "code", "unknown")
        detail = ""
        try:
            body = exc.read()
        except (OSError, ValueError):
            body = b""
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                error = parsed.get("error")
                description = parsed.get("error_description")
                detail = ": ".join(
                    str(part) for part in (error, description) if part
                )
        message = f"token endpoint returned HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
    elif isinstance(exc, urllib.error.URLError):
        message = f"token endpoint request failed: {exc.reason}"
    else:
        message = str(exc)
    return _sanitize_text(message, secrets)


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def refresh_access_token() -> str:
    """Exchange the stored refresh token without mutating credentials on failure."""
    credentials = {}
    tokens = {}
    installed = {}
    try:
        credentials = _read_json(CRED_PATH)
        tokens = credentials["tokens"]
        installed = _read_json(KEYS_PATH)["installed"]
        if not isinstance(tokens, dict) or not isinstance(installed, dict):
            raise TypeError("OAuth credential sections must be objects")
        refresh_token = tokens["refresh_token"]
        client_id = installed["client_id"]
        client_secret = installed["client_secret"]
        if not all(
            isinstance(value, str) and value
            for value in (refresh_token, client_id, client_secret)
        ):
            raise ValueError("OAuth credentials are incomplete")
        fresh = _post(
            TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not isinstance(fresh, dict):
            raise TypeError("token endpoint response must be an object")
        access = fresh["access_token"]
        if not isinstance(access, str) or not access:
            raise ValueError("token endpoint response missing access token")
        expires_in = int(fresh.get("expires_in", 3600))
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        TimeoutError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as exc:
        raise OAuthError(
            type(exc).__name__,
            _safe_oauth_error(exc, tokens, installed),
        ) from exc

    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = access
    updated_tokens["expiry_date"] = int((time.time() + expires_in) * 1000)
    updated_credentials = dict(credentials)
    updated_credentials["tokens"] = updated_tokens
    _atomic_write_json(CRED_PATH, updated_credentials)
    return access


def access_token(force_refresh: bool = False) -> str:
    """Return a usable cached token, or explicitly exchange the refresh token."""
    if force_refresh:
        return refresh_access_token()

    try:
        credentials = _read_json(CRED_PATH)
        tokens = credentials["tokens"]
        if (
            isinstance(tokens, dict)
            and tokens.get("access_token")
            and tokens.get("expiry_date", 0) > (time.time() + 60) * 1000
        ):
            return tokens["access_token"]
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return refresh_access_token()


def _expiry_timestamp(credentials_path):
    credentials = _read_json(credentials_path)
    expiry_ms = int(credentials["tokens"]["expiry_date"])
    return (
        datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_timestamp():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def check_auth(result_path):
    """Force one OAuth exchange and write only safe operational metadata."""
    result_path = os.fspath(result_path)
    parent = os.path.dirname(os.path.abspath(result_path)) or "."
    os.makedirs(parent, exist_ok=True)
    base = {
        "schema_version": 1,
        "checked_at": _utc_timestamp(),
        "refresh_attempted": True,
    }
    try:
        access_token(force_refresh=True)
        result = {
            **base,
            "status": "ok",
            "access_token_expires_at": _expiry_timestamp(CRED_PATH),
        }
    except OAuthError as exc:
        result = {
            **base,
            "status": "failed",
            "error_type": exc.error_type,
            "error": _sanitize_text(str(exc), _credential_secrets()),
        }
        _atomic_write_json(result_path, result)
        print(result["error"], file=sys.stderr)
        return 1

    _atomic_write_json(result_path, result)
    return 0


def build_message(payload, recipient, subject_prefix=None):
    subject = payload["subject"]
    if subject_prefix:
        subject = f"{subject_prefix} {subject}"

    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(payload["text"])
    msg.add_alternative(payload["html"], subtype="html")
    return msg


def _safe_delivery_error(exc):
    return _sanitize_text(str(exc), _credential_secrets())


def send_message(message: EmailMessage) -> dict:
    """Send exactly one Gmail message and require its non-empty message ID."""
    token = access_token()
    try:
        result = _post(
            GMAIL_SEND_URL,
            json.dumps(
                {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
            ).encode(),
            {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
    except (
        OSError,
        TimeoutError,
        ValueError,
        TypeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as exc:
        raise SenderError(_safe_delivery_error(exc)) from exc

    message_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(message_id, str) or not message_id.strip():
        raise SenderError("Gmail accepted no message id")
    return {"id": message_id}


def send_payload(payload, recipient, subject_prefix=None):
    return send_message(build_message(payload, recipient, subject_prefix))


def build_failure_alert(run_date, step, error, artifacts, recipient):
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = f"[FAILED] Daily Trading Brief {run_date}"
    message.set_content(
        "\n".join(
            [
                f"Daily Trading Brief failed for {run_date}.",
                f"Step: {step}",
                f"Error: {error}",
                "Artifacts:",
                *[f"- {path}" for path in artifacts],
            ]
        )
        + "\n"
    )
    return message


def send_failure_alert(run_date, step, error_path, artifacts, recipient):
    try:
        with open(error_path, encoding="utf-8") as error_file:
            original_error = error_file.read()
    except (OSError, UnicodeError) as exc:
        raise SenderError(_safe_delivery_error(exc)) from exc

    secrets = _credential_secrets()
    safe_error = _sanitize_text(original_error, secrets)
    safe_run_date = _sanitize_text(run_date, secrets)
    safe_step = _sanitize_text(step, secrets)
    safe_artifacts = [_sanitize_text(path, secrets) for path in artifacts]
    return send_message(
        build_failure_alert(
            safe_run_date,
            safe_step,
            safe_error,
            safe_artifacts,
            _sanitize_text(recipient, secrets),
        )
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Send a rendered Gmail message or run the OAuth preflight."
    )
    parser.add_argument("payload", nargs="?", help="rendered JSON payload")
    parser.add_argument("subject_prefix", nargs="?", help="optional subject prefix")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-auth", action="store_true", help="force an OAuth preflight"
    )
    modes.add_argument(
        "--failure-alert",
        action="store_true",
        help="send one plaintext failure alert",
    )
    parser.add_argument("--result-path", help="OAuth preflight result path")
    parser.add_argument("--run-date", help="failed run date")
    parser.add_argument("--step", help="failed pipeline step")
    parser.add_argument("--error-file", help="path containing the original error")
    parser.add_argument(
        "--artifact", action="append", default=[], help="generated artifact path"
    )
    return parser


def _reject_unrelated_options(parser, args, allowed):
    values = {
        "result-path": args.result_path,
        "run-date": args.run_date,
        "step": args.step,
        "error-file": args.error_file,
        "artifact": args.artifact,
    }
    unexpected = [
        name for name, value in values.items() if name not in allowed and value
    ]
    if unexpected:
        parser.error(
            f"options not valid for this mode: {', '.join(unexpected)}"
        )


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)

    if args.check_auth:
        _reject_unrelated_options(parser, args, {"result-path"})
        if not args.result_path or args.payload or args.subject_prefix:
            parser.error(
                "--check-auth requires --result-path and no positional payload"
            )
        return check_auth(args.result_path)

    if args.failure_alert:
        _reject_unrelated_options(
            parser,
            args,
            {"run-date", "step", "error-file", "artifact"},
        )
        if (
            args.payload
            or args.subject_prefix
            or not args.run_date
            or not args.step
            or not args.error_file
        ):
            parser.error(
                "--failure-alert requires --run-date, --step, --error-file, "
                "and no positional payload"
            )
        try:
            result = send_failure_alert(
                args.run_date,
                args.step,
                args.error_file,
                args.artifact,
                TO,
            )
        except SenderError as exc:
            print(_safe_delivery_error(exc), file=sys.stderr)
            return 1
        print(json.dumps({"id": result["id"]}))
        return 0

    if not args.payload:
        parser.error("normal send requires a rendered JSON payload")
    _reject_unrelated_options(parser, args, set())
    try:
        with open(args.payload, encoding="utf-8") as payload_file:
            payload = json.load(payload_file)
        result = send_payload(payload, TO, args.subject_prefix)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(_safe_delivery_error(exc), file=sys.stderr)
        return 1
    except SenderError as exc:
        print(_safe_delivery_error(exc), file=sys.stderr)
        return 1
    subject = build_message(payload, TO, args.subject_prefix)["Subject"]
    print(json.dumps({"to": TO, "subject": subject, "id": result["id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
