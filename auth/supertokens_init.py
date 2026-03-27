"""SuperTokens initialization for ZugaCore auth.

Configures EmailPassword + ThirdParty + Session recipes.
Uses header-based token transfer (Authorization: Bearer) to match existing frontend.

Usage:
    from core.auth.supertokens_init import init_supertokens
    init_supertokens()  # Call once at app startup, before any requests
"""

import logging
from typing import Any, Dict, List, Optional, Union

from supertokens_python import InputAppInfo, SupertokensConfig, init
from supertokens_python.recipe import emailpassword, session, thirdparty
from supertokens_python.recipe.thirdparty.provider import (
    ProviderClientConfig,
    ProviderConfig,
    ProviderInput,
)

from core.auth.config import (
    get_api_domain,
    get_apple_client_id,
    get_apple_key_id,
    get_apple_private_key,
    get_apple_team_id,
    get_github_client_id,
    get_github_client_secret,
    get_google_client_id,
    get_google_client_secret,
    get_microsoft_client_id,
    get_microsoft_client_secret,
    get_supertokens_api_key,
    get_supertokens_connection_uri,
    get_website_domain,
)

logger = logging.getLogger(__name__)


def _build_providers() -> List[ProviderInput]:
    """Build the list of OAuth providers from environment configuration."""
    providers: List[ProviderInput] = []

    # Google
    google_id = get_google_client_id()
    google_secret = get_google_client_secret()
    if google_id and google_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="google",
                    clients=[
                        ProviderClientConfig(
                            client_id=google_id,
                            client_secret=google_secret,
                        )
                    ],
                )
            )
        )

    # Microsoft (Azure AD / Entra ID) via OIDC
    ms_id = get_microsoft_client_id()
    ms_secret = get_microsoft_client_secret()
    if ms_id and ms_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="microsoft",
                    name="Microsoft",
                    clients=[
                        ProviderClientConfig(
                            client_id=ms_id,
                            client_secret=ms_secret,
                        )
                    ],
                    oidc_discovery_endpoint="https://login.microsoftonline.com/common/v2.0",
                )
            )
        )

    # GitHub
    gh_id = get_github_client_id()
    gh_secret = get_github_client_secret()
    if gh_id and gh_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="github",
                    clients=[
                        ProviderClientConfig(
                            client_id=gh_id,
                            client_secret=gh_secret,
                        )
                    ],
                )
            )
        )

    # Apple
    apple_id = get_apple_client_id()
    apple_key_id = get_apple_key_id()
    apple_team_id = get_apple_team_id()
    apple_private_key = get_apple_private_key()
    if apple_id and apple_key_id and apple_team_id and apple_private_key:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="apple",
                    clients=[
                        ProviderClientConfig(
                            client_id=apple_id,
                            client_secret="",  # Apple uses key-based auth, not client secret
                            additional_config={
                                "keyId": apple_key_id,
                                "teamId": apple_team_id,
                                "privateKey": apple_private_key,
                            },
                        )
                    ],
                )
            )
        )

    return providers


def _get_token_transfer_method(
    request: Any, for_create_new_session: bool, user_context: Dict[str, Any]
) -> str:
    """Always use header-based auth to match the existing frontend pattern.

    The frontend stores tokens in localStorage and sends via Authorization: Bearer.
    This is simpler than cookies for cross-port studio proxying.
    """
    return "header"


def init_supertokens() -> None:
    """Initialize SuperTokens SDK. Call once at app startup."""
    connection_uri = get_supertokens_connection_uri()
    api_key = get_supertokens_api_key()
    api_domain = get_api_domain()
    website_domain = get_website_domain()

    providers = _build_providers()
    provider_names = [p.config.third_party_id for p in providers]
    logger.info(
        "Initializing SuperTokens: core=%s, providers=%s",
        connection_uri,
        provider_names or "(none configured)",
    )

    recipe_list = [
        emailpassword.init(),
        session.init(
            get_token_transfer_method=_get_token_transfer_method,
            anti_csrf="NONE",  # Not needed with header-based auth
        ),
    ]

    # Only add ThirdParty recipe if at least one provider is configured
    if providers:
        recipe_list.append(
            thirdparty.init(
                sign_in_and_up_feature=thirdparty.SignInAndUpFeature(
                    providers=providers
                )
            )
        )

    init(
        app_info=InputAppInfo(
            app_name="ZugaApp",
            api_domain=api_domain,
            website_domain=website_domain,
            api_base_path="/api/auth/st",  # SuperTokens internal routes (not our custom ones)
            website_base_path="/auth",
        ),
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key,
        ),
        framework="fastapi",
        mode="asgi",  # Required for FastAPI (ASGI) — prevents blocking the event loop
        recipe_list=recipe_list,
    )

    logger.info("SuperTokens initialized successfully")
