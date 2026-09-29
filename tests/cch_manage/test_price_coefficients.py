from cch_manage.price_coefficients import unavailable_status_slugs
from cch_manage.status_page import inject_status_script


def test_unavailable_when_every_backing_provider_is_disabled():
    providers = [
        {"id": 1, "groupTag": "claude", "isEnabled": False},
        {"id": 2, "groupTag": "claude", "isEnabled": False},
        {"id": 3, "groupTag": "glm", "isEnabled": True},
        {"id": 4, "groupTag": "glm", "isEnabled": False},
    ]
    public_groups = [
        {"publicGroupSlug": "lux"},
        {"publicGroupSlug": "glm"},
        {"publicGroupSlug": "image"},
    ]

    assert unavailable_status_slugs(
        providers,
        public_groups,
        {"lux": ["claude"]},
    ) == ["lux"]


def test_unavailable_is_empty_when_a_provider_is_enabled():
    providers = [
        {"id": 1, "groupTag": "claude", "isEnabled": False},
        {"id": 2, "groupTag": "claude", "isEnabled": True},
    ]

    assert unavailable_status_slugs(
        providers,
        [{"publicGroupSlug": "lux"}],
        {"lux": ["claude"]},
    ) == []


def test_unavailable_ignores_a_card_with_no_providers():
    assert unavailable_status_slugs(
        [{"id": 1, "groupTag": "glm", "isEnabled": False}],
        [{"publicGroupSlug": "image"}],
        None,
    ) == []


def test_status_script_marks_unavailable_groups():
    html = inject_status_script("<body></body>", ["lux"], 300)
    assert "Unavailable" in html
    assert "data-cch-unavailable" in html
    assert 'insertAdjacentElement("afterend", marker)' in html
    assert "margin-left:6px;border:1px solid rgb(239 68 68 / .6)" in html
