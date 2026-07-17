import pytest

from syncmymoodle import totp as totp_module

RFC_SHA1_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("counter", "expected"),
    list(
        enumerate(
            [
                "755224",
                "287082",
                "359152",
                "969429",
                "338314",
                "254676",
                "287922",
                "162583",
                "399871",
                "520489",
            ]
        )
    ),
)
def test_hotp_matches_rfc_4226(counter: int, expected: str):
    assert totp_module.hotp(RFC_SHA1_SECRET, counter) == expected


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1_111_111_109, "07081804"),
        (1_111_111_111, "14050471"),
        (1_234_567_890, "89005924"),
        (2_000_000_000, "69279037"),
        (20_000_000_000, "65353130"),
    ],
)
def test_totp_matches_rfc_6238(timestamp: int, expected: str, monkeypatch):
    monkeypatch.setattr(totp_module.time, "time", lambda: timestamp)

    assert totp_module.totp(RFC_SHA1_SECRET, digits=8, digest="sha1") == expected
