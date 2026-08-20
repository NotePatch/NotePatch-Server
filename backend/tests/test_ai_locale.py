from notepatch.modules.ai.services.locale import (
    preferred_accept_language,
    resolve_client_locale,
)


def test_accept_language_uses_highest_quality_supported_tag():
    assert preferred_accept_language("en-US;q=0.5, pt-br;q=0.9, *;q=1") == "pt-BR"


def test_locale_resolution_uses_deployment_fallback_for_ambiguous_header():
    assert resolve_client_locale(None, "*;q=1, invalid_locale", "es-MX") == "es-MX"
