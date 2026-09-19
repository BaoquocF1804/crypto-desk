from __future__ import annotations


def test_the_vn_manager_is_never_offered_reduce_or_exit():
    """Bất biến 8: không có nguồn vị thế cho cổ phiếu, nên REDUCE/EXIT luôn bị
    _validate_manager từ chối và biến một quyết định thật thành lỗi kỹ thuật."""
    from crypto_desk.vn_prompts import VN_ROLE_PROMPTS

    manager = VN_ROLE_PROMPTS["manager"]

    assert "ACCUMULATE" in manager
    assert "HOLD" in manager
    assert "NO_TRADE" in manager
    assert "REDUCE" not in manager
    assert "EXIT" not in manager


def test_every_vn_specialist_has_a_prompt_and_an_evidence_mapping():
    from crypto_desk.vn_prompts import (
        VN_ROLE_PROMPTS,
        VN_SPECIALISTS,
        VN_SPECIALIST_EVIDENCE,
    )

    assert len(VN_SPECIALISTS) == 5
    for role in VN_SPECIALISTS:
        assert role in VN_ROLE_PROMPTS
        assert role in VN_SPECIALIST_EVIDENCE
    for role in ("bull", "bear", "manager"):
        assert role in VN_ROLE_PROMPTS


def test_fundamentals_is_the_only_optional_kind():
    from crypto_desk.vn_prompts import VN_OPTIONAL_KINDS, VN_SPECIALIST_EVIDENCE

    kinds = {kind for kind, _ in VN_SPECIALIST_EVIDENCE.values()}

    assert kinds == {"spot", "news", "flow", "fundamentals"}
    assert VN_OPTIONAL_KINDS == frozenset({"fundamentals"})
