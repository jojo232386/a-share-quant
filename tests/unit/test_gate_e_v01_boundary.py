from aquant.backtest.runner import _implementation_digest


def test_v01_research_implementation_digest_remains_frozen() -> None:
    assert _implementation_digest() == (
        "75740270db998f1bff4bb8bc7501b2ac3fa53e747815c6b90ce2eb9e57ec64c5"
    )
