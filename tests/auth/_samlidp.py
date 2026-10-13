"""A test identity provider for the SAML tests (not a test module).

An RSA key and self-signed certificate are generated in memory per call to
`keypair()`; nothing is written to disk or committed. `saml_response()`
builds a SAML Response with a signed Assertion the way a real IdP would, so
the service provider's checks run against real XML signatures.
"""
import base64
import re
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

API = "https://api.test"
WEB = "https://app.test"
IDP_ENTITY = "https://idp.test/metadata"
IDP_SSO = "https://idp.test/sso"
SLUG = "acme"
ACS = f"{API}/api/auth/saml/{SLUG}/acs"
SP_ENTITY = f"{API}/api/auth/saml/{SLUG}/metadata"


def keypair() -> tuple[str, str]:
    """(private key PEM, certificate PEM), generated in memory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def saml_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def saml_response(
    idp,
    *,
    name_id="jane",
    attributes=None,
    not_on_or_after=None,
    sign_with=None,
    sign=True,
    audience=SP_ENTITY,
    recipient=ACS,
    destination=ACS,
    assertion_id=None,
    issuer=IDP_ENTITY,
    in_response_to=None,
) -> str:
    """A base64 SAML Response carrying one (by default signed) Assertion.

    `in_response_to` names the AuthnRequest this answers (SP-initiated);
    None makes an unsolicited, IdP-initiated response.
    """
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    now = datetime.now(timezone.utc)
    nooa = not_on_or_after or (now + timedelta(minutes=5))
    aid = assertion_id or ("_" + uuid.uuid4().hex)
    attributes = {"email": ["jane@customer.example"]} if attributes is None else attributes
    irt_resp = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    attrs_xml = "".join(
        f'<saml:Attribute Name="{n}" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:unspecified">'
        + "".join(f'<saml:AttributeValue xsi:type="xs:string">{v}</saml:AttributeValue>' for v in vals)
        + "</saml:Attribute>"
        for n, vals in attributes.items()
    )
    assertion = (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ID="{aid}" Version="2.0" IssueInstant="{saml_time(now)}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">{name_id}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{saml_time(nooa)}" Recipient="{recipient}"{irt_resp}/>'
        f"</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{saml_time(now - timedelta(minutes=2))}" NotOnOrAfter="{saml_time(nooa)}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
        f"</saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{saml_time(now)}" SessionIndex="_s{uuid.uuid4().hex[:8]}">'
        f"<saml:AuthnContext><saml:AuthnContextClassRef>"
        f"urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        + (f"<saml:AttributeStatement>{attrs_xml}</saml:AttributeStatement>" if attrs_xml else "")
        + "</saml:Assertion>"
    )
    if sign:
        key = sign_with or idp["key"]
        cert = idp["other_cert"] if sign_with else idp["cert"]
        signed = OneLogin_Saml2_Utils.add_sign(assertion, key, cert)
        assertion = signed.decode() if isinstance(signed, bytes) else signed
    response = (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_{uuid.uuid4().hex}" Version="2.0" '
        f'IssueInstant="{saml_time(now)}" Destination="{destination}"{irt_resp}>'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    return base64.b64encode(response.encode()).decode()


def start_login(ent, slug=SLUG, next_path=None) -> str:
    """GET /login like a browser would and return the AuthnRequest id the SP issued."""
    r = ent.get(f"/api/auth/saml/{slug}/login", params={"next": next_path} if next_path else None, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    xml = zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15).decode()
    m = re.search(r'\bID="([^"]+)"', xml)
    assert m, xml
    return m.group(1)


def sp_initiated_response(ent, idp, slug=SLUG, **kwargs) -> str:
    """A response that answers a request this SP just issued."""
    return saml_response(idp, in_response_to=start_login(ent, slug), **kwargs)
