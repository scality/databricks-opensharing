"""The configurations the golden files are rendered from.

Shared by the golden test and the generator that refreshes them, so a golden can
never be regenerated from a configuration different from the one asserted
against. Hostnames are example.com / example.test throughout.
"""

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"

BASE = {
    "platform": "ring",
    "endpoint_mode": "trusted",
    "s3_endpoint": "https://s3.example.com",
    "bucket": "delta-share",
    "access_key": "AKIAEXAMPLEACCESSKEY",
    "secret_key": "example-secret-key-value",
    "region": "us-east-1",
    "share_public_url": "https://share.example.com",
    "ca_pem_sha256": "",
    "tables": [{"prefix": "opensharing-poc/customers", "share": "scality",
                "schema": "poc", "table": "customers"}],
}

THREE_TABLES = dict(BASE, tables=[
    {"prefix": "opensharing-poc/customers", "share": "scality",
     "schema": "poc", "table": "customers"},
    {"prefix": "opensharing-poc/orders", "share": "scality",
     "schema": "poc", "table": "orders"},
    {"prefix": "warehouse/finance/ledger", "share": "scality",
     "schema": "finance", "table": "ledger"},
])

HTTP_MODE = dict(BASE, endpoint_mode="http", s3_endpoint="http://s3.example.test",
                 share_public_url="")

PRIVATE_CA = dict(BASE, endpoint_mode="private_ca",
                  s3_endpoint="https://s3.example.test",
                  ca_pem_sha256="a" * 64)

CASES = (
    ("one_table_https", BASE),
    ("three_tables_two_schemas", THREE_TABLES),
    ("http_mode", HTTP_MODE),
    ("private_ca", PRIVATE_CA),
)
