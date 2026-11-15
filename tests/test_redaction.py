import base64
import re
import time
from collections.abc import Iterable
from typing import Any

import pytest

from promptmend.redaction import compile_extra, is_bare_token, safe_repr, scan, scan_draft


# scan() flags a Luhn-valid payment card number.
def test_detects_payment_card() -> None:
    assert "payment_card" in scan("card 4111 1111 1111 1111")


# scan() does not flag a digit run that fails the Luhn checksum.
def test_ignores_non_luhn_digit_run() -> None:
    assert "payment_card" not in scan("order id 1234 5678 9012 3456")


# scan() flags an email address.
def test_detects_email() -> None:
    assert "email" in scan("contact john.doe@example.com")


# scan() flags a confidentiality label like "CONFIDENTIAL".
def test_detects_confidential_label() -> None:
    assert "confidential_label" in scan("This is CONFIDENTIAL material")


# scan() flags a Vietnamese national ID number.
def test_detects_vietnam_id() -> None:
    assert "vietnam_id_12" in scan("ID 012345678901")


# scan() flags a 9-digit Vietnamese ID only alongside a context keyword.
def test_detects_vietnam_id_9_with_context() -> None:
    assert "vietnam_id_9" in scan("CMND so 123456789")


# scan() does not flag a bare 9-digit number with no ID context.
def test_ignores_bare_9_digit_number() -> None:
    assert scan("123456789") == []


# scan() does not flag a large plain number like a formatted amount.
def test_ignores_large_plain_number() -> None:
    assert scan("budget is 1,500,000,000 VND") == []


# scan() flags an AWS access key ID.
def test_detects_aws_access_key() -> None:
    assert "aws_access_key" in scan("key AKIAIOSFODNN7EXAMPLE")


# scan() flags a JWT (assembled at runtime, like the fake keys below).
def test_detects_jwt() -> None:
    token = ".".join(
        [
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ]
    )
    assert "jwt" in scan(f"auth header {token}")


# scan() flags PEM, OpenSSH and PGP private key headers.
@pytest.mark.parametrize(
    "kind", ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK"]
)
def test_detects_pem_private_key(kind: str) -> None:
    assert "pem_private_key" in scan(f"-----BEGIN {kind}-----\nMIIB...")


# Text with no sensitive matches returns no findings.
def test_clean_text_has_no_findings() -> None:
    assert scan("Summarize the quarterly risk trend in plain terms") == []


# Text with a sensitive match returns findings.
def test_dirty_text_has_findings() -> None:
    assert scan("customer data for account 4111 1111 1111 1111") != []


# Fake keys are assembled at runtime so no key-shaped literal sits in the repo for
# secret scanners to flag.
_BODY = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


# scan() flags each vendor key format, including ones with '-'/'_' after a short prefix.
@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("sk-or-v1-" + "0123456789abcdef" * 4, "openrouter_key"),
        ("sk-ant-api03-" + _BODY, "anthropic_key"),
        ("sk-proj-" + _BODY, "openai_key"),
        ("sk-" + _BODY, "openai_key"),
        ("sk_live_" + _BODY, "stripe_key"),
        ("ghp_" + _BODY + "ab", "github_token"),
        ("github_pat_11" + _BODY, "github_token"),
        ("glpat-" + _BODY[:20], "gitlab_token"),
        ("hf_" + _BODY[:34], "huggingface_token"),
        ("xoxb-1234-" + _BODY, "slack_token"),
        ("AIza" + _BODY[:35], "google_api_key"),
        ("xai-" + _BODY, "xai_key"),
    ],
)
def test_detects_vendor_keys(text: str, name: str) -> None:
    assert name in scan(f"use key {text} please")


# An OpenRouter or Anthropic key is reported under its own name, not also as an OpenAI key.
def test_vendor_keys_are_not_double_reported_as_openai() -> None:
    assert "openai_key" not in scan("sk-or-v1-" + "0123456789abcdef" * 4)
    assert "openai_key" not in scan("sk-ant-api03-" + _BODY)


# The longest real-shaped secrets are still caught now that every repeat is bounded: a JWS
# whose header carries an x5c certificate chain (App Store Server API style, ~5.5k chars), a
# large payload, an RS512/4096-bit signature, and long vendor keys and bearer tokens.
@pytest.mark.parametrize(
    ("text", "name"),
    [
        (
            ".".join(["eyJ" + "a1B2-c3_D" * 600, "eyJ" + "x9Y8z7" * 2_000, "s1-G_" * 137]),
            "jwt",
        ),
        ("sk-or-v1-" + "0123456789abcdef" * 4, "openrouter_key"),
        ("sk-ant-api03-" + _BODY * 3 + "-AAAA", "anthropic_key"),
        ("sk-proj-" + _BODY * 5, "openai_key"),
        ("github_pat_11" + _BODY * 3, "github_token"),
        ("Bearer " + _BODY * 60, "bearer_token"),
        ("Bearer " + "a" * 3_000 + "-" + "b" * 3_000, "bearer_token"),
    ],
    ids=["jwt-x5c", "openrouter", "anthropic", "openai-proj", "github-pat", "bearer", "bearer-6k"],
)
def test_detects_longest_real_secrets(text: str, name: str) -> None:
    assert name in scan(f"use {text} please")


# The bounded jwt, bearer_token and pem_private_key patterns flag everything the unbounded
# ones before #36 flagged, at any length: a bound only stops the traversal of what search()
# does not need, and a run longer than a bound counts as a match.
_UNBOUNDED = {
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    "bearer_token": r"(?i)\bBearer\s+[A-Za-z0-9._-]{16,}\b",
    "pem_private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
}
_SIG = "s1-G_" * 137


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Bearer " + "a" * 5_000, "bearer_token"),
        ("Bearer" + " " * 40 + _BODY, "bearer_token"),
        ("Bearer\n" + "\t" * 200 + _BODY, "bearer_token"),
        (".".join(["eyJ" + "a1B2" * 10, "eyJ" + "x9Y8" * 4_300, _SIG]), "jwt"),
        (".".join(["eyJ" + "a1B2" * 2_250, "eyJ" + "x9Y8" * 10, _SIG]), "jwt"),
        ("Bearer " + ".".join(["eyJ" + "a1B2" * 10, "eyJ" + "x9Y8" * 4_300, _SIG]), "jwt"),
        ("-----BEGIN " + "ENCRYPTED " * 7 + "PRIVATE KEY-----", "pem_private_key"),
    ],
    ids=[
        "bearer-5k-alnum",
        "bearer-40-spaces",
        "bearer-200-tabs",
        "jwt-17k-payload",
        "jwt-9k-header",
        "bearer-jwt-17k",
        "pem-70-label",
    ],
)
def test_bounded_patterns_flag_what_unbounded_ones_did(text: str, name: str) -> None:
    assert re.search(_UNBOUNDED[name], text)
    assert name in scan(f"use {text} please")


# Every repeat in a built-in pattern has an upper bound, as the comment on _PATTERNS says:
# an unbounded one is tried at every start position and makes a long run quadratic.
def test_builtin_patterns_have_no_unbounded_repeat() -> None:
    import re._constants as sre
    import re._parser as parser

    from promptmend.redaction import _PATTERNS

    def unbounded(items: Iterable[tuple[Any, Any]]) -> bool:
        for op, av in items:
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                if av[1] == sre.MAXREPEAT or unbounded(av[2]):
                    return True
            elif op in (sre.SUBPATTERN, sre.ASSERT, sre.ASSERT_NOT):
                if unbounded(av[-1]):
                    return True
            elif (op is sre.ATOMIC_GROUP and unbounded(av)) or (
                op is sre.BRANCH and any(unbounded(b) for b in av[1])
            ):
                return True
        return False

    flagged = [n for n, p in _PATTERNS.items() if unbounded(parser.parse(p.pattern, p.flags))]
    assert flagged == []


# The short jwt and bearer_token patterns add no false positives of their own: a JWT's payload
# is base64url JSON too (eyJ), and a Bearer token holds a letter, digit or underscore within
# its first 16 characters.
_B64_JSON = "eyJ" + "a1B2" * 5


@pytest.mark.parametrize(
    ("text", "name"),
    [
        (f"cache file {_B64_JSON}.jsonl_backup saved", "jwt"),
        (f"{_B64_JSON}.payload_here_xx", "jwt"),
        ("Bearer\n----------------", "bearer_token"),
        ("Bearer ................ 12", "bearer_token"),
    ],
    ids=["jsonl-backup", "plain-payload", "bearer-dashes", "bearer-dots"],
)
def test_short_patterns_add_no_false_positive(text: str, name: str) -> None:
    assert name not in scan(text)


# Short or prose-like near misses are not flagged as keys.
@pytest.mark.parametrize("text", ["sk-learn is a library", "the ghp_ prefix", "AIza short"])
def test_ignores_key_near_misses(text: str) -> None:
    assert scan(text) == []


# scan() flags credentials embedded in a URL.
def test_detects_url_credentials() -> None:
    assert "url_credentials" in scan("dsn postgres://app:s3cretpw@db.internal:5432/x")


# A URL without a user:password part is not flagged.
def test_ignores_url_without_credentials() -> None:
    assert scan("see https://example.com:8080/path") == []


# scan() flags a password or token assignment whose value looks like a secret, including
# env-style names with a prefix, JSON keys and the AWS secret key. Values are assembled at
# runtime so no secret-shaped literal sits in the repo.
_VALUE = "a1b2c3" * 2


@pytest.mark.parametrize(
    "name",
    [
        "password=",
        "API_KEY: ",
        "DB_PASSWORD=",
        "client_secret: ",
        "access_token=",
        '"password": ',
        "AWS_SECRET_ACCESS_KEY=",
        "export GITHUB_TOKEN=",
    ],
)
def test_detects_secret_assignment(name: str) -> None:
    assert "secret_assignment" in scan(f"{name}'{_VALUE}'")


# Prose after "token:", or a longer word that merely contains a key name, is not a secret.
@pytest.mark.parametrize(
    "text",
    [
        "token: explanation of the design",
        "tokenizer: bert-base-2",
        "token_count=12345678",
        "max_tokens=24000000",
        "passwords: see policy doc",
    ],
)
def test_ignores_secret_assignment_near_misses(text: str) -> None:
    assert scan(text) == []


# A dot-separated card number is still a card.
def test_detects_dotted_payment_card() -> None:
    assert "payment_card" in scan("card 4111.1111.1111.1111")


# User-defined patterns are reported by position, never by pattern text.
def test_extra_patterns_reported_by_position() -> None:
    extra = compile_extra("project[- ]falcon; CUST-\\d{6}")
    assert scan("status of Project Falcon for CUST-123456", extra) == ["custom_1", "custom_2"]


# Empty entries in PROMPT_EXTRA_PATTERNS are skipped.
def test_extra_patterns_skip_empty_entries() -> None:
    assert compile_extra(" ; ;") == ()


# An invalid user regex raises ValueError naming its position, not its text, with the
# custom_N name a finding of that entry gets (both count the non-empty entries from 1).
def test_extra_patterns_invalid_regex() -> None:
    with pytest.raises(ValueError, match=r"entry 2 \(custom_2\) is not") as exc:
        compile_extra("ok;secret[")
    assert "secret" not in str(exc.value)


# scan() flags a Bearer token.
def test_detects_bearer_token() -> None:
    assert "bearer_token" in scan("Authorization: Bearer abcdefghijklmnop1234")


# scan() flags confidentiality labels in English and Vietnamese.
def test_detects_confidentiality_label_variants() -> None:
    for text in (
        "marked RESTRICTED",
        "for internal only use",
        "t\u00e0i li\u1ec7u mật",
        "l\u01b0u h\u00e0nh nội bộ",
    ):
        assert "confidential_label" in scan(text)


# scan() returns every matching finding, not just the first.
def test_scan_returns_multiple_findings() -> None:
    findings = scan("CONFIDENTIAL: contact john.doe@example.com about card 4111 1111 1111 1111")
    assert {"confidential_label", "email", "payment_card"} <= set(findings)


# A Luhn-failing digit run before a real card must not hide the card: scan() checks
# every candidate match, not just the first one.
def test_detects_card_after_non_luhn_digit_run() -> None:
    assert "payment_card" in scan("order 1234 5678 9012 3456, card 4111 1111 1111 1111")


# A Luhn-failing digit run after a real card is irrelevant to the finding.
def test_detects_card_before_non_luhn_digit_run() -> None:
    assert "payment_card" in scan("card 4111 1111 1111 1111, order 1234 5678 9012 3456")


# Separators copied from web pages and PDFs cannot split a card number: no-break space,
# zero-width space, soft hyphen, variation selectors, Hangul fillers and fullwidth digits are
# folded before scanning.
@pytest.mark.parametrize(
    "text",
    [
        "card 4111\u00a01111\u00a01111\u00a01111",
        "card 4111\u200b1111\u200b1111\u200b1111",
        "card 4111\u00ad1111\u00ad1111\u00ad1111",
        "card 4111\ufe0f1111\ufe0f1111\ufe0f1111",
        "card 4111\U000e01011111\U000e01011111\U000e01011111",
        "card 4111\u31641111\u31641111\u31641111",
        "card 4111\uffa01111\uffa01111\uffa01111",
        "card 4111\u034f1111\u034f1111\u034f1111",
        "card 4111\u180b1111\u180b1111\u180b1111",
        "card 4111\u17b41111\u17b41111\u17b41111",
        "card 4111\ufff01111\ufff01111\ufff01111",
        "card 4111\U000e02001111\U000e02001111\U000e02001111",
        "card 4111\u28001111\u28001111\u28001111",
        # Fullwidth digits U+FF10..FF19.
        "card "
        + "4111 1111 1111 1111".translate({ord(d): ord(d) + 0xFF10 - 0x30 for d in "0123456789"}),
    ],
)
def test_detects_card_behind_unicode_separators(text: str) -> None:
    assert "payment_card" in scan(text)


# A zero-width space inside a vendor key does not hide it.
def test_detects_key_split_by_zero_width_space() -> None:
    key = "sk-ant-" + "\u200b".join("a" * 30)
    assert "anthropic_key" in scan(key)


# Normalization only adds findings: a raw-text match still counts.
def test_custom_pattern_matches_raw_text() -> None:
    assert scan("\uff21BC", compile_extra("\uff21BC")) == ["custom_1"]


# scan() stays linear on long runs that used to make the email, URL and secret patterns
# quadratic (80k chars took 15 s and froze Espanso). CPU time, not wall time, so a busy
# machine cannot fail it; a quadratic pattern still takes many times the budget.
# Explicit ids: a 200k-char node id breaks Windows, where pytest puts it in an env var.
@pytest.mark.parametrize(
    "text",
    [
        "a." * 100_000,
        "a-" * 100_000,
        "Bearer " + "a." * 100_000,
        "token=" * 30_000,
        "_token='" * 30_000,
        "-----BEGIN " + "A " * 100_000,
        "4\ufe0f" * 100_000,
        "curl " * 40_000,
        "curl -u " * 25_000,
        "password is " * 16_000,
        "m\u1eadt kh\u1ea9u " * 20_000,
        "Basic " + "A" * 200_000,
        "1\n" * 100_000,
        "1  " * 66_000,
        "1\u2013" * 100_000,
        "CZ65" + " 0" * 100_000,
        "aToken" * 30_000,
        "&sig=" * 40_000,
        "AccountKey=" * 20_000,
        "password a " * 20_000,
        "m\u1eadt kh\u1ea9u a " * 20_000,
        "password" + " " * 200_000,
        "'password' => " * 15_000,
        "curl \\\n" * 40_000,
        "CZ65-0-" * 30_000,
        "1/" * 100_000,
        "aPASSWORD" * 20_000,
        "-u a:b " * 30_000,
        "curl\n-u a:b\n" * 17_000,
        "curlx -u a:b " * 16_000,
        "\n**Confidential" * 15_000,
        "Classification: " * 12_000,
        "a@b.cd " * 30_000,
        "\n- confidential " * 12_000,
        "tài liệu " * 25_000,
        "[confidential " * 15_000,
        "eyJ-" * 50_000,
        "eyJa.eyJb." * 20_000,
        "sk-" * 66_000,
        "ghp_" * 50_000,
        "xoxb-" * 40_000,
        "Bearer -" * 25_000,
    ],
    ids=[
        "dots",
        "dashes",
        "bearer",
        "assignments",
        "prefixed-assignments",
        "pem",
        "selectors",
        "curl",
        "curl-u",
        "password-is",
        "mat-khau",
        "basic",
        "newline-digits",
        "double-spaced-digits",
        "en-dashed-digits",
        "iban",
        "camel-case",
        "sas",
        "azure",
        "password-words",
        "mat-khau-words",
        "assignment-spaces",
        "php-arrows",
        "curl-continuations",
        "iban-dashes",
        "slashed-digits",
        "compound-names",
        "user-options",
        "curl-then-options",
        "not-curl-options",
        "label-markdown",
        "classification-fields",
        "emails",
        "label-lines",
        "vn-label",
        "brackets",
        "jwt-starts",
        "jwt-segments",
        "key-prefixes",
        "github-prefixes",
        "slack-prefixes",
        "bearer-dashes",
    ],
)
def test_scan_is_fast_on_adversarial_input(text: str) -> None:
    started = time.process_time()
    scan_draft(text)
    assert time.process_time() - started < 2


# Error messages repeat a short, harmless value; anything that could be a secret is described.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", "'abc'"),
        ("", "''"),
        ("x" * 40, repr("x" * 40)),
        ("x" * 41, "<redacted, 41 chars>"),
        ("jane.doe@example.com", "<redacted, 20 chars>"),
        ("4111 1111 1111 1111", "<redacted, 19 chars>"),
        ("a=b", "<redacted, 3 chars; two .env lines may have run together>"),
    ],
)
def test_safe_repr(raw: str, expected: str) -> None:
    assert safe_repr(raw) == expected


# Gate corpus (#20, #21). Every value is fake; anything key-shaped is assembled at runtime.
_B64_KEY = base64.b64encode(b"not-a-real-storage-account-key").decode()
_BASIC = base64.b64encode(b"admin:" + b"Winter2026").decode()
_PW = "Winter" + "2026!"
_FAKE = "Zq81" + "Lm0Pw9x2"
# Kept apart so a secret scanner does not read the test commands as real credentials.
_HTTP_CLI = "curl"

# Secret shapes that the gate must flag, with the finding each must produce.
MUST_DETECT = [
    pytest.param(f"SECRET_KEY = 'django-insecure-{_FAKE}'", "secret_assignment", id="django"),
    pytest.param(f"secret_key_base: {_FAKE}", "secret_assignment", id="rails"),
    pytest.param(f'{{"clientSecret": "Xy7~{_FAKE}"}}', "secret_assignment", id="json-camel"),
    pytest.param(f'{{"accessToken": "{_FAKE}"}}', "secret_assignment", id="access-token"),
    pytest.param(f"refreshToken={_FAKE}", "secret_assignment", id="refresh-token-camel"),
    pytest.param(f"const dbPassword = '{_FAKE}';", "secret_assignment", id="js-camel"),
    pytest.param(f"PRIVATE_KEY=0x{_FAKE}", "secret_assignment", id="private-key"),
    pytest.param(f"ENCRYPTION_KEY={_FAKE}", "secret_assignment", id="encryption-key"),
    pytest.param(f"signingKey: '{_FAKE}'", "secret_assignment", id="signing-key"),
    pytest.param("password: hunter2", "secret_assignment", id="short-password"),
    pytest.param('{"pwd": "abc123x"}', "secret_assignment", id="json-pwd"),
    pytest.param("//registry.npmjs.org/:_authToken=" + "npm_" + _BODY, "npm_token", id="npmrc"),
    pytest.param(f"//registry.npmjs.org/:_authToken={_FAKE}", "secret_assignment", id="auth"),
    pytest.param(f"the admin password is {_PW}", "password_value", id="prose-password"),
    pytest.param("password was abc123def", "password_value", id="prose-password-was"),
    pytest.param("mật khẩu: Hanoi2026x", "password_value", id="vn-password"),
    pytest.param("mật khẩu là Saigon2026", "password_value", id="vn-password-la"),
    pytest.param("aws_access_key_id = " + "ASIA" + "Z7EXAMPLE1234567", "aws_access_key", id="asia"),
    pytest.param(f"Authorization: Basic {_BASIC}", "basic_auth", id="basic-auth"),
    pytest.param(
        f"{_HTTP_CLI} -u admin:{_PW} https://api.example.com",
        "curl_user",
        id="dash-u-user-password",
    ),
    pytest.param(
        f"{_HTTP_CLI} -X POST --user=deploy:{_FAKE} https://x.example.com", "curl_user", id="curl"
    ),
    pytest.param(
        f"DefaultEndpointsProtocol=https;AccountName=x;AccountKey={_B64_KEY};",
        "azure_key",
        id="azure-connection-string",
    ),
    pytest.param(f"Endpoint=sb://x/;SharedAccessKey={_B64_KEY}", "azure_key", id="service-bus"),
    pytest.param("https://x.blob.core.windows.net/c?sv=2022&sig=" + _BODY, "azure_key", id="sas"),
    pytest.param("4111  1111  1111  1111", "payment_card", id="card-double-space"),
    pytest.param("4111\u20131111\u20131111\u20131111", "payment_card", id="card-en-dash"),
    pytest.param("4111\t1111\t1111\t1111", "payment_card", id="card-tab"),
    pytest.param("4111 1111\n1111 1111", "payment_card", id="card-wrapped"),
    pytest.param("card 4111 1111 1111 1111\n2. next item", "payment_card", id="card-then-list"),
    pytest.param("ref 12 4111 1111 1111 1111", "payment_card", id="card-after-number"),
    pytest.param("CZ65 0800 0000 1920 0014 5399", "iban", id="iban-cz"),
    pytest.param("DE89 3704 0044 0532 0130 00", "iban", id="iban-de"),
    pytest.param("pay to GB29NWBK60161331926819 today", "iban", id="iban-gb-compact"),
    pytest.param(_PW + "x", "bare_token", id="bare-password"),
    pytest.param("xK9mQ2vL8pR4tW7nB3cF6hJ1", "bare_token", id="bare-token"),
    pytest.param("P@ss" + "w0rd", "bare_token", id="bare-short-password"),
    pytest.param("  " + _PW + "x\n", "bare_token", id="bare-password-padded"),
    pytest.param("Winter" + "2026", "bare_token", id="bare-word-and-year"),
    pytest.param("Passw" + "0rd", "bare_token", id="bare-leet"),
    pytest.param("Tiger-Lily-" + "2026", "bare_token", id="bare-dashed-words"),
    pytest.param("Blue_Moon_" + "77", "bare_token", id="bare-underscored-words"),
    pytest.param(
        "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY", "bare_token", id="bare-aws-secret"
    ),
    pytest.param("/Summer" + "2026x!", "bare_token", id="bare-leading-slash"),
    pytest.param("xK9mQ2vL8pR4." + "tW", "bare_token", id="bare-dotted"),
    pytest.param("P@ss!." + "word1", "bare_token", id="bare-at-sign"),
    # Config shapes: PHP, Go, WordPress, Python, compound env names, aligned values.
    pytest.param(f"'password' => '{_FAKE}',", "secret_assignment", id="php-arrow"),
    pytest.param(f"'api_key' => '{_FAKE}',", "secret_assignment", id="php-arrow-key"),
    pytest.param(f'password := "{_FAKE}"', "secret_assignment", id="go-walrus"),
    pytest.param(f"define( 'DB_PASSWORD', '{_FAKE}' );", "secret_assignment", id="wp-define"),
    pytest.param(f'os.environ["API_KEY"] = "{_FAKE}"', "secret_assignment", id="py-environ"),
    pytest.param(f"PGPASSWORD={_FAKE} psql -h db", "secret_assignment", id="pgpassword"),
    pytest.param(f"set dbpassword={_FAKE} in the file", "secret_assignment", id="lower-compound"),
    pytest.param(f"password:{' ' * 20}{_FAKE}", "secret_assignment", id="aligned-value"),
    pytest.param(f"password is: {_FAKE}", "password_value", id="prose-is-colon"),
    pytest.param(
        f"the password for the admin account is {_FAKE}", "password_value", id="prose-words"
    ),
    pytest.param("mật khẩu đăng nhập: Hanoi2026", "password_value", id="vn-login-password"),
    pytest.param("mật khẩu wifi là Hanoi2026", "password_value", id="vn-wifi-password"),
    pytest.param("mật khẩu của tôi là Hanoi2026", "password_value", id="vn-my-password"),
    pytest.param(
        f"{_HTTP_CLI} https://x.example.com \\\n  -u admin:{_FAKE}", "curl_user", id="continued"
    ),
    pytest.param("cz65 0800 0000 1920 0014 5399", "iban", id="iban-lower"),
    pytest.param("CZ65-0800-0000-1920-0014-5399", "iban", id="iban-dashes"),
    pytest.param(
        "CZ65 0800 0000 1920 0014 5398 DE89 3704 0044 0532 0130 00", "iban", id="iban-after-bad"
    ),
    pytest.param("RU02 0445 2560 0407 0281 0412 3456 7890 1", "iban", id="iban-ru"),
    pytest.param("4111/1111/1111/1111", "payment_card", id="card-slash"),
    pytest.param("4111\u22121111\u22121111\u22121111", "payment_card", id="card-minus"),
    pytest.param("3782 822463 10005", "payment_card", id="card-amex"),
    pytest.param(
        "Basic " + base64.b64encode("jos\u00e9:contrase\u00f1a1".encode()).decode(),
        "basic_auth",
        id="basic-utf8",
    ),
    pytest.param("Basic " + base64.b64encode(b"a:b").decode(), "basic_auth", id="basic-short"),
    # Labels and IDs that stay flagged after #21 narrowed them.
    pytest.param("CONFIDENTIAL: Q3 forecast", "confidential_label", id="label-upper"),
    pytest.param("Confidential: Q3 forecast", "confidential_label", id="label-line-start"),
    pytest.param("> Restricted - board only", "confidential_label", id="label-quoted-dash"),
    pytest.param("[restricted] Q3 forecast", "confidential_label", id="label-bracketed"),
    pytest.param("Strictly confidential, do not forward", "confidential_label", id="strictly"),
    pytest.param("Please do not distribute this deck", "confidential_label", id="no-distribution"),
    pytest.param("Đây là tài liệu tối mật của công ty", "confidential_label", id="vn-top-secret"),
    pytest.param("Thông tin mật: kế hoạch sáp nhập", "confidential_label", id="vn-secret-info"),
    pytest.param("MẬT\nKế hoạch quý 4", "confidential_label", id="vn-header"),
    pytest.param("Tài liệu lưu hành nội bộ", "confidential_label", id="vn-internal"),
    pytest.param("CCCD: 001099012345", "vietnam_id_12", id="cccd"),
    pytest.param("079203001234 là số của tôi", "vietnam_id_12", id="cccd-bare"),
    pytest.param("Số CMND 123456789 của anh ấy", "vietnam_id_9", id="cmnd"),
    pytest.param("hộ chiếu số 012345678", "vietnam_id_9", id="vn-passport"),
    pytest.param("Số CMT 123456789", "vietnam_id_9", id="cmt"),
    pytest.param("Confidential\nQ3 forecast", "confidential_label", id="label-alone"),
    pytest.param("# Confidential", "confidential_label", id="label-heading"),
    pytest.param("**Confidential**: Q3 forecast", "confidential_label", id="label-markdown"),
    pytest.param("Highly confidential \u2013 board", "confidential_label", id="highly"),
    pytest.param("Company Confidential", "confidential_label", id="company"),
    pytest.param("Proprietary and Confidential", "confidential_label", id="proprietary"),
    pytest.param("Classification: Restricted", "confidential_label", id="classification"),
    pytest.param("Sensitivity: Internal", "confidential_label", id="sensitivity"),
    pytest.param("see the [confidential] notes", "confidential_label", id="bracket-mid-line"),
    pytest.param("Độ mật: Mật", "confidential_label", id="vn-field"),
    pytest.param("Tiêu đề: MẬT - Kế hoạch", "confidential_label", id="vn-upper-mid-line"),
    pytest.param("Kế hoạch (MẬT)", "confidential_label", id="vn-upper-paren"),
    pytest.param("Mật: thông tin sáp nhập", "confidential_label", id="vn-line-start"),
    pytest.param("Nội bộ\nKế hoạch", "confidential_label", id="vn-internal-alone"),
    pytest.param("Yarn: install deps\n079203001234: A", "vietnam_id_12", id="cccd-after-yarn"),
    # An email next to a password: hard, so -iok- cannot send it as just "email".
    pytest.param(f"jane@example.com:{_FAKE}!", "credential_pair", id="email-colon-password"),
    pytest.param(f"smtp login jane@example.com / {_FAKE}", "credential_pair", id="email-slash"),
    pytest.param(f"redis://:{_FAKE}@cache.example.com:6379", "url_credentials", id="url-no-user"),
    pytest.param(f"Tài khoản: jane@example.com, mk: {_FAKE}", "password_value", id="vn-mk"),
]

# Ordinary drafts the gate must let through.
MUST_PASS = [
    pytest.param("Output restricted to 5 bullets.", id="restricted"),
    pytest.param(
        "Draft an NDA template that defines what counts as confidential information.", id="nda"
    ),
    pytest.param(
        "Write a script that deduplicates customer data from two CRMs.", id="customer-data"
    ),
    pytest.param("Viết hướng dẫn bảo mật cho nhân viên mới.", id="vn-security"),
    pytest.param("Phân tích mật độ dân số Hà Nội.", id="vn-density"),
    pytest.param("Công thức trà mật ong gừng.", id="vn-honey"),
    pytest.param("Viết email nhắc nhân viên đổi mật khẩu mỗi 90 ngày.", id="vn-password-prose"),
    pytest.param("Fix this policy for arn:aws:iam::123456789012:role/Deploy", id="arn"),
    pytest.param("Customer asks about order 202410041234, draft a reply.", id="order-12"),
    pytest.param("Bug ID: ticket 123456789 crashes on login", id="bug-id-9"),
    pytest.param("Tóm tắt hợp đồng số 123456789 cho sếp.", id="vn-contract-9"),
    pytest.param("123e4567-e89b-12d3-a456-426614174000", id="uuid"),
    pytest.param("the model has a token count of 128000 tokens", id="token-count"),
    pytest.param("Write a policy explaining why passwords must be 12 characters.", id="pw-policy"),
    pytest.param("Explain how password: hashing with bcrypt works", id="pw-colon-prose"),
    pytest.param("password is at least 12 characters long", id="pw-rule"),
    pytest.param("Basic understanding of Kubernetes is enough", id="basic-prose"),
    pytest.param("Basic authentication should be replaced by OAuth2", id="basic-auth-prose"),
    pytest.param("curl -u admin https://api.example.com", id="curl-prompts-for-password"),
    pytest.param('curl -u "$USER:$PASS" https://api.example.com', id="curl-env-vars"),
    pytest.param("set maxTokens=24000000 in the config", id="max-tokens-camel"),
    pytest.param("tokenCount: 12345678", id="token-count-camel"),
    pytest.param("api_key_v2_rotation", id="snake-case"),
    pytest.param("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", id="hash"),
    pytest.param("https://example.com/path?q=1", id="url"),
    pytest.param("~/Projects/app/src/main.py", id="path"),
    pytest.param("C:\\Users\\me\\report2026.docx", id="windows-path"),
    pytest.param("openai/gpt-4o-mini", id="slug"),
    pytest.param("v2.10.3-rc1", id="semver"),
    pytest.param("2026-10-04T12:00:00Z", id="iso-date"),
    pytest.param("report2026.xlsx", id="file-name"),
    pytest.param("db01.internal:5432", id="host-port"),
    pytest.param("Summarize!", id="one-word"),
    pytest.param("call +420 777 123 456 tomorrow", id="phone"),
    pytest.param("2021\n2022\n2023\n2024", id="years-on-lines"),
    # Luhn-valid only when read across three line breaks: a card wraps over one at most.
    pytest.param("4111\n1111\n1111\n1111", id="digit-groups-on-lines"),
    pytest.param("ISBN 978-3-16-148410-0", id="isbn"),
    pytest.param("PL2024Q3 budget review", id="code-like-word"),
    pytest.param("Contacts:\n0927 138 188\n0955 528 171", id="phone-list"),
    pytest.param("Gọi:\n0912 345 678\n0987 654 321\n0903 111 222", id="vn-phone-list"),
    pytest.param("Deadlines: 4.10.2024 5.11.2024 6.12.2025", id="dates"),
    pytest.param("Sprints: 1.10.2025 15.10.2025 29.10.2025", id="sprint-dates"),
    pytest.param("Explain why the password is base64-encoded in the Basic header.", id="pw-b64"),
    pytest.param("Write docs: the password is SHA256-hashed with a salt.", id="pw-hashed"),
    pytest.param("The new password is 6-digit, numeric only.", id="pw-6-digit"),
    pytest.param("The password was bcrypt2-hashed before 2020.", id="pw-bcrypt"),
    pytest.param("Hướng dẫn đổi mật khẩu Office365 cho nhân viên mới.", id="vn-pw-product"),
    pytest.param("maxToken: 16384000", id="max-token-count"),
    pytest.param("Rename variable masterKeyId: 12345678", id="key-id"),
    pytest.param("jwks signing_key_id: kid-2024-10-04", id="signing-key-id"),
    pytest.param("meta-llama/llama-3.1-8b-instruct", id="model-slug"),
    pytest.param("notes2026.md", id="file-name-lower"),
    pytest.param("Prepare an FAQ; access is restricted: admins only", id="restricted-colon-prose"),
    pytest.param("Gửi thông tin mật khẩu mới cho nhân viên", id="vn-password-info"),
    pytest.param("Kiểm tra thông tin mật độ xây dựng", id="vn-density-info"),
    pytest.param("account 123456789012 owns the bucket", id="aws-account-id"),
    # An account ID that happens to be CCCD-shaped is still not an ID inside an ARN.
    pytest.param("Grant arn:aws:iam::001099012345:role/Deploy read access", id="arn-cccd-shaped"),
    pytest.param("Use the CCCD format 0790xxxxxxxx in the form", id="cccd-placeholder"),
    pytest.param("Order 001499123456 shipped", id="cccd-bad-century"),
    pytest.param("Ticket 003012345678 closed", id="cccd-bad-province"),
    pytest.param("Read arn:aws:s3:::bucket/001099012345.json", id="arn-resource"),
    pytest.param("Mật ong có tốt không?", id="vn-honey-line-start"),
    pytest.param("Confidential computing explained for managers", id="confidential-computing"),
    pytest.param("Restricted-access endpoints need review", id="restricted-hyphen"),
    pytest.param("Giữ thông tin mật thiết với khách hàng", id="vn-close"),
    pytest.param("Viết tài liệu mật thư cho trò chơi", id="vn-cipher"),
    pytest.param("chứng minh rằng 100000007 là số nguyên tố", id="vn-prove"),
    pytest.param("cmt line 123456789", id="cmt-lower"),
    pytest.param("labels: internal-api", id="k8s-label"),
]


@pytest.mark.parametrize(("text", "name"), MUST_DETECT)
def test_gate_corpus_detects(text: str, name: str) -> None:
    assert name in scan_draft(text)


@pytest.mark.parametrize("text", MUST_PASS)
def test_gate_corpus_passes(text: str) -> None:
    assert scan_draft(text) == []


# The camelCase name boundary is case-sensitive: MYAPIKEY is one word, not MY + APIKEY, even
# though the pattern ignores case. Names that end a compound env name (PASSWORD, TOKEN,
# SECRET) match anywhere, but not as the start of a longer word.
def test_secret_name_boundaries() -> None:
    assert "secret_assignment" not in scan("rename MYAPIKEY=abc12345678 to X")
    assert "secret_assignment" in scan("rename myApiKey=abc12345678 to X")
    assert "secret_assignment" in scan("rename MYTOKEN=abc12345678 to X")
    assert "secret_assignment" not in scan("rename MYTOKENIZER=abc12345678 to X")


# bare_token is a whole-draft rule: a password-like word inside a sentence is left to the
# other patterns, and only the gate applies it (safe_repr still quotes a short setting value).
def test_bare_token_is_whole_draft_only() -> None:
    assert not is_bare_token(f"my vault entry {_PW}x is old")
    assert is_bare_token(_PW + "x")
    assert scan(_PW + "x") == []
    assert safe_repr(_PW + "x") == repr(_PW + "x")


# Single words that are too short, too long or of one character class are not tokens.
@pytest.mark.parametrize(
    "text", ["Ab1!x", "A" * 201 + "b1!", "abcdefgh1", "ABCD-1234", "Abcdefghij", "abc_def!x"]
)
def test_bare_token_needs_length_and_classes(text: str) -> None:
    assert not is_bare_token(text)


# An IBAN needs its country's length and a valid checksum; a broken one or an unknown
# country is not flagged.
@pytest.mark.parametrize(
    "text", ["CZ65 0800 0000 1920 0014 5398", "QQ65 0800 0000 1920 0014 5399", "DE89 3704 0044"]
)
def test_iban_needs_checksum_and_length(text: str) -> None:
    assert "iban" not in scan(text)


# Basic credentials that are not valid base64 or do not decode to user:password are ignored.
@pytest.mark.parametrize(
    "text",
    [
        "Basic " + base64.b64encode(b"no colon here").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:binary").decode(),
        "Basic abc=defgh",
    ],
)
def test_basic_auth_needs_user_password(text: str) -> None:
    assert "basic_auth" not in scan(text)


# -u belongs to curl only on the same logical line; a later line or another command is not curl.
@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (f"{_HTTP_CLI} https://x \\\n  -H 'a: b' \\\n  -u admin:{_FAKE}", True),
        (f"{_HTTP_CLI} https://x\nssh -u admin:{_FAKE}", False),
        (f"curly -u admin:{_FAKE}", False),
        (f"tar -u admin:{_FAKE}", False),
    ],
    ids=["continued-twice", "next-line", "other-word", "other-command"],
)
def test_curl_user_needs_the_curl_command(text: str, flagged: bool) -> None:
    assert ("curl_user" in scan(text)) == flagged


# An email next to a date, a description or a word without digits is just an email (soft).
@pytest.mark.parametrize(
    "text",
    [
        "Send to jane@example.com, 2024-10-04 latest",
        "jane@example.com 2FA-enabled account",
        "jane@example.com about the Q3 plan",
    ],
)
def test_email_without_password_is_soft(text: str) -> None:
    assert scan_draft(text) == ["email"]


# safe_repr() and redact_words() also describe a value that matches one of the user's
# PROMPT_EXTRA_PATTERNS once settings have loaded them (#32).
def test_safe_repr_honours_user_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import redaction
    from promptmend.config import Settings

    assert safe_repr("PRJ-12345") == "'PRJ-12345'"
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", r"PRJ-\d+")
    Settings.load()
    assert safe_repr("PRJ-12345") == "<redacted, 9 chars>"
    assert redaction.redact_words("got PRJ-12345") == "got <redacted, 9 chars>"
    redaction.set_user_patterns(())
    assert safe_repr("PRJ-12345") == "'PRJ-12345'"


# _normalize() returns ASCII text as is, without unicodedata (#312): exactly what the full
# fold gives, for every ASCII code point and for random ASCII strings (controls, tabs and
# newlines included), so the gate's findings cannot change.
def test_normalize_ascii_fast_path_equals_the_fold() -> None:
    import random

    from promptmend import redaction

    for code in range(128):
        assert redaction._fold(chr(code)) == chr(code) == redaction._normalize(chr(code))
    every = "".join(map(chr, range(128)))
    assert redaction._fold(every) == every
    rng = random.Random(312)  # noqa: S311 - test data, not a secret
    for _ in range(2000):
        text = "".join(chr(rng.randrange(128)) for _ in range(rng.randrange(1, 80)))
        assert redaction._fold(text) == text == redaction._normalize(text)


# The gate's findings for ASCII drafts are the same with the full fold in place of the fast
# path.
def test_scan_findings_unchanged_by_the_ascii_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import redaction

    drafts = [
        "card 4111 1111 1111 1111",
        "contact john.doe@example.com\tCONFIDENTIAL\r\n",
        "IBAN DE89 3704 0044 0532 0130 00\x00\x1b[0m",
        "password: hunter2\x7f",
        "plain text, nothing to flag",
    ]
    fast = [scan_draft(d) for d in drafts]
    monkeypatch.setattr(redaction, "_normalize", redaction._fold)
    assert [scan_draft(d) for d in drafts] == fast
    assert any(fast)


# Non-ASCII text still takes the fold: fullwidth digits become ASCII, a zero-width space goes.
def test_normalize_folds_non_ascii() -> None:
    from promptmend import redaction

    assert redaction._normalize("\uff14\uff11\uff11\uff11\u200b") == "4111"
